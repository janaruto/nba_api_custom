"""
Players the model has no line for: take ESPN's own 2027 projection
===================================================================
The model pool only holds players with a qualifying NBA season (plus a few "proxy line" rookies that exist for the draft log
and are never recommended). After the draft the rosters contain rookies and new signings that the pool lacks or only carries
as a placeholder (AJ Dybantsa, Cameron Boozer, ...). For those the league tools and the weekly advisor use ESPN's projection
instead (the same numbers ESPN's draft room shows: stat source 'projected', id 102027), turned into the pool's columns:

  * per-game means M_*       : ESPN's projected minutes / PTS / REB / AST / STL / BLK / 3PM / FGM / FGA / FTM / FTA
  * double-doubles M_DD      : ESPN shows 0.0 for everybody, so derived from the stat line with the same kind of logistic as the
                               v4 notebook (here refitted on the pool's own per-game means)
  * per-game spread S_*, SI_*: ESPN gives no spreads; taken from the pool's relation "spread vs. mean" (log-log fit over all
                               regular pool players), so a rookie is neither treated as a sure thing nor as a lottery ticket
  * availability             : ESPN's projected games / 82, scaled by how optimistic ESPN is against the model for regular
                               players (median ratio), then the usual injury handling applies on top
  * team, positions, price   : ESPN's pro team, eligible slots and auction value

Players without any ESPN projection (e.g. Aday Mara, Udonis Haslem) cannot be evaluated and stay out.
This is a stop-gap: a rookie's ESPN line is a guess without any NBA minutes behind it.
"""

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

import names

APP_DIR = Path(__file__).parent
CACHE = APP_DIR.parent / "cache" / "espn_players_2027.json"
CACHE_OLD = APP_DIR.parent / "cache" / "espn_market_2027.json"        # the notebook's copy (older, same structure)
PROJ_ID = "102027"
MAX_GAMES = 82
ROW_TYPE = "ESPN-Projektion"
PROXY_ROW_TYPES = ("ESPN-only (proxy line)",)

# ESPN pro team id -> NBA tricode (as the schedule uses them)
ESPN_TEAM = {1: "ATL", 2: "BOS", 3: "NOP", 4: "CHI", 5: "CLE", 6: "DAL", 7: "DEN", 8: "DET", 9: "GSW", 10: "HOU", 11: "IND",
             12: "LAC", 13: "LAL", 14: "MIA", 15: "MIL", 16: "MIN", 17: "BKN", 18: "NYK", 19: "ORL", 20: "PHI", 21: "PHX",
             22: "POR", 23: "SAC", 24: "SAS", 25: "OKC", 26: "UTA", 27: "WAS", 28: "TOR", 29: "MEM", 30: "CHA"}
ESPN_SLOT = {0: "PG", 1: "SG", 2: "SF", 3: "PF", 4: "C"}
ESPN_DEFAULT_POS = {1: "PG", 2: "SG", 3: "SF", 4: "PF", 5: "C"}
ORDER = ["PG", "SG", "SF", "PF", "C"]
RAW = ["MIN", "PTS", "REB", "AST", "STL", "BLK", "FG3M", "FGM", "FGA", "FTM", "FTA", "DD"]
# spread columns and the pool mean they scale with
SPREAD = {"S_PTS": "M_PTS", "S_REB": "M_REB", "S_AST": "M_AST", "S_STL": "M_STL", "S_BLK": "M_BLK", "S_FG3M": "M_FG3M",
          "SI_FG": "M_FGA", "SI_FT": "M_FTA", "SI_PPM": "M_MIN"}          # S_DD is Bernoulli-shaped and handled separately


# ── ESPN's list ──────────────────────────────────────────────────────────────────────────────────────
def load_espn(max_age_h: float = 12) -> list:
    """ESPN's public player list: from the cache if fresh, else fetched (and cached); an old copy if the network fails."""
    if CACHE.exists() and time.time() - CACHE.stat().st_mtime < max_age_h * 3600:
        return json.loads(CACHE.read_text())
    try:
        import refresh_injuries
        data = refresh_injuries.fetch()
        CACHE.parent.mkdir(exist_ok=True)
        CACHE.write_text(json.dumps(data))
        return data
    except Exception as e:
        for old in (CACHE, CACHE_OLD):
            if old.exists():
                print(f"(ESPN nicht erreichbar: {type(e).__name__} - nutze gespeicherte Liste von {time.strftime('%d.%m.', time.localtime(old.stat().st_mtime))})")
                return json.loads(old.read_text())
        raise


