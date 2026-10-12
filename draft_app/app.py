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
  * CALL ORDER      the players come up in ESPN's own draft-room order (ESPN_RANK from the notebook). The first
                     unsold one is shown with his live ceiling; you only enter team and price. Anybody else the room
                     calls first can be picked in "Anderer Spieler aufgerufen?".
  * BREAK-EVEN BID   for every player: the most you can pay and still finish with a roster at least as good as
                     the best one you could build without him. Bidding that number is optimal in a second-price
                     auction; paying a dollar more gives away the edge, paying less loses players you wanted.

All of this comes from draft_app/engine.py (validated against a bootstrap of real games in the v4 notebook).
Data: player_pool.csv + league_meta.json, written by the last cell of fantasy_draft_2026_27_v4_engine.ipynb.
The draft log is persisted to draft_log.json after every change, so a refresh or restart loses nothing.
"""

import html
import json
import os
import sys
import time
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

APP_DIR = Path(__file__).parent
sys.path.insert(0, str(APP_DIR))
import availability  # noqa: E402
import engine  # noqa: E402

POOL_CSV = APP_DIR / "player_pool.csv"
META_JSON = APP_DIR / "league_meta.json"
LOG_JSON = Path(os.environ.get("DRAFT_LOG_JSON", APP_DIR / "draft_log.json"))     # env override: tests / a second instance
NOM_JSON = APP_DIR / "current_nomination.json"          # written by bridge.py: who ESPN has up right now
UNRES_JSON = APP_DIR / "bridge_unresolved.json"          # sales the bridge could not match: enter by hand
INJ_JSON = APP_DIR / "injuries.json"                     # written by refresh_injuries.py (ESPN status, outlook)
OVR_JSON = APP_DIR / "availability_overrides.json"       # {player name: expected games}, edited in the app
NOMINATION_MAX_AGE_S = 600

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


def _mt(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return 0


def load_injuries() -> dict:
    try:
        return json.loads(INJ_JSON.read_text()).get("players", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_overrides() -> dict:
    try:
        return json.loads(OVR_JSON.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_log() -> list[dict]:
    return json.loads(LOG_JSON.read_text()) if LOG_JSON.exists() else []


def save_log(entries: list[dict]) -> None:
    """Atomic write, and remember our own write so the auto-sync below does not mistake it for one from the bridge."""
    tmp = LOG_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2))
    os.replace(tmp, LOG_JSON)
    mt = st.session_state.get("_sync_mt", (0, 0, 0))
    st.session_state["_sync_mt"] = (_mt(LOG_JSON), mt[1], mt[2])


def load_nomination() -> dict | None:
    """The player ESPN currently has up (from the bridge), unless the file is stale."""
    try:
        nom = json.loads(NOM_JSON.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return nom if time.time() - float(nom.get("ts", 0)) <= NOMINATION_MAX_AGE_S else None


def load_unresolved() -> list[dict]:
    try:
        return json.loads(UNRES_JSON.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


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
# availability: the notebook's value, then ESPN's OUT status and manual overrides on top (availability.py)
STAMP = (META_JSON.stat().st_mtime, _mt(INJ_JSON), _mt(OVR_JSON))
pool = availability.adjust_pool(pool, load_injuries(), load_overrides())
PID2I = {int(p): i for i, p in enumerate(pool["PLAYER_ID"])}
MODEL_OK = pool["MODEL_OK"].astype(bool).to_numpy()


@st.cache_resource
def build_engine(stamp: tuple):
    rules = engine.Rules.from_meta(meta)
    model = engine.build_pool_model(pool, rules, meta["league_rates"])
    cal = meta.get("calibration")
    if cal:
        engine.CAL_A, engine.CAL_B = cal["cal_a"], cal["cal_b"]
    fields = {k: engine.Field.from_dict(v) for k, v in meta["field"].items()}
    return rules, model, fields[MARKET_NAME] if MARKET_NAME in fields else next(iter(fields.values()))


RULES, MODEL, FIELD = build_engine(STAMP)

if "draft_log" not in st.session_state:
    st.session_state.draft_log = load_log()
    st.session_state["_sync_mt"] = (_mt(LOG_JSON), _mt(NOM_JSON), _mt(UNRES_JSON))
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
def plan(log_key: tuple, stamp: tuple):
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
    # Only teams with an open roster slot can still spend money: leftovers of full rosters are dead money and would
    # inflate every forecast price (late in the draft that made $1 players look like $1.5 ones).
    budget_left = sum(BUDGET - spend_t[t] for t in TEAM_NAMES if ROSTER_SIZE - picks_t[t] > 0)
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
               team=None, wtp=np.zeros(n), p_win=None, e_cats=None, p_cat=None, lam=None, note="", bench=[], bench_cap=0)

    def add_bench(team_list, unbought_cost: float):
        """Bench candidates for the open bench slots (never lets a failure here break the plan)."""
        if bench_left <= 0 or not team_list:
            return
        try:
            cap = max(MIN_BID, int((my_budget - unbought_cost) // max(bench_left, 1)))
            rows = engine.bench_suggestions(MODEL, FIELD, team_list, avail, price, n_bench=bench_left,
                                            fixed_centers_off_lineup=off_c, max_price=cap + 0.5)
            out["bench"] = rows or engine.bench_suggestions(MODEL, FIELD, team_list, avail, price, n_bench=bench_left,
                                                            fixed_centers_off_lineup=off_c)
            out["bench_cap"] = cap
        except Exception:
            out["bench"], out["bench_cap"] = [], 0

    if len(fixed) >= ns:
        out["team"] = fixed
        ev = engine.evaluate(MODEL, fixed, FIELD)
        out.update(p_win=ev["p_win"], e_cats=ev["e_cats"], p_cat=ev["p_cat"], note="Startaufstellung komplett")
        out["wtp"] = np.where(avail, float(MIN_BID), 0.0)
        add_bench(fixed, 0.0)
        return out
    pr = engine.Problem(MODEL, FIELD, np.maximum(price, MIN_BID), avail, fixed=fixed, budget=my_budget,
                        bench_reserve=bench_left * MIN_BID, fixed_centers_off_lineup=off_c)
    team, f, lam, scale, pr_used = engine.solve_robust(pr)
    if scale is None:                                    # last resort: no legal roster at all among the available players
        ev = engine.evaluate(MODEL, team, FIELD)
        out.update(team=team, p_win=ev["p_win"], e_cats=ev["e_cats"], p_cat=ev["p_cat"], lam=lam,
                   wtp=np.where(avail, float(MIN_BID), 0.0), target_price=pr.price,
                   note="Notfallplan: kein Kader mit dem Restbudget passt zu den Positionen. Gezeigt sind die besten verfügbaren Spieler, "
                        "unabhängig vom Preis.")
        add_bench(team, 0.0)
        return out
    wtp = engine.max_bids(pr_used, team, max(lam, 1e-4))
    ev = engine.evaluate(MODEL, team, FIELD)
    out.update(team=team, wtp=np.where(avail, wtp, 0.0), p_win=ev["p_win"], e_cats=ev["e_cats"], p_cat=ev["p_cat"], lam=lam,
               target_price=pr_used.price)
    add_bench(team, float(sum(pr_used.price[i] for i in team if i not in set(fixed))))
    if scale < 0.999:
        out["note"] = (f"Dein Restbudget (\\${my_budget}) reicht nicht für den Zielkader zu den Prognosepreisen. Der Zielkader "
                       + ("rechnet jeden offenen Platz mit dem Mindestgebot (\\$1)" if scale < 0.01 else f"rechnet mit auf {scale:.0%} gekürzten Prognosepreisen")
                       + ": er geht nur auf, wenn du die Spieler zum Mindestpreis bekommst. Nominiere und biete \\$1.")
    return out


log_key = tuple((int(e["player_id"]), e["team"], int(e["price"]), bool(e.get("is_center", False))) for e in log)
try:
    PLAN = plan(log_key, STAMP)
    st.session_state["_last_plan"] = PLAN
except Exception as ex:                                  # never let the planner take the draft-night app down
    st.error(f"Optimierer-Fehler: {ex}.")
    if "_last_plan" in st.session_state:                 # keep showing the last good plan rather than an empty one
        PLAN = dict(st.session_state["_last_plan"])
        PLAN["note"] = "Der Optimierer hatte einen Fehler: gezeigt ist der Stand vor dem letzten Pick (Zielkader und Max-Gebote sind etwas veraltet)."
    else:
        PLAN = dict(price=pool["MARKET_$"].to_numpy(float), inflation=1.0, tier_factor=np.ones(4), n_sold=len(log),
                    mine=[], fixed=[], team=None, wtp=np.zeros(MODEL.n), p_win=None, e_cats=None, p_cat=None, lam=None,
                    note="Optimierer nicht verfügbar")

available["LIVE_$"] = PLAN["price"][available.index].round(1)
available["YOUR_MAX_BID"] = np.minimum(PLAN["wtp"][available.index], max_bid_cap).round(1)
available["EDGE"] = (available["YOUR_MAX_BID"] - available["LIVE_$"]).round(1)
BENCH = PLAN.get("bench", [])
BENCH_CAP = PLAN.get("bench_cap", 0)
available["BENCH_RANK"] = np.nan
available["BENCH_OPT"] = False
for _b in BENCH:                                         # bench candidates are bid on up to the bench price limit only
    if _b["idx"] in available.index:
        available.loc[_b["idx"], "BENCH_OPT"] = True
        if _b["rank"]:
            available.loc[_b["idx"], "BENCH_RANK"] = _b["rank"]
        if available.at[_b["idx"], "YOUR_MAX_BID"] < MIN_BID and min(BENCH_CAP, max_bid_cap) >= MIN_BID:
            available.loc[_b["idx"], "YOUR_MAX_BID"] = float(min(BENCH_CAP, max_bid_cap))
            available.loc[_b["idx"], "EDGE"] = round(float(min(BENCH_CAP, max_bid_cap)) - float(available.at[_b["idx"], "LIVE_$"]), 1)
in_target = set(PLAN["team"] or [])
available["IN_TARGET"] = [i in in_target for i in available.index]
model_ok_av = available["MODEL_OK"].astype(bool)
available["ACTION"] = np.select(
    [~model_ok_av, available["YOUR_MAX_BID"] < MIN_BID, available["EDGE"] > 0.5, available["EDGE"] < -0.5],
    ["kein Modell", "skip", "BID UP", "let go"], default="at market")
available.loc[available["BENCH_OPT"] & ~available.index.isin(in_target), "ACTION"] = "Bank"
center_capped = your_centers >= MAX_CENTERS

# ── ESPN draft-room order: who is called next ──────────────────────────────────────────────
# ESPN lists the auction pool by its own pre-draft rank (rank type STANDARD = H2H categories, exported by the notebook as
# ESPN_RANK). The app follows that order: the first unsold player is "up", you only enter team and price. If the room
# calls somebody else, pick him in "Anderer Spieler aufgerufen?"; "Nicht aufgerufen" sends the current one to the back.
_rank = pool["ESPN_RANK"].astype(float) if "ESPN_RANK" in pool.columns else pd.Series(np.nan, index=pool.index)
_order = (pd.DataFrame({"rk": _rank.fillna(1e9), "val": -pool["ESPN_$_RAW"].fillna(-1), "mkt": -pool["MARKET_$"]})
          .sort_values(["rk", "val", "mkt"]).index)
ESPN_POS = pd.Series(np.arange(len(_order)), index=_order).sort_index()          # position in ESPN's order, by pool index
available["ESPN_RANK"] = _rank[available.index]
available["_ORD"] = ESPN_POS[available.index]
if "skipped" not in st.session_state:
    st.session_state.skipped = []
_queue = available.sort_values("_ORD")
_skipped = _queue["PLAYER_ID"].isin(st.session_state.skipped)
_queue = pd.concat([_queue[~_skipped], _queue[_skipped]])                         # skipped players go to the back
_called = st.session_state.get("call_override")
_label_av = available["PLAYER_NAME"].map(ascii_label)
_by_label = available[_label_av == _called] if _called else available.iloc[0:0]
CUR = _by_label.iloc[0] if len(_by_label) else (_queue.iloc[0] if len(_queue) else None)
CUR_IS_OVERRIDE = bool(len(_by_label))
NOM = load_nomination()
CUR_FROM_ESPN = False
if not CUR_IS_OVERRIDE and NOM and NOM.get("player_id") is not None:       # the room has somebody up: follow it
    _nm = available[available["PLAYER_ID"] == int(NOM["player_id"])]
    if len(_nm):
        CUR, CUR_FROM_ESPN = _nm.iloc[0], True


def _live_status(r):
    """(level, text) for the live auction: bid on, drop out, or you lead. None without a bid from the room."""
    if not CUR_FROM_ESPN or NOM is None or NOM.get("bid") is None:
        return None
    bid, lead, ceiling = int(NOM["bid"]), NOM.get("team") or "?", float(r["YOUR_MAX_BID"])
    if lead == YOUR_TEAM:
        return "success", f"DU FÜHRST mit \\${bid}. Nur weiter bieten, wenn überboten wird (Limit \\${ceiling:,.0f})."
    if ceiling >= MIN_BID and ceiling >= bid + 1:
        return "success", f"BIETEN bis \\${ceiling:,.0f}. Aktuell \\${bid} ({lead}), nächstes Gebot \\${bid + 1}."
    return "error", f"AUSSTEIGEN. Aktuell \\${bid} ({lead}), dein Limit ist \\${ceiling:,.0f}."


def _verdict(r) -> tuple[str, str]:
    """(level, text) for the one-line recommendation under a player."""
    ceiling, live = float(r["YOUR_MAX_BID"]), float(r["LIVE_$"])
    if str(r.get("ACTION")) == "Bank":
        rk = r.get("BENCH_RANK")
        return "success", (f"**Bank-Kandidat" + (f" (Rang {int(rk)})" if pd.notna(rk) else " (Option)") + f":** bis \\${ceiling:,.0f} bieten, nicht mehr. "
                           "Er gehört nicht in den Zielkader, springt aber ein, wenn ein Starter ausfällt.")
    if not bool(r["MODEL_OK"]):
        return "info", "Keine eigene Projektion (Rookie / ohne Stats): ESPN-Preis als Referenz, der Optimierer empfiehlt ihn nie."
    if ceiling < MIN_BID:
        return "info", "Für deinen besten Kader nicht nötig oder nicht rosterbar: auslassen."
    if ceiling > live + 0.5:
        return "success", (f"**Er ist dir mehr wert als der Raum zahlt.** Bis **\\${ceiling:,.0f}** mitgehen; "
                           f"der Raum hört vermutlich bei ~\\${live:,.0f} auf.")
    if ceiling < live - 0.5:
        return "warning", (f"**Der Raum zahlt mehr als er deinem Kader bringt** (Prognose \\${live:,.0f} > dein \\${ceiling:,.0f}): laufen lassen.")
    return "info", "Fair bewertet: zum erwarteten Preis mitnehmen, nicht darüber."


def _injury_line(r):
    """(level, text) about ESPN's injury status and what the model does with it; None for healthy players."""
    status, src = str(r.get("INJURY_STATUS") or ""), str(r.get("AVAIL_SOURCE") or "")
    base, now = int(round(float(r["AVAIL_BASE"]) * 82)), int(r["EXP_GP"])
    note = str(r.get("INJURY_NOTE") or "").strip()
    if src.startswith("manuell"):
        return "info", f"Von dir angepasst: {now} Spiele erwartet (Modell ohne Anpassung: {base})." + (f" ESPN: {status}." if status else "")
    if src.startswith("ESPN"):
        return "warning", (f"⚠ ESPN: {status or 'verletzt'}. " + (note[:260] + ("…" if len(note) > 260 else "") + " " if note else "")
                           + f"Das Modell rechnet deshalb mit {now} statt {base} Spielen (Annahme: {availability.OUT_GAMES_LOST} Spiele weniger, unten anpassbar).")
    if status == "DAY_TO_DAY":
        return "caption", "ESPN: täglich fraglich (keine Anpassung im Modell)."
    return None


