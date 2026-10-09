"""
Auction simulation for the Swiss See Ballers league: the environment the v4 notebook evaluates
bidding strategies in, and the check that the live app's logic actually wins.

Contents
--------
* bootstrap_weeks  : weekly category totals by resampling REAL games (replaces v3's independent
                     normals, which made FT% ~4x too noisy and FG% ~1.8x too noisy)
* Bidder           : a market-anchored bidder with a private valuation (v3's, unchanged in behaviour)
* EngineBidder     : bids its break-even price from engine.max_bids and re-plans as the room moves
* run_auction      : simultaneous English auction, clearing at second-highest ceiling + $1
* run_trial        : one full 14-team auction -> my result against the 13 rosters that came out
* market scenarios : how the ESPN board is stretched from ESPN's ~10-team curve to this 14-team pot
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import engine
from engine import ALL_POS, CATS, COUNTING, Rules

RAWB = ["MIN", "PTS", "REB", "AST", "STL", "BLK", "FG3M", "FGM", "FGA", "FTM", "FTA", "DD"]


# ═════════════════════════════════════════════════════════════════════════════════════════
# Market scenarios
# ═════════════════════════════════════════════════════════════════════════════════════════
def float_prices(value, n_drafted, budget, min_bid=1.0):
    """Value above replacement -> dollars (float), replacement = the n_drafted-th best player."""
    v = np.asarray(value, float)
    order = np.argsort(-v)
    repl = v[order[min(n_drafted, len(v)) - 1]]
    var = np.clip(v - repl, 0, None)
    pot = budget - n_drafted * min_bid
    tot = var[order[:n_drafted]].sum()
    drafted = np.zeros(len(v), bool)
    drafted[order[:n_drafted]] = True
    return np.where(drafted, (var / tot) * pot + min_bid, 0.0)


def market_uniform(espn_raw, n_drafted, budget, min_bid=1.0):
    """v3's assumption: stretch ESPN's published values uniformly until the top n_drafted sum to the pot."""
    e = np.asarray(espn_raw, float)
    top = np.sort(e[~np.isnan(e)])[::-1][:n_drafted]
    scale = (budget - n_drafted * min_bid) / max(top.sum() - n_drafted * min_bid, 1.0)
    return np.where(np.isnan(e), np.nan, min_bid + np.clip(e - min_bid, 0, None) * scale)


def market_structural(espn_raw, value, n_teams=14, espn_teams=10, per_team=200, per_roster=13, min_bid=1.0):
    """ESPN's published averages behave like a ~10-team curve (their top 130 sum to ~$1,833 of a
    $2,000 pot, ranks 131+ are ~$0). Moving to 14 teams lifts the MID-tier a lot (those players
    become starters) and stars only ~10-20%.

    The size of the lift depends on RANK, so it is looked up by each player's ESPN rank (not by his rank
    under our own valuation: where the two disagree hard -- ESPN pays Siakam like a top-10 player, our
    model does not -- a model-rank lookup applies a mid-tier multiplier to a star price).
    `value` is only used to measure the SHAPE of a value-above-replacement price curve."""
    v = np.sort(np.asarray(value, float))[::-1]
    p10 = float_prices(v, espn_teams * per_roster, espn_teams * per_team, min_bid)
    p14 = float_prices(v, n_teams * per_roster, n_teams * per_team, min_bid)
    k = np.where(p10 > 1.5, (p14 - min_bid) / np.maximum(p10 - min_bid, 1e-9), np.nan)
    last = np.nanmedian(k[95:120]) if np.isfinite(k[95:120]).any() else 2.0
    kappa_by_rank = np.where(np.isfinite(k), k, last)
    e = np.asarray(espn_raw, float)
    order = np.argsort(-np.where(np.isnan(e), -1e9, e))
    rank = np.empty(len(e), int)
    rank[order] = np.arange(len(e))
    kappa = kappa_by_rank[np.minimum(rank, len(kappa_by_rank) - 1)]
    out = np.where(np.isnan(e), np.nan, min_bid + np.clip(e - min_bid, 0, None) * kappa)
    # no ESPN value: fall back to the model's own 14-team price at that player's model rank
    vv = np.asarray(value, float)
    p14_by_player = np.empty(len(vv))
    p14_by_player[np.argsort(-vv)] = p14[:len(vv)] if len(p14) >= len(vv) else np.pad(p14, (0, len(vv) - len(p14)), constant_values=min_bid)
    out = np.where(np.isnan(out), p14_by_player, out)
    top = np.sort(out)[::-1][:n_teams * per_roster]
    need = n_teams * per_team - n_teams * per_roster * min_bid
    return min_bid + np.clip(out - min_bid, 0, None) * (need / max((top - min_bid).sum(), 1e-9))


def dollars_for(P, Z, KEY, strategy_value, auction_prices, plan: str):
    """Private dollar valuation of every player under a category plan like 'REB+AST+DD+PTS+PPM'."""
    tg = [KEY[x] for x in plan.split("+")]
    return auction_prices(strategy_value(Z, tg, P["DUR_W"])).astype(float)


# ═════════════════════════════════════════════════════════════════════════════════════════
# Weekly simulator: bootstrap of real games
# ═════════════════════════════════════════════════════════════════════════════════════════
class GameTables:
    """Per-player table of real game rows, rescaled to the projected means when sampled.

    Resampling whole games keeps the joint distribution that matters: FGM moves with FGA, FTM with
    FTA, a double-double IS a game with two big stats. v3 drew each quantity independently, which
    inflated the weekly standard deviation of a team's FT% by ~4x (0.18 vs 0.04)."""

    def __init__(self, logs: pd.DataFrame, pool: pd.DataFrame, cur_season: str, prev_season: str,
                 w_cur: float = 0.70):
        self.tab = {}
        by = {pid: d for pid, d in logs.groupby("PLAYER_ID")}
        proxy = pool["PROXY_PID"] if "PROXY_PID" in pool.columns else pd.Series(np.nan, index=pool.index)
        for pid, px in zip(pool["PLAYER_ID"], proxy):
            src = by.get(int(px) if pd.notna(px) else pid)
            if src is None:
                continue
            cur = src[src.SEASON == cur_season][RAWB].to_numpy(float)
            prv = src[src.SEASON == prev_season][RAWB].to_numpy(float)
            if len(cur) and len(prv):
                rows = np.vstack([cur, prv])
                w = np.r_[np.full(len(cur), w_cur / len(cur)), np.full(len(prv), (1 - w_cur) / len(prv))]
            else:
                rows = cur if len(cur) else prv
                w = np.full(len(rows), 1.0 / max(len(rows), 1))
            w = w / w.sum()
            self.tab[pid] = (rows, np.cumsum(w), (rows * w[:, None]).sum(0))