def projection(p: dict) -> dict | None:
    """ESPN's per-game projection for 2027 or None. Keys are the pool's raw stat names (without DD)."""
    for s in p.get("stats") or []:
        if s.get("id") == PROJ_ID and s.get("stats"):
            st = s["stats"]
            gp = st.get("42") or 0
            if gp <= 0 or st.get("29") is None:
                return None
            return {"GP": gp, "MIN": st.get("28") or 0.0, "PTS": st.get("29"), "REB": st.get("30") or 0.0, "AST": st.get("26") or 0.0,
                    "STL": st.get("31") or 0.0, "BLK": st.get("27") or 0.0, "FG3M": (st.get("17") or 0) / gp,
                    "FGM": (st.get("13") or 0) / gp, "FGA": (st.get("14") or 0) / gp, "FTM": (st.get("15") or 0) / gp,
                    "FTA": (st.get("16") or 0) / gp}
    return None


def positions(p: dict) -> str:
    """Compact eligibility string as the pool writes it ('PGSG', 'SFPF', 'C')."""
    slots = set(p.get("eligibleSlots") or ())
    pos = [ESPN_SLOT[i] for i in range(5) if i in slots]
    if not pos:
        pos = [ESPN_DEFAULT_POS.get(p.get("defaultPositionId"), "SF")]
    return "".join(pos)


# ── calibration against the pool ─────────────────────────────────────────────────────────────────────
def _dd_features(d: pd.DataFrame) -> np.ndarray:
    X5 = np.sort(d[["PTS", "REB", "AST", "STL", "BLK"]].to_numpy(float) / 10.0, axis=1)[:, ::-1]
    s1, s2, s3 = X5[:, 0], X5[:, 1], X5[:, 2]
    return np.c_[np.ones(len(d)), s1, s2, s3, s2 ** 2, s1 * s2, s2 ** 3]


def _fit_logit(X, y, iters=80, l2=1e-3):
    b = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(X @ b)))
        W = p * (1 - p) + 1e-9
        step = np.linalg.solve((X * W[:, None]).T @ X + l2 * np.eye(X.shape[1]), X.T @ (y - p) - l2 * b)
        b += step
        if np.abs(step).max() < 1e-9:
            break
    return b


def regular_rows(pool: pd.DataFrame) -> pd.DataFrame:
    return pool[pool["MODEL_OK"].astype(bool) & ~pool["ROW_TYPE"].isin(PROXY_ROW_TYPES)]


def calibrate(pool: pd.DataFrame, espn_by_key: dict) -> dict:
    """Everything needed to turn an ESPN line into a full pool row, learned from the regular pool players."""
    reg = regular_rows(pool)
    cal = {"spread": {}}
    for col, mean_col in SPREAD.items():                        # log-log fit: spread = exp(a) * mean ** b
        x, y = reg[mean_col].to_numpy(float), reg[col].to_numpy(float)
        ok = (x > 0.05) & (y > 0.01)
        b, a = np.polyfit(np.log(x[ok]), np.log(y[ok]), 1)
        cal["spread"][col] = (float(a), float(b), float(x[ok].min()), float(x[ok].max()))
    d = reg[["M_PTS", "M_REB", "M_AST", "M_STL", "M_BLK"]].rename(columns=lambda c: c[2:])
    cal["dd"] = _fit_logit(_dd_features(d), reg["M_DD"].to_numpy(float))
    p = reg["M_DD"].to_numpy(float)
    ok = (p > 0.02) & (p < 0.98)
    cal["dd_spread"] = float(np.median(reg["S_DD"].to_numpy(float)[ok] / np.sqrt(p[ok] * (1 - p[ok]))))   # a game is a yes/no event
    ratios = []
    for pid, nm, av in zip(reg["PLAYER_ID"], reg["PLAYER_NAME"], reg["AVAIL"]):
        pr = projection(espn_by_key.get(names.name_key(nm), {}))
        if pr:
            ratios.append(float(av) / (pr["GP"] / MAX_GAMES))
    cal["avail_k"] = float(np.median(ratios)) if ratios else 0.85
    return cal