def _save_override(name: str, games: int) -> None:
    ov = load_overrides(); ov[name] = int(games)
    OVR_JSON.write_text(json.dumps(ov, indent=1, ensure_ascii=False))


def _delete_override(name: str) -> None:
    ov = load_overrides(); ov.pop(name, None)
    OVR_JSON.write_text(json.dumps(ov, indent=1, ensure_ascii=False))


# ── Pick callbacks (run before the script reruns, so they may reset widget state) ──────────────────────
def _pick_error(msg: str) -> None:
    st.session_state["entry_error"] = msg


def _pick_team(team: str) -> None:
    """Tap a team to select it (tap again to deselect); the pick is only logged by the entry button."""
    st.session_state["entry_team"] = None if st.session_state.get("entry_team") == team else team


def _submit_pick(pid: int, name: str) -> None:
    """Logs the player for the selected team at the price in the price field."""
    team = st.session_state.get("entry_team")
    if team is None:
        return _pick_error("Bitte zuerst das Team antippen, das ihn geholt hat.")
    price = int(st.session_state.get("entry_price") or MIN_BID)
    entries = st.session_state.draft_log
    if any(int(e["player_id"]) == pid for e in entries):
        return _pick_error(f"{name} ist schon geloggt.")
    free_slots = ROSTER_SIZE - sum(1 for e in entries if e["team"] == team)
    free_money = BUDGET - sum(int(e["price"]) for e in entries if e["team"] == team)
    if free_slots <= 0:
        return _pick_error(f"{team} hat keinen freien Roster-Slot mehr.")
    if price > free_money - (free_slots - 1) * MIN_BID:
        return _pick_error(f"{team} kann höchstens \\${free_money - (free_slots - 1) * MIN_BID} bieten "
                           f"(Rest \\${free_money}, {free_slots} Slots offen). Preis prüfen.")
    entries.append({"player_id": pid, "player_name": name, "team": team, "price": price})
    save_log(entries)
    st.session_state["last_pick"] = f"{name} → {team} für \\${price}"
    st.session_state["call_override"] = None
    st.session_state["entry_team"] = None


