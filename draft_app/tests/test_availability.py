"""Injury-aware availability: ESPN OUT status and manual overrides on top of the notebook's AVAIL."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import availability  # noqa: E402


@pytest.fixture
def pool():
    return pd.DataFrame({"PLAYER_ID": [1, 2, 3, 4], "PLAYER_NAME": ["Brandon Ingram", "Nikola Jokić", "Day Tripper", "Fit Guy"],
                         "AVAIL": [0.74, 0.73, 0.70, 0.75], "EXP_GP": [61, 60, 57, 62]})


def test_out_status_takes_games_off(pool):
    inj = {"1": {"status": "OUT", "injured": True, "outlook": "foot surgery"}}
    p = availability.adjust_pool(pool, inj, {})
    assert p.loc[0, "AVAIL"] == pytest.approx(0.74 - availability.OUT_GAMES_LOST / 82)
    assert p.loc[0, "EXP_GP"] == round(p.loc[0, "AVAIL"] * 82)
    assert p.loc[0, "AVAIL_BASE"] == 0.74 and p.loc[0, "AVAIL_SOURCE"].startswith("ESPN OUT")
    assert p.loc[0, "INJURY_NOTE"] == "foot surgery"
    assert (p.loc[1:, "AVAIL"] == [0.73, 0.70, 0.75]).all()                 # everybody else untouched


def test_day_to_day_changes_nothing(pool):
    p = availability.adjust_pool(pool, {"3": {"status": "DAY_TO_DAY", "injured": False, "outlook": ""}}, {})
    assert p.loc[2, "AVAIL"] == 0.70 and p.loc[2, "INJURY_STATUS"] == "DAY_TO_DAY"


def test_manual_override_wins_over_espn_and_ignores_accents(pool):
    inj = {"2": {"status": "OUT", "injured": True, "outlook": ""}}
    p = availability.adjust_pool(pool, inj, {"Nikola Jokic": 70})
    assert p.loc[1, "AVAIL"] == pytest.approx(70 / 82) and p.loc[1, "EXP_GP"] == 70
    assert p.loc[1, "AVAIL_SOURCE"] == "manuell: 70 Spiele"


def test_floor_and_cap(pool):
    p = availability.adjust_pool(pool, {"1": {"status": "OUT", "injured": True}}, {"Fit Guy": 200})
    assert p.loc[0, "AVAIL"] >= availability.AVAIL_FLOOR_OUT and p.loc[3, "AVAIL"] == 0.95


def test_input_frame_is_not_modified_and_unknown_ids_are_ignored(pool):
    before = pool.copy()
    availability.adjust_pool(pool, {"999": {"status": "OUT", "injured": True}}, {"Nobody": 10})
    pd.testing.assert_frame_equal(pool, before)
