"""
Weekly lineup advisor: who should start in this week's matchup?
===============================================================
For one matchup week it takes both rosters, counts how many games each player's NBA team plays in that week (the weekly
lineup lock means a four-game player is worth about twice a two-game player), applies each player's availability and
per-game projection, models the two teams' category totals, and picks the ten starters (legal under the league's slot rules,
max 4 centres on the roster) that maximise YOUR chance to win the matchup (6 of 10 categories, 5-5 counts half).

    python draft_app/weekly_lineup.py --init-rosters                 # once: league_rosters.json from the draft log
    python draft_app/weekly_lineup.py --week 1                       # opponent and dates from league_schedule.json
    python draft_app/weekly_lineup.py --opponent "Brick City"        # this coming Mon-Sun, opponent given by hand
    python draft_app/weekly_lineup.py --opponent "Brick City" --start 2026-10-20 --end 2026-10-25 --refresh

league_rosters.json ({team: [player names]}) is YOUR source of truth after the draft: edit it after trades, adds and drops.
The NBA schedule comes from nba_api (cached in cache/schedule_2026-27.json); ESPN injury status from refresh_injuries.py.
What it does NOT know: which of your players ESPN's own lineup rules block on a given day, and the opponent's actual choices:
the opponent is assumed to play his best legal ten.
"""

import argparse
import datetime as dt
import itertools
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

APP_DIR = Path(__file__).parent
ROOT = APP_DIR.parent
sys.path.insert(0, str(APP_DIR))
import availability  # noqa: E402
import engine  # noqa: E402
import names  # noqa: E402

SEASON = "2026-27"
SCHEDULE_CACHE = ROOT / "cache" / f"schedule_{SEASON}.json"
ROSTERS_JSON = APP_DIR / "league_rosters.json"
SCHEDULE_JSON = APP_DIR / "league_schedule.json"      # {"periods": [{"week":1,"start":"..","end":"..","matchups":[[A,B],..]}]}
NEUTRAL_GAMES = 3.45                                   # used for players whose team is unknown
TEAM_ALIASES = {"Unsupervised Chaos": "Team 15"}      # ESPN renamed the team after the draft; league_rosters.json keeps the draft-log name


# ── schedule ─────────────────────────────────────────────────────────────────────────────────────────
def load_schedule(refresh: bool = False) -> list:
    """[(date, home tricode, away tricode)] of the regular season."""
    if SCHEDULE_CACHE.exists() and not refresh and time.time() - SCHEDULE_CACHE.stat().st_mtime < 12 * 3600:
        return [tuple(x) for x in json.loads(SCHEDULE_CACHE.read_text())]
    from nba_api.stats.endpoints import scheduleleaguev2
    data = scheduleleaguev2.ScheduleLeagueV2(season=SEASON, timeout=60).get_dict()["leagueSchedule"]["gameDates"]
    games = []
    for day in data:
        for g in day["games"]:
            if str(g.get("gameId", "")).startswith("002"):               # regular season only (not preseason / playoffs)
                games.append((g["gameDateEst"][:10], g["homeTeam"]["teamTricode"], g["awayTeam"]["teamTricode"]))
    SCHEDULE_CACHE.parent.mkdir(exist_ok=True)
    SCHEDULE_CACHE.write_text(json.dumps(games))
    return games


def games_per_team(schedule: list, start: str, end: str) -> dict:
    """Number of regular-season games each NBA team plays from `start` to `end` (both included, YYYY-MM-DD)."""
    out: dict = {}
    for day, home, away in schedule:
        if start <= day <= end:
            out[home] = out.get(home, 0) + 1
            out[away] = out.get(away, 0) + 1
    return out


def next_monday(today: dt.date) -> dt.date:
    return today + dt.timedelta(days=(7 - today.weekday()) % 7 or 7)