def _skip(pid: int) -> None:
    if pid not in st.session_state.skipped:
        st.session_state.skipped.append(pid)
    st.session_state["call_override"] = None


def _undo_last() -> None:
    entries = st.session_state.draft_log
    if entries:
        gone = entries.pop()
        save_log(entries)
        st.session_state["last_pick"] = f"zurückgenommen: {gone['player_name']}"
        st.session_state["call_override"] = None


# ── Sidebar ───────────────────────────────────────────────────────────────────
st.sidebar.title(meta["league_name"])
COMPACT = st.sidebar.toggle("Kompakte Live-Ansicht", key="compact",
                            help="Nur Spieler, Gebot und dein Limit, gross: für ein schmales Fenster neben dem ESPN-Draft-Room.")
st.sidebar.caption(f"Projektionen: {meta.get('generated_at', '?')[:16].replace('T', ' ')} · {meta.get('model_version', '?')}")
st.sidebar.caption(f"Board-Kurve: {MARKET_NAME} · wird live aus den Verkäufen nachgezogen")
st.sidebar.markdown("---")
st.sidebar.subheader("🔔 Jetzt aufgerufen")
if CUR is None:
    st.sidebar.caption("Alle Spieler vergeben.")
else:
    _c = float(CUR["YOUR_MAX_BID"])
    st.sidebar.markdown(f"**{CUR['PLAYER_NAME']}**  \n{CUR['POS_ELIGIBLE_STR']} · {CUR['CURRENT_TEAM_ABBR']}")
    st.sidebar.metric("ZAHLE BIS", "nicht bieten" if _c < MIN_BID else f"${_c:,.0f}",
                      delta=None if _c < MIN_BID else f"{_c - float(CUR['LIVE_$']):+.0f} vs Preisprognose ${float(CUR['LIVE_$']):,.0f}")
    _ls = _live_status(CUR)
    if _ls:
        getattr(st.sidebar, _ls[0])(_ls[1])
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
                  "Slots offen": [remaining_slots[t] for t in TEAM_NAMES],
                  # the most a team can bid on ONE player: it must keep the minimum bid for every other open slot
                  "Max-Gebot": [max(remaining_budget[t] - (remaining_slots[t] - 1) * MIN_BID, 0) if remaining_slots[t] > 0 else 0
                                for t in TEAM_NAMES]}).sort_values("Max-Gebot", ascending=False),
    hide_index=True, width="stretch")