def bootstrap_weeks(pool: pd.DataFrame, tables: GameTables, starters, n_weeks: int, rng,
                    games_per_week: float = 3.45):
    """Weekly totals for a lineup. `starters` are positional row indices into `pool`.
    Pool columns used: PLAYER_ID, AVAIL and M_<raw> (projected per-game means)."""
    tot = np.zeros((n_weeks, len(RAWB)))
    for i in starters:
        rows, cum, emp = tables.tab[pool["PLAYER_ID"].iat[i]]
        proj = np.array([pool["M_" + r].iat[i] for r in RAWB], float)
        scale = np.where(emp > 1e-9, proj / np.maximum(emp, 1e-9), 1.0)
        team_games = rng.binomial(4, games_per_week / 4.0, size=n_weeks)
        g = rng.binomial(team_games, float(pool["AVAIL"].iat[i]))
        n = int(g.sum())
        if n == 0:
            continue
        idx = np.minimum(np.searchsorted(cum, rng.random(n)), len(rows) - 1)
        draw = rows[idx] * scale
        wk = np.repeat(np.arange(n_weeks), g)
        for j in range(len(RAWB)):
            tot[:, j] += np.bincount(wk, weights=draw[:, j], minlength=n_weeks)
    ix = {c: j for j, c in enumerate(RAWB)}
    out = {c: tot[:, ix[c]] for c in COUNTING}
    z = np.zeros(n_weeks)
    out["FG_PCT"] = np.divide(tot[:, ix["FGM"]], tot[:, ix["FGA"]], out=z.copy(), where=tot[:, ix["FGA"]] > 0)
    out["FT_PCT"] = np.divide(tot[:, ix["FTM"]], tot[:, ix["FTA"]], out=z.copy(), where=tot[:, ix["FTA"]] > 0)
    out["PTS_MIN"] = np.divide(tot[:, ix["PTS"]], tot[:, ix["MIN"]], out=z.copy(), where=tot[:, ix["MIN"]] > 0)
    return out