# ── data ─────────────────────────────────────────────────────────────────────────────────────────────
def load_pool_and_meta():
    meta = json.loads((APP_DIR / "league_meta.json").read_text())
    pool = pd.read_csv(APP_DIR / "player_pool.csv").reset_index(drop=True)
    try:
        inj = json.loads((APP_DIR / "injuries.json").read_text()).get("players", {})
    except FileNotFoundError:
        inj = {}
    try:
        ovr = json.loads((APP_DIR / "availability_overrides.json").read_text())
    except FileNotFoundError:
        ovr = {}
    pool = availability.adjust_pool(pool, inj, ovr)
    cal = meta.get("calibration")
    if cal:
        engine.CAL_A, engine.CAL_B = cal["cal_a"], cal["cal_b"]
    return pool, meta


def init_rosters() -> dict:
    log = json.loads((APP_DIR / "draft_log.json").read_text())
    rosters: dict = {}
    for e in log:
        rosters.setdefault(e["team"], []).append(e["player_name"])
    ROSTERS_JSON.write_text(json.dumps(rosters, indent=1, ensure_ascii=False))
    return rosters


def resolve_roster(pool: pd.DataFrame, player_names: list):
    """Pool indices for a list of names; second value: names that are not in the pool (ignored)."""
    idx = names.build_index(pool["PLAYER_ID"], pool["PLAYER_NAME"])
    pid2i = {int(p): i for i, p in enumerate(pool["PLAYER_ID"])}
    ok, missing = [], []
    for n in player_names:
        hit = names.resolve_player(idx, n)
        (ok if hit else missing).append(pid2i[hit[0]] if hit else n)
    return ok, missing


# ── the weekly model ─────────────────────────────────────────────────────────────────────────────────
def week_model(pool: pd.DataFrame, meta: dict, team_games: dict):
    """PoolModel for ONE week: every player's expected games = his team's games that week x his availability."""
    g = pool["CURRENT_TEAM_ABBR"].map(team_games).astype(float)
    unknown = g.isna()
    g = g.fillna(NEUTRAL_GAMES).to_numpy()
    p = pool.copy()
    out = p["INJURY_STATUS"].fillna("") == "OUT"
    p.loc[out, "AVAIL"] = 0.05                                                  # ESPN says OUT: assume he does not play this week
    rules = engine.Rules.from_meta(meta)
    model = engine.build_pool_model(p, rules, meta["league_rates"], games=g)
    return model, g, unknown.to_numpy(), out.to_numpy()


def best_lineup(model, roster, vs_mom, calibrate=True):
    """Exhaustive search over which ten of the roster start: returns (lineup, p_win, p_cat) against a fixed opponent."""
    roster = [int(i) for i in roster]
    ns = model.rules.n_starters
    if len(roster) <= ns:
        mom = model.team_moments(roster)
        pc = engine.phi((mom[0] - vs_mom[0]) / np.sqrt(mom[1] ** 2 + vs_mom[1] ** 2))
        return roster, float(engine.matchup_prob(pc, model.rules)), pc
    best = None
    for bench in itertools.combinations(roster, len(roster) - ns):
        st = [i for i in roster if i not in bench]
        if not engine.lineup_legal(model, st):
            continue
        mu, sd = model.team_moments(st)
        pc = engine.phi((mu - vs_mom[0]) / np.sqrt(sd ** 2 + vs_mom[1] ** 2))
        p = float(engine.matchup_prob(pc, model.rules))
        if best is None or p > best[1]:
            best = (st, p, pc)
    return best


def neutral_field(model, rosters: dict):
    """Mean category moments of every team's top ten by weekly expected production (a stand-in opponent for 'his best ten')."""
    z = ((model.E - model.E.mean(0)) / np.maximum(model.E.std(0), 1e-9)).sum(1)
    moms = []
    for r in rosters.values():
        top = sorted(r, key=lambda i: -z[i])[:model.rules.n_starters]
        moms.append(model.team_moments(top))
    return np.mean([m[0] for m in moms], axis=0), np.sqrt(np.mean([m[1] ** 2 for m in moms], axis=0))


def alternatives(model, roster, lineup, vs_mom, base_p, k=4):
    """The closest changes to the recommended ten: swap one starter for one bench player (legal ones only)."""
    bench = [i for i in roster if i not in lineup]
    rows = []
    for s_ in lineup:
        for b in bench:
            t = [b if x == s_ else x for x in lineup]
            if not engine.lineup_legal(model, t):
                continue
            mu, sd = model.team_moments(t)
            p = float(engine.matchup_prob(engine.phi((mu - vs_mom[0]) / np.sqrt(sd ** 2 + vs_mom[1] ** 2)), model.rules))
            rows.append((p - base_p, s_, b))
    return sorted(rows, reverse=True)[:k]


