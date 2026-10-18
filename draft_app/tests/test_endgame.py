"""Regression tests for the endgame: a nearly empty wallet must still give a target roster (reported on 2026-10-12)."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine  # noqa: E402
from test_engine import synthetic_pool  # noqa: E402


@pytest.fixture(scope="module")
def setup():
    d = synthetic_pool()
    m = engine.build_pool_model(d, engine.Rules())
    fld = engine.Field.from_rosters(m, [list(range(k, k + 10)) for k in range(18, 28)])
    return m, fld


def test_eight_dollars_for_eight_slots_still_gives_a_roster(setup):
    m, fld = setup
    price = np.full(m.n, 1.48)                       # every remaining player forecast above the minimum bid
    avail = np.ones(m.n, bool); avail[:5] = False    # five bought elsewhere / by me
    fixed = [0, 1, 2, 3, 4]
    avail[fixed] = False
    pr = engine.Problem(m, fld, price, avail, fixed=fixed, budget=8, bench_reserve=3)
    assert engine.solve(pr) is None                  # the plain solver gives up here ...
    team, f, lam, scale, used = engine.solve_robust(pr)
    assert scale is not None and scale < 1.0         # ... the robust one shrinks the prices instead
    assert len(team) == 10 and team[:5] == fixed
    assert sum(used.price[i] for i in team[5:]) <= 8 - 3 + 1e-9
    assert engine.lineup_legal(m, team)


def test_comfortable_budget_is_untouched(setup):
    m, fld = setup
    price = np.full(m.n, 3.0)
    pr = engine.Problem(m, fld, price, np.ones(m.n, bool), budget=200)
    team, f, lam, scale, used = engine.solve_robust(pr)
    assert scale == 1.0 and len(team) == 10


def test_max_bids_stay_defined_in_the_relaxed_problem(setup):
    m, fld = setup
    price = np.full(m.n, 1.48)
    avail = np.ones(m.n, bool); avail[:5] = False
    pr = engine.Problem(m, fld, price, avail, fixed=[0, 1, 2, 3, 4], budget=8, bench_reserve=3)
    team, f, lam, scale, used = engine.solve_robust(pr)
    wtp = engine.max_bids(used, team, max(lam, 1e-4))
    assert np.isfinite(wtp).all() and (wtp >= 0).all()
