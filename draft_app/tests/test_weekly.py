"""Weekly lineup advisor: schedule counting, the week model and the exhaustive lineup search."""
import itertools
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import datetime as dt  # noqa: E402

import engine  # noqa: E402
import weekly_lineup as wl  # noqa: E402
from test_engine import synthetic_pool  # noqa: E402


def test_games_per_team_counts_the_window_inclusively():
    sched = [("2026-10-20", "BOS", "NYK"), ("2026-10-22", "BOS", "MIA"), ("2026-10-25", "NYK", "MIA"), ("2026-10-26", "BOS", "NYK")]
    assert wl.games_per_team(sched, "2026-10-20", "2026-10-25") == {"BOS": 2, "NYK": 2, "MIA": 2}
    assert wl.games_per_team(sched, "2026-10-26", "2026-10-26") == {"BOS": 1, "NYK": 1}
    assert wl.games_per_team(sched, "2027-01-01", "2027-01-07") == {}


def test_next_monday_is_always_in_the_future():
    assert wl.next_monday(dt.date(2026, 10, 18)) == dt.date(2026, 10, 19)       # Sunday -> next day
    assert wl.next_monday(dt.date(2026, 10, 19)) == dt.date(2026, 10, 26)       # Monday -> a week later
    assert wl.next_monday(dt.date(2026, 10, 22)).weekday() == 0


def test_week_model_scales_with_the_number_of_games():
    d = synthetic_pool(n=30)
    m2 = engine.build_pool_model(d, engine.Rules(), games=np.full(len(d), 2.0))
    m4 = engine.build_pool_model(d, engine.Rules(), games=np.full(len(d), 4.0))
    np.testing.assert_allclose(m4.E, 2.0 * m2.E)                                  # expected production is linear in games
    assert (m4.V > m2.V).all()


def test_best_lineup_is_legal_and_beats_every_other_legal_ten():
    d = synthetic_pool(n=40)
    m = engine.build_pool_model(d, engine.Rules(), games=np.full(len(d), 3.0))
    opp = m.team_moments(list(range(15, 25)))
    roster = [0, 3, 5, 8, 12, 14, 2, 21, 30, 33, 36, 4, 9]                         # 13 players, mixed positions
    lu, p, pc = wl.best_lineup(m, roster, opp)
    assert len(lu) == 10 and engine.lineup_legal(m, lu)
    for bench in itertools.combinations(roster, 3):
        st = [i for i in roster if i not in bench]
        if engine.lineup_legal(m, st):
            mu, sd = m.team_moments(st)
            q = float(engine.matchup_prob(engine.phi((mu - opp[0]) / np.sqrt(sd ** 2 + opp[1] ** 2)), m.rules))
            assert q <= p + 1e-12


def test_players_without_games_this_week_are_benched():
    d = synthetic_pool(n=40)
    g = np.full(len(d), 3.0); g[[0, 3, 5]] = 0.0                                   # their teams do not play
    m = engine.build_pool_model(d, engine.Rules(), games=g)
    opp = m.team_moments(list(range(15, 25)))
    roster = [0, 3, 5, 8, 12, 14, 2, 21, 30, 33, 36, 4, 9]
    lu, _, _ = wl.best_lineup(m, roster, opp)
    assert not ({0, 3, 5} & set(lu))
