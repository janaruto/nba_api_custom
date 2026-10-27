"""
This season's per-game averages (2026-27 so far) for the weekly matchup predictions
==================================================================================
The pre-season projection (a mix of last seasons and ESPN's forecast) is the best guess before anybody has played. Once the
season runs, the weekly advisor uses what the players actually do: the per-game averages of this season's regular-season games,
from stats.nba.com (nba_api `LeagueGameLog`, one request for all players).

For every player with at least `min_games` games this season the per-game means (MIN, PTS, REB, AST, STL, BLK, 3PM, FGM, FGA,
FTM, FTA and the double-double RATE = share of games with two or more of PTS/REB/AST/STL/BLK at 10+) replace the projection.
Players without a game yet (injured, not played so far) keep their projection. The per-game spreads stay those of the projection:
a few games say nothing reliable about a player's spread.

`shrink` K > 0 mixes the average with the projection by sample size, w = games / (games + K): K = 0 is the pure season average (what is
used by default), K = 10 would trust a 3-game average only 23 %. After a handful of games the averages are noisy: one 30-point night
moves a 3-game scoring average by 10 points.

A game in which a player got hurt is NOT counted: if his minutes are far below his usual (under 55 % of the median of his other games, or
under 10 minutes if he has no other game; only for rotation players with at least 15 median minutes) and he then missed the next game of his team, the short game is an injury, not his form. Without
this rule one 10-minute game after an injury drags the average of, say, a 28-minute centre down so far that the league tools keep offering
him in trades. Blowout games do not trigger it: the player appears in his team's next game.

The answer is cached aggregated per player (cache/season_stats_<season>.csv) and refreshed after a few hours, because games are added daily.
"""

import time
from pathlib import Path

import numpy as np
import pandas as pd

import names

SEASON = "2026-27"
ROOT = Path(__file__).parent.parent
CACHE = ROOT / "cache" / f"season_stats_{SEASON}.csv"
MAX_AGE_H = 3.0
INJURY_MIN_SHARE = 0.55        # an "injury game" has fewer minutes than this share of the median of the player's other games ...
INJURY_ABS_MIN = 10.0          # ... or fewer than this many minutes if he has no other game, and he misses his team's next game
INJURY_ROTATION = 15.0         # only for rotation players (median of the other games at least this many minutes): fringe players sit out often
NAME_MATCHED = ["ESPN-Projektion", "ESPN-only (proxy line)", "Waiver (Saisonwerte)"]    # rows with ESPN ids: matched by name, never by id
RAW = ["MIN", "PTS", "REB", "AST", "STL", "BLK", "FG3M", "FGM", "FGA", "FTM", "FTA", "DD"]


def _minutes(v) -> float:
    if isinstance(v, str) and ":" in v:
        a, b = v.split(":")
        return int(a) + int(b) / 60
    try:
        return float(v) if pd.notna(v) else 0.0
    except (TypeError, ValueError):
        return 0.0


def fetch_gamelog(season: str = SEASON) -> pd.DataFrame:
    from nba_api.stats.endpoints import leaguegamelog
    return leaguegamelog.LeagueGameLog(season=season, player_or_team_abbreviation="P",
                                       season_type_all_star="Regular Season", timeout=60).get_data_frames()[0]


def injury_games(d: pd.DataFrame) -> pd.Series:
    """Boolean per row of `d` (rows = games actually played): True for a game cut short by an injury (see the module docstring)."""
    d = d.reset_index(drop=True)
    appeared = set(zip(d["PLAYER_ID"], d["GAME_DATE"].astype(str)))
    team_dates = {t: sorted(set(g["GAME_DATE"].astype(str))) for t, g in d.groupby("TEAM_ABBREVIATION")} if "TEAM_ABBREVIATION" in d else {}
    flag = np.zeros(len(d), bool)
    for pid, g in d.groupby("PLAYER_ID"):
        for ix, row in g.iterrows():
            others = g.loc[g.index != ix, "MIN"]
            if not others.empty and float(others.median()) < INJURY_ROTATION:
                continue
            short = row["MIN"] < INJURY_ABS_MIN if others.empty else row["MIN"] < INJURY_MIN_SHARE * float(others.median())
            if not short:
                continue
            dates = team_dates.get(row.get("TEAM_ABBREVIATION"), [])
            later = [x for x in dates if x > str(row["GAME_DATE"])]
            if later and (pid, later[0]) not in appeared:               # missed his team's very next game
                flag[ix] = True
    return pd.Series(flag, index=d.index)


