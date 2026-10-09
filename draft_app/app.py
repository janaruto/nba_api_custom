"""
Swiss See Ballers — Live Draft Assistant   (v4: optimiser-driven)
==================================================================
Streamlit app for live use during the Oct 17, 2026 ESPN salary-cap auction. Log every sale as ESPN announces it;
after each one the app re-plans:

  * PRICE FORECAST   what every remaining player will cost: the pre-draft board, scaled so that all the money
                     still in the room is spent on the players still to be drafted (money conservation). The
                     per-tier price shape (do stars / mid-tier / cheap players clear above or below the board?)
                     is shown for information; acting on it did not help in the stress tests, so it does not move bids.
  * TARGET ROSTER    the roster that maximises P(win >= 6 of 10 categories) given what you already own, your
                     remaining money, your open slots, the 4-Center cap and those forecast prices.
  * BREAK-EVEN BID   for every player: the most you can pay and still finish with a roster at least as good as
                     the best one you could build without him. Bidding that number is optimal in a second-price
                     auction; paying a dollar more gives away the edge, paying less loses players you wanted.

All of this comes from draft_app/engine.py (validated against a bootstrap of real games in the v4 notebook).
Data: player_pool.csv + league_meta.json, written by the last cell of fantasy_draft_2026_27_v4_engine.ipynb.
The draft log is persisted to draft_log.json after every change, so a refresh or restart loses nothing.
"""

import json
import sys
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

APP_DIR = Path(__file__).parent
sys.path.insert(0, str(APP_DIR))
import engine  # noqa: E402

POOL_CSV = APP_DIR / "player_pool.csv"
META_JSON = APP_DIR / "league_meta.json"
LOG_JSON = APP_DIR / "draft_log.json"

st.set_page_config(page_title="Live Draft Assistant", layout="wide")