st.sidebar.caption("Max-Gebot = was das Team für EINEN Spieler höchstens bieten darf. Nur wer hier mehr hat als du, kann dich überbieten.")

# ── Header ──────────────────────────────────────────────────────────────────
st.title("🏀 Live Draft Assistant")


@st.fragment(run_every=1.5)
def _autosync():
    """Pick up what bridge.py wrote (new sales, a new nomination) without any click."""
    now = (_mt(LOG_JSON), _mt(NOM_JSON), _mt(UNRES_JSON))
    seen = st.session_state.get("_sync_mt")
    if now != seen:
        if seen is None or now[0] != seen[0]:
            st.session_state.draft_log = load_log()
        st.session_state["_sync_mt"] = now
        st.rerun()


_autosync()


def _clear_unresolved() -> None:
    UNRES_JSON.write_text("[]")
    mt = st.session_state.get("_sync_mt", (0, 0, 0))
    st.session_state["_sync_mt"] = (mt[0], mt[1], _mt(UNRES_JSON))


_unres = load_unresolved()
if _unres:
    for _u in _unres:
        _sg = f" Meintest du: {', '.join(_u['suggestions'])}?" if _u.get("suggestions") else ""
        st.warning(f"**ESPN meldet einen Verkauf, den ich nicht zuordnen konnte** ({_u['why']}): {_u['espn_name']} → {_u.get('team')} "
                   f"für \\${_u.get('price')}.{_sg} Bitte unten von Hand eintragen.")
    st.button("Erledigt (von Hand eingetragen)", on_click=_clear_unresolved)