def aggregate(logs: pd.DataFrame) -> pd.DataFrame:
    """One row per player: GP (games counted), the per-game means of RAW (DD = rate of double-double games), the name, the team,
    the last game date and EXCLUDED = games left out because an injury cut them short. A player whose only games were such
    games has GP 0 (he keeps his projection)."""
    d = logs.copy()
    d["MIN"] = d["MIN"].map(_minutes)
    d = d[d["MIN"] > 0].reset_index(drop=True)                          # a row without minutes is a player who did not play
    d["INJ"] = injury_games(d)
    d["DD"] = ((d[["PTS", "REB", "AST", "STL", "BLK"]] >= 10).sum(axis=1) >= 2).astype(float)
    ok = d[~d["INJ"]]
    g, every = ok.groupby("PLAYER_ID"), d.groupby("PLAYER_ID")
    out = g[RAW].mean().reindex(every.size().index)                       # a player whose only games were injury games stays, with GP 0
    out.insert(0, "GP", g.size().reindex(out.index).fillna(0).astype(int))
    out["PLAYER_NAME"] = every["PLAYER_NAME"].last()
    out["TEAM"] = every["TEAM_ABBREVIATION"].last() if "TEAM_ABBREVIATION" in d else ""
    out["LAST_GAME"] = every["GAME_DATE"].max().astype(str)
    out["EXCLUDED"] = every["INJ"].sum().astype(int)
    return out.reset_index()


def load(refresh: bool = False, max_age_h: float = MAX_AGE_H):
    """(stats, note): the aggregated table or None. Fresh cache first, else a new request; a stale cache if the request fails."""
    if CACHE.exists() and not refresh and time.time() - CACHE.stat().st_mtime < max_age_h * 3600:
        cached = pd.read_csv(CACHE)
        if "EXCLUDED" in cached:                                       # a cache from before the injury rule is thrown away
            return cached, None
    try:
        stats = aggregate(fetch_gamelog())
        if len(stats) == 0:
            raise ValueError("keine Spiele")
        CACHE.parent.mkdir(exist_ok=True)
        stats.to_csv(CACHE, index=False)
        return stats, None
    except Exception as e:
        if CACHE.exists():
            age = (time.time() - CACHE.stat().st_mtime) / 3600
            return pd.read_csv(CACHE), f"stats.nba.com nicht erreichbar ({type(e).__name__}), nehme den Stand von vor {age:.0f} Std."
        return None, f"Saison-Schnitte nicht verfügbar ({type(e).__name__})"


def apply(pool: pd.DataFrame, stats: pd.DataFrame, min_games: int = 1, shrink: float = 0.0):
    """Copy of the pool whose per-game means M_* (and the PRED_* display columns) are this season's averages. Adds SEASON_GP
    (games so far, 0 = none) and SEASON_SRC ('Saison', 'Mischung' or 'Projektion'). Returns (pool, info)."""
    p = pool.copy()
    by_id = {int(r.PLAYER_ID): r for r in stats.itertuples(index=False)}
    by_key: dict = {}
    for r in stats.itertuples(index=False):
        k = names.name_key(r.PLAYER_NAME)
        by_key[k] = None if k in by_key else r                         # two players with the same key: never guess
    espn_rows = p["ROW_TYPE"].isin(NAME_MATCHED).to_numpy() if "ROW_TYPE" in p else np.zeros(len(p), bool)
    gp = np.zeros(len(p), int)
    src = np.array(["Projektion"] * len(p), dtype=object)
    new = {r: p["M_" + r].to_numpy(float).copy() for r in RAW}
    for i, (pid, nm) in enumerate(zip(p["PLAYER_ID"], p["PLAYER_NAME"])):
        # pool rows from NBA data carry NBA ids; rows filled from ESPN carry ESPN ids and are matched by name
        r = None if espn_rows[i] else by_id.get(int(pid))
        if r is None:
            r = by_key.get(names.name_key(nm))
        if r is None:
            continue
        n = int(r.GP)
        gp[i] = n
        if n < max(min_games, 1):
            continue
        w = 1.0 if shrink <= 0 else n / (n + shrink)
        for k in RAW:
            new[k][i] = w * float(getattr(r, k)) + (1 - w) * new[k][i]
        src[i] = "Saison" if w >= 1.0 else "Mischung"
    for k in RAW:
        p["M_" + k] = new[k]
    used = src != "Projektion"
    for k in ("PTS", "REB", "AST", "STL", "BLK", "FG3M", "DD"):        # the columns the tools print next to a player's name
        if f"PRED_{k}_PG" in p:
            p.loc[used, f"PRED_{k}_PG"] = new[k][used]
    for col, num, den in (("PRED_FG_PCT", "FGM", "FGA"), ("PRED_FT_PCT", "FTM", "FTA"), ("PRED_PTS_MIN", "PTS", "MIN")):
        if col in p:
            ok = used & (new[den] > 0)
            p.loc[ok, col] = new[num][ok] / new[den][ok]
    p["SEASON_GP"], p["SEASON_SRC"] = gp, src
    excl = int(stats["EXCLUDED"].sum()) if "EXCLUDED" in stats else 0
    info = dict(n_used=int(used.sum()), n_pool=len(p), last_game=str(stats["LAST_GAME"].max()), n_injury_games=excl,
                median_gp=float(np.median(gp[used])) if used.any() else 0.0)
    return p, info
