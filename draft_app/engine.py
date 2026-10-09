"""
Analytic team evaluator + roster optimiser for the Swiss See Ballers auction.

Why this exists
---------------
The v3 notebook scores a roster by *simulating* thousands of weeks. That is the right ground
truth, but it is far too slow to optimise against or to run live while an auction is going on.
This module computes the same quantity analytically:

    a team's weekly total in a category is a sum over its 10 starters, so its mean and variance
    are sums of per-player contributions; P(win category) is a normal CDF of the standardised
    gap to the field; P(win the matchup) is P(>= 6 of 10) over those category probabilities.

Everything is additive over players, which is what makes it fast: swapping one starter is a
vector update, so ALL swaps are evaluated in one numpy call.

What is modelled (and matches the notebook's weekly simulator)
--------------------------------------------------------------
* counting cats (PTS REB AST STL BLK 3PM DD) are summed over games played;
* ratio cats (FG% FT% PPM) are team aggregates: sum(num)/sum(den), via the "impact" form
  I = num - league_rate * den, so a high-volume shooter moves the team ratio more;
* games played per player per week = Binomial(team games, availability): both the mean and the
  extra variance from missing games are included;
* the field is a benchmark (mean and spread across the 13 other teams), not one opponent.

Known simplification: category outcomes are treated as independent when turning the ten
probabilities into P(win >= 6). In reality categories move together within a week; the notebook
measures how much that matters (validate_engine) and `rho_shrink` corrects for it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# ── league rules: defaults, overridden by league_meta.json via Rules.from_meta ──────────────
CATS = ["FG_PCT", "FT_PCT", "FG3M", "REB", "AST", "STL", "BLK", "DD", "PTS", "PTS_MIN"]
LABEL = {"FG_PCT": "FG%", "FT_PCT": "FT%", "FG3M": "3PM", "REB": "REB", "AST": "AST",
         "STL": "STL", "BLK": "BLK", "DD": "DD", "PTS": "PTS", "PTS_MIN": "PPM"}
KEY = {v: k for k, v in LABEL.items()}
COUNTING = ["FG3M", "REB", "AST", "STL", "BLK", "DD", "PTS"]
RATIO = {"FG_PCT": ("FGM", "FGA"), "FT_PCT": ("FTM", "FTA"), "PTS_MIN": ("PTS", "MIN")}
ALL_POS = ["PG", "SG", "SF", "PF", "C"]


@dataclass
class Rules:
    games_per_week: float = 3.45          # NBA team games in a 7-day matchup, on average
    cats_to_win: int = 6
    roster_size: int = 13
    bench_count: int = 3
    budget: int = 200
    min_bid: int = 1
    max_centers: int = 4
    slots: tuple = (("PG", ("PG",)), ("SG", ("SG",)), ("SF", ("SF",)), ("PF", ("PF",)),
                    ("C", ("C",)), ("G", ("PG", "SG")), ("F", ("SF", "PF")),
                    ("UTIL", tuple(ALL_POS)), ("UTIL", tuple(ALL_POS)), ("UTIL", tuple(ALL_POS)))

    @property
    def n_starters(self) -> int:
        return len(self.slots)

    @classmethod
    def from_meta(cls, meta: dict) -> "Rules":
        return cls(
            games_per_week=float(meta.get("games_per_week", 3.45)),
            cats_to_win=int(meta.get("cats_to_win", 6)),
            roster_size=int(meta.get("roster_size", 13)),
            bench_count=int(meta.get("bench_count", 3)),
            budget=int(meta.get("budget_per_team", 200)),
            min_bid=int(meta.get("min_bid", 1)),
            max_centers=int(meta.get("max_centers", 4)),
            slots=tuple((n, tuple(a)) for n, a in meta.get("starter_slots", cls().slots)),
        )


def phi(x):
    """Standard normal CDF, vectorised (no scipy dependency)."""
    return 0.5 * (1.0 + _erf_vec(np.asarray(x, float) / math.sqrt(2.0)))


def _erf_vec(x):
    # Abramowitz & Stegun 7.1.26, max abs error 1.5e-7 -- plenty for a win probability.
    s = np.sign(x); x = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * x)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t
               + 0.254829592) * t * np.exp(-x * x)
    return s * y


def poisson_binomial_pmf(p: np.ndarray) -> np.ndarray:
    """P(exactly k successes), k = 0..n, for independent Bernoulli(p_c). `p` is (..., n_cats)."""
    p = np.asarray(p, float)
    dp = np.zeros(p.shape[:-1] + (p.shape[-1] + 1,))
    dp[..., 0] = 1.0
    for c in range(p.shape[-1]):
        pc = p[..., c:c + 1]
        dp[..., 1:] = dp[..., 1:] * (1 - pc) + dp[..., :-1] * pc
        dp[..., 0] = dp[..., 0] * (1 - pc[..., 0])
    return dp


def poisson_binomial_ge(p: np.ndarray, k: int) -> np.ndarray:
    """P(at least k successes) for independent Bernoulli(p_c)."""
    return poisson_binomial_pmf(p)[..., k:].sum(axis=-1)


# ── player features ─────────────────────────────────────────────────────────────────────────
RAW = ["MIN", "PTS", "REB", "AST", "STL", "BLK", "FG3M", "FGM", "FGA", "FTM", "FTA", "DD"]


@dataclass
class PoolModel:
    """Array view of the player pool: per-player weekly contribution means/variances.

    E[i, c]   expected weekly contribution of player i to category c (impact units for ratios)
    V[i, c]   variance of that weekly contribution
    D[i, r]   expected weekly volume (FGA, FTA, MIN) for the three ratio categories
    """
    rules: Rules
    ids: np.ndarray
    names: list
    pos: list                      # list[frozenset[str]]
    isC: np.ndarray
    E: np.ndarray
    V: np.ndarray
    D: np.ndarray
    rate: dict                     # league rate per ratio category (FG_PCT, FT_PCT, PTS_MIN)
    avail: np.ndarray
    cat_index: dict = field(default_factory=lambda: {c: i for i, c in enumerate(CATS)})

    @property
    def n(self) -> int:
        return len(self.ids)

    # ---- team aggregates ------------------------------------------------------------------
    def team_moments(self, idx):
        """(mu_cat, sd_cat) for a set of starters, in the units each category is scored in."""
        idx = np.asarray(idx, int)
        return self._moments(self.E[idx].sum(0), self.V[idx].sum(0), self.D[idx].sum(0))

    def _moments(self, SE, SV, SD):
        mu = SE.copy().astype(float)
        sd = np.sqrt(np.maximum(SV, 1e-12))
        for r, (c, _) in enumerate(RATIO.items()):
            ci = self.cat_index[c]
            den = np.maximum(SD[..., r], 1e-9)
            mu[..., ci] = self.rate[c] + SE[..., ci] / den
            sd[..., ci] = np.sqrt(np.maximum(SV[..., ci], 1e-12)) / den
        return mu, sd


def build_pool_model(df: pd.DataFrame, rules: Rules | None = None, rate: dict | None = None) -> PoolModel:
    """Build the array view from a table that carries per-game means (M_<raw>), per-game stds
    (S_<counting cat>, SI_FG / SI_FT / SI_PPM for the ratio impacts), AVAIL and POS_ELIGIBLE_STR."""
    rules = rules or Rules()
    df = df.reset_index(drop=True)
    g = rules.games_per_week
    a = df["AVAIL"].clip(0.05, 1.0).to_numpy(float)
    p_team = g / 4.0
    var_g = g * a * (1 - a) + 4 * p_team * (1 - p_team) * a ** 2         # Var(games played in a week)
    eg = g * a                                                           # E[games played in a week]

    m = {r: df["M_" + r].to_numpy(float) for r in RAW}
    if rate is None:      # league rate per ratio category = pooled numerator / pooled denominator
        rate = {c: float(df["M_" + n].sum() / df["M_" + d].sum()) for c, (n, d) in RATIO.items()}
    E = np.zeros((len(df), len(CATS))); V = np.zeros_like(E); D = np.zeros((len(df), 3))
    for ci, c in enumerate(CATS):
        if c in COUNTING:
            mean_g = m[c]; sd_g = df["S_" + c].to_numpy(float)
        else:
            num, den = RATIO[c]
            mean_g = m[num] - rate[c] * m[den]
            sd_g = df[{"FG_PCT": "SI_FG", "FT_PCT": "SI_FT", "PTS_MIN": "SI_PPM"}[c]].to_numpy(float)
        E[:, ci] = eg * mean_g
        V[:, ci] = eg * sd_g ** 2 + var_g * mean_g ** 2
    for r, c in enumerate(RATIO):
        D[:, r] = eg * m[RATIO[c][1]]

    pos = [frozenset(s) for s in df["POS_ELIGIBLE_STR"].map(_parse_pos)]
    return PoolModel(rules=rules, ids=df["PLAYER_ID"].to_numpy(), names=df["PLAYER_NAME"].tolist(),
                     pos=pos, isC=np.array(["C" in s for s in pos]), E=E, V=V, D=D, rate=rate, avail=a)


def _parse_pos(s) -> list:
    s = str(s)
    if "/" in s:
        return [p for p in s.split("/") if p]
    # compact form from the notebook: 'PGSG', 'SFPF', 'C', 'PFC'
    out, i = [], 0
    while i < len(s):
        if s[i:i + 2] in ("PG", "SG", "SF", "PF"):
            out.append(s[i:i + 2]); i += 2
        elif s[i] == "C":
            out.append("C"); i += 1
        else:
            i += 1
    return out


# ── the field benchmark ─────────────────────────────────────────────────────────────────────
@dataclass
class Field:
    """Opponent benchmark: mean of the other teams' category means, their average weekly sd, and
    the spread of team means across the league (some opponents are simply better than others)."""
    mu: np.ndarray
    sd: np.ndarray
    tau: np.ndarray

    @classmethod
    def from_rosters(cls, model: PoolModel, rosters) -> "Field":
        mus, sds = zip(*[model.team_moments(r) for r in rosters])
        mus, sds = np.array(mus), np.array(sds)
        return cls(mu=mus.mean(0), sd=np.sqrt((sds ** 2).mean(0)), tau=mus.std(0))

    def to_dict(self) -> dict:
        return {"mu": self.mu.tolist(), "sd": self.sd.tolist(), "tau": self.tau.tolist()}

    @classmethod
    def from_dict(cls, d: dict) -> "Field":
        return cls(np.array(d["mu"], float), np.array(d["sd"], float), np.array(d["tau"], float))


def cat_probs(mu, sd, fld: Field):
    """P(win each category) for team moments (mu, sd); broadcasts over leading dims."""
    gap = mu - fld.mu
    den = np.sqrt(sd ** 2 + fld.sd ** 2 + fld.tau ** 2)
    return phi(gap / den)


# Two corrections turn the independent-categories calculation into the matchup win probability the
# league actually pays out on:
#   1. TIES. Winning 5 of 10 is a 5-5 tie and counts as half a win ("no tie breakers"), so
#        P(win) = P(>= 6) + 0.5 * P(exactly 5).
#   2. CORRELATION. Categories move together within a week (a big-rebounding week is also a big
#      double-double week), so the independent calculation is too extreme: weak teams look worse and
#      strong teams better than they are. Measured against the bootstrap simulator over 90 rosters
#      (notebook, "validate the engine"):  logit(sim) = 0.66 * logit(independent), RMSE ~0.016
#      (uncalibrated 0.062). The intercept is fixed at 0 on purpose: a team exactly as good as the field must be
#      50%. The map is monotone, so it never changes which of two rosters is better.
CAL_A, CAL_B = 0.0, 0.66


def matchup_prob(p, rules: Rules, rho_shrink: float = 0.0, calibrate: bool = True):
    """Expected matchup result (win = 1, 5-5 tie = 0.5) from the ten category probabilities."""
    p = 0.5 + (np.asarray(p, float) - 0.5) * (1.0 - rho_shrink)
    pmf = poisson_binomial_pmf(p)
    k = rules.cats_to_win
    q = pmf[..., k:].sum(axis=-1) + 0.5 * pmf[..., k - 1]
    if calibrate and (CAL_A != 0.0 or CAL_B != 1.0):
        q = np.clip(q, 1e-6, 1 - 1e-6)
        q = 1.0 / (1.0 + np.exp(-(CAL_A + CAL_B * np.log(q / (1 - q)))))
    return q


def evaluate(model: PoolModel, idx, fld: Field, rho_shrink: float = 0.0) -> dict:
    mu, sd = model.team_moments(idx)
    p = cat_probs(mu, sd, fld)
    return {"p_cat": dict(zip(CATS, p.tolist())), "e_cats": float(p.sum()),
            "p_win": float(matchup_prob(p, model.rules, rho_shrink)), "mu": mu, "sd": sd}


# ═════════════════════════════════════════════════════════════════════════════════════════
# Roster optimiser
# ═════════════════════════════════════════════════════════════════════════════════════════
def assign_slots(model: PoolModel, idx) -> dict:
    """Augmenting-path (Kuhn) matching of players to the typed starter slots.
    Returns {slot_index: pool_index}; complete iff every slot got a player."""
    slots = [frozenset(a) for _, a in model.rules.slots]
    owner, filled = {}, {}

    def aug(si, seen):
        for pi in idx:
            if pi in seen or not (model.pos[pi] & slots[si]):
                continue
            seen.add(pi)
            if pi not in owner or aug(owner[pi], seen):
                owner[pi] = si; filled[si] = pi
                return True
        return False

    for si in sorted(range(len(slots)), key=lambda s: len(slots[s])):
        aug(si, set())
    return filled


def lineup_legal(model: PoolModel, starters, extra_centers: int = 0) -> bool:
    starters = [int(i) for i in starters]
    if len(starters) < model.rules.n_starters:
        return int(model.isC[starters].sum()) + extra_centers <= model.rules.max_centers
    if int(model.isC[starters].sum()) + extra_centers > model.rules.max_centers:
        return False
    return len(assign_slots(model, starters)) == model.rules.n_starters


def objective(model: PoolModel, fld: Field, SE, SV, SD, kind="pwin", rho=0.0):
    mu, sd = model._moments(SE, SV, SD)
    p = cat_probs(mu, sd, fld)
    return matchup_prob(p, model.rules, rho) if kind == "pwin" else p.sum(-1)


@dataclass
class Problem:
    """One optimisation instance: who is already mine, what is left, and what things cost."""
    model: PoolModel
    fld: Field
    price: np.ndarray                 # expected clearing price per pool player (>= min_bid)
    available: np.ndarray             # bool: can still be bought
    fixed: list = field(default_factory=list)        # my starters already bought (pool indices)
    budget: int = 200                 # money I still have
    bench_reserve: int = 3            # $ kept back to fill the bench at the minimum
    fixed_centers_off_lineup: int = 0 # centers I already own who are NOT in the starting ten
    kind: str = "pwin"
    rho: float = 0.0


def _surrogate_value(model: PoolModel, avail_idx):
    """Balanced z-score of expected weekly contribution; only used to seed the search."""
    E = model.E[avail_idx]
    z = (E - E.mean(0)) / np.maximum(E.std(0), 1e-9)
    return z.sum(1)


def _init_team(pr: Problem, alpha: float, rng=None):
    """Slot-by-slot greedy seed: tightest typed slots first, keeping $min_bid per open slot."""
    m, rules = pr.model, pr.model.rules
    avail_idx = np.flatnonzero(pr.available)
    val = np.full(m.n, -1e9); val[avail_idx] = _surrogate_value(m, avail_idx)
    team = list(pr.fixed)
    spent = 0                                                      # fixed players are already paid for
    slots = [frozenset(a) for _, a in rules.slots]
    open_slots = sorted(range(len(slots)), key=lambda s: len(slots[s]))
    # slots already satisfiable by fixed players are consumed by matching them first
    filled = assign_slots(m, team) if team else {}
    free_slots = [s for s in open_slots if s not in filled]
    budget = pr.budget - pr.bench_reserve
    ncen = int(m.isC[team].sum()) + pr.fixed_centers_off_lineup if team else pr.fixed_centers_off_lineup
    for k, si in enumerate(free_slots):
        left = len(free_slots) - k - 1
        cands = [i for i in avail_idx if i not in team and (m.pos[i] & slots[si])
                 and not (m.isC[i] and ncen >= rules.max_centers)]
        if not cands:
            return None
        cands = np.array(cands)
        cap = budget - spent - left * rules.min_bid
        ok = cands[pr.price[cands] <= cap]
        pool = ok if len(ok) else cands[np.argsort(pr.price[cands])][:1]
        score = val[pool] / np.maximum(pr.price[pool], 1.0) ** alpha
        pick = int(pool[np.argmax(score)])
        team.append(pick); spent += int(pr.price[pick]); ncen += int(m.isC[pick])
    return team


def _local_search(pr: Problem, team, lam: float, max_iter: int = 60):
    """Steepest-ascent swaps on  f(team) - lam * cost(team)  over the free starters."""
    m = pr.model
    team = list(team)
    nfix = len(pr.fixed)
    cand_all = np.flatnonzero(pr.available)
    for _ in range(max_iter):
        in_team = np.zeros(m.n, bool); in_team[team] = True
        cand = cand_all[~in_team[cand_all]]
        SE, SV, SD = m.E[team].sum(0), m.V[team].sum(0), m.D[team].sum(0)
        f_cur = float(objective(m, pr.fld, SE, SV, SD, pr.kind, pr.rho))
        tm = np.array(team)
        # (k, j): remove team[k], add cand[j]   -> arrays (n_team, n_cand, n_cat)
        SEk = (SE[None, :] - m.E[tm])[:, None, :] + m.E[cand][None, :, :]
        SVk = (SV[None, :] - m.V[tm])[:, None, :] + m.V[cand][None, :, :]
        SDk = (SD[None, :] - m.D[tm])[:, None, :] + m.D[cand][None, :, :]
        f = objective(m, pr.fld, SEk, SVk, SDk, pr.kind, pr.rho)               # (n_team, n_cand)
        dcost = pr.price[cand][None, :] - pr.price[tm][:, None]
        gain = f - f_cur - lam * dcost
        gain[:nfix, :] = -np.inf                                               # fixed players stay
        order = np.argsort(-gain, axis=None)[:40]
        moved = False
        for flat in order:
            k, j = divmod(int(flat), len(cand))
            if gain[k, j] <= 1e-9:
                break
            trial = team[:k] + team[k + 1:] + [int(cand[j])]
            if lineup_legal(m, trial, pr.fixed_centers_off_lineup):
                team = team[:k] + [int(cand[j])] + team[k + 1:]
                moved = True
                break
        if not moved:
            break
    return team


def _cost(pr: Problem, team) -> int:
    """Money still to be spent: only the free (not yet bought) starters cost anything."""
    return int(sum(pr.price[i] for i in team[len(pr.fixed):]))


def solve(pr: Problem, restarts=(0.0, 0.6, 1.0)):
    """Best legal starting ten within budget. Returns (team, f, lam).

    The budget is handled with a Lagrange price `lam` (win probability per dollar): at lam the
    unconstrained optimum of  f - lam*cost  is found by local search, and lam is bisected until
    the resulting team just fits the budget. Several seeds are tried; the best feasible one wins.
    """
    m = pr.model
    cap = pr.budget - pr.bench_reserve
    best = None
    for alpha in restarts:
        seed = _init_team(pr, alpha)
        if seed is None:
            continue
        team0 = _local_search(pr, seed, 0.0)
        if _cost(pr, team0) <= cap:
            lo = hi = 0.0; cand = team0; lam_used = 0.0
        else:
            lo, hi, cand, lam_used = 0.0, 0.02, None, 0.02
            team_hi = _local_search(pr, seed, hi)
            while _cost(pr, team_hi) > cap and hi < 1.0:
                hi *= 2; team_hi = _local_search(pr, team_hi, hi)
            cand = team_hi
            for _ in range(9):
                mid = 0.5 * (lo + hi)
                t = _local_search(pr, cand, mid)
                if _cost(pr, t) <= cap:
                    hi, cand, lam_used = mid, t, mid
                else:
                    lo = mid
        # polish: with the budget now hard, take any remaining strictly improving swap that fits
        cand = _polish(pr, cand, cap)
        if _cost(pr, cand) > cap or not lineup_legal(m, cand, pr.fixed_centers_off_lineup):
            continue
        SE, SV, SD = m.E[cand].sum(0), m.V[cand].sum(0), m.D[cand].sum(0)
        f = float(objective(m, pr.fld, SE, SV, SD, pr.kind, pr.rho))
        if best is None or f > best[1]:
            best = (cand, f, lam_used)
    return best


def _polish(pr: Problem, team, cap: int, max_iter=30):
    m = pr.model; team = list(team); nfix = len(pr.fixed)
    cand_all = np.flatnonzero(pr.available)
    for _ in range(max_iter):
        in_team = np.zeros(m.n, bool); in_team[team] = True
        cand = cand_all[~in_team[cand_all]]
        SE, SV, SD = m.E[team].sum(0), m.V[team].sum(0), m.D[team].sum(0)
        f_cur = float(objective(m, pr.fld, SE, SV, SD, pr.kind, pr.rho)); tm = np.array(team)
        SEk = (SE[None, :] - m.E[tm])[:, None, :] + m.E[cand][None, :, :]
        SVk = (SV[None, :] - m.V[tm])[:, None, :] + m.V[cand][None, :, :]
        SDk = (SD[None, :] - m.D[tm])[:, None, :] + m.D[cand][None, :, :]
        f = objective(m, pr.fld, SEk, SVk, SDk, pr.kind, pr.rho)
        cost_after = _cost(pr, team) + pr.price[cand][None, :] - pr.price[tm][:, None]
        gain = np.where(cost_after <= cap, f - f_cur, -np.inf); gain[:nfix, :] = -np.inf
        moved = False
        for flat in np.argsort(-gain, axis=None)[:40]:
            k, j = divmod(int(flat), len(cand))
            if gain[k, j] <= 1e-9:
                break
            trial = team[:k] + [int(cand[j])] + team[k + 1:]
            if lineup_legal(m, trial, pr.fixed_centers_off_lineup):
                team = trial; moved = True; break
        if not moved:
            break
    return team


def max_bids(pr: Problem, team, lam: float) -> np.ndarray:
    """Break-even price for every pool player, from the shadow price `lam` of the budget.

    Forcing player j onto the team in place of starter k changes win probability by d_f and cost
    by (p_j - price_k). At the optimum, j is worth buying iff  d_f - lam*(p_j - price_k) >= 0, i.e.
        p_j <= price_k + d_f / lam                                 (best k)
    For a player already on the optimal team the same logic runs against the best replacement
    (the price at which he stops being worth it). One vectorised pass covers every player.
    Returns an array of dollars (0 where he is never worth a bid)."""
    m = pr.model; team = list(team); nfix = len(pr.fixed); tm = np.array(team)
    lam = max(lam, 1e-6)
    SE, SV, SD = m.E[team].sum(0), m.V[team].sum(0), m.D[team].sum(0)
    f_cur = float(objective(m, pr.fld, SE, SV, SD, pr.kind, pr.rho))
    cand = np.flatnonzero(pr.available & ~np.isin(np.arange(m.n), team))
    SEk = (SE[None, :] - m.E[tm])[:, None, :] + m.E[cand][None, :, :]
    SVk = (SV[None, :] - m.V[tm])[:, None, :] + m.V[cand][None, :, :]
    SDk = (SD[None, :] - m.D[tm])[:, None, :] + m.D[cand][None, :, :]
    f = objective(m, pr.fld, SEk, SVk, SDk, pr.kind, pr.rho)               # (n_team, n_cand)
    wtp_c = pr.price[tm][:, None] + (f - f_cur) / lam
    wtp_c[:nfix, :] = -np.inf
    out = np.zeros(m.n)
    # each candidate takes the best victim whose swap is LEGAL (Center cap, typed slots still fillable)
    order_k = np.argsort(-wtp_c, axis=0)
    for jj, cj in enumerate(cand):
        if wtp_c[order_k[0, jj], jj] < pr.model.rules.min_bid:
            continue
        for rank in range(min(5, len(team))):
            k = int(order_k[rank, jj])
            if not np.isfinite(wtp_c[k, jj]):
                break
            if lineup_legal(m, team[:k] + [int(cj)] + team[k + 1:], pr.fixed_centers_off_lineup):
                out[cj] = max(wtp_c[k, jj], 0.0)
                break
    # members of the optimal team: price at which the best replacement becomes preferable
    for k in range(nfix, len(team)):
        rest = [t for t in team if t != team[k]]
        SEr, SVr, SDr = m.E[rest].sum(0), m.V[rest].sum(0), m.D[rest].sum(0)
        SEa = SEr[None, :] + m.E[cand]; SVa = SVr[None, :] + m.V[cand]; SDa = SDr[None, :] + m.D[cand]
        fa = objective(m, pr.fld, SEa, SVa, SDa, pr.kind, pr.rho)
        La = fa - lam * pr.price[cand]
        legal = np.array([lineup_legal(m, rest + [int(c)], pr.fixed_centers_off_lineup) for c in cand[np.argsort(-La)[:15]]])
        top = cand[np.argsort(-La)[:15]]
        j = int(np.argmax(legal)) if legal.any() else None
        if j is None:
            out[team[k]] = pr.price[team[k]]
            continue
        a = top[j]
        f_alt = float(fa[np.where(cand == a)[0][0]])
        out[team[k]] = max(pr.price[a] + (f_cur - f_alt) / lam, 0.0)
    return out


# ═════════════════════════════════════════════════════════════════════════════════════════
# Live price forecast
# ═════════════════════════════════════════════════════════════════════════════════════════
TIER_EDGES = (8, 20, 50)          # market-price tiers: <8, 8-20, 20-50, 50+


def live_prices(market, sold_idx, paid, avail, budget_left: float, slots_left: int,
                min_bid: float = 1.0, prior_picks: float = 3.0):
    """Forecast what each still-available player will cost, given how the room has actually paid.

    Two effects are separated, because they need different data:
      * LEVEL: money conservation. All remaining budget will be spent on the `slots_left` players
        still to be drafted, so the forecast is scaled until the likely buys cost exactly that.
      * SHAPE: do stars / mid-tier / cheap players clear above or below the pre-draft board?
        Learned per price tier from the sales so far, shrunk toward the room-wide ratio with
        `prior_picks` pseudo-sales so a single odd result cannot swing a whole tier.
    market  : pre-draft market price per pool player (array)
    sold_idx: pool indices already sold;  paid: price each sold for
    avail   : bool mask of players still on the board
    """
    market = np.asarray(market, float)
    tier = np.digitize(market, TIER_EDGES)
    sold_idx = np.asarray(sold_idx, int); paid = np.asarray(paid, float)
    glob = (paid.sum() / max(market[sold_idx].sum(), 1.0)) if len(sold_idx) else 1.0
    factor = np.ones(len(TIER_EDGES) + 1)
    for b in range(len(factor)):
        in_b = tier[sold_idx] == b if len(sold_idx) else np.zeros(0, bool)
        m_bar = market[(tier == b)].mean() if (tier == b).any() else 1.0
        sm = market[sold_idx][in_b].sum() if in_b.any() else 0.0
        sp = paid[in_b].sum() if in_b.any() else 0.0
        ratio = (sp + prior_picks * glob * m_bar) / (sm + prior_picks * m_bar)
        factor[b] = ratio / max(glob, 1e-9)
    raw = market * factor[tier]
    idx = np.flatnonzero(avail)
    likely = idx[np.argsort(-market[idx])][:max(int(slots_left), 1)]            # who will actually be bought
    spendable = budget_left - min_bid * 0                                       # all remaining money gets spent
    c = spendable / max(float(raw[likely].sum()), 1e-9)
    return np.maximum(raw * c, min_bid), factor
