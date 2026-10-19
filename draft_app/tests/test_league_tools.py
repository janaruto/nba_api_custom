"""League tools: ESPN roster parsing and the 'what changed since last time' report (no network, no model files needed)."""
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import league_tools as lt  # noqa: E402

REAL_FETCH = lt.fetch_with_cookies                                              # before any test patches it


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
    monkeypatch.setattr(lt, "fetch_with_cookies", lambda creds=None: None)
    return paths


def test_refresh_without_a_source_explains_what_to_do(no_files):
    with pytest.raises(SystemExit) as e:
        lt.refresh(types.SimpleNamespace(file=None))
    assert "espn_league.json" in str(e.value) and "login" in str(e.value)


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


# ── which file counts, and does the tool tell you when it is stale? ──────────────────────────────────
def _age(path, hours):
    import os
    t = __import__("time").time() - hours * 3600
    os.utime(path, (t, t))


def test_the_newest_file_wins_even_if_the_app_folder_has_an_older_one(no_files, monkeypatch):
    monkeypatch.setattr(lt, "LOCAL_JSON", no_files / "local_config.json")
    dl = no_files / "home" / "Downloads"; dl.mkdir(parents=True)
    old = no_files / "espn_league.json"; old.write_text(json.dumps(espn({"A": ["P1"], "B": ["P2"]}))); _age(old, 30)
    new = dl / "espn-league (1).json"; new.write_text(json.dumps(espn({"A": ["P1", "P3"], "B": ["P2"]}))); _age(new, 1)
    j, path = lt.find_league_file()
    assert path == new and len(lt.parse_espn_league(j)["A"]) == 2
    _age(new, 40)                                                                   # now the app folder's file is the newer one
    assert lt.find_league_file()[1] == old


def test_a_page_saved_as_html_and_a_file_without_extension_are_read(no_files, monkeypatch):
    monkeypatch.setattr(lt, "LOCAL_JSON", no_files / "local_config.json")
    (no_files / "local_config.json").write_text(json.dumps({"league_id": 4242}))
    dl = no_files / "home" / "Downloads"; dl.mkdir(parents=True)
    payload = json.dumps(espn({"A": ["P1"], "B": ["P2"]})).replace('"', "&quot;")
    (dl / "4242").write_text(f"<html><body><pre style=\"word-wrap: break-word;\">{payload}</pre></body></html>")      # Chrome: 'Webseite, nur HTML'
    j, path = lt.find_league_file()
    assert path.name == "4242" and len(j["teams"]) == 2


def test_refresh_warns_when_the_file_is_old_and_shows_espns_trade_counter(no_files, monkeypatch, capsys):
    monkeypatch.setattr(lt, "LOCAL_JSON", no_files / "local_config.json")
    (no_files / "local_config.json").write_text(json.dumps({"league_id": 4242}))
    data = espn({"A": ["P1"], "B": ["P2"]})
    data["teams"][0]["transactionCounter"] = {"trades": 1}
    data["teams"][1]["transactionCounter"] = {"trades": 1}
    f = no_files / "espn.json"; f.write_text(json.dumps(data)); _age(f, 27)
    lt.refresh(types.SimpleNamespace(file=str(f)))
    out = capsys.readouterr().out
    assert "1 Trade(s)" in out and "ACHTUNG" in out and "27 Std." in out and "leagues/4242?view=mRoster" in out
    lt.refresh(types.SimpleNamespace(file=str(f)))                                  # second run: same file, nothing changed -> says so
    assert "Keine Veränderung" in capsys.readouterr().out
    _age(f, 0.1)
    lt.refresh(types.SimpleNamespace(file=str(f)))
    assert "ACHTUNG" not in capsys.readouterr().out                                 # a fresh file gets no warning


# ── live fetch from ESPN (mocked: no network, no real cookies) ───────────────────────────────────────
SECRET_S2, SECRET_SWID = "AEBsecretvalue123", "{11111111-2222-3333-4444-555555555555}"


def test_live_data_is_used_first_saved_locally_and_never_flagged_as_old(no_files, monkeypatch, capsys):
    stale = no_files / "espn_league.json"; stale.write_text(json.dumps(espn({"A": ["OLD1"], "B": ["OLD2"]}))); _age(stale, 30)
    monkeypatch.setattr(lt, "fetch_with_cookies", lambda creds=None: espn({"A": ["P1", "P3"], "B": ["P2"]}))
    new = lt.refresh(types.SimpleNamespace(file=None))
    out = capsys.readouterr().out
    assert new["A"] == ["P1", "P3"] and "live von ESPN" in out and "ACHTUNG" not in out
    assert json.loads(stale.read_text())["teams"][0]["roster"]["entries"][1]["playerPoolEntry"]["player"]["fullName"] == "P3"   # local copy refreshed