def line(pr: dict, cal: dict) -> dict:
    """Pool columns M_*, S_*, SI_* from an ESPN projection."""
    out = {f"M_{r}": float(pr[r]) for r in RAW if r != "DD"}
    out["M_DD"] = float(1 / (1 + np.exp(-(_dd_features(pd.DataFrame([{k: pr[k] for k in ["PTS", "REB", "AST", "STL", "BLK"]}])) @ cal["dd"])[0])))
    for col, mean_col in SPREAD.items():
        a, b, lo, hi = cal["spread"][col]
        m = float(np.clip(out[mean_col], lo, hi))
        out[col] = float(np.exp(a) * m ** b)
    out["S_DD"] = float(cal["dd_spread"] * np.sqrt(out["M_DD"] * (1 - out["M_DD"])))
    # the display columns the tools print (the model reads M_*, a person reads PRED_*)
    for c in ("PTS", "REB", "AST", "STL", "BLK", "FG3M", "DD"):
        out[f"PRED_{c}_PG"] = out[f"M_{c}"]
    out["PRED_FG_PCT"] = out["M_FGM"] / out["M_FGA"] if out["M_FGA"] > 0 else np.nan
    out["PRED_FT_PCT"] = out["M_FTM"] / out["M_FTA"] if out["M_FTA"] > 0 else np.nan
    out["PRED_PTS_MIN"] = out["M_PTS"] / out["M_MIN"] if out["M_MIN"] > 0 else np.nan
    return out


def espn_by_key(espn: list) -> dict:
    """name key -> ESPN player (the one with the higher auction value if a key occurs twice)."""
    by_key: dict = {}
    for p in espn:
        k = names.name_key(p.get("fullName", ""))
        cur = by_key.get(k)
        if cur is None or ((p.get("ownership") or {}).get("auctionValueAverage") or 0) > ((cur.get("ownership") or {}).get("auctionValueAverage") or 0):
            by_key[k] = p
    return by_key


# ── the extension ────────────────────────────────────────────────────────────────────────────────────
def extend_pool(pool: pd.DataFrame, wanted: list, espn: list | None = None):
    """Pool with an ESPN-projection row for every wanted name that is missing or only a proxy placeholder.
    Returns (pool, injuries for these players {pid: {...}}, [names filled], [names without any ESPN projection])."""
    index = names.build_index(pool["PLAYER_ID"], pool["PLAYER_NAME"])
    pid2i = {int(p): i for i, p in enumerate(pool["PLAYER_ID"])}
    todo = []
    for n in wanted:
        hit = names.resolve_player(index, n)
        if hit is None:
            todo.append((n, None))
        elif not bool(pool["MODEL_OK"].iat[pid2i[hit[0]]]):
            todo.append((n, pid2i[hit[0]]))
    if not todo:
        return pool, {}, [], []
    espn = espn if espn is not None else load_espn()
    by_key = espn_by_key(espn)
    cal = calibrate(pool, by_key)
    pool = pool.copy()
    new_rows, filled, missing, injuries = [], [], [], {}
    used = set(pid2i)
    for n, i in todo:
        p = by_key.get(names.name_key(n))
        pr = projection(p) if p else None
        if pr is None:
            missing.append(n)
            continue
        avail = float(np.clip(cal["avail_k"] * pr["GP"] / MAX_GAMES, 0.15, 0.9))
        fields = {**line(pr, cal), "POS_ELIGIBLE_STR": positions(p), "CURRENT_TEAM_ABBR": ESPN_TEAM.get(p.get("proTeamId")),
                  "AVAIL": avail, "AVAILABILITY": avail, "EXP_GP": round(avail * MAX_GAMES), "ROW_TYPE": ROW_TYPE, "MODEL_OK": True,
                  "ESPN_$_LIST": float(round((p.get("ownership") or {}).get("auctionValueAverage") or 0.0)),
                  "ESPN_INJURY": p.get("injuryStatus"), "IS_ROOKIE": True}
        if i is not None:                                                    # placeholder row of the pool: overwrite in place
            for k, v in fields.items():
                if k == "CURRENT_TEAM_ABBR" and v is None:
                    continue
                if k not in pool.columns:
                    pool[k] = pd.Series(np.nan, index=pool.index, dtype=float if isinstance(v, (int, float)) and not isinstance(v, bool) else object)
                elif pool[k].dtype != object and isinstance(v, (str, bool)):      # e.g. an all-NaN float column that now gets text
                    pool[k] = pool[k].astype(object)
                pool.iat[i, pool.columns.get_loc(k)] = v
            pid = int(pool["PLAYER_ID"].iat[i])
        else:
            pid = int(p["id"])
            while pid in used:                                               # ESPN and NBA ids live in different number ranges; be safe
                pid += 10_000_000
            used.add(pid)
            new_rows.append({"PLAYER_ID": pid, "PLAYER_NAME": p["fullName"], **fields})
        filled.append(n)
        status = p.get("injuryStatus")
        if status not in (None, "ACTIVE") or p.get("injured"):
            injuries[str(pid)] = {"status": status or "UNKNOWN", "injured": bool(p.get("injured")),
                                  "outlook": (p.get("seasonOutlook") or "")[:600]}
    if new_rows:
        pool = pd.concat([pool, pd.DataFrame(new_rows)], ignore_index=True)
    return pool, injuries, filled, missing