def cat_win_probs(mine, theirs):
    """P(win each category) in a week; no tie-breakers, so an exact tie is half a win."""
    return {c: float((mine[c] > theirs[c]).mean() + 0.5 * (mine[c] == theirs[c]).mean()) for c in CATS}


def cats_won(mine, theirs):
    w = np.zeros(len(mine["PTS"]))
    for c in CATS:
        w += (mine[c] > theirs[c]).astype(float) + 0.5 * (mine[c] == theirs[c])
    return w


def matchup_win_prob(mine, theirs, need=6):
    w = cats_won(mine, theirs)
    return float((w > need - 1).mean() + 0.5 * (w == need - 1).mean())


# ═════════════════════════════════════════════════════════════════════════════════════════
# Bidders
# ═════════════════════════════════════════════════════════════════════════════════════════
class Bidder:
    """One manager: private $ valuation, budget, 13 slots. Ceiling = market + capture * (private - market),
    i.e. anchored to the board everybody can see, bid up only where he sees value the room does not."""

    def __init__(self, name, model: engine.PoolModel, dollars, market, plan=None, aggression=1.0,
                 surplus_capture=0.60):
        self.name, self.plan = name, plan
        self.M, self.rules = model, model.rules
        self.val = np.asarray(dollars, float)
        self.market = np.asarray(market, float)
        self.aggr, self.capture = aggression, surplus_capture
        self.budget = self.rules.budget
        self.roster, self.prices, self.ncen = [], [], 0
        self.slot_masks = [frozenset(a) for _, a in self.rules.slots]
        self.slots_open = [self.slot_masks[si] for si in
                           sorted(range(len(self.slot_masks)), key=lambda s: len(self.slot_masks[s]))] \
            + [None] * self.rules.bench_count

    @property
    def spots(self):
        return self.rules.roster_size - len(self.roster)

    @property
    def done(self):
        return self.spots == 0

    @property
    def max_bid(self):
        return int(max(self.rules.min_bid, self.budget - (self.spots - 1) * self.rules.min_bid))

    def slot_for(self, i):
        typed = [s for s in self.slots_open if s is not None and (self.M.pos[i] & s)]
        if typed:
            return min(typed, key=len)
        return "BENCH" if None in self.slots_open else None

    def can_take(self, i):
        if self.done or (self.M.isC[i] and self.ncen >= self.rules.max_centers):
            return False
        return self.slot_for(i) is not None

    def wtp(self, i):
        if not self.can_take(i):
            return 0
        mkt = self.market[i]
        v = (mkt + (self.val[i] - mkt) * self.capture) * self.aggr
        s = self.slot_for(i)
        if s != "BENCH" and len(s) <= 1:
            v *= 1.08                                   # scarce typed slot still unfilled
        if v < self.rules.min_bid:
            return 0
        return int(max(0, min(v, self.max_bid)))

    def take(self, i, price):
        s = self.slot_for(i)
        self.slots_open.remove(s if s != "BENCH" else None)
        self.roster.append(int(i))
        self.prices.append(int(price))
        self.budget -= int(price)
        self.ncen += int(self.M.isC[i])

    def notify_sale(self, i, price, avail):
        pass


