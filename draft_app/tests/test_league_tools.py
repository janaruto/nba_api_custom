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


# ── want: several players per side, several options ──────────────────────────────────────────────────
def res(d_me, d_x, v_give=10.0, v_get=10.0):
    return {"d_me": d_me, "d_x": d_x, "v_give": v_give, "v_get": v_get}


def test_parse_count_numbers_ranges_and_errors():
    assert lt.parse_count("2") == [2] and lt.parse_count("1-3") == [1, 2, 3] and lt.parse_count("3-1") == [1, 2, 3]
    assert lt.parse_count(None, "1-2") == [1, 2] and lt.parse_count("", "1") == [1]
    for bad in ("0", "4", "1-4", "zwei", "1,2"):
        with pytest.raises(SystemExit):
            lt.parse_count(bad)


def test_options_are_sorted_by_their_own_goal_and_never_repeat_a_trade():
    both_small = ([1], [9], res(0.004, 0.004))
    both_big = ([2], [9], res(0.012, 0.010))
    me_only = ([3], [9], res(0.030, -0.008))
    fair_only = ([4], [9], res(0.006, -0.002, v_give=12, v_get=12))
    hurts_me = ([5], [9], res(-0.01, 0.02))
    allr = [both_small, me_only, hurts_me, both_big, fair_only]
    one = [[x[0] for x in c] for _, c in lt.pick_options(allr, top=1)]
    assert one == [[[2]], [[3]], [[4]]]                                                              # one answer per option, each by its own goal
    two = [[x[0] for x in c] for _, c in lt.pick_options(allr, top=2)]
    assert two[0] == [[2], [1]]                                                                      # by the smaller of the two gains
    shown = [tuple(g) for c in two for g in c]
    assert len(shown) == len(set(shown)) and (5,) not in shown                                        # a trade that costs you is never offered


def test_a_trade_that_only_adds_pieces_without_changing_anything_is_dropped():
    small = ([1], [9], res(0.004, 0.002))
    padded_give = ([1, 2], [9], res(0.004, 0.002))                                                   # 2 sits on the bench: no effect
    padded_get = ([1], [9, 8], res(0.004, 0.002))
    different = ([1, 3], [9], res(0.009, 0.002))
    kept = lt.drop_redundant([small, padded_give, padded_get, different])
    assert sorted((tuple(g), tuple(t)) for g, t, _ in kept) == [((1,), (9,)), ((1, 3), (9,))]


def test_candidates_cover_the_requested_sizes_and_respect_keep_and_skip():
    L = types.SimpleNamespace(you="me", rosters={"me": [1, 2, 3, 4], "them": [9, 8, 7, 6, 5]})
    c = lt.want_candidates(L, "them", [9], give_ns=[1, 2], get_ns=[1, 2], keep={4}, skip={8})
    sizes = {(len(g), len(t)) for g, t in c}
    assert sizes == {(1, 1), (1, 2), (2, 1), (2, 2)}
    assert all(9 in t and 4 not in g and 8 not in t for g, t in c)
    assert len(c) == (3 + 3) * (1 + 3)                                                                # give 1 or 2 of my 3 offerable · get nothing extra or 1 of {7, 6, 5}
