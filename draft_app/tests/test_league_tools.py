"""League tools: ESPN roster parsing and the 'what changed since last time' report (no network, no model files needed)."""
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import league_tools as lt  # noqa: E402


def espn(teams: dict) -> dict:
    return {"teams": [{"id": i + 1, "name": n, "roster": {"entries": [{"playerPoolEntry": {"player": {"fullName": p}}} for p in ps]}}
                      for i, (n, ps) in enumerate(teams.items())]}


def test_parse_uses_name_or_location_and_nickname():
    j = {"teams": [{"id": 1, "name": "Brick City", "roster": {"entries": [{"playerPoolEntry": {"player": {"fullName": "A B"}}}]}},
                   {"id": 2, "location": "Team", "nickname": "Two", "roster": {"entries": []}},
                   {"id": 3, "roster": {"entries": [{"playerPoolEntry": {}}]}}]}
    assert lt.parse_espn_league(j) == {"Brick City": ["A B"], "Team Two": [], "Team 3": []}


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(lt, "ROSTERS_JSON", tmp_path / "league_rosters.json")
    monkeypatch.setattr(lt, "ROSTERS_PREV", tmp_path / "league_rosters_prev.json")
    return tmp_path


def test_refresh_reports_trades_adds_drops_and_keeps_the_old_team_key(paths, capsys):
    old = {"Team 15": ["P1", "P2"], "Other": ["P3", "P4"]}
    lt.ROSTERS_JSON.write_text(json.dumps(old))
    (paths / "espn.json").write_text(json.dumps(espn({"Unsupervised Chaos": ["P1", "P3", "New Guy"], "Other": ["P2"]})))
    new = lt.refresh(types.SimpleNamespace(file=str(paths / "espn.json")))
    out = capsys.readouterr().out
    assert "P3: Other -> Team 15" in out and "P2: Team 15 -> Other" in out        # the two moves are trades
    assert "+ New Guy (Team 15)" in out and "- P4 (Other)" in out
    assert "Team 15" in new and "Unsupervised Chaos" not in new                  # legacy key kept so older tools keep working
    assert json.loads(lt.ROSTERS_PREV.read_text()) == old


@pytest.fixture
def no_files(paths, monkeypatch):
    """No league file anywhere (neither in the app folder nor in ~/Downloads) and no network."""
    monkeypatch.setattr(lt, "LEAGUE_JSON", paths / "espn_league.json")
    monkeypatch.setattr(lt, "AUTH_JSON", paths / "espn_auth.json")
    monkeypatch.setattr(lt.Path, "home", classmethod(lambda cls: paths / "home"))
    monkeypatch.setattr(lt, "fetch_with_cookies", lambda: None)
    return paths


def test_refresh_without_a_source_explains_what_to_do(no_files):
    with pytest.raises(SystemExit) as e:
        lt.refresh(types.SimpleNamespace(file=None))
    assert "espn_league.json" in str(e.value) and "Geprüft" in str(e.value)


def test_a_missing_explicit_file_is_named(no_files):
    with pytest.raises(SystemExit) as e:
        lt.refresh(types.SimpleNamespace(file=str(no_files / "nope.json")))
    assert "existiert nicht" in str(e.value)


def test_league_data_saved_under_the_wrong_name_is_found(no_files):
    (no_files / "espn_auth.json").write_text(json.dumps(espn({"A": ["P1"], "B": ["P2"]})))   # the real-life mistake
    j, path = lt.find_league_file()
    assert path.name == "espn_auth.json" and len(j["teams"]) == 2


def test_a_cookie_file_is_never_taken_for_league_data(no_files):
    (no_files / "espn_auth.json").write_text(json.dumps({"espn_s2": "x", "swid": "{y}"}))
    assert lt.find_league_file() == (None, None)


def test_league_file_in_downloads_is_found(no_files):
    dl = no_files / "home" / "Downloads"; dl.mkdir(parents=True)
    (dl / "espn-league (1).json").write_text(json.dumps(espn({"A": ["P1"], "B": ["P2"]})))
    assert lt.find_league_file()[1].parent == dl


def test_a_non_json_file_is_explained(paths):
    (paths / "page.json").write_text("<html>login</html>")
    with pytest.raises(SystemExit) as e:
        lt.refresh(types.SimpleNamespace(file=str(paths / "page.json")))
    assert "kein JSON" in str(e.value)


def test_json_without_teams_is_explained(paths):
    (paths / "other.json").write_text(json.dumps({"hello": 1}))
    with pytest.raises(SystemExit) as e:
        lt.refresh(types.SimpleNamespace(file=str(paths / "other.json")))
    assert "keine ESPN-Teamliste" in str(e.value)