# ── report ───────────────────────────────────────────────────────────────────────────────────────────
def fmt(x, d=1):
    return f"{x:.{d}f}"


def run(args) -> int:
    pool, meta = load_pool_and_meta()
    you = args.team or meta["your_team_name"]
    if not ROSTERS_JSON.exists():
        print("league_rosters.json fehlt: wird aus dem Draft-Log erzeugt (danach nach Trades/Adds/Drops von Hand anpassen).")
        init_rosters()
    rosters_names = json.loads(ROSTERS_JSON.read_text())

    # week: dates and opponent
    opponent, start, end, label = args.opponent, args.start, args.end, ""
    if args.week and SCHEDULE_JSON.exists():
        per = next((p for p in json.loads(SCHEDULE_JSON.read_text())["periods"] if int(p["week"]) == args.week), None)
        if per is None:
            print(f"Woche {args.week} steht nicht in league_schedule.json"); return 2
        start, end = start or per["start"], end or per["end"]
        label = f"Woche {args.week}"
        if not opponent:
            for a, b in per["matchups"]:
                if a == you: opponent = b
                if b == you: opponent = a
    if not opponent:
        print("Gegner fehlt: --opponent \"Teamname\" oder --week N mit league_schedule.json"); return 2
    opponent = TEAM_ALIASES.get(opponent, opponent) if opponent else opponent
    you = TEAM_ALIASES.get(you, you)
    if opponent not in rosters_names or you not in rosters_names:
        print("Teams in league_rosters.json:", ", ".join(rosters_names)); return 2
    if not start:
        start = next_monday(dt.date.today()).isoformat()
    if not end:
        end = (dt.date.fromisoformat(start) + dt.timedelta(days=6)).isoformat()

    schedule = load_schedule(args.refresh)
    tg = games_per_team(schedule, start, end)
    if not tg:
        print(f"Keine NBA-Spiele zwischen {start} und {end} im Spielplan."); return 2

    model, games, unknown, out_mask = week_model(pool, meta, tg)
    resolved = {t: resolve_roster(pool, ns) for t, ns in rosters_names.items()}
    rosters = {t: r for t, (r, _) in resolved.items()}
    for t in (you, opponent):
        if resolved[t][1]:
            print(f"Hinweis {t}: nicht im Pool, ignoriert: {', '.join(resolved[t][1])}")
    my_roster, opp_roster = rosters[you], rosters[opponent]

    field = neutral_field(model, rosters)
    opp_lineup, _, _ = best_lineup(model, opp_roster, field)
    opp_mom = model.team_moments(opp_lineup)
    lineup, p_win, pcat = best_lineup(model, my_roster, opp_mom)
    my_mom = model.team_moments(lineup)
    slots = engine.assign_slots(model, lineup)

    print(f"\n{'=' * 100}\n{label + ': ' if label else ''}{you}  vs  {opponent}      {start} bis {end}\n{'=' * 100}")
    print(f"Siegchance in dieser Woche: {100 * p_win:.0f} %   ·   erwartete Kategorien: {pcat.sum():.1f} von 10")
    dist = {}
    for n_ in tg.values():
        dist[n_] = dist.get(n_, 0) + 1
    print("NBA-Teams nach Spielzahl in dieser Woche: " + ", ".join(f"{n_} Spiele: {c} Teams" for n_, c in sorted(dist.items())))

    def row(i):
        e = model.E[i]
        return dict(P=model.names[i], POS=pool["POS_ELIGIBLE_STR"].iat[i], TEAM=pool["CURRENT_TEAM_ABBR"].iat[i], G=int(round(games[i])),
                    EXP=model.avail[i] * games[i], PTS=e[engine.CATS.index("PTS")], REB=e[engine.CATS.index("REB")],
                    AST=e[engine.CATS.index("AST")], STL=e[engine.CATS.index("STL")], BLK=e[engine.CATS.index("BLK")],
                    TPM=e[engine.CATS.index("FG3M")], DD=e[engine.CATS.index("DD")], ST=pool["INJURY_STATUS"].iat[i] or "")
    slot_of = {pl: model.rules.slots[sl][0] for sl, pl in slots.items()}
    rows = []
    for i in sorted(my_roster, key=lambda i: (i not in lineup, -model.E[i].sum())):
        r = row(i); r["START"] = "START " + slot_of.get(i, "") if i in lineup else "Bank"
        rows.append(r)
    t = pd.DataFrame(rows)[["START", "P", "POS", "TEAM", "G", "EXP", "PTS", "REB", "AST", "STL", "BLK", "TPM", "DD", "ST"]]
    t = t.rename(columns={"P": "Spieler", "G": "Spiele (Team)", "EXP": "erw. Spiele", "TPM": "3PM", "ST": "ESPN"})
    pd.set_option("display.width", 200)
    print("\nDein Kader und die empfohlene Aufstellung (erwartete Wochensummen):")
    print(t.round(1).to_string(index=False))

    print("\nKategorien: erwartete Werte und Gewinnchance (du / Gegner)")
    cats = engine.CATS
    for k, c in enumerate(cats):
        a, b = my_mom[0][k], opp_mom[0][k]
        if c in ("FG_PCT", "FT_PCT", "PTS_MIN"):
            fa, fb = f"{a:.3f}", f"{b:.3f}"
        else:
            fa, fb = f"{a:.1f}", f"{b:.1f}"
        print(f"  {engine.LABEL[c]:<6} {fa:>8} vs {fb:>8}   Gewinnchance {100 * pcat[k]:3.0f} %  " + ("█" * int(round(pcat[k] * 20))))
    swing = [engine.LABEL[c] for k, c in enumerate(cats) if 0.35 <= pcat[k] <= 0.65]
    print("  Umkämpft (35-65 %):", ", ".join(swing) if swing else "keine")

    alts = alternatives(model, my_roster, lineup, opp_mom, p_win)
    print("\nNächstbeste Änderungen (ein Starter gegen einen Bankspieler):")
    for dv, s_, b in alts:
        print(f"  statt {model.names[s_]} ({int(round(games[s_]))} Spiele) -> {model.names[b]} ({int(round(games[b]))} Spiele): {100 * dv:+.1f} Prozentpunkte")

    flags = []
    for i in my_roster:
        if i in lineup and games[i] <= 2:
            flags.append(f"{model.names[i]} hat nur {int(round(games[i]))} Spiele")
        if out_mask[i]:
            flags.append(f"{model.names[i]} ist bei ESPN OUT")
        if unknown[i]:
            flags.append(f"{model.names[i]}: Team {pool['CURRENT_TEAM_ABBR'].iat[i]} nicht im Spielplan, mit {NEUTRAL_GAMES} Spielen gerechnet")
    if flags:
        print("\nAchtung:", "; ".join(flags))
    print("\nGegnerische Aufstellung (angenommen: sein bestes Zehner):", ", ".join(model.names[i] for i in opp_lineup))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--opponent", help="Teamname des Gegners (wie in league_rosters.json)")
    ap.add_argument("--week", type=int, help="Matchup-Woche aus league_schedule.json (Gegner und Daten automatisch)")
    ap.add_argument("--start", help="Start YYYY-MM-DD (Standard: kommender Montag)")
    ap.add_argument("--end", help="Ende YYYY-MM-DD (Standard: Start + 6 Tage)")
    ap.add_argument("--team", help="dein Team (Standard: aus league_meta.json)")
    ap.add_argument("--rosters", help="Pfad zu einer anderen Roster-Datei")
    ap.add_argument("--refresh", action="store_true", help="NBA-Spielplan neu laden")
    ap.add_argument("--init-rosters", action="store_true", help="league_rosters.json aus dem Draft-Log erzeugen und beenden")
    args = ap.parse_args()
    if args.rosters:
        global ROSTERS_JSON
        ROSTERS_JSON = Path(args.rosters)
    if args.init_rosters:
        r = init_rosters()
        print(f"league_rosters.json geschrieben: {len(r)} Teams, {sum(len(v) for v in r.values())} Spieler")
        return 0
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
