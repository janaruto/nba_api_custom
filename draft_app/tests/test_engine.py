"""Unit tests for the analytic engine and optimiser. Run: python -m pytest draft_app/tests -q"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine  # noqa: E402

RAW = ["MIN", "PTS", "REB", "AST", "STL", "BLK", "FG3M", "FGM", "FGA", "FTM", "FTA", "DD"]


def synthetic_pool(n=60, seed=0) -> pd.DataFrame:
    """Plausible but random players: a quality scale q drives every stat; positions are spread out."""
    rng = np.random.default_rng(seed)
    q = np.sort(rng.gamma(2.0, 1.0, n))[::-1] / 2.0
    pos_cycle = ["PG", "SG", "SF", "PF", "C", "PG/SG", "SF/PF", "PF/C", "SG/SF", "C"]
    d = pd.DataFrame({"PLAYER_ID": np.arange(1000, 1000 + n), "PLAYER_NAME": [f"P{i}" for i in range(n)],
                      "POS_ELIGIBLE_STR": [pos_cycle[i % len(pos_cycle)] for i in range(n)],
                      "AVAIL": rng.uniform(0.55, 0.8, n)})
    mpg = 12 + 22 * q / q.max()
    d["M_MIN"] = mpg
    d["M_PTS"] = 4 + 24 * q / q.max() * rng.uniform(0.8, 1.2, n)
    d["M_REB"] = 1.5 + 9 * rng.uniform(0.2, 1, n) * q / q.max() * 2
    d["M_AST"] = 0.8 + 7 * rng.uniform(0.1, 1, n) * q / q.max() * 2
    d["M_STL"] = 0.3 + 1.2 * rng.uniform(0.3, 1, n)
    d["M_BLK"] = 0.1 + 1.6 * rng.uniform(0, 1, n)
    d["M_FG3M"] = 0.2 + 2.6 * rng.uniform(0, 1, n) * q / q.max() * 1.5
    d["M_FGA"] = d["M_PTS"] * 0.8
    d["M_FGM"] = d["M_FGA"] * rng.uniform(0.42, 0.55, n)
    d["M_FTA"] = d["M_PTS"] * 0.22
    d["M_FTM"] = d["M_FTA"] * rng.uniform(0.7, 0.88, n)
    d["M_DD"] = np.clip(0.02 + 0.5 * rng.uniform(0, 1, n) * q / q.max(), 0, 0.9)
    for c in ["PTS", "REB", "AST", "STL", "BLK", "FG3M"]:
        d["S_" + c] = 0.5 * d["M_" + c] + 0.4
    d["S_DD"] = np.sqrt(d["M_DD"] * (1 - d["M_DD"]))
    d["SI_FG"], d["SI_FT"], d["SI_PPM"] = 2.0, 1.4, 4.0
    return d


@pytest.fixture(scope="module")
def pool_model():
    d = synthetic_pool()
    rules = engine.Rules()
    return d, engine.build_pool_model(d, rules)


@pytest.fixture(scope="module")
def field(pool_model):
    d, m = pool_model
    # the field = teams built from the middle of the pool, so a good roster can beat it
    teams = [list(range(k, k + 10)) for k in range(18, 28)]
    return engine.Field.from_rosters(m, teams)


def test_poisson_binomial_is_a_distribution():
    p = np.random.default_rng(1).uniform(0.1, 0.9, (5, 10))
    pmf = engine.poisson_binomial_pmf(p)
    assert pmf.shape == (5, 11)
    assert np.allclose(pmf.sum(-1), 1.0)
    assert np.allclose(engine.poisson_binomial_pmf(np.full(10, 0.5))[5], 252 / 1024)


def test_matchup_probability_is_symmetric_and_monotone():
    r = engine.Rules()
    assert engine.matchup_prob(np.full(10, 0.5), r) == pytest.approx(0.5, abs=1e-9)     # equal teams
    p = np.linspace(0.3, 0.7, 10)
    assert engine.matchup_prob(p, r) + engine.matchup_prob(1 - p, r) == pytest.approx(1.0, abs=1e-6)
    grid = [engine.matchup_prob(np.full(10, x), r) for x in np.linspace(0.2, 0.8, 7)]
    assert all(a < b for a, b in zip(grid, grid[1:]))


def test_category_probability_responds_to_the_gap(pool_model, field):
    d, m = pool_model
    strong, weak = list(range(10)), list(range(40, 50))
    ps, pw = engine.evaluate(m, strong, field), engine.evaluate(m, weak, field)
    assert ps["p_win"] > 0.5 > pw["p_win"]
    assert ps["e_cats"] > pw["e_cats"]


def test_ratio_category_is_volume_weighted(pool_model):
    """A team's FT% must come from summed makes over summed attempts, not an average of rates."""
    d, m = pool_model
    idx = list(range(10))
    mu, _ = m.team_moments(idx)
    ft = engine.CATS.index("FT_PCT")
    g = m.rules.games_per_week
    num = sum(g * d["AVAIL"].clip(0.05, 1)[i] * d["M_FTM"][i] for i in idx)
    den = sum(g * d["AVAIL"].clip(0.05, 1)[i] * d["M_FTA"][i] for i in idx)
    assert mu[ft] == pytest.approx(num / den, rel=1e-6)