# ── Live panel: who is up, what the room bid, what you may pay ────────────────────────────────────────────
st.markdown("""<style>
.st-key-livepanel [data-testid="stMetricValue"] {font-size: 3rem; line-height: 1.1;}
.st-key-livepanel [data-testid="stMetricLabel"] p {font-size: 1rem; font-weight: 600;}
</style>""", unsafe_allow_html=True)
with st.container(key="livepanel", border=True):
    if CUR is None:
        st.success("Alle Spieler vergeben.")
    else:
        _r = CUR
        _ceil, _live = float(_r["YOUR_MAX_BID"]), float(_r["LIVE_$"])
        _src = "live aus dem ESPN-Draft-Room" if CUR_FROM_ESPN else ("von dir gewählt" if CUR_IS_OVERRIDE else "nächster in ESPN-Reihenfolge (keine Verbindung zum Raum)")
        st.markdown(f"<div style='font-size:2.6rem;font-weight:800;line-height:1.1'>{html.escape(str(_r['PLAYER_NAME']))}</div>"
                    f"<div style='opacity:.7;margin-bottom:.4rem'>{html.escape(str(_r['POS_ELIGIBLE_STR']))} · {html.escape(str(_r['CURRENT_TEAM_ABBR']))} · {html.escape(_src)}</div>",
                    unsafe_allow_html=True)
        _il = _injury_line(_r)
        if _il:
            getattr(st, _il[0])(_il[1])
        _bid = NOM.get("bid") if (CUR_FROM_ESPN and NOM) else None
        _lead = (NOM.get("team") or "") if (CUR_FROM_ESPN and NOM) else ""
        lc1, lc2, lc3 = st.columns(3)
        lc1.metric("Aktuelles Gebot", f"${int(_bid)}" if _bid is not None else "—", delta=_lead or None, delta_color="off")
        lc2.metric("ZAHLE BIS", "nicht bieten" if _ceil < MIN_BID else f"${_ceil:,.0f}")
        lc3.metric("Preisprognose", f"${_live:,.0f}")
        _st = _live_status(_r)
        if _st:
            getattr(st, _st[0])(_st[1])
        else:
            _lvl, _txt = _verdict(_r)
            getattr(st, _lvl)(_txt)

