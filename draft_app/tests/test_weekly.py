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


# ── matchup week pick, what-if swaps ─────────────────────────────────────────────────────────────────
PERIODS = [{"week": 1, "start": "2026-10-20", "end": "2026-10-25"}, {"week": 2, "start": "2026-10-26", "end": "2026-11-01"},
           {"week": 3, "start": "2026-11-03", "end": "2026-11-08"}]


def test_pick_week_takes_the_running_one_else_the_next_else_none():
    assert wl.pick_week(PERIODS, dt.date(2026, 10, 22))["week"] == 1                 # inside week 1
    assert wl.pick_week(PERIODS, dt.date(2026, 10, 19))["week"] == 1                 # before the season: the first week
    assert wl.pick_week(PERIODS, dt.date(2026, 11, 2))["week"] == 3                  # in the gap between two weeks: the next one
    assert wl.pick_week(PERIODS, dt.date(2026, 11, 8))["week"] == 3                  # last day included
    assert wl.pick_week(PERIODS, dt.date(2026, 11, 9)) is None


def test_parse_swaps_accepts_several_forms_and_rejects_nonsense():
    assert wl.parse_swaps(["Kyle Filipowski=Jalen Suggs"]) == [("Kyle Filipowski", "Jalen Suggs")]
    assert wl.parse_swaps(["A = B; C -> D", "E=F"]) == [("A", "B"), ("C", "D"), ("E", "F")]
    assert wl.parse_swaps(None) == []
    for bad in ("nur ein Name", "=B", "A="):
        with pytest.raises(SystemExit):
            wl.parse_swaps([bad])


def _setup(n=40):
    d = synthetic_pool(n=n)
    d["CURRENT_TEAM_ABBR"] = "BOS"
    m = engine.build_pool_model(d, engine.Rules(), games=np.full(len(d), 3.0))
    opp = m.team_moments(list(range(15, 25)))
    roster = [0, 3, 5, 8, 12, 14, 2, 21, 30, 33, 36, 4, 9]
    return d, m, opp, roster


def test_a_forced_swap_is_respected_and_can_never_beat_the_free_best():
    d, m, opp, roster = _setup()
    lu, p, _ = wl.best_lineup(m, roster, opp)
    out = lu[0]
    inn = next(i for i in roster if i not in lu)
    lu2, p2, _ = wl.best_lineup(m, roster, opp, must={inn}, forbid={out})
    assert inn in lu2 and out not in lu2 and engine.lineup_legal(m, lu2)
    assert p2 <= p + 1e-12


def test_impossible_constraints_give_none():
    d, m, opp, roster = _setup()
    assert wl.best_lineup(m, roster, opp, forbid=set(roster[:4])) is None            # only 3 bench places for 4 players who must sit
    assert wl.best_lineup(m, roster, opp, must=set(roster[:11])) is None             # 11 players cannot all start


@pytest.mark.parametrize("swap", [("P0", "P1"), ("P0", "P30"), ("P0", "P25")])
def test_swap_report_runs_for_lineup_wishes_and_for_players_from_other_rosters(capsys, swap):
    d, m, opp, roster = _setup()
    wl.swap_report(m, d, np.full(len(d), 3.0), roster, 0.5, np.full(10, 0.5), opp, [swap], np.zeros(len(d), bool))
    out = capsys.readouterr().out
    assert "Tausch-Rechnung" in out and "Siegchance" in out and "Aufstellung mit Tausch" in out
    if swap[1] == "P25":
        assert "verlässt den Kader" in out and "P25" in out


def test_swap_report_names_unknown_and_foreign_out_players(capsys):
    d, m, opp, roster = _setup()
    with pytest.raises(SystemExit):
        wl.swap_report(m, d, np.full(len(d), 3.0), roster, 0.5, np.full(10, 0.5), opp, [("P0", "Nobody Atall")], np.zeros(len(d), bool))
    with pytest.raises(SystemExit):                                                  # P20 is not on the roster, so it cannot be swapped out
        wl.swap_report(m, d, np.full(len(d), 3.0), roster, 0.5, np.full(10, 0.5), opp, [("P20", "P0")], np.zeros(len(d), bool))


# ── a single matchup week assumes every player plays all of his team's games ─────────────────────────
WEEK_META = {"league_rates": {"FG_PCT": 0.47, "FT_PCT": 0.78, "PTS_MIN": 0.49}}


def _week_pool(n=30):
    d = synthetic_pool(n=n)
    d["CURRENT_TEAM_ABBR"] = ["BOS", "NYK"] * (n // 2)
    d["INJURY_STATUS"] = ""
    d.loc[3, "INJURY_STATUS"] = "OUT"                                                # player 3 plays for NYK
    d.loc[5, "INJURY_STATUS"] = "DAY_TO_DAY"
    return d


def test_every_player_plays_all_team_games_by_default():
    d = _week_pool()
    m, g, unknown, out = wl.week_model(d, WEEK_META, {"BOS": 3, "NYK": 4})
    pts = engine.CATS.index("PTS")
    assert g[0] == 3 and g[1] == 4
    assert m.E[0, pts] == pytest.approx(3 * d.M_PTS[0])                              # BOS player: 3 games x per-game mean, no availability discount
    assert m.E[1, pts] == pytest.approx(4 * d.M_PTS[1])
    assert m.V[0, pts] == pytest.approx(3 * d.S_PTS[0] ** 2)                         # games are known and certain: no games-played variance
    assert m.E[5, pts] == pytest.approx(4 * d.M_PTS[5])                              # day-to-day counts as playing
    assert out[3] and out.sum() == 1
    assert m.E[3, pts] < 0.1 * m.E[1, pts]                                           # ESPN OUT: practically nothing


def test_out_players_can_be_counted_in_and_expected_games_can_be_switched_on():
    d = _week_pool()
    pts = engine.CATS.index("PTS")
    m, _, _, out = wl.week_model(d, WEEK_META, {"BOS": 3, "NYK": 4}, ignore_out=True)
    assert m.E[3, pts] == pytest.approx(4 * d.M_PTS[3]) and not out.any()
    m2, _, _, _ = wl.week_model(d, WEEK_META, {"BOS": 3, "NYK": 4}, full_games=False)
    assert m2.E[0, pts] == pytest.approx(3 * float(d.AVAIL[0]) * d.M_PTS[0])         # the old weighting by the model's availability
