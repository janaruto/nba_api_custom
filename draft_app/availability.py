"""Injury-aware availability for the live app.

The notebook's AVAIL comes from last seasons' games played and knows nothing about who is hurt NOW. On top of it:

  * ESPN status OUT (or ESPN's `injured` flag): he misses the start of the season, usually with no return date, so
    OUT_GAMES_LOST games are taken off his expected games. This number is an ASSUMPTION, not a measurement: set it in one
    place and override single players with `availability_overrides.json` when you know better.
  * Manual overrides {player name: expected games} always win.
  * DAY_TO_DAY and every other status change nothing (82 players at the moment: far too common to mean anything).
"""

import numpy as np
import pandas as pd

import names

MAX_GAMES = 82
OUT_GAMES_LOST = 25
AVAIL_FLOOR_OUT = 0.12


def adjust_pool(pool: pd.DataFrame, injuries: dict, overrides: dict) -> pd.DataFrame:
    """Copy of the pool with AVAIL / EXP_GP adjusted and INJURY_STATUS, INJURY_NOTE, AVAIL_BASE, AVAIL_SOURCE added."""
    p = pool.copy()
    p["AVAIL_BASE"] = p["AVAIL"].astype(float)
    p["AVAIL_SOURCE"] = "Modell"
    p["INJURY_STATUS"] = ""
    p["INJURY_NOTE"] = ""
    pid_to_pos = {int(pid): i for i, pid in enumerate(p["PLAYER_ID"])}
    for pid, v in (injuries or {}).items():
        i = pid_to_pos.get(int(pid))
        if i is None:
            continue
        status = str(v.get("status") or "")
        p.iat[i, p.columns.get_loc("INJURY_STATUS")] = status
        p.iat[i, p.columns.get_loc("INJURY_NOTE")] = str(v.get("outlook") or "")
        if status == "OUT" or v.get("injured"):
            a = max(AVAIL_FLOOR_OUT, float(p["AVAIL_BASE"].iat[i]) - OUT_GAMES_LOST / MAX_GAMES)
            p.iat[i, p.columns.get_loc("AVAIL")] = a
            p.iat[i, p.columns.get_loc("AVAIL_SOURCE")] = f"ESPN {status or 'verletzt'}: -{OUT_GAMES_LOST} Spiele"
    keys = {names.name_key(n): g for n, g in (overrides or {}).items()}
    for i, nm in enumerate(p["PLAYER_NAME"]):
        g = keys.get(names.name_key(nm))
        if g is not None:
            p.iat[i, p.columns.get_loc("AVAIL")] = float(np.clip(float(g) / MAX_GAMES, 0.02, 0.95))
            p.iat[i, p.columns.get_loc("AVAIL_SOURCE")] = f"manuell: {int(g)} Spiele"
    p["EXP_GP"] = (p["AVAIL"] * MAX_GAMES).round(0)
    return p