if COMPACT:
    st.caption(f"Dein Rest: \\${your_remaining_budget} · {your_remaining_slots} Slots offen · Preisniveau {PLAN['inflation']:.2f}× Board · {len(log)}/{TOTAL_DRAFTED} Picks")
    for _e in reversed(log[-4:]):
        st.caption(f"Zuletzt verkauft: {_e['player_name']} → {_e['team']} für \\${_e['price']}")
    st.stop()

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

# ── Who is up: player card + price entry, next to the upcoming queue ───────────────────────────────────────
st.subheader("🔔 Jetzt aufgerufen")
if "last_pick" in st.session_state:
    st.caption(f"Zuletzt: {st.session_state['last_pick']}")
if "entry_error" in st.session_state:
    st.error(st.session_state.pop("entry_error"))

with st.container():
    _opts = sorted(_label_av)
    _gone = next((e for e in log if ascii_label(e["player_name"]) == _called), None) if _called and not CUR_IS_OVERRIDE else None
    if _called and not CUR_IS_OVERRIDE:                      # the chosen player left the pool (e.g. a pick was logged elsewhere)
        st.session_state["call_override"] = None
    st.selectbox("Anderer Spieler aufgerufen? (leer = nächster in ESPN-Reihenfolge)", _opts, index=None,
                 placeholder="tippen: z.B. jokic, sengun, ware …", key="call_override")
    if _gone is not None:
        st.error(f"**{_gone['player_name']}** ist weg: {_gone['team']} hat ihn für **\\${_gone['price']}** geholt.")
    if CUR is None:
        st.success("Alle Spieler vergeben.")
    else:
        r = CUR
        ceiling, live = float(r["YOUR_MAX_BID"]), float(r["LIVE_$"])
        rk = r.get("ESPN_RANK")
        st.markdown(f"### {r['PLAYER_NAME']}")
        st.caption(f"{r['POS_ELIGIBLE_STR']} · {r['CURRENT_TEAM_ABBR']} · {r['AGE']:.0f} Jahre · {r['RISK_FLAG']} "
                   f"({int(r['EXP_GP'])} Spiele erwartet)"
                   + (f" · ESPN-Rang {int(rk)}" if pd.notna(rk) else "")
                   + (" · **im aktuellen Zielkader**" if r["IN_TARGET"] else "")
                   + (" · von dir gewählt, nicht der nächste in der ESPN-Reihenfolge" if CUR_IS_OVERRIDE else "")
                   + (" · **live aus dem ESPN-Draft-Room**" if CUR_FROM_ESPN else ""))
        k1, k2 = st.columns(2)
        k1.metric("ZAHLE BIS", "nicht bieten" if ceiling < MIN_BID else f"${ceiling:,.0f}",
                  delta=None if ceiling < MIN_BID else f"{ceiling - live:+.0f} vs Preisprognose",
                  help="Break-even: bis hierher bleibt dein bester Kader mit ihm mindestens so gut wie ohne ihn.")
        k2.metric("Preisprognose (live)", f"${live:,.0f}")
        k2.caption(f"ESPN-Liste {'$' + format(float(r['ESPN_$_LIST']), ',.0f') if pd.notna(r.get('ESPN_$_LIST')) else '—'} · Aktion: **{r['ACTION']}**")
        _ls = _live_status(r)
        if _ls:
            getattr(st, _ls[0])(_ls[1])
        _lvl, _txt = _verdict(r)
        getattr(st, _lvl)(_txt)

        _cur_pid, _cur_name = int(r["PLAYER_ID"]), str(r["PLAYER_NAME"])
        # the price field starts at the live forecast for whoever is up (re-set whenever the player changes)
        if st.session_state.get("_price_for") != _cur_pid:
            st.session_state["entry_price"] = int(min(max(round(live), MIN_BID), BUDGET))
            st.session_state["_price_for"] = _cur_pid
            st.session_state["entry_team"] = None
        st.number_input("Preis ($) — Prognose ist vorgewählt, bei Bedarf ändern", min_value=MIN_BID, max_value=BUDGET, step=1,
                        key="entry_price")
        st.markdown("**Wer hat ihn geholt?** Team antippen, dann „Eintragen“.")
        _entry_price = int(st.session_state.get("entry_price") or MIN_BID)
        _sel = st.session_state.get("entry_team")
        # teams alphabetically, two compact columns on the left; the big entry button (and skip / undo) on the right
        st.markdown("""<style>
        .st-key-entry_btn button {min-height: 15rem;}
        .st-key-entry_btn button p {font-size: 1.6rem; line-height: 1.6;}
        .st-key-teamgrid button {padding: 0.35rem 0.4rem;}
        .st-key-teamgrid button p {font-size: 0.85rem;}
        </style>""", unsafe_allow_html=True)
        _teams_sorted = sorted(TEAM_NAMES, key=lambda t: t.casefold())
        grid, side = st.columns([3, 2])
        with grid:
            with st.container(key="teamgrid"):
                tcols = st.columns(2)
                half = (len(_teams_sorted) + 1) // 2
                for n, t in enumerate(_teams_sorted):
                    cap = remaining_budget[t] - max(remaining_slots[t] - 1, 0) * MIN_BID    # most this team can still pay for one player
                    can = remaining_slots[t] > 0 and _entry_price <= cap
                    tcols[0 if n < half else 1].button(
                        f"{'★ ' if t == YOUR_TEAM else ''}{t}  \n${remaining_budget[t]} · {remaining_slots[t]} Slots",
                        key=f"team_btn_{TEAM_NAMES.index(t)}", on_click=_pick_team, args=(t,), width="stretch",
                        type="primary" if t == _sel else "secondary", disabled=not can,
                        help=None if can else f"{t} kann höchstens ${max(cap, 0)} bieten (oder hat keinen freien Slot).")
        with side:
            _sel_ok = (_sel is not None and remaining_slots[_sel] > 0
                       and _entry_price <= remaining_budget[_sel] - max(remaining_slots[_sel] - 1, 0) * MIN_BID)
            st.button(f"Eintragen ✓  \n**{_sel}**  \n${_entry_price}" if _sel else "Eintragen ✓  \n(erst ein Team antippen)",
                      key="entry_btn", on_click=_submit_pick, args=(_cur_pid, _cur_name), width="stretch",
                      type="primary", disabled=not _sel_ok)
            st.button("⏭ Nicht aufgerufen — weiter", on_click=_skip, args=(_cur_pid,), width="stretch",
                      help="Schiebt ihn ans Ende der Reihenfolge, falls der Raum einen anderen zuerst aufruft.")
            st.button("↶ Letzten Pick zurücknehmen", on_click=_undo_last, disabled=not log, width="stretch")

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