class EngineBidder(Bidder):
    """Bids its break-even price from the analytic optimiser, and re-plans as the auction unfolds.

    Every ceiling answers one question: what is the most I can pay for this player and still end
    up with a roster at least as good as the best one I could build without him?  Prices of the
    alternatives come from engine.live_prices. By default only the price LEVEL adapts (money conservation:
    all remaining budgets get spent on the players still to be drafted). Learning the price SHAPE by tier is
    available (use_tiers=True) but the notebook's stress test found no gain from it, so it is off."""

    def __init__(self, name, model, fld: engine.Field, believed_market, all_bidders, aggression=1.0,
                 use_tiers=False, resolve_every=12, kind="pwin"):
        super().__init__(name, model, believed_market, believed_market, aggression=aggression)
        self.fld, self.all, self.use_tiers, self.resolve_every, self.kind = fld, all_bidders, use_tiers, resolve_every, kind
        self.avail_mask = np.ones(model.n, bool)
        self.sold = []
        self._v, self._stale, self._since = None, True, 0
        self.solves = 0

    def notify_sale(self, i, price, avail):
        self.avail_mask = avail.copy()
        self.sold.append((int(i), float(price)))
        self._since += 1
        if self._since >= self.resolve_every:
            self._stale = True

    def take(self, i, price):
        super().take(i, price)
        self._stale = True

    def wtp(self, i):
        if not self.can_take(i):
            return 0
        if self._stale or self._v is None:
            self._recompute()
        v = self._v[i]
        if v < self.rules.min_bid:
            return 0
        return int(max(0, min(v, self.max_bid)))

    @property
    def val(self):                      # nomination logic reads .val[i] - market[i]
        if self._stale or self._v is None:
            self._recompute()
        return self._v

    @val.setter
    def val(self, v):
        pass

    def _recompute(self):
        self._stale, self._since = False, 0
        M, rules = self.M, self.rules
        mine = list(self.roster)
        rem_budget = sum(b.budget for b in self.all)
        n_left = max(sum(b.spots for b in self.all), 1)
        if self.use_tiers:
            price, _ = engine.live_prices(self.market, [a for a, _ in self.sold], [b for _, b in self.sold],
                                          self.avail_mask, rem_budget, n_left, rules.min_bid)
        else:
            idx = np.flatnonzero(self.avail_mask)
            top = np.sort(self.market[idx])[::-1][:n_left]
            price = self.market * rem_budget / max(top.sum(), 1.0)
        price = np.maximum(rules.min_bid, np.round(price)).astype(float)
        ns = rules.n_starters
        if len(mine) > ns:
            z = ((M.E - M.E.mean(0)) / np.maximum(M.E.std(0), 1e-9)).sum(1)
            fixed = sorted(mine, key=lambda i: -z[i])[:ns]
        else:
            fixed = mine
        off_c = int(M.isC[[i for i in mine if i not in fixed]].sum()) if len(mine) > len(fixed) else 0
        bench_left = max(0, rules.bench_count - max(0, len(mine) - ns))
        if len(fixed) >= ns:
            self._v = np.where(self.avail_mask, float(rules.min_bid), 0.0)
            return
        avail = self.avail_mask.copy()
        avail[mine] = False
        pr = engine.Problem(M, self.fld, price, avail, fixed=fixed, budget=self.budget,
                            bench_reserve=bench_left * rules.min_bid, fixed_centers_off_lineup=off_c, kind=self.kind)
        out = engine.solve(pr, restarts=(0.6,))
        self.solves += 1
        if out is None:
            self._v = np.where(self.avail_mask, float(rules.min_bid), 0.0)
            return
        team, f, lam = out
        w = engine.max_bids(pr, team, max(lam, 1e-4))
        self._v = np.where(avail, w, 0.0)


# ═════════════════════════════════════════════════════════════════════════════════════════
# Auction
# ═════════════════════════════════════════════════════════════════════════════════════════
def run_auction(model: engine.PoolModel, market, bidders, rng):
    """Simultaneous English auction. Nominations rotate; 50% the nominator's best surplus, 20% a star to
    drain rivals, 30% an arbitrary name. A player clears at the second-highest ceiling + $1 (you pay what
    it took to outlast the next-most-interested manager). Leftover slots are filled at the minimum."""
    rules = model.rules
    mkt = np.maximum(np.asarray(market, float), rules.min_bid)
    avail = np.ones(model.n, bool)
    passed = np.zeros(model.n, bool)
    log, turn, guard = [], 0, 0
    while any(not b.done for b in bidders) and guard < 5000:
        guard += 1
        active = [b for b in bidders if not b.done]
        pool = np.flatnonzero(avail & ~passed)
        if len(pool) == 0:
            break
        nom = active[turn % len(active)]
        turn += 1
        can = [i for i in pool if nom.can_take(i)] or [i for i in pool if not model.isC[i]] or list(pool)
        r = rng.random()
        if r < 0.50:
            pick = max(can, key=lambda i: nom.val[i] - mkt[i])
        elif r < 0.70:
            pick = max(can, key=lambda i: mkt[i])
        else:
            pick = int(rng.choice(can))
        bids = sorted(((b.wtp(pick), b) for b in bidders if b.can_take(pick)), key=lambda t: -t[0])
        if not bids or bids[0][0] < rules.min_bid:
            passed[pick] = True
            continue
        win = bids[0][1]
        second = bids[1][0] if len(bids) > 1 else 0
        price = max(rules.min_bid, min(bids[0][0], second + 1))
        win.take(pick, price)
        avail[pick] = False
        for b in bidders:
            b.notify_sale(pick, price, avail)
        log.append({"i": int(pick), "PAID": int(price), "MARKET": float(mkt[pick]), "TEAM": win.name,
                    "N_BIDDERS": len(bids)})
    for b in bidders:                                                   # fill phase: leftovers at the minimum
        while b.spots > 0:
            cands = [i for i in np.flatnonzero(avail) if b.can_take(i)]
            if not cands:
                break
            pick = max(cands, key=lambda i: b.val[i])
            b.take(pick, rules.min_bid)
            avail[pick] = False
            for o in bidders:
                o.notify_sale(pick, rules.min_bid, avail)
            log.append({"i": int(pick), "PAID": rules.min_bid, "MARKET": float(mkt[pick]), "TEAM": b.name, "N_BIDDERS": 1})
    return pd.DataFrame(log)


