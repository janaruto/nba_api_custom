"""ESPN projections for players the model has no line for (synthetic pool and ESPN list, no network)."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import espn_extra as ex  # noqa: E402


def make_pool(n=60, seed=1):
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        mn = rng.uniform(15, 36)
        pts, reb, ast = mn * rng.uniform(.4, .8), mn * rng.uniform(.1, .35), mn * rng.uniform(.05, .3)
        stl, blk, fg3 = mn * rng.uniform(.02, .06), mn * rng.uniform(.0, .06), mn * rng.uniform(0, .1)
        fga = pts / 2.3
        dd = float(1 / (1 + np.exp(-(0.5 * (reb - 8) + 0.4 * (ast - 7) + 0.1 * (pts - 25) - 3))))
        sc = lambda m, k: k * np.sqrt(max(m, .05))
        rows.append({"PLAYER_ID": 1000 + i, "PLAYER_NAME": f"Player {i}", "POS_ELIGIBLE_STR": "SF", "CURRENT_TEAM_ABBR": "BOS",
                     "MODEL_OK": True, "ROW_TYPE": "normal", "AVAIL": 0.7, "M_MIN": mn, "M_PTS": pts, "M_REB": reb, "M_AST": ast,
                     "M_STL": stl, "M_BLK": blk, "M_FG3M": fg3, "M_FGM": fga * .46, "M_FGA": fga, "M_FTM": pts * .12, "M_FTA": pts * .15,
                     "M_DD": dd, "S_PTS": sc(pts, 2.5), "S_REB": sc(reb, 1.2), "S_AST": sc(ast, 1.1), "S_STL": sc(stl, 1.0),
                     "S_BLK": sc(blk, 1.0), "S_FG3M": sc(fg3, 1.1), "S_DD": np.sqrt(dd * (1 - dd)), "SI_FG": sc(fga, .54),
                     "SI_FT": sc(pts * .15, .43), "SI_PPM": mn ** .8 * .37, "PRED_PTS_PG": pts})
    return pd.DataFrame(rows)


def espn_player(pid, name, gp=70, pts=18.0, reb=6.0, ast=3.0, team=24, slots=(2, 3, 6), status="ACTIVE", value=5.0):
    return {"id": pid, "fullName": name, "proTeamId": team, "defaultPositionId": 3, "eligibleSlots": list(slots), "injuryStatus": status,
            "injured": status == "OUT", "ownership": {"auctionValueAverage": value},
            "stats": [{"id": ex.PROJ_ID, "stats": {"42": gp, "28": 30.0, "29": pts, "30": reb, "26": ast, "31": 1.0, "27": 0.5,
                                                   "17": 1.0 * gp, "13": 6.0 * gp, "14": 13.0 * gp, "15": 3.0 * gp, "16": 4.0 * gp}}]}


def espn_list(pool):
    base = [espn_player(9000 + i, n, gp=60 + i % 10) for i, n in enumerate(pool.PLAYER_NAME)]       # regular players, for the availability ratio
    return base


def test_missing_player_gets_an_espn_row():
    pool = make_pool()
    espn = espn_list(pool) + [espn_player(4_900_000, "Rookie Guy", pts=20.0, reb=8.0, ast=2.0, team=27)]
    new, inj, filled, missing = ex.extend_pool(pool, ["Player 3", "Rookie Guy"], espn=espn)
    assert filled == ["Rookie Guy"] and missing == [] and len(new) == len(pool) + 1
    r = new[new.PLAYER_NAME == "Rookie Guy"].iloc[0]
    assert r.MODEL_OK and r.ROW_TYPE == ex.ROW_TYPE and r.CURRENT_TEAM_ABBR == "WAS" and r.POS_ELIGIBLE_STR == "SFPF"
    assert (r.M_PTS, r.M_REB, r.M_AST, r.M_MIN) == (20.0, 8.0, 2.0, 30.0) and r.PRED_PTS_PG == 20.0
    assert r.M_FGM / r.M_FGA == pytest.approx(6 / 13) and r.PRED_FG_PCT == pytest.approx(6 / 13)
    assert 0.15 <= r.AVAIL <= 0.9 and r["ESPN_$_LIST"] == 5.0
    assert all(r[c] > 0 for c in ["S_PTS", "S_REB", "S_AST", "S_DD", "SI_FG", "SI_FT", "SI_PPM"])
    pd.testing.assert_frame_equal(new.iloc[:len(pool)][list(pool.columns)], pool, check_dtype=False)      # regular players untouched


def test_placeholder_row_is_overwritten_in_place():
    pool = make_pool()
    pool.loc[5, ["MODEL_OK", "ROW_TYPE", "M_PTS", "PRED_PTS_PG"]] = [False, "ESPN-only (proxy line)", 8.0, 8.0]
    pid = int(pool.PLAYER_ID[5])
    espn = [e for e in espn_list(pool) if e["fullName"] != pool.PLAYER_NAME[5]] + [espn_player(1, pool.PLAYER_NAME[5], pts=19.0)]
    new, _, filled, _ = ex.extend_pool(pool, [pool.PLAYER_NAME[5]], espn=espn)
    assert len(new) == len(pool) and filled == [pool.PLAYER_NAME[5]]
    r = new.iloc[5]
    assert int(r.PLAYER_ID) == pid and r.MODEL_OK and r.M_PTS == 19.0 and r.PRED_PTS_PG == 19.0 and r.ROW_TYPE == ex.ROW_TYPE


def test_player_without_a_projection_is_reported_and_left_out():
    pool = make_pool()
    nobody = espn_player(7, "No Line")
    nobody["stats"] = []
    new, _, filled, missing = ex.extend_pool(pool, ["No Line", "Not In ESPN Either"], espn=espn_list(pool) + [nobody])
    assert filled == [] and sorted(missing) == ["No Line", "Not In ESPN Either"] and len(new) == len(pool)


def test_nothing_to_do_means_no_network(monkeypatch):
    monkeypatch.setattr(ex, "load_espn", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")))
    pool = make_pool()
    new, inj, filled, missing = ex.extend_pool(pool, ["Player 1", "Player 2"])
    assert new is pool and inj == {} and filled == [] and missing == []


def test_triple_double_type_line_gets_a_higher_double_double_rate():
    pool = make_pool()
    espn = espn_list(pool) + [espn_player(11, "Big Man", pts=20, reb=12, ast=5), espn_player(12, "Scorer", pts=24, reb=4, ast=2)]
    new, *_ = ex.extend_pool(pool, ["Big Man", "Scorer"], espn=espn)
    dd = new.set_index("PLAYER_NAME").M_DD
    assert dd["Big Man"] > 3 * dd["Scorer"] and 0 < dd["Scorer"] < dd["Big Man"] < 1


def test_injury_status_is_passed_on_and_ids_never_collide():
    pool = make_pool()
    espn = espn_list(pool) + [espn_player(1003, "Hurt Rookie", status="OUT")]            # 1003 is already a pool id
    new, inj, filled, _ = ex.extend_pool(pool, ["Hurt Rookie"], espn=espn)
    r = new[new.PLAYER_NAME == "Hurt Rookie"].iloc[0]
    assert new.PLAYER_ID.is_unique and int(r.PLAYER_ID) != 1003
    assert inj[str(int(r.PLAYER_ID))]["status"] == "OUT"


def test_positions_and_team_mapping():
    assert ex.positions({"eligibleSlots": [0, 1, 5, 7, 8, 11]}) == "PGSG"
    assert ex.positions({"eligibleSlots": [4, 9, 10, 11]}) == "C"
    assert ex.positions({"eligibleSlots": [], "defaultPositionId": 5}) == "C"
    assert ex.ESPN_TEAM[17] == "BKN" and len(set(ex.ESPN_TEAM.values())) == 30
