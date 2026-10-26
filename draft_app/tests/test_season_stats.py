"""This season's per-game averages replace the projection (synthetic logs and pool, no network)."""
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import season_stats as ss  # noqa: E402
from test_engine import synthetic_pool  # noqa: E402


def logs():
    rows = []

    def g(pid, name, date, mn, pts, reb, ast, stl=0, blk=0, fg3m=0, fgm=0, fga=0, ftm=0, fta=0):
        rows.append(dict(PLAYER_ID=pid, PLAYER_NAME=name, GAME_DATE=date, MIN=mn, PTS=pts, REB=reb, AST=ast, STL=stl, BLK=blk,
                         FG3M=fg3m, FGM=fgm, FGA=fga, FTM=ftm, FTA=fta))
    g(1, "Ann One", "2026-10-20", "30:00", 20, 10, 4, 1, 0, 2, 8, 16, 2, 2)           # a double-double
    g(1, "Ann One", "2026-10-22", 28.0, 10, 4, 6, 3, 1, 1, 4, 10, 2, 4)
    g(1, "Ann One", "2026-10-24", 0, 0, 0, 0)                                          # did not play: not a game
    g(2, "Bob Two", "2026-10-21", "12:30", 6, 2, 1, 0, 0, 0, 2, 5, 2, 2)
    return pd.DataFrame(rows)


def test_aggregate_means_double_double_rate_and_games_played():
    a = ss.aggregate(logs()).set_index("PLAYER_ID")
    assert a.loc[1, "GP"] == 2 and a.loc[2, "GP"] == 1                                  # the 0-minute row is not a game
    assert a.loc[1, "PTS"] == 15 and a.loc[1, "MIN"] == 29.0 and a.loc[1, "FGA"] == 13 and a.loc[1, "FTM"] == 2
    assert a.loc[1, "DD"] == 0.5 and a.loc[2, "DD"] == 0.0                              # 1 of 2 games with two categories at 10+
    assert a.loc[2, "MIN"] == pytest.approx(12.5)
    assert a.loc[1, "LAST_GAME"] == "2026-10-22" and a.loc[1, "PLAYER_NAME"] == "Ann One"


def pool_with(stats_ids=(1, 2)):
    d = synthetic_pool(n=6)
    d["PLAYER_ID"] = [1, 2, 3, 4, 5, 6]
    d["PLAYER_NAME"] = ["Ann One", "Bob Two", "Cy Three", "Di Four", "Ed Five", "Fay Six"]
    d["ROW_TYPE"] = "normal"
    d["M_MIN"], d["M_PTS"], d["M_FGM"], d["M_FGA"] = 30.0, 18.0, 6.0, 13.0
    d["PRED_PTS_PG"] = 18.0; d["PRED_FG_PCT"] = 6 / 13; d["PRED_PTS_MIN"] = 0.6
    return d


def test_season_averages_replace_the_projection_for_players_with_games():
    d = pool_with()
    stats = ss.aggregate(logs())
    p, info = ss.apply(d, stats)
    assert p.M_PTS[0] == 15 and p.M_MIN[0] == 29 and p.M_DD[0] == 0.5 and p.PRED_PTS_PG[0] == 15
    assert p.PRED_FG_PCT[0] == pytest.approx(6 / 13) and p.PRED_PTS_MIN[0] == pytest.approx(15 / 29)      # display columns follow
    assert list(p.SEASON_GP) == [2, 1, 0, 0, 0, 0] and list(p.SEASON_SRC[:3]) == ["Saison", "Saison", "Projektion"]
    assert (p.M_PTS[2:] == 18.0).all() and info["n_used"] == 2 and info["last_game"] == "2026-10-22"
    assert (d.M_PTS == 18.0).all()                                                       # the input pool is not modified


def test_min_games_and_shrink():
    d, stats = pool_with(), ss.aggregate(logs())
    p, _ = ss.apply(d, stats, min_games=2)
    assert p.M_PTS[0] == 15 and p.M_PTS[1] == 18.0 and p.SEASON_SRC[1] == "Projektion" and p.SEASON_GP[1] == 1   # Bob has 1 game: projection
    p2, _ = ss.apply(d, stats, shrink=8.0)
    w = 2 / (2 + 8)                                                                      # 2 games, K = 8: the average counts 20 %
    assert p2.M_PTS[0] == pytest.approx(w * 15 + (1 - w) * 18) and p2.SEASON_SRC[0] == "Mischung"


def test_espn_rows_are_matched_by_name_and_never_by_a_colliding_id():
    d = pool_with()
    d.loc[2, ["PLAYER_ID", "PLAYER_NAME", "ROW_TYPE"]] = [1, "Ann One", "ESPN-Projektion"]           # an ESPN id that equals Ann's NBA id... and her name
    d.loc[3, ["PLAYER_ID", "PLAYER_NAME", "ROW_TYPE"]] = [2, "Rookie Nobody", "ESPN-Projektion"]      # ESPN id 2 = Bob's NBA id, but another person
    p, _ = ss.apply(d, ss.aggregate(logs()))
    assert p.SEASON_GP[2] == 2                                                           # matched through the name
    assert p.SEASON_GP[3] == 0 and p.M_PTS[3] == 18.0                                    # NOT Bob's numbers


def test_two_players_with_the_same_name_key_are_not_guessed():
    lg = logs()
    extra = lg.iloc[[3]].copy(); extra["PLAYER_ID"] = 99; extra["PLAYER_NAME"] = "Bob Two Jr."          # same key as "Bob Two" once suffixes go
    stats = ss.aggregate(pd.concat([lg, extra]))
    d = pool_with()
    d.loc[2, ["PLAYER_ID", "PLAYER_NAME", "ROW_TYPE"]] = [777, "Bob Two", "ESPN-Projektion"]
    p, _ = ss.apply(d, stats)
    assert p.SEASON_GP[2] == 0                                                           # ambiguous: stays on the projection


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "CACHE", tmp_path / "cache" / "season_stats.csv")
    return ss.CACHE


def test_a_fresh_cache_is_used_without_a_request(cache, monkeypatch):
    cache.parent.mkdir(); ss.aggregate(logs()).to_csv(cache, index=False)
    monkeypatch.setattr(ss, "fetch_gamelog", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")))
    stats, note = ss.load()
    assert note is None and len(stats) == 2


def test_stale_cache_is_refreshed_and_if_that_fails_the_old_one_is_used(cache, monkeypatch):
    cache.parent.mkdir(); ss.aggregate(logs()).to_csv(cache, index=False)
    old = time.time() - 10 * 3600; os.utime(cache, (old, old))
    monkeypatch.setattr(ss, "fetch_gamelog", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    stats, note = ss.load()
    assert len(stats) == 2 and "nicht erreichbar" in note and "10 Std." in note
    monkeypatch.setattr(ss, "fetch_gamelog", lambda *a, **k: logs().iloc[:1])
    stats2, note2 = ss.load()
    assert note2 is None and len(stats2) == 1 and time.time() - cache.stat().st_mtime < 60


def test_no_cache_and_no_network_means_no_season_stats(cache, monkeypatch):
    monkeypatch.setattr(ss, "fetch_gamelog", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    stats, note = ss.load()
    assert stats is None and "nicht verfügbar" in note