with st.expander("Verletzung / erwartete Spiele anpassen"):
    st.caption(f"ESPN-Status OUT zieht automatisch {availability.OUT_GAMES_LOST} Spiele ab (eine Annahme, keine Messung). Hier legst du für einen "
               "Spieler die erwarteten Spiele (von 82) selbst fest: gilt sofort für alle Max-Gebote und den Zielkader. "
               "ESPN-Status aktualisieren: `python draft_app/refresh_injuries.py`.")
    _ov_labels = {ascii_label(n): n for n in pool["PLAYER_NAME"]}
    _ov_pick = st.selectbox("Spieler", sorted(_ov_labels), index=None, placeholder="tippen: z.B. ingram …", key="ovr_player")
    if _ov_pick:
        _ov_name = _ov_labels[_ov_pick]
        _row = pool[pool["PLAYER_NAME"] == _ov_name].iloc[0]
        _line = _injury_line(_row)
        if _line:
            getattr(st, _line[0])(_line[1])
        _g = st.number_input("Erwartete Spiele (von 82)", min_value=0, max_value=82, value=int(_row["EXP_GP"]), step=1, key=f"ovr_games_{int(_row['PLAYER_ID'])}")
        _b1, _b2 = st.columns(2)
        _b1.button("Speichern", on_click=_save_override, args=(_ov_name, int(_g)), width="stretch")
        _b2.button("Anpassung löschen", on_click=_delete_override, args=(_ov_name,), width="stretch", disabled=_ov_name not in load_overrides())
    _ovs = load_overrides()
    if _ovs:
        st.dataframe(pd.DataFrame({"Spieler": list(_ovs), "Erwartete Spiele": list(_ovs.values())}), hide_index=True, width="stretch")

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
                     "Preis $": paid_price if bought else round(float(PLAN.get("target_price", PLAN["price"])[i]), 1),
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