def best_lineup(model: engine.PoolModel, fld: engine.Field, roster):
    """The legal starting ten out of a 13-man roster that maximises matchup win probability."""
    roster = [int(i) for i in roster]
    ns = model.rules.n_starters
    if len(roster) <= ns:
        return roster
    import itertools
    best, best_f = None, -1.0
    for bench in itertools.combinations(roster, len(roster) - ns):
        st = [i for i in roster if i not in bench]
        if not engine.lineup_legal(model, st):
            continue
        f = engine.evaluate(model, st, fld)["p_win"]
        if f > best_f:
            best, best_f = st, f
    return best if best is not None else roster[:ns]


def run_trial(world: dict, kind: str, seed: int, believed_market, actual_market=None, plan=None,
              n_sharp=0, n_weeks=1200, use_tiers=False, rival_plans=None, surplus_capture=0.60,
              want_sims=False):
    """One full auction. `kind` is 'engine' (the optimiser bids for me) or 'plan' (a v3-style bidder with
    `plan` = array of private dollar values). Returns my result against the 13 rosters that came out.

    world: dict with model, fld, tables, pool (DataFrame with M_*, AVAIL, PLAYER_ID), plan_dollars (dict
           name->array for the rivals' punt plans), balanced_dollars (array)."""
    M, fld, pool = world["model"], world["fld"], world["pool"]
    rng = np.random.default_rng(seed)
    actual = np.asarray(actual_market if actual_market is not None else believed_market, float)
    believed = np.asarray(believed_market, float)
    names = [n for n in world["team_names"] if n != world["your_team"]]
    rp = rival_plans if rival_plans is not None else world["rival_plans"]
    bidders: list = []
    if kind == "engine":
        me = EngineBidder("ME", M, fld, believed, bidders, use_tiers=use_tiers)
    else:
        me = Bidder("ME", M, plan, actual, plan=True, surplus_capture=surplus_capture)
    bidders.append(me)
    for j, nm in enumerate(names):
        if n_sharp and j >= len(names) - n_sharp:
            noisy = actual * np.exp(rng.normal(0, 0.15, len(actual)))
            bidders.append(EngineBidder(nm, M, fld, noisy, bidders, use_tiers=False))
            continue
        dol = rp[j] if j < len(rp) else actual.copy()
        bidders.append(Bidder(nm, M, dol * rng.uniform(0.92, 1.08), actual,
                              aggression=float(rng.uniform(0.92, 1.10)), surplus_capture=surplus_capture))
    sales = run_auction(M, actual, bidders, rng)
    starters = [best_lineup(M, fld, b.roster) for b in bidders]
    sims = [bootstrap_weeks(pool, world["tables"], s, n_weeks, np.random.default_rng(seed * 100 + k))
            for k, s in enumerate(starters)]
    mine = sims[0]
    pw = float(np.mean([matchup_win_prob(mine, s) for s in sims[1:]]))
    ec = float(np.mean([sum(cat_win_probs(mine, s).values()) for s in sims[1:]]))
    out = {"p_win": pw, "e_cats": ec, "spend": int(sum(me.prices)), "complete": me.spots == 0,
           "my_roster": list(me.roster), "my_prices": list(me.prices), "my_starters": starters[0],
           "sales": sales, "solves": getattr(me, "solves", 0)}
    if want_sims:
        out["rosters"] = [(b.name, list(b.roster), list(b.prices), st) for b, st in zip(bidders, starters)]
        out["sims"] = sims
    return out