def strip_accents(s: str) -> str:
    """'Jokić' -> 'Jokic' — so typing plain ASCII during a fast live draft still matches accented names."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def ascii_label(name: str) -> str:
    return strip_accents(str(name))


# ── Data ───────────────────────────────────────────────────────────────────
@st.cache_data
def load_pool(stamp: float) -> pd.DataFrame:
    return pd.read_csv(POOL_CSV)


@st.cache_data
def load_meta(stamp: float) -> dict:
    return json.loads(META_JSON.read_text())


def load_log() -> list[dict]:
    return json.loads(LOG_JSON.read_text()) if LOG_JSON.exists() else []


def save_log(entries: list[dict]) -> None:
    LOG_JSON.write_text(json.dumps(entries, indent=2))


if not POOL_CSV.exists() or not META_JSON.exists():
    st.error("player_pool.csv / league_meta.json fehlen in draft_app/. "
             "Die letzte Zelle von fantasy_draft_2026_27_v4_engine.ipynb ausführen.")
    st.stop()

pool = load_pool(POOL_CSV.stat().st_mtime)
meta = load_meta(META_JSON.stat().st_mtime)
if "M_PTS" not in pool.columns or "field" not in meta:
    st.error("Dieser Pool stammt nicht aus dem v4-Notebook (es fehlen die Engine-Spalten). "
             "fantasy_draft_2026_27_v4_engine.ipynb ausführen, dann die App neu laden.")
    st.stop()

TEAM_NAMES = meta["team_names"]
YOUR_TEAM = meta["your_team_name"]
BUDGET = meta["budget_per_team"]
ROSTER_SIZE = meta["roster_size"]
TOTAL_DRAFTED = meta["total_drafted"]
MIN_BID = meta["min_bid"]
MAX_CENTERS = meta["max_centers"]
MARKET_NAME = meta.get("market_model", "STRUCTURAL")

pool = pool.reset_index(drop=True)
PID2I = {int(p): i for i, p in enumerate(pool["PLAYER_ID"])}
MODEL_OK = pool["MODEL_OK"].astype(bool).to_numpy()


@st.cache_resource
def build_engine(stamp: float):
    rules = engine.Rules.from_meta(meta)
    model = engine.build_pool_model(pool, rules, meta["league_rates"])
    cal = meta.get("calibration")
    if cal:
        engine.CAL_A, engine.CAL_B = cal["cal_a"], cal["cal_b"]
    fields = {k: engine.Field.from_dict(v) for k, v in meta["field"].items()}
    return rules, model, fields[MARKET_NAME] if MARKET_NAME in fields else next(iter(fields.values()))


RULES, MODEL, FIELD = build_engine(META_JSON.stat().st_mtime)

if "draft_log" not in st.session_state:
    st.session_state.draft_log = load_log()
log = st.session_state.draft_log


# ── Derived draft state ──────────────────────────────────────────────────────
def entry_is_center(e: dict) -> bool:
    i = PID2I.get(int(e["player_id"]))
    return bool(MODEL.isC[i]) if i is not None else bool(e.get("is_center", False))


spend = {t: 0 for t in TEAM_NAMES}
picks_count = {t: 0 for t in TEAM_NAMES}
centers_drafted = {t: 0 for t in TEAM_NAMES}
for e in log:
    spend[e["team"]] = spend.get(e["team"], 0) + e["price"]
    picks_count[e["team"]] = picks_count.get(e["team"], 0) + 1
    if entry_is_center(e):
        centers_drafted[e["team"]] = centers_drafted.get(e["team"], 0) + 1
remaining_budget = {t: BUDGET - spend[t] for t in TEAM_NAMES}
remaining_slots = {t: ROSTER_SIZE - picks_count[t] for t in TEAM_NAMES}
your_remaining_budget, your_remaining_slots, your_centers = (
    remaining_budget[YOUR_TEAM], remaining_slots[YOUR_TEAM], centers_drafted[YOUR_TEAM])
max_bid_cap = max(your_remaining_budget - max(your_remaining_slots - 1, 0) * MIN_BID, 0) if your_remaining_slots > 0 else 0
sold_ids = {int(e["player_id"]) for e in log}
available = pool[~pool["PLAYER_ID"].isin(sold_ids)].copy()


# ── The optimiser: one solve per distinct draft state ───────────────────────────
@st.cache_data(show_spinner="Optimierer rechnet …")
def plan(log_key: tuple, stamp: float):
    """log_key = ((player_id, team, price, unlisted_is_center), ...). Returns forecast prices, break-even bids,
    my target roster and its win probabilities. Everything is derived from the key, so it caches per state."""
    n = MODEL.n
    spend_t = {t: 0 for t in TEAM_NAMES}; picks_t = {t: 0 for t in TEAM_NAMES}
    sold_idx, paid, mine, unlisted_mine = [], [], [], 0
    for pid, team, price, _is_c in log_key:
        spend_t[team] += price; picks_t[team] += 1
        i = PID2I.get(int(pid))
        if i is not None:
            sold_idx.append(i); paid.append(price)
            if team == YOUR_TEAM:
                mine.append(i)
        elif team == YOUR_TEAM:
            unlisted_mine += 1
    budget_left = sum(BUDGET - spend_t[t] for t in TEAM_NAMES)
    slots_left = sum(ROSTER_SIZE - picks_t[t] for t in TEAM_NAMES)
    sold_mask = np.zeros(n, bool); sold_mask[sold_idx] = True
    market = pool["MARKET_$"].to_numpy(float)
    # Decisions use the price LEVEL only (money conservation). The tier SHAPE is computed for display: the notebook's
    # stress test found that acting on it did not improve results, so it informs but does not move the bids.
    price, _ = engine.live_prices(market, sold_idx, paid, ~sold_mask, budget_left, slots_left, MIN_BID, prior_picks=1e9)
    _, tier_factor = engine.live_prices(market, sold_idx, paid, ~sold_mask, budget_left, slots_left, MIN_BID)
    likely = np.flatnonzero(~sold_mask)
    likely = likely[np.argsort(-market[likely])][:max(slots_left, 1)]
    inflation = budget_left / max(market[likely].sum(), 1.0)

    my_budget = BUDGET - spend_t[YOUR_TEAM]
    ns = RULES.n_starters
    if len(mine) > ns:                                  # extras are bench: start the ten with the best balanced value
        z = ((MODEL.E - MODEL.E.mean(0)) / np.maximum(MODEL.E.std(0), 1e-9)).sum(1)
        fixed = sorted(mine, key=lambda i: -z[i])[:ns]
    else:
        fixed = list(mine)
    off_c = int(MODEL.isC[[i for i in mine if i not in fixed]].sum()) if len(mine) > len(fixed) else 0
    bench_left = max(0, RULES.bench_count - max(0, len(mine) - ns) - unlisted_mine)
    avail = (~sold_mask) & MODEL_OK
    avail[mine] = False
    out = dict(price=price, inflation=inflation, tier_factor=tier_factor, n_sold=len(sold_idx), mine=mine, fixed=fixed,
               team=None, wtp=np.zeros(n), p_win=None, e_cats=None, p_cat=None, lam=None, note="")
    if len(fixed) >= ns:
        out["team"] = fixed
        ev = engine.evaluate(MODEL, fixed, FIELD)
        out.update(p_win=ev["p_win"], e_cats=ev["e_cats"], p_cat=ev["p_cat"], note="Startaufstellung komplett")
        out["wtp"] = np.where(avail, float(MIN_BID), 0.0)
        return out
    pr = engine.Problem(MODEL, FIELD, np.maximum(price, MIN_BID), avail, fixed=fixed, budget=my_budget,
                        bench_reserve=bench_left * MIN_BID, fixed_centers_off_lineup=off_c)
    res = engine.solve(pr)
    if res is None:
        out["note"] = "Kein legaler Kader mit dem Restbudget baubar"
        return out
    team, f, lam = res
    wtp = engine.max_bids(pr, team, max(lam, 1e-4))
    ev = engine.evaluate(MODEL, team, FIELD)
    out.update(team=team, wtp=np.where(avail, wtp, 0.0), p_win=ev["p_win"], e_cats=ev["e_cats"], p_cat=ev["p_cat"], lam=lam)
    return out


log_key = tuple((int(e["player_id"]), e["team"], int(e["price"]), bool(e.get("is_center", False))) for e in log)
try:
    PLAN = plan(log_key, META_JSON.stat().st_mtime)
except Exception as ex:                                  # never let the planner take the draft-night app down
    st.error(f"Optimierer-Fehler: {ex}. Die Tabellen zeigen vorerst nur Marktpreise.")
    PLAN = dict(price=pool["MARKET_$"].to_numpy(float), inflation=1.0, tier_factor=np.ones(4), n_sold=len(log),
                mine=[], fixed=[], team=None, wtp=np.zeros(MODEL.n), p_win=None, e_cats=None, p_cat=None, lam=None,
                note="Optimierer nicht verfügbar")

available["LIVE_$"] = PLAN["price"][available.index].round(1)
available["YOUR_MAX_BID"] = np.minimum(PLAN["wtp"][available.index], max_bid_cap).round(1)
available["EDGE"] = (available["YOUR_MAX_BID"] - available["LIVE_$"]).round(1)
in_target = set(PLAN["team"] or [])
available["IN_TARGET"] = [i in in_target for i in available.index]
model_ok_av = available["MODEL_OK"].astype(bool)
available["ACTION"] = np.select(
    [~model_ok_av, available["YOUR_MAX_BID"] < MIN_BID, available["EDGE"] > 0.5, available["EDGE"] < -0.5],
    ["kein Modell", "skip", "BID UP", "let go"], default="at market")
center_capped = your_centers >= MAX_CENTERS

# ── Sidebar ───────────────────────────────────────────────────────────────────
st.sidebar.title(meta["league_name"])
st.sidebar.caption(f"Projektionen: {meta.get('generated_at', '?')[:16].replace('T', ' ')} · {meta.get('model_version', '?')}")
st.sidebar.caption(f"Board-Kurve: {MARKET_NAME} · wird live aus den Verkäufen nachgezogen")
st.sidebar.markdown("---")
st.sidebar.subheader(f"🏆 {YOUR_TEAM}")
st.sidebar.metric("Restbudget", f"${your_remaining_budget}")
st.sidebar.metric("Offene Roster-Slots", f"{your_remaining_slots} / {ROSTER_SIZE}")
st.sidebar.metric("Center gedraftet", f"{your_centers} / {MAX_CENTERS}")
st.sidebar.metric("Max. Gebot (Budget-Grenze)", f"${max_bid_cap}")
st.sidebar.caption("= Restbudget minus $1 für jeden weiteren offenen Slot. Mehr geht für EINEN Spieler nicht, "
                   "ohne die übrigen Slots zu gefährden.")
st.sidebar.markdown("---")
st.sidebar.subheader("Alle Teams — Restbudget")
st.sidebar.dataframe(
    pd.DataFrame({"Team": TEAM_NAMES, "Rest-$": [remaining_budget[t] for t in TEAM_NAMES],
                  "Slots offen": [remaining_slots[t] for t in TEAM_NAMES]}).sort_values("Rest-$", ascending=False),
    hide_index=True, width="stretch")

# ── Header ──────────────────────────────────────────────────────────────────
st.title("🏀 Live Draft Assistant")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Preisniveau", f"{PLAN['inflation']:.2f}× Board",
          help="Restgeld aller Teams ÷ Board-Wert der Spieler, die noch gekauft werden. >1 = der Raum zahlt mehr als das Board.")
c2.metric("Picks geloggt", f"{len(log)} / {TOTAL_DRAFTED}")
c3.metric("Spieler verfügbar", len(available))
if PLAN["p_win"] is not None:
    c4.metric("Zielkader: P(Matchup-Sieg)", f"{PLAN['p_win']*100:.0f}%",
              help=f"{PLAN['e_cats']:.1f} von 10 Kategorien pro Woche im Schnitt. Modellwert unter der Annahme, dass die Projektionen stimmen: "
                   "die reale Quote liegt niedriger. Wichtig ist der Vergleich zwischen Kadern, nicht die absolute Zahl.")
if PLAN["n_sold"] >= 6:
    tf = PLAN["tier_factor"]
    st.caption("Preis-Form bisher (Verkauf ÷ Board, relativ zum Raum): "
               f"billig (unter \\$8) ×{tf[0]:.2f} · mittel (\\$8-20) ×{tf[1]:.2f} · teuer (\\$20-50) ×{tf[2]:.2f} · Stars (über \\$50) ×{tf[3]:.2f}")
if PLAN["note"]:
    st.warning(PLAN["note"])
if center_capped:
    st.warning(f"Center-Limit erreicht ({your_centers}/{MAX_CENTERS}): reine Center sind nicht mehr rosterbar (Max-Gebot $0).")

# ── "What should I pay for him?" ───────────────────────────────────────────────────
st.subheader("💵 Was soll ich für ihn zahlen?")
_look = pool.assign(__label=pool["PLAYER_NAME"].map(ascii_label))
_label_to_name = dict(zip(_look["__label"], _look["PLAYER_NAME"]))
picked_label = st.selectbox("Spieler (tippen zum Filtern)", sorted(_look["__label"]), index=None,
                            placeholder="z.B. jokic, sengun, ware …", key="lookup")
if picked_label:
    real_name = _label_to_name[picked_label]
    gone = next((e for e in log if e["player_name"] == real_name), None)
    row_av = available[available["PLAYER_NAME"] == real_name]
    if gone is not None:
        st.error(f"**{real_name}** ist weg: {gone['team']} hat ihn für **\\${gone['price']}** geholt.")
    elif row_av.empty:
        st.warning(f"**{real_name}** steht nicht im Pool.")
    else:
        r = row_av.iloc[0]
        ceiling, live = float(r["YOUR_MAX_BID"]), float(r["LIVE_$"])
        k1, k2, k3, k4 = st.columns([2, 1, 1, 1])
        k1.metric("ZAHLE BIS", "nicht bieten" if ceiling < MIN_BID else f"${ceiling:,.0f}",
                  delta=None if ceiling < MIN_BID else f"{ceiling - live:+.0f} vs Preisprognose",
                  help="Break-even: bis hierher bleibt dein bester Kader mit ihm mindestens so gut wie ohne ihn.")
        k2.metric("Preisprognose (live)", f"${live:,.0f}")
        k3.metric("ESPN-Liste", f"${float(r['ESPN_$_LIST']):,.0f}" if pd.notna(r.get("ESPN_$_LIST")) else "—")
        k4.metric("Aktion", str(r["ACTION"]))
        st.caption(f"{r['POS_ELIGIBLE_STR']} · {r['CURRENT_TEAM_ABBR']} · {r['AGE']:.0f} Jahre · {r['RISK_FLAG']} "
                   f"({int(r['EXP_GP'])} Spiele erwartet)" + (" · **im aktuellen Zielkader**" if r["IN_TARGET"] else ""))
        if not bool(r["MODEL_OK"]):
            st.info("Keine eigene Projektion (Rookie / ohne Stats): ESPN-Preis als Referenz, der Optimierer empfiehlt ihn nie.")
        elif ceiling < MIN_BID:
            st.info("Für deinen besten Kader nicht nötig oder nicht rosterbar: auslassen.")
        elif ceiling > live + 0.5:
            st.success(f"**Er ist dir mehr wert als der Raum zahlt.** Bis **\\${ceiling:,.0f}** mitgehen; der Raum hört vermutlich bei ~\\${live:,.0f} auf.")
        elif ceiling < live - 0.5:
            st.warning(f"**Der Raum zahlt mehr als er deinem Kader bringt** (Prognose \\${live:,.0f} > dein \\${ceiling:,.0f}): laufen lassen.")
        else:
            st.info("Fair bewertet: zum erwarteten Preis mitnehmen, nicht darüber.")

st.divider()

# ── Pick entry ─────────────────────────────────────────────────────────────────────────
st.subheader("Pick eintragen")
st.caption("Reihenfolge wie bei ESPN: Spieler wird aufgerufen → oben nachschlagen → hier eintragen, für wen und für wie viel er weggeht.")
_av = available.assign(__label=available["PLAYER_NAME"].map(ascii_label)).sort_values("MARKET_$", ascending=False)
_entry_to_name = dict(zip(_av["__label"], _av["PLAYER_NAME"]))
_bid_by_label = dict(zip(_av["__label"], _av["YOUR_MAX_BID"]))


def _entry_fmt(lbl: str) -> str:
    b = _bid_by_label.get(lbl, 0)
    return f"{lbl}  —  bis ${b:,.0f}" if b >= MIN_BID else f"{lbl}  —  nicht bieten"


with st.form("log_pick", clear_on_submit=True):
    fc1, fc2, fc3, fc4 = st.columns([3, 2, 1, 1])
    picked_entry = fc1.selectbox(f"Spieler ({len(_av)} verfügbar)", _av["__label"].tolist(), index=None,
                                 format_func=_entry_fmt, placeholder="tippen zum Filtern…")
    picked_team = fc2.selectbox("Team", TEAM_NAMES, index=TEAM_NAMES.index(YOUR_TEAM))
    picked_price = fc3.number_input("Preis ($)", min_value=MIN_BID, max_value=BUDGET, value=MIN_BID, step=1)
    submitted = fc4.form_submit_button("Eintragen", width="stretch")
    if submitted:
        if picked_entry is None:
            st.error("Bitte einen Spieler auswählen.")
        else:
            name = _entry_to_name[picked_entry]
            pid = int(pool.loc[pool["PLAYER_NAME"] == name, "PLAYER_ID"].iloc[0])
            log.append({"player_id": pid, "player_name": name, "team": picked_team, "price": int(picked_price)})
            save_log(log)
            st.rerun()

with st.expander("Spieler nicht in der Liste? (z.B. ein Rookie, den ESPN aufruft)"):
    st.caption("Zählt für Budget und Roster-Slot des Teams. Nicht im Pool = keine Projektion, deshalb kann der Optimierer "
               "ihn nicht in deinen Kader rechnen; er belegt dort eine Bank-Stelle.")
    with st.form("log_unlisted", clear_on_submit=True):
        uc1, uc2, uc3, uc4, uc5 = st.columns([3, 2, 1, 1, 1])
        u_name = uc1.text_input("Name")
        u_team = uc2.selectbox("Team", TEAM_NAMES, index=TEAM_NAMES.index(YOUR_TEAM), key="u_team")
        u_price = uc3.number_input("Preis ($)", min_value=MIN_BID, max_value=BUDGET, value=MIN_BID, step=1, key="u_price")
        u_c = uc4.checkbox("Center?")
        if uc5.form_submit_button("Eintragen") and u_name.strip():
            nid = -1 - sum(1 for e in log if int(e["player_id"]) < 0)
            log.append({"player_id": nid, "player_name": u_name.strip(), "team": u_team, "price": int(u_price),
                        "unlisted": True, "is_center": bool(u_c)})
            save_log(log)
            st.rerun()

# ── Draft log + undo ───────────────────────────────────────────────────────────────────
with st.expander(f"Draft-Log ({len(log)} Picks) — zum Korrigieren aufklappen", expanded=False):
    if log:
        log_df = pd.DataFrame(log).iloc[::-1].reset_index(drop=True)
        log_df.index = range(len(log) - 1, -1, -1)
        st.dataframe(log_df[["player_name", "team", "price"]], width="stretch")
        remove_idx = st.selectbox(
            "Eintrag entfernen (Index)", options=list(range(len(log))),
            format_func=lambda i: f"#{i}: {log[i]['player_name']} — {log[i]['team']} (${log[i]['price']})",
            index=None, placeholder="Index wählen…")
        rc1, rc2 = st.columns(2)
        if rc1.button("Ausgewählten Eintrag entfernen") and remove_idx is not None:
            log.pop(remove_idx); save_log(log); st.rerun()
        if rc2.button("⚠️ Kompletten Draft zurücksetzen"):
            st.session_state.draft_log = []; save_log([]); st.rerun()
    else:
        st.caption("Noch keine Picks geloggt.")

# ── Target roster ────────────────────────────────────────────────────────────────────────
st.subheader("🎯 Mein Zielkader (Optimierer)")
if PLAN["team"]:
    fixed_set = set(PLAN["fixed"])
    rows = []
    for i in PLAN["team"]:
        bought = i in fixed_set
        paid_price = next((e["price"] for e in log if PID2I.get(int(e["player_id"])) == i and e["team"] == YOUR_TEAM), None)
        rows.append({"Status": "gekauft" if bought else "Ziel", "Spieler": MODEL.names[i],
                     "Pos": pool["POS_ELIGIBLE_STR"].iat[i], "Team": pool["CURRENT_TEAM_ABBR"].iat[i],
                     "Preis $": paid_price if bought else round(float(PLAN["price"][i]), 1),
                     "Max-Gebot $": None if bought else round(float(min(PLAN["wtp"][i], max_bid_cap)), 1),
                     "Verfügbarkeit": round(float(MODEL.avail[i]), 2)})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    if PLAN["p_cat"]:
        cats = pd.DataFrame({"Kategorie": [engine.LABEL[c] for c in engine.CATS],
                             "P(Kategorie gewonnen)": [round(PLAN["p_cat"][c], 2) for c in engine.CATS]}).set_index("Kategorie")
        st.caption("Gewinnchance je Kategorie pro Woche (gegen den typischen Gegner) — der Optimierer verteilt dein Geld so, dass "
                   "möglichst viele Kategorien über 50 % liegen, nicht auf einen festen Punt-Plan:")
        st.dataframe(cats.T, width="stretch")
else:
    st.caption("Noch kein Zielkader berechenbar.")

# ── Board ────────────────────────────────────────────────────────────────────────────
st.subheader("Verfügbare Spieler — live bewertet")
pos_filter = st.multiselect("Position filtern", ["PG", "SG", "SF", "PF", "C"], default=[])
board = available.copy()
if pos_filter:
    board = board[board["POS_ELIGIBLE_STR"].apply(lambda s: any(p in str(s).split("/") for p in pos_filter))]
board = board.sort_values("LIVE_$", ascending=False)
display_cols = ["PLAYER_NAME", "POS_ELIGIBLE_STR", "CURRENT_TEAM_ABBR", "AGE", "RISK_FLAG", "YOUR_MAX_BID", "LIVE_$", "EDGE",
                "ACTION", "IN_TARGET", "ESPN_$_LIST", "PRED_PTS_PG", "PRED_REB_PG", "PRED_AST_PG", "PRED_STL_PG", "PRED_BLK_PG",
                "PRED_FG3M_PG", "PRED_DD_PG"]
rename = {"PLAYER_NAME": "Spieler", "POS_ELIGIBLE_STR": "Pos", "CURRENT_TEAM_ABBR": "Team", "AGE": "Alter", "RISK_FLAG": "Risiko",
          "YOUR_MAX_BID": "Dein Max-Gebot", "LIVE_$": "Preisprognose", "EDGE": "Vorteil $", "ACTION": "Aktion",
          "IN_TARGET": "Im Zielkader", "ESPN_$_LIST": "ESPN-Liste", "PRED_PTS_PG": "PTS", "PRED_REB_PG": "REB",
          "PRED_AST_PG": "AST", "PRED_STL_PG": "STL", "PRED_BLK_PG": "BLK", "PRED_FG3M_PG": "3PM", "PRED_DD_PG": "DD"}
st.dataframe(board[display_cols].rename(columns=rename).head(140), width="stretch", height=600, hide_index=True)
