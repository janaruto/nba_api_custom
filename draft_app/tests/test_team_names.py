"""Teams are renamed in ESPN all the time: follow them by ESPN's team id, resolve every earlier name."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import names  # noqa: E402


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setattr(names, "TEAMS_FILE", tmp_path / "league_teams.json")
    monkeypatch.setattr(names, "LOCAL_FILE", tmp_path / "local_config.json")
    return tmp_path


def teams(*pairs):
    return [{"id": i, "name": n} for i, n in pairs]


def test_first_run_seeds_earlier_names_from_the_draft_time_list_in_espns_team_order(reg):
    renames = names.update_team_registry(teams((5, "Janaruto Ninjas"), (1, "Brick City"), (9, "Repeating Champs")),
                                         seed_names=["Brick City", "Janartuo Ninjas", "Inaugural Champs"])      # sorted by id: 1, 5, 9
    assert sorted(renames) == [("Inaugural Champs", "Repeating Champs"), ("Janartuo Ninjas", "Janaruto Ninjas")]
    assert names.team_aliases() == {"Janartuo Ninjas": "Janaruto Ninjas", "Inaugural Champs": "Repeating Champs"}


def test_later_renames_are_found_by_id_and_chains_resolve(reg):
    names.update_team_registry(teams((1, "Alpha"), (2, "Beta")))
    assert names.update_team_registry(teams((1, "Alpha"), (2, "Beta"))) == []                                    # nothing changed
    assert names.update_team_registry(teams((1, "Alpha 2"), (2, "Beta"))) == [("Alpha", "Alpha 2")]
    names.update_team_registry(teams((1, "Alpha 3"), (2, "Beta")))
    for old in ("Alpha", "Alpha 2", "Alpha 3"):
        assert names.canonical_team(old) == "Alpha 3"
    assert names.same_team("Alpha", "Alpha 3") and not names.same_team("Alpha", "Beta")
    assert names.canonical_team("Never Heard Of") == "Never Heard Of"


def test_no_seeding_when_the_lists_do_not_line_up(reg):
    assert names.update_team_registry(teams((1, "A"), (2, "B")), seed_names=["X"]) == []
    assert names.team_aliases() == {}


def test_manual_aliases_from_the_local_config_work_and_a_missing_registry_is_fine(reg):
    assert names.team_aliases() == {}
    (reg / "local_config.json").write_text(json.dumps({"team_aliases": {"Old Name": "New Name"}, "league_id": 1}))
    assert names.canonical_team("Old Name") == "New Name" and names.same_team("old name", "NEW NAME")


def test_espn_team_name_falls_back_to_location_and_nickname():
    assert names.espn_team_name({"id": 3, "location": "Team", "nickname": "Three"}) == "Team Three"
    assert names.espn_team_name({"id": 4}) == "Team 4"