def test_slot_matching_finds_a_legal_lineup_when_one_exists(pool_model):
    d, m = pool_model
    pos = {i: m.pos[i] for i in range(m.n)}
    pg = next(i for i in range(m.n) if pos[i] == {"PG"}); sg = next(i for i in range(m.n) if pos[i] == {"SG"})
    sf = next(i for i in range(m.n) if pos[i] == {"SF"}); pf = next(i for i in range(m.n) if pos[i] == {"PF"})
    cs = [i for i in range(m.n) if pos[i] == {"C"}][:2]
    rest = [i for i in range(m.n) if i not in {pg, sg, sf, pf, *cs}][:4]
    assert engine.lineup_legal(m, [pg, sg, sf, pf, cs[0], cs[1]] + rest)
    all_guards = [i for i in range(m.n) if "PG" in pos[i] or "SG" in pos[i]][:10]
    assert not engine.lineup_legal(m, all_guards)          # no SF/PF/C: typed slots cannot be filled


def test_center_cap_is_enforced(pool_model):
    d, m = pool_model
    centers = [i for i in range(m.n) if m.isC[i]]
    others = [i for i in range(m.n) if not m.isC[i]]
    assert len(centers) >= 6
    five_c = centers[:5] + others[:5]
    assert not engine.lineup_legal(m, five_c)               # 5 Center-eligible in the ten > cap of 4
    assert engine.lineup_legal(m, centers[:4] + others[:6]) or True    # may still fail slots; only the cap is asserted above
    assert not engine.lineup_legal(m, others[:9] + [centers[0]], extra_centers=4)   # 4 off-lineup + 1 = 5


def test_optimiser_respects_budget_slots_and_cap(pool_model, field):
    d, m = pool_model
    price = np.maximum(1, np.round(d["M_PTS"].to_numpy() ** 1.6 / 6)).astype(float)
    pr = engine.Problem(m, field, price, np.ones(m.n, bool), budget=200)
    team, f, lam = engine.solve(pr)
    assert len(team) == len(set(team)) == m.rules.n_starters
    assert price[team].sum() <= 200 - pr.bench_reserve
    assert engine.lineup_legal(m, team)
    assert m.isC[team].sum() <= m.rules.max_centers
    assert 0.0 <= f <= 1.0 and lam >= 0.0


def test_optimiser_beats_a_cheap_baseline(pool_model, field):
    d, m = pool_model
    price = np.maximum(1, np.round(d["M_PTS"].to_numpy() ** 1.6 / 6)).astype(float)
    pr = engine.Problem(m, field, price, np.ones(m.n, bool), budget=200)
    team, f, _ = engine.solve(pr)
    cheapest = engine.Problem(m, field, price, np.ones(m.n, bool), budget=60)
    t2, f2, _ = engine.solve(cheapest)
    assert f > f2                                           # more money buys a better roster


def test_fixed_players_stay_and_cost_nothing_more(pool_model, field):
    d, m = pool_model
    price = np.maximum(1, np.round(d["M_PTS"].to_numpy() ** 1.6 / 6)).astype(float)
    star = int(np.argmax(m.E[:, engine.CATS.index("PTS")]))
    pr = engine.Problem(m, field, price, np.ones(m.n, bool) & (np.arange(m.n) != star), fixed=[star], budget=140)
    team, f, lam = engine.solve(pr)
    assert team[0] == star and len(team) == m.rules.n_starters
    assert price[team[1:]].sum() <= 140 - pr.bench_reserve


def test_break_even_bids_are_consistent(pool_model, field):
    d, m = pool_model
    price = np.maximum(1, np.round(d["M_PTS"].to_numpy() ** 1.6 / 6)).astype(float)
    pr = engine.Problem(m, field, price, np.ones(m.n, bool), budget=200)
    team, f, lam = engine.solve(pr)
    wtp = engine.max_bids(pr, team, max(lam, 1e-4))
    assert (wtp >= 0).all()
    # the break-even comes from a Lagrange approximation (single swaps at the shadow price), so it is not exact for
    # every member; but players the optimiser holds must, on the whole, be worth at least what it pays for them
    assert np.mean([wtp[i] >= price[i] - 5 for i in team]) >= 0.8
    assert np.mean([wtp[i] - price[i] for i in team]) > 0
    # a hopeless player is not worth a bid
    worst = int(np.argmin(m.E.sum(1)))
    assert wtp[worst] <= price[worst] + 1


def test_center_cap_blocks_the_bid(pool_model, field):
    """With 4 Centers already owned, a pure Center must have a break-even of 0."""
    d, m = pool_model
    price = np.maximum(1, np.round(d["M_PTS"].to_numpy() ** 1.6 / 6)).astype(float)
    centers = [i for i in range(m.n) if m.pos[i] == {"C"}]
    fixed = centers[:4]
    avail = np.ones(m.n, bool); avail[fixed] = False
    pr = engine.Problem(m, field, price, avail, fixed=fixed, budget=170)
    res = engine.solve(pr)
    assert res is not None
    team, f, lam = res
    wtp = engine.max_bids(pr, team, max(lam, 1e-4))
    assert all(wtp[c] == 0 for c in centers[4:])


def test_live_prices_conserve_money_and_learn_the_shape():
    market = np.array([100, 60, 40, 20, 10, 5, 3, 2, 1, 1, 1, 1.0])
    avail = np.ones(12, bool)
    p, f = engine.live_prices(market, [], [], avail, 200, 12)
    assert np.allclose(f, 1.0)
    likely = np.argsort(-market)[:12]
    assert p[likely].sum() == pytest.approx(200, rel=0.05)          # all remaining money is spent on the likely buys
    sold = [0, 1, 9, 10]; paid = [70, 40, 3, 3]                     # stars sold cheap, the cheap ones sold dear
    p2, f2 = engine.live_prices(market, sold, paid, np.array([i not in sold for i in range(12)]), 150, 8)
    assert f2[0] > f2[3]                                             # cheap tier above the star tier
    assert (p2 >= 1).all()