FA_ROW_TYPE = "Waiver (Saisonwerte)"


def extend_free_agents(pool: pd.DataFrame, stats: pd.DataFrame, exclude_keys: set, espn: list | None = None,
                       min_games: int = 2, min_mpg: float = 14.0, top_n: int = 60):
    """Waiver candidates the model pool does not know: players with real games this season (stats = season_stats.aggregate) who
    are neither in the pool nor on a league roster (`exclude_keys` = name keys of all rostered players). They get a row built from
    this season's averages (spreads and double-double rate calibrated like the ESPN stand-in lines), ESPN's positions, team and
    injury status. Only the `top_n` by a simple production score are added; players ESPN does not list are skipped (no positions).
    Returns (pool, injuries, [names added])."""
    have = {names.name_key(n) for n in pool["PLAYER_NAME"]}
    cand = stats[(stats["GP"] >= min_games) & (stats["MIN"] >= min_mpg)].copy()
    cand["KEY"] = cand["PLAYER_NAME"].map(names.name_key)
    cand = cand[~cand["KEY"].isin(have | set(exclude_keys))]
    if cand.empty:
        return pool, {}, []
    cand["SCORE"] = cand.PTS + 1.2 * cand.REB + 1.5 * cand.AST + 3 * (cand.STL + cand.BLK) + cand.FG3M
    cand = cand.sort_values("SCORE", ascending=False).head(top_n)
    espn = espn if espn is not None else load_espn()
    by_key = espn_by_key(espn)
    cal = calibrate(pool, by_key)
    used = {int(i) for i in pool["PLAYER_ID"]}
    rows, injuries, added = [], {}, []
    for r in cand.itertuples():
        p = by_key.get(r.KEY)
        if p is None:
            continue
        pr = {k: float(getattr(r, k)) for k in ("MIN", "PTS", "REB", "AST", "STL", "BLK", "FG3M", "FGM", "FGA", "FTM", "FTA")}
        proj = projection(p)
        avail = float(np.clip(cal["avail_k"] * proj["GP"] / MAX_GAMES, 0.15, 0.9)) if proj else 0.55
        pid = int(p["id"])
        while pid in used:
            pid += 10_000_000
        used.add(pid)
        team = getattr(r, "TEAM", "") or ESPN_TEAM.get(p.get("proTeamId"))
        rows.append({"PLAYER_ID": pid, "PLAYER_NAME": p["fullName"], **line(pr, cal), "POS_ELIGIBLE_STR": positions(p),
                     "CURRENT_TEAM_ABBR": team, "AVAIL": avail, "AVAILABILITY": avail, "EXP_GP": round(avail * MAX_GAMES), "ROW_TYPE": FA_ROW_TYPE,
                     "MODEL_OK": True, "ESPN_$_LIST": float(round((p.get("ownership") or {}).get("auctionValueAverage") or 0.0)),
                     "ESPN_INJURY": p.get("injuryStatus"), "IS_ROOKIE": False})
        added.append(p["fullName"])
        status = p.get("injuryStatus")
        if status not in (None, "ACTIVE") or p.get("injured"):
            injuries[str(pid)] = {"status": status or "UNKNOWN", "injured": bool(p.get("injured")), "outlook": (p.get("seasonOutlook") or "")[:600]}
    if rows:
        pool = pd.concat([pool, pd.DataFrame(rows)], ignore_index=True)
    return pool, injuries, added