def test_if_espn_refuses_the_saved_file_is_used_and_the_user_is_told(no_files, monkeypatch, capsys):
    f = no_files / "espn_league.json"; f.write_text(json.dumps(espn({"A": ["P1"], "B": ["P2"]}))); _age(f, 30)
    def refuse(creds=None):
        raise lt.LeagueFetchError("ESPN lehnt die Zugangsdaten ab (HTTP 401), vermutlich abgelaufen: `./draft_app/league.sh login` erneut ausführen")
    monkeypatch.setattr(lt, "fetch_with_cookies", refuse)
    new = lt.refresh(types.SimpleNamespace(file=None))
    out = capsys.readouterr().out
    assert new["A"] == ["P1"] and "Live-Abruf bei ESPN nicht möglich" in out and "HTTP 401" in out and "ACHTUNG" in out


def test_offline_switch_and_explicit_file_skip_the_live_fetch(no_files, monkeypatch):
    f = no_files / "espn_league.json"; f.write_text(json.dumps(espn({"A": ["P1"], "B": ["P2"]})))
    monkeypatch.setattr(lt, "fetch_with_cookies", lambda creds=None: (_ for _ in ()).throw(AssertionError("must not call ESPN")))
    monkeypatch.setenv("LEAGUE_OFFLINE", "1")
    lt.refresh(types.SimpleNamespace(file=None))
    monkeypatch.delenv("LEAGUE_OFFLINE")
    lt.refresh(types.SimpleNamespace(file=str(f)))


def test_http_errors_are_explained_without_leaking_the_cookies(no_files, monkeypatch):
    monkeypatch.setattr(lt, "LOCAL_JSON", no_files / "local_config.json")
    (no_files / "local_config.json").write_text(json.dumps({"league_id": 4242}))
    def boom(req, **kw):
        raise lt.urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
    monkeypatch.setattr(lt.urllib.request, "urlopen", boom)
    with pytest.raises(lt.LeagueFetchError) as e:
        REAL_FETCH((SECRET_S2, SECRET_SWID))
    assert "401" in str(e.value) and "login" in str(e.value) and SECRET_S2 not in str(e.value) and "1111" not in str(e.value)
    monkeypatch.setattr(lt.urllib.request, "urlopen", lambda req, **kw: (_ for _ in ()).throw(OSError(f"cookie {SECRET_S2}")))
    with pytest.raises(lt.LeagueFetchError) as e2:
        REAL_FETCH((SECRET_S2, SECRET_SWID))
    assert SECRET_S2 not in str(e2.value)                                           # only the exception TYPE is shown


def test_credentials_come_from_env_or_file_and_a_league_file_is_not_a_credential_file(no_files, monkeypatch):
    monkeypatch.delenv("ESPN_S2", raising=False); monkeypatch.delenv("ESPN_SWID", raising=False)
    assert lt.credentials() is None
    (no_files / "espn_auth.json").write_text(json.dumps(espn({"A": ["P1"], "B": ["P2"]})))          # the old mistake: league data under this name
    assert lt.credentials() is None
    (no_files / "espn_auth.json").write_text(json.dumps({"espn_s2": SECRET_S2, "swid": SECRET_SWID}))
    assert lt.credentials() == (SECRET_S2, SECRET_SWID)
    monkeypatch.setenv("ESPN_S2", "fromenv"); monkeypatch.setenv("ESPN_SWID", "{x}")
    assert lt.credentials() == ("fromenv", "{x}")


def test_login_checks_the_values_stores_them_privately_and_never_prints_them(no_files, monkeypatch, capsys):
    import getpass, stat
    answers = iter([f'"{SECRET_S2}"', SECRET_SWID.strip("{}")])                      # pasted with quotes / without braces
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    seen = {}
    def accept(creds=None):
        seen["creds"] = creds
        return espn({"A": ["P1"], "B": ["P2"]})
    monkeypatch.setattr(lt, "fetch_with_cookies", accept)
    lt.cmd_login()
    out = capsys.readouterr().out
    auth = no_files / "espn_auth.json"
    assert json.loads(auth.read_text()) == {"espn_s2": SECRET_S2, "swid": SECRET_SWID} and seen["creds"] == (SECRET_S2, SECRET_SWID)
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600
    assert SECRET_S2 not in out and "11111111" not in out and "OK:" in out and (no_files / "espn_league.json").exists()


def test_login_with_rejected_values_stores_nothing(no_files, monkeypatch):
    import getpass
    answers = iter([SECRET_S2, SECRET_SWID])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    def reject(creds=None):
        raise lt.LeagueFetchError("ESPN lehnt die Zugangsdaten ab (HTTP 401), vermutlich abgelaufen")
    monkeypatch.setattr(lt, "fetch_with_cookies", reject)
    with pytest.raises(SystemExit) as e:
        lt.cmd_login()
    assert "Nicht gespeichert" in str(e.value) and not (no_files / "espn_auth.json").exists()
