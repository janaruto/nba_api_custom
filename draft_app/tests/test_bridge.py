"""Tests for name matching and the ESPN bridge's event handling (no HTTP, no browser)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge  # noqa: E402
import names  # noqa: E402

TEAMS = ["Brick City", "Janartuo Ninjas", "Switchler to the moon", "Noah's Nasty Team", "Team 15"]
POOL = [(203999, "Nikola Jokić"), (1641705, "Victor Wembanyama"), (1630162, "Jaren Jackson Jr."), (7, "Alex Sarr"), (8, "Alex Sarr")]


@pytest.fixture
def br(tmp_path):
    return bridge.Bridge([p for p, _ in POOL], [n for _, n in POOL], TEAMS, tmp_path / "log.json", tmp_path / "nom.json",
                         tmp_path / "unres.json", tmp_path)


def test_name_key_ignores_accents_suffix_and_punctuation():
    assert names.name_key("Nikola Jokić") == names.name_key("Nikola Jokic") == "nikolajokic"
    assert names.name_key("Jaren Jackson Jr.") == names.name_key("Jaren Jackson")
    assert names.name_key("De'Aaron Fox") == names.name_key("DeAaron Fox") == "deaaronfox"


def test_ambiguous_names_are_never_guessed(br):
    assert names.resolve_player(br.index, "Alex Sarr") is None       # two different players share the key


def test_team_matching():
    assert names.resolve_team(TEAMS, "brick city") == "Brick City"
    assert names.resolve_team(TEAMS, "Noahs Nasty Team") == "Noah's Nasty Team"
    assert names.resolve_team(TEAMS, "Team 15 (Hans Muster)") == "Team 15"        # owner name appended by ESPN
    assert names.resolve_team(TEAMS, "Unbekannt FC") is None
    assert names.resolve_team(TEAMS, "") is None


def test_sold_is_logged_once(br):
    ev = {"type": "sold", "player": "Nikola Jokic", "team": "brick city", "price": "$107"}
    assert br.handle(ev)["status"] == "logged"
    assert br.handle(ev)["status"].startswith("duplicate")
    log = json.loads(br.log_json.read_text())
    assert log == [{"player_id": 203999, "player_name": "Nikola Jokić", "team": "Brick City", "price": 107, "source": "espn"}]


def test_unmatched_sale_is_not_logged_but_reported(br):
    res = br.handle({"type": "sold", "player": "Nikola Jokik", "team": "Brick City", "price": 50})
    assert not res["ok"] and not br.log_json.exists()
    items = json.loads(br.unresolved_json.read_text())
    assert items[0]["why"] == "Spieler nicht im Pool" and "Nikola Jokić" in items[0]["suggestions"]
    res = br.handle({"type": "sold", "player": "Victor Wembanyama", "team": "Nobody FC", "price": 50})
    assert not res["ok"] and json.loads(br.unresolved_json.read_text())[-1]["why"] == "Team nicht erkannt"
    assert not br.log_json.exists()


def test_bad_price_is_rejected(br):
    assert not br.handle({"type": "sold", "player": "Victor Wembanyama", "team": "Brick City", "price": "n/a"})["ok"]
    assert not br.handle({"type": "sold", "player": "Victor Wembanyama", "team": "Brick City", "price": 0})["ok"]


def test_nomination_then_sale_clears_it(br):
    br.handle({"type": "nominated", "player": "Victor Wembanyama", "bid": 12, "team": "Brick City"})
    nom = json.loads(br.nom_json.read_text())
    assert nom["player_id"] == 1641705 and nom["bid"] == 12 and nom["team"] == "Brick City"
    br.handle({"type": "nominated", "player": "Victor Wembanyama", "bid": "$13", "team": "Janartuo Ninjas"})
    assert json.loads(br.nom_json.read_text())["bid"] == 13
    br.handle({"type": "sold", "player": "Victor Wembanyama", "team": "Janartuo Ninjas", "price": 13})
    assert not br.nom_json.exists()


def test_sale_of_another_player_keeps_the_nomination(br):
    br.handle({"type": "nominated", "player": "Victor Wembanyama", "bid": 1})
    br.handle({"type": "sold", "player": "Nikola Jokić", "team": "Brick City", "price": 90})
    assert br.nom_json.exists()


def test_unknown_nominee_is_still_shown_without_an_id(br):
    br.handle({"type": "nominated", "player": "Some Rookie", "bid": 3})
    nom = json.loads(br.nom_json.read_text())
    assert nom["player_id"] is None and nom["espn_name"] == "Some Rookie"


def test_clear_and_unknown_type(br):
    br.handle({"type": "nominated", "player": "Victor Wembanyama", "bid": 1})
    assert br.handle({"type": "clear"})["ok"] and not br.nom_json.exists()
    assert not br.handle({"type": "bogus"})["ok"]


def test_dump_file_name_is_sanitised(br, tmp_path):
    p = br.dump("../../etc/pass wd", "<html></html>")
    assert p.parent == tmp_path and p.name == "draft_room_dump_etcpasswd.html"
