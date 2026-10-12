"""Bench suggestions: who covers for absent starters, within the centre cap and a price limit."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine  # noqa: E402
from test_engine import synthetic_pool  # noqa: E402


@pytest.fixture(scope="module")
def setup():
    d = synthetic_pool(n=80)
    m = engine.build_pool_model(d, engine.Rules())
    fld = engine.Field.from_rosters(m, [list(range(k, k + 10)) for k in range(30, 40)])
    pr = engine.Problem(m, fld, np.full(m.n, 5.0), np.ones(m.n, bool), budget=200)
    team, *_ = engine.solve(pr)
    return m, fld, team


def test_never_suggests_team_members_or_unavailable_players(setup):
    m, fld, team = setup
    avail = np.ones(m.n, bool); avail[:25] = False
    res = engine.bench_suggestions(m, fld, team, avail, np.full(m.n, 1.0))
    assert res and not ({r["idx"] for r in res} & (set(team) | set(range(25))))


def test_picks_are_ranked_first_and_values_are_ordered(setup):
    m, fld, team = setup
    res = engine.bench_suggestions(m, fld, team, np.ones(m.n, bool), np.full(m.n, 1.0), n_bench=3)
    ranks = [r["rank"] for r in res]
    assert ranks[:3] == [1, 2, 3] and all(r is None for r in ranks[3:])
    assert all(r["marginal"] > 0 for r in res[:3])
    rest = [r["value"] for r in res[3:]]
    assert rest == sorted(rest, reverse=True)


def test_second_pick_adds_less_than_it_would_alone(setup):
    m, fld, team = setup
    res = engine.bench_suggestions(m, fld, team, np.ones(m.n, bool), np.full(m.n, 1.0), n_bench=3)
    assert res[1]["marginal"] <= res[1]["value"] + 1e-12


def test_price_limit_is_respected(setup):
    m, fld, team = setup
    price = np.full(m.n, 9.0); price[60:] = 1.0
    res = engine.bench_suggestions(m, fld, team, np.ones(m.n, bool), price, max_price=1.0)
    assert res and all(r["idx"] >= 60 for r in res)
    assert engine.bench_suggestions(m, fld, team, np.ones(m.n, bool), np.full(m.n, 9.0), max_price=1.0) == []


def test_no_centre_on_the_bench_when_the_cap_is_reached(setup):
    m, fld, team = setup
    res = engine.bench_suggestions(m, fld, team, np.ones(m.n, bool), np.full(m.n, 1.0),
                                   fixed_centers_off_lineup=m.rules.max_centers - int(m.isC[team].sum()))
    assert res and not any(m.isC[r["idx"]] for r in res if r["rank"])


def test_empty_cases(setup):
    m, fld, team = setup
    assert engine.bench_suggestions(m, fld, [], np.ones(m.n, bool), np.ones(m.n)) == []
    assert engine.bench_suggestions(m, fld, team, np.ones(m.n, bool), np.ones(m.n), n_bench=0) == []