# ── Bench suggestions ──────────────────────────────────────────────────────────────────────────────────
st.subheader("🪑 Bank-Vorschläge")
if BENCH:
    st.caption(f"Wen du nach dem Zielkader für die Bank holen sollst (Bank-Preislimit je Platz: \\${BENCH_CAP}). Bewertet wird, wie viel ein Spieler hilft, "
               "wenn ein Starter ausfällt: ohne IR und mit wöchentlicher Aufstellung bleibt der Platz sonst leer. Rang 1 bis "
               f"{sum(1 for _b in BENCH if _b['rank'])} sind die besten Kombinationen (decken verschiedene Starter ab). Der Index ist relativ (bester Spieler = 100), "
               "keine Gewinnprognose; Center zählen ins Limit von 4.")
    _rows = []
    for _b in BENCH:
        _i = _b["idx"]
        _rows.append({"Rang": str(_b["rank"]) if _b["rank"] else "", "Spieler": MODEL.names[_i], "Pos": pool["POS_ELIGIBLE_STR"].iat[_i],
                      "Team": pool["CURRENT_TEAM_ABBR"].iat[_i], "Prognose $": round(float(PLAN["price"][_i]), 1),
                      "Wert (Index)": int(round(100 * _b["value"] / max(max(x["value"] for x in BENCH), 1e-9))),
                      "Springt ein für": ", ".join(MODEL.names[k] for k in _b["covers"]), "Verfügbarkeit": round(float(MODEL.avail[_i]), 2)})
    st.dataframe(pd.DataFrame(_rows), hide_index=True, width="stretch")
elif your_remaining_slots > 0 and len(PLAN["fixed"]) < 10:
    st.caption("Noch keine Vorschläge: sie erscheinen, sobald der Zielkader berechnet ist und Bankplätze offen sind.")
else:
    st.caption("Keine offenen Bankplätze oder keine passenden Kandidaten.")

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
