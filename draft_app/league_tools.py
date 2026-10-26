"""
League tools: current rosters, rankings, trade ideas, offers and wish players
=============================================================================
    python draft_app/league_tools.py login                                    # once: set up the automatic fetch from ESPN
    python draft_app/league_tools.py refresh                                  # rosters of ALL teams as they are now + trades since last time
    python draft_app/league_tools.py rank                                     # rankings with win chances (simulated season)
    python draft_app/league_tools.py suggest                                  # trade ideas for you
    python draft_app/league_tools.py offer --team "Brick City" --asks "Jamal Murray"        # somebody asks for YOUR player(s): what to ask back
    python draft_app/league_tools.py want "Alperen Sengun"                    # you want a player: what to offer, how the stats change, a message for the manager
    python draft_app/league_tools.py want "Alperen Sengun" --give 2 --get 2   # ... as a 2-for-2 (the wish player + one more of his team); also 1-3 ranges,
                                                                              #     --keep "A;B" (never offer them), --skip "C" (do not ask for them), --top N
    python draft_app/league_tools.py all                                      # refresh + rank + suggest

Where the rosters come from (`refresh`, and automatically before rank / suggest / offer / want / all and the weekly lineup), in this order:
  1. LIVE from ESPN, if you ran `login` once: your ESPN cookies (espn_s2, SWID) are kept in draft_app/espn_auth.json (mode 600, git-ignored) or in
     the environment (ESPN_S2, ESPN_SWID). Nothing is ever printed, and nothing leaves this computer except the request to ESPN itself. The answer is also
     saved as draft_app/espn_league.json. If ESPN refuses (cookies expired) or is unreachable, the tool says so and falls back to 2.
  2. draft_app/espn_league.json (or the newest ESPN league file in ~/Downloads / ~/Desktop): the league data saved from YOUR browser:
     open  https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/2027/segments/0/leagues/<leagueId>?view=mRoster&view=mTeam
     while logged in to ESPN and save the page (Cmd+S) as draft_app/espn_league.json. The league id is in local_config.json.
     `refresh --file X` reads only that file; LEAGUE_OFFLINE=1 skips the live fetch.
Players the model has no line for (rookies, new signings) are evaluated with ESPN's own projection (espn_extra.py); players ESPN does not project either are ignored and listed.
All numbers are model estimates with the same assumptions as the rest of this project (ESPN blend, injuries, season-average weeks).
"""

import argparse
import html
import itertools
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

APP_DIR = Path(__file__).parent
sys.path.insert(0, str(APP_DIR))
import engine  # noqa: E402
import names  # noqa: E402
import weekly_lineup as wl  # noqa: E402

ROSTERS_JSON = APP_DIR / "league_rosters.json"
ROSTERS_PREV = APP_DIR / "league_rosters_prev.json"
LEAGUE_JSON = APP_DIR / "espn_league.json"
AUTH_JSON = APP_DIR / "espn_auth.json"
LOCAL_JSON = APP_DIR / "local_config.json"
ALIASES = {"Unsupervised Chaos": "Team 15"}                 # same team: ESPN renamed it after the draft
FAIR_TOL = 3                                                # a trade looks fair to the manager if he gets at least (value given away) - $3
MAX_EXACT = 1200                                            # trades evaluated exactly (lineups re-optimised); larger searches are pre-filtered
CATN = engine.CATS


# ── rosters from ESPN ────────────────────────────────────────────────────────────────────────────────
def league_id() -> str:
    try:
        return str(json.loads(LOCAL_JSON.read_text())["league_id"])
    except Exception:
        raise SystemExit('league_id fehlt: in draft_app/local_config.json {"league_id": 1234567} eintragen (steht in der ESPN-URL der Liga).')


def parse_espn_league(j: dict) -> dict:
    """{team name: [player full names]} from ESPN's league JSON (views mRoster + mTeam)."""
    out = {}
    for t in j.get("teams", []):
        name = (t.get("name") or f"{t.get('location', '')} {t.get('nickname', '')}".strip() or f"Team {t.get('id')}").strip()
        out[name] = [e["playerPoolEntry"]["player"]["fullName"] for e in (t.get("roster") or {}).get("entries", [])
                     if e.get("playerPoolEntry", {}).get("player", {}).get("fullName")]
    return out


def _ssl():
    try:
        import ssl

        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


class LeagueFetchError(Exception):
    """The live fetch from ESPN failed; the message is safe to print (it never contains the cookies)."""


def credentials():
    """(espn_s2, swid) from the environment or draft_app/espn_auth.json, else None. The values are never printed."""
    s2, swid = os.environ.get("ESPN_S2"), os.environ.get("ESPN_SWID")
    if not (s2 and swid) and AUTH_JSON.exists():
        try:
            a = json.loads(AUTH_JSON.read_text())
            s2, swid = a.get("espn_s2"), a.get("swid")
        except (json.JSONDecodeError, AttributeError):                  # e.g. a saved league page that was named espn_auth.json
            return None
    if not (s2 and swid) or "EINFUEGEN" in s2 + swid:                   # the template file was created but not filled in yet
        return None
    return s2.strip(), swid.strip()


def fetch_with_cookies(creds=None) -> dict | None:
    """The league's rosters straight from ESPN (as you, with your cookies). None if no credentials are configured."""
    creds = creds or credentials()
    if not creds:
        return None
    s2, swid = creds
    req = urllib.request.Request(league_url(), headers={"Cookie": f"espn_s2={s2}; SWID={swid}", "Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=60, context=_ssl()) as r:
            j = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:                                 # never print the request (it carries the cookies), only the status
        if e.code in (401, 403):
            raise LeagueFetchError(f"ESPN lehnt die Zugangsdaten ab (HTTP {e.code}), vermutlich abgelaufen: `./draft_app/league.sh login` erneut ausführen")
        raise LeagueFetchError(f"ESPN antwortet mit HTTP {e.code}")
    except Exception as e:
        raise LeagueFetchError(f"keine Verbindung zu ESPN ({type(e).__name__})")
    if not (isinstance(j, dict) and isinstance(j.get("teams"), list) and j["teams"]):
        raise LeagueFetchError("ESPN hat keine Teamliste geliefert (Liga-ID in local_config.json prüfen)")
    return j


def save_league_file(j: dict) -> None:
    """Keep a local copy of the live answer (atomic), so the next offline run still has the latest state."""
    tmp = LEAGUE_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(j, ensure_ascii=False))
    tmp.replace(LEAGUE_JSON)


def cmd_login(args=None):
    """One-time setup of the automatic fetch: asks for the two ESPN cookie values (hidden input), checks them against ESPN
    and stores them in draft_app/espn_auth.json (mode 600, git-ignored). They are never printed."""
    import getpass
    print("ESPN-Zugang für den automatischen Kader-Abruf. Die Werte bleiben auf diesem Rechner (draft_app/espn_auth.json, nur du darfst sie lesen, von git ignoriert).\n"
          "So findest du sie: in Chrome auf fantasy.espn.com eingeloggt sein, Rechtsklick > Untersuchen > Tab \"Application\" > Cookies > https://fantasy.espn.com,\n"
          "dort die Zeilen `espn_s2` und `SWID` suchen und jeweils den Wert kopieren (Doppelklick auf die Spalte \"Value\").\n")
    s2 = getpass.getpass("espn_s2 (Eingabe bleibt unsichtbar): ").strip().strip('"\'')
    swid = getpass.getpass("SWID (mit den geschweiften Klammern): ").strip().strip('"\'')
    if swid and not swid.startswith(("{", "%7B", "%7b")):
        swid = "{" + swid.strip("{}") + "}"
    if not s2 or not swid:
        raise SystemExit("Beide Werte werden gebraucht, nichts gespeichert.")
    try:
        j = fetch_with_cookies((s2, swid))
    except LeagueFetchError as e:
        raise SystemExit(f"Nicht gespeichert: {e}")
    fd = os.open(AUTH_JSON, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"espn_s2": s2, "swid": swid}, f)
    os.chmod(AUTH_JSON, 0o600)
    save_league_file(j)
    print(f"OK: ESPN akzeptiert die Zugangsdaten ({len(j['teams'])} Teams). Ab jetzt holen `refresh`, `rank`, `suggest`, `offer`, `want` und die Wochen-Aufstellung die Kader selbst.\n"
          "Läuft der Zugang irgendwann ab, sagt das Werkzeug es und `./draft_app/league.sh login` erneuert ihn.")


def _read_league(path: Path) -> dict | None:
    """The parsed file if it is ESPN's league JSON (has a 'teams' list), else None. Also accepts a page saved as HTML
    ('Webseite, nur HTML'), where the browser wraps the JSON in <pre>. Never prints anything from the file."""
    try:
        text = path.read_text(errors="ignore")
        try:
            j = json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"<pre[^>]*>(.*?)</pre>", text, re.S | re.I)
            j = json.loads(html.unescape(m.group(1))) if m else None
    except Exception:
        return None
    return j if isinstance(j, dict) and isinstance(j.get("teams"), list) and j["teams"] else None


def find_league_file(explicit=None):
    """(parsed json, path) of the saved league data. Tolerates the usual mistakes: wrong file name (e.g. saved as
    espn_auth.json), no extension, or still sitting in ~/Downloads or ~/Desktop. If several files qualify, the NEWEST wins
    (so a fresh download is never shadowed by an older espn_league.json)."""
    if explicit:
        c = Path(explicit)
        j = _read_league(c) if c.exists() else None
        return (j, c) if j is not None else (None, None)
    cands = [LEAGUE_JSON, AUTH_JSON]
    lid = league_id_or_none()
    for d in (Path.home() / "Downloads", Path.home() / "Desktop"):
        try:
            cands += [p for p in d.iterdir() if p.is_file() and p.suffix.lower() in ("", ".json", ".txt", ".html")
                      and ("espn" in p.name.lower() or lid in p.name) and p.stat().st_size < 30_000_000
                      and time.time() - p.stat().st_mtime < 30 * 86400]
        except OSError:
            pass
    found = []
    for c in cands:
        if c.exists() and (j := _read_league(c)) is not None:
            found.append((c.stat().st_mtime, c, j))
    if not found:
        return None, None
    _, c, j = max(found, key=lambda f: f[0])
    return j, c


def league_url() -> str:
    lid = league_id_or_none()
    lid = lid if lid != "\0" else "<Liga-ID>"
    return f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/2027/segments/0/leagues/{lid}?view=mRoster&view=mTeam"


def fmt_age(seconds: float) -> str:
    h = seconds / 3600
    return f"{h * 60:.0f} Min." if h < 1 else f"{h:.0f} Std." if h < 48 else f"{h / 24:.0f} Tagen"


def league_id_or_none() -> str:
    try:
        return str(json.loads(LOCAL_JSON.read_text())["league_id"])
    except Exception:
        return "\0"


def refresh(args=None, brief: bool = False) -> dict:
    """Rosters of all teams. Order: live from ESPN (if credentials are set up, see `login`), else the saved league file.
    brief=True (used before rank/suggest/offer/want/weekly): one status line, plus changes and warnings only."""
    explicit = getattr(args, "file", None)
    brief = brief or bool(getattr(args, "brief", False))
    j, path, saved_at, src = None, None, None, None
    if not explicit and not os.environ.get("LEAGUE_OFFLINE"):
        try:
            j = fetch_with_cookies()
        except LeagueFetchError as e:
            print(f"(Live-Abruf bei ESPN nicht möglich: {e}. Ich nehme die gespeicherte Datei.)")
        if j is not None:
            save_league_file(j)
            saved_at, src = time.time(), "live von ESPN abgerufen"
    if j is None:
        j, path = find_league_file(explicit)
        if j is None and explicit:
            if not Path(explicit).exists():
                raise SystemExit(f"Datei {explicit} existiert nicht (Beispiel: python league_tools.py refresh --file espn_league.json).")
            try:
                json.loads(Path(explicit).read_text())
            except json.JSONDecodeError:
                raise SystemExit(f"{Path(explicit).name} ist kein JSON (als 'Webseite, nur HTML' speichern oder den Text der Seite kopieren und als .json ablegen).")
            raise SystemExit(f"{Path(explicit).name} enthält keine ESPN-Teamliste (falsche Seite gespeichert? Die URL braucht view=mRoster und view=mTeam).")
        if j is not None:
            saved_at = path.stat().st_mtime
            src = f"gespeicherte Datei {path.name} (gespeichert {time.strftime('%d.%m. %H:%M', time.localtime(saved_at))}, vor {fmt_age(time.time() - saved_at)})"
            if path != LEAGUE_JSON and not explicit:
                print(f"Hinweis: die Liga-Daten lagen in {path} - am besten umbenennen in draft_app/espn_league.json.")
    if j is None:
        raise SystemExit("Keine Roster-Quelle gefunden. Einmalig einrichten: `./draft_app/league.sh login` (dann holt das Werkzeug die Kader selbst),\n"
                         "oder die ESPN-Liga-Daten aus dem Browser als draft_app/espn_league.json speichern (Anleitung ganz oben in league_tools.py).")
    new = parse_espn_league(j)
    if len(new) < 2:
        raise SystemExit("Die Datei enthält keine Teams (falsche Seite gespeichert?).")
    old = json.loads(ROSTERS_JSON.read_text()) if ROSTERS_JSON.exists() else {}
    if old:
        ROSTERS_PREV.write_text(json.dumps(old, indent=1, ensure_ascii=False))
    # keep the previous key of a renamed team so older tools keep working
    for k_new in list(new):
        if ALIASES.get(k_new) in old and k_new not in old:
            new[ALIASES[k_new]] = new.pop(k_new)
    ROSTERS_JSON.write_text(json.dumps(new, indent=1, ensure_ascii=False))
    live = src.startswith("live")
    print(f"Kader: {src} · {len(new)} Teams, {sum(len(v) for v in new.values())} Spieler" + ("" if brief else " -> league_rosters.json"))
    counted = {(t.get("name") or t.get("nickname") or f"Team {t.get('id')}").strip(): int((t.get("transactionCounter") or {}).get("trades") or 0)
               for t in j.get("teams", [])}
    if any(counted.values()) and not brief:                     # ESPN counts every trade a team has made: a check that a trade is in the data
        print(f"ESPN-Zähler: {sum(counted.values()) // 2} Trade(s) in der Liga ({', '.join(f'{t} {n}' for t, n in counted.items() if n)})")
    stale = (not live) and saved_at is not None and time.time() - saved_at > 3 * 3600
    if stale:
        print(f"ACHTUNG: Die gespeicherte Datei ist {fmt_age(time.time() - saved_at)} alt; ein Trade danach wäre nicht enthalten. Einmal `./draft_app/league.sh login` einrichten, dann holt das Werkzeug die Kader selbst.\n"
              f"  (Von Hand: diese Adresse im eingeloggten Chrome öffnen und mit Cmd+S als draft_app/espn_league.json speichern: {league_url()})")
    if old:
        where_old = {names.name_key(p): t for t, ps in old.items() for p in ps}
        where_new = {names.name_key(p): (t, p) for t, ps in new.items() for p in ps}
        moves = [(p, where_old[k], t) for k, (t, p) in where_new.items() if k in where_old and where_old[k] != t]
        adds = [(p, t) for k, (t, p) in where_new.items() if k not in where_old]
        drops = [(p, where_old[k]) for k in where_old if k not in where_new for p in [next((x for ps in old.values() for x in ps if names.name_key(x) == k), k)]]
        if not (moves or adds or drops):
            if not brief or stale:
                print("Keine Veränderung gegenüber dem letzten Stand." + (" Wenn du einen neuen Trade erwartest: die Datei ist vermutlich älter als der Trade." if stale else ""))
        else:
            print(f"\nTrades / Wechsel seit dem letzten Stand: {len(moves)}")
            for p, a, b in sorted(moves, key=lambda m: (m[1], m[2])):
                print(f"  {p}: {a} -> {b}")
            if adds or drops:
                print(f"Neu verpflichtet: {len(adds)}  ·  entlassen: {len(drops)}")
                for p, t in adds[:12]:
                    print(f"  + {p} ({t})")
                for p, t in drops[:12]:
                    print(f"  - {p} ({t})")
            print()
    return new


# ── the league model ─────────────────────────────────────────────────────────────────────────────────
class League:
    def __init__(self, season=True, min_games=1, shrink=0.0, refresh=False):
        if not ROSTERS_JSON.exists():
            wl.init_rosters()
        raw = json.loads(ROSTERS_JSON.read_text())
        # rookies/new signings: ESPN's projection; every player with games this season: this season's per-game averages
        self.pool, self.meta = wl.load_pool_and_meta([n for ns in raw.values() for n in ns], season=season, min_games=min_games, shrink=shrink, refresh=refresh)
        self.espn_filled, self.espn_missing = self.pool.attrs.get("espn_filled", []), self.pool.attrs.get("espn_missing", [])
        self.rules = engine.Rules.from_meta(self.meta)
        self.model = engine.build_pool_model(self.pool, self.rules, self.meta["league_rates"])
        self.keys = list(raw)
        self.you = self.find_team(self.meta["your_team_name"])
        self.rosters, self.unknown = {}, {}
        for t, ns in raw.items():
            ok, miss = wl.resolve_roster(self.pool, ns)
            self.rosters[t] = [i for i in ok if bool(self.pool.MODEL_OK.iat[i])]
            self.unknown[t] = miss + [self.model.names[i] for i in ok if not bool(self.pool.MODEL_OK.iat[i])]
        try:
            log = json.loads((APP_DIR / "draft_log.json").read_text())
            pid2i = {int(p): i for i, p in enumerate(self.pool.PLAYER_ID)}
            self.paid = {pid2i[int(e["player_id"])]: e["price"] for e in log if int(e["player_id"]) in pid2i}
        except Exception:
            self.paid = {}
        self.T = list(self.rosters)
        self.n = len(self.T)
        z = self.z = ((self.model.E - self.model.E.mean(0)) / np.maximum(self.model.E.std(0), 1e-9)).sum(1)
        self.lineups = {t: sorted(r, key=lambda i: -z[i])[:10] for t, r in self.rosters.items()}
        for _ in range(3):
            self.fld = engine.Field.from_rosters(self.model, list(self.lineups.values()))
            self.lineups = {t: self.opt_lineup(r, self.lineups[t]) for t, r in self.rosters.items()}
        self.fld = engine.Field.from_rosters(self.model, list(self.lineups.values()))
        self.mom = {t: self.model.team_moments(l) for t, l in self.lineups.items()}
        self.base = {t: self.win_pct(t, self.mom[t]) for t in self.T}
        self.base_cat = {t: self.cat_probs(t, self.mom[t]) for t in self.T}

    # -- helpers
    def find_team(self, name: str) -> str:
        cand = [name, ALIASES.get(name, ""), *[k for k, v in ALIASES.items() if v == name]]
        for c in cand:
            if c in self.keys:
                return c
        low = {names.name_key(k): k for k in self.keys}
        hit = low.get(names.name_key(name))
        if hit:
            return hit
        raise SystemExit(f"Team '{name}' nicht gefunden. Teams: " + ", ".join(self.keys))

    def find_player(self, text: str) -> int:
        hit = names.resolve_player(names.build_index(self.pool.PLAYER_ID, self.pool.PLAYER_NAME), text)
        if hit is None:
            raise SystemExit(f"Spieler '{text}' nicht im Pool (Schreibweise prüfen; Spieler ohne ESPN-Projektion können nicht bewertet werden).")
        return {int(p): i for i, p in enumerate(self.pool.PLAYER_ID)}[hit[0]]

    def owner(self, i: int):
        return next((t for t, r in self.rosters.items() if i in r), None)

    def pw(self, lineup) -> float:
        return engine.evaluate(self.model, lineup, self.fld)["p_win"]

    def opt_lineup(self, roster, start):
        m = self.model
        lu = [p for p in start if p in roster]
        bench = [p for p in roster if p not in lu]
        while len(lu) < 10 and bench:
            best = None
            for b in bench:
                t = lu + [b]
                if len(t) == 10 and not engine.lineup_legal(m, t):
                    continue
                v = self.pw(t)
                if best is None or v > best[0]:
                    best = (v, b)
            if best is None:
                break
            lu.append(best[1]); bench.remove(best[1])
        for _ in range(4):
            base, imp = self.pw(lu), None
            for s in lu:
                for b in bench:
                    t = [b if x == s else x for x in lu]
                    if engine.lineup_legal(m, t):
                        v = self.pw(t)
                        if v > base + 1e-6 and (imp is None or v > imp[0]):
                            imp = (v, s, b)
            if imp is None:
                break
            _, s, b = imp
            lu = [b if x == s else x for x in lu]
            bench = [x for x in bench if x != b] + [s]
        return lu

    def cat_probs(self, t, mom, others=None):
        acc, k = np.zeros(10), 0
        for u in self.T:
            if u == t:
                continue
            om = (others or {}).get(u, self.mom[u])
            acc += engine.phi((mom[0] - om[0]) / np.sqrt(mom[1] ** 2 + om[1] ** 2)); k += 1
        return acc / k

    def win_pct(self, t, mom, others=None):
        """Mean P(matchup) of team t (moments `mom`) against every other team; `others` overrides single opponents' moments."""
        oth = [(others or {}).get(u, self.mom[u]) for u in self.T if u != t]
        om, osd = np.array([o[0] for o in oth]), np.array([o[1] for o in oth])
        pc = engine.phi((mom[0] - om) / np.sqrt(mom[1] ** 2 + osd ** 2))               # (opponents, categories)
        return float(np.mean(engine.matchup_prob(pc, self.rules)))

    def value(self, i: int) -> float:
        """What managers see: ESPN's pre-draft value and what was paid in the draft (mean of both when known)."""
        e = float(self.pool["ESPN_$_LIST"].iat[i]) if pd.notna(self.pool["ESPN_$_LIST"].iat[i]) else 0.0
        return 0.5 * (e + self.paid[i]) if i in self.paid else e

    # -- trade evaluation: me gives `give`, partner gives `get` (lists of pool indices; unequal sizes: the receiver of more drops his weakest bench)
    def trade(self, me, partner, give, get):
        m = self.model
        def new_roster(t, out, inc):
            r = [p for p in self.rosters[t] if p not in out] + list(inc)
            while len(r) > len(self.rosters[t]):
                lu = self.opt_lineup(r, self.lineups[t])
                bench = [p for p in r if p not in lu and p not in inc] or [p for p in r if p not in lu] or r
                r.remove(min(bench, key=lambda p: m.E[p].sum()))
            return r
        rm, rp = new_roster(me, give, get), new_roster(partner, get, give)
        lm, lp = self.opt_lineup(rm, self.lineups[me]), self.opt_lineup(rp, self.lineups[partner])
        mm, mp = m.team_moments(lm), m.team_moments(lp)
        over_me = {partner: mp}
        over_p = {me: mm}
        res = dict(d_me=self.win_pct(me, mm, over_me) - self.base[me], d_x=self.win_pct(partner, mp, over_p) - self.base[partner],
                   cat_me=self.cat_probs(me, mm, over_me) - self.base_cat[me], cat_x=self.cat_probs(partner, mp, over_p) - self.base_cat[partner],
                   mom_me=mm, mom_x=mp, lineup_me=lm, lineup_x=lp,
                   v_give=sum(self.value(p) for p in give), v_get=sum(self.value(p) for p in get))
        return res

    # -- cheap approximation of a trade (no re-optimised lineups, no legality check): only used to pre-filter big searches
    def quick_lineup(self, t, out, inc):
        z = self.z
        lu = [p for p in self.lineups[t] if p not in out]
        rest = sorted([p for p in self.rosters[t] if p not in out and p not in lu] + list(inc), key=lambda p: -z[p])
        while len(lu) < 10 and rest:
            lu.append(rest.pop(0))
        for p in [q for q in inc if q not in lu]:                 # an incoming player better than the weakest starter takes his place
            w = min((q for q in lu if q not in inc), key=lambda q: z[q], default=None)
            if w is not None and z[p] > z[w]:
                lu[lu.index(w)] = p
        return lu

    def quick_trade(self, me, partner, give, get):
        mm = self.model.team_moments(self.quick_lineup(me, give, get))
        mp = self.model.team_moments(self.quick_lineup(partner, get, give))
        return (self.win_pct(me, mm, {partner: mp}) - self.base[me], self.win_pct(partner, mp, {me: mm}) - self.base[partner])

    # -- standings
    def standings(self, sims=3000, seed=7):
        P = np.zeros((self.n, self.n))
        for a, ta in enumerate(self.T):
            for b, tb in enumerate(self.T):
                if a != b:
                    pc = engine.phi((self.mom[ta][0] - self.mom[tb][0]) / np.sqrt(self.mom[ta][1] ** 2 + self.mom[tb][1] ** 2))
                    P[a, b] = float(engine.matchup_prob(pc, self.rules))
        weeks = int(self.meta.get("regular_season_matchups", 18)); top = int(self.meta.get("playoff_teams", 4))
        rng = np.random.default_rng(seed)
        W = np.zeros((sims, self.n))
        for s in range(sims):
            for _ in range(weeks):
                perm = rng.permutation(self.n)
                for k in range(0, self.n - 1, 2):
                    a, b = perm[k], perm[k + 1]
                    W[s, a if rng.random() < P[a, b] else b] += 1
        rank = (-W + rng.random(W.shape) * 1e-3).argsort(1).argsort(1) + 1
        rows = [dict(team=t, win=self.base[t], exp_w=weeks * self.base[t], cats=float(self.base_cat[t].sum()), top=float((rank[:, a] <= top).mean()),
                     first=float((rank[:, a] == 1).mean())) for a, t in enumerate(self.T)]
        df = pd.DataFrame(rows).sort_values("win", ascending=False).reset_index(drop=True)
        df.index += 1
        return df, P, top, weeks


# ── explanations ─────────────────────────────────────────────────────────────────────────────────────
STAT_COLS = [("PTS", "PRED_PTS_PG"), ("REB", "PRED_REB_PG"), ("AST", "PRED_AST_PG"), ("STL", "PRED_STL_PG"), ("BLK", "PRED_BLK_PG"),
             ("3PM", "PRED_FG3M_PG"), ("DD", "PRED_DD_PG"), ("FG%", "PRED_FG_PCT"), ("FT%", "PRED_FT_PCT")]


def stat_line(L: League, i: int) -> str:
    p = L.pool
    parts = []
    for lab, col in STAT_COLS:
        v = p[col].iat[i]
        parts.append(f"{lab} {v:.3f}".replace("0.", ".") if lab.endswith("%") else f"{lab} {v:.1f}" if lab != "DD" else f"DD {v:.2f}")
    seen = f"; Saison-Schnitt aus {int(p.SEASON_GP.iat[i])} Sp." if "SEASON_GP" in p and p.SEASON_SRC.iat[i] != "Projektion" else ""
    return f"{L.model.names[i]} ({p.POS_ELIGIBLE_STR.iat[i]}, {p.CURRENT_TEAM_ABBR.iat[i]}; {p.EXP_GP.iat[i]:.0f} Spiele erw.{seen}): " + ", ".join(parts)


def cat_delta_text(d, k=3) -> str:
    pairs = sorted([(engine.LABEL[c], 100 * d[j]) for j, c in enumerate(CATN)], key=lambda z: -abs(z[1]))[:k]
    return ", ".join(f"{lab} {v:+.0f}" for lab, v in pairs if abs(v) >= 0.5) or "kaum Veränderung"


def reason(L: League, partner, give, get, r) -> str:
    gs, ts = ", ".join(L.model.names[i] for i in give), ", ".join(L.model.names[i] for i in get)
    lines = [f"  Du gibst {gs}, du bekommst {ts}",
             f"    {stat_line(L, give[0])}" if len(give) == 1 else "",
             f"    {stat_line(L, get[0])}" if len(get) == 1 else "",
             f"    Deine Siegchance pro Woche {100 * r['d_me']:+.1f} Pp (Kategorien in Pp: {cat_delta_text(r['cat_me'])}); {partner} {100 * r['d_x']:+.1f} Pp ({cat_delta_text(r['cat_x'])})",
             f"    Wert aus Sicht des Managers (ESPN-Liste/Draft-Preis): er bekommt ${r['v_give']:.0f}, gibt ${r['v_get']:.0f}"
             + ("  -> wirkt fair oder besser für ihn" if r["v_give"] >= r["v_get"] - 3 else "  -> er verliert Wert, braucht ein Argument")]
    return "\n".join(x for x in lines if x)


# ── commands ─────────────────────────────────────────────────────────────────────────────────────────
def cmd_rank(L: League, args=None):
    df, P, top, weeks = L.standings()
    print(f"\nRangliste (Saison {weeks} Wochen, Top {top} in die Playoffs; 3000 simulierte Saisons)")
    print(f"{'#':>2} {'Team':<28}{'Siegchance/Woche':>17}{'erw. Siege':>11}{'Kat./Woche':>11}{'Top '+str(top):>8}{'Platz 1':>9}")
    for r, x in df.iterrows():
        mark = "  <- du" if x.team == L.you else ""
        print(f"{r:>2} {x.team:<28}{100 * x['win']:>16.0f}%{x['exp_w']:>11.1f}{x['cats']:>11.1f}{100 * x['top']:>7.0f}%{100 * x['first']:>8.0f}%{mark}")
    me = df[df.team == L.you].iloc[0]
    iy = L.T.index(L.you)
    rivals = sorted([(1 - P[iy, b], L.T[b]) for b in range(L.n) if b != iy], reverse=True)[:3]
    print("\nGefährlichste Gegner für dich (Chance, dass sie dich in einer Woche schlagen): " + ", ".join(f"{t} {100 * p:.0f} %" for p, t in rivals))
    print("Deine Kategorien gegen den Durchschnitt: " + ", ".join(f"{engine.LABEL[c]} {100 * L.base_cat[L.you][j]:.0f}%" for j, c in enumerate(CATN)))
    if L.espn_filled:
        print(f"  (ESPN-Projektion statt Modell, {len(L.espn_filled)} Spieler ohne NBA-Daten: {', '.join(L.espn_filled)})")
    for t, u in L.unknown.items():
        if u:
            print(f"  (nicht bewertbar, ignoriert: {t}: {', '.join(u[:6])}{' ...' if len(u) > 6 else ''})")
    return df


def cmd_suggest(L: League, args=None):
    me = L.you
    rows = []
    for x in L.T:
        if x == me:
            continue
        for a in L.rosters[me]:
            for b in L.rosters[x]:
                r = L.trade(me, x, [a], [b])
                rows.append((x, a, b, r))
    best = sorted([z for z in rows if z[3]["d_me"] > 0.001], key=lambda z: -z[3]["d_me"])
    win_win = [z for z in best if z[3]["d_x"] >= -0.001][:6]
    fair = [z for z in best if z[3]["v_give"] >= z[3]["v_get"] - 3 and z not in win_win][:6]
    for title, lst in (("Win-Win (auch der Partner wird laut Modell nicht schlechter)", win_win), ("Wirken für den Manager fair (er bekommt mindestens fast so viel Wert)", fair)):
        print(f"\n== Trade-Ideen: {title} ==")
        for x, a, b, r in lst:
            print(reason(L, x, [a], [b], r)); print(f"    Partner: {x}\n")
    if not (win_win or fair):
        print("Keine Ein-gegen-Eins-Trades gefunden, die dein Team verbessern.")


def cmd_offer(L: League, args):
    me, x = L.you, L.find_team(args.team)
    asked = [L.find_player(p.strip()) for p in args.asks.split(";") if p.strip()]
    for a in asked:
        if a not in L.rosters[me]:
            raise SystemExit(f"{L.model.names[a]} gehört nicht zu deinem Kader ({me}).")
    base_loss = L.trade(me, x, asked, [])                     # what it costs to just hand them over
    print(f"\n{x} fragt nach {', '.join(L.model.names[a] for a in asked)}.")
    for a in asked:
        print("  " + stat_line(L, a))
    print(f"Ohne Gegenleistung: deine Siegchance {100 * base_loss['d_me']:+.1f} Pp, {x} {100 * base_loss['d_x']:+.1f} Pp.")
    cands = []
    pool = L.rosters[x]
    for k in (1, 2):
        for get in itertools.combinations(pool, k):
            r = L.trade(me, x, asked, list(get))
            cands.append((get, r))
    ok = [c for c in cands if c[1]["v_give"] >= c[1]["v_get"] - 3]
    print("\n== Was du zurückverlangen kannst (nach deinem Gewinn sortiert; nur, was dem Manager fair vorkommt) ==")
    for get, r in sorted(ok, key=lambda c: -c[1]["d_me"])[:6]:
        print(reason(L, x, asked, list(get), r)); print()
    print("== Beste Antworten nach Modell, auch wenn der Manager Wert abgibt ==")
    for get, r in sorted(cands, key=lambda c: -c[1]["d_me"])[:3]:
        print(reason(L, x, asked, list(get), r)); print()


def parse_count(text, default="1-2", lo=1, hi=3) -> list:
    """'2' -> [2], '1-3' -> [1, 2, 3] (number of players on one side of a trade)."""
    t = str(default if text in (None, "") else text).strip()
    m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", t)
    if not m:
        raise SystemExit(f"Zahl oder Bereich erwartet (z. B. 2 oder 1-3), nicht '{text}'.")
    a, b = sorted((int(m.group(1)), int(m.group(2) or m.group(1))))
    if a < lo or b > hi:
        raise SystemExit(f"Erlaubt sind {lo} bis {hi} Spieler pro Seite.")
    return list(range(a, b + 1))


def fair_value(r) -> bool:
    return r["v_give"] >= r["v_get"] - FAIR_TOL


# (title, accepts the trade, sort key)  - several answers to "what could I offer", because "best" depends on what you want
WANT_OPTIONS = [
    ("Beide profitieren (sortiert nach dem kleineren der beiden Gewinne)",
     lambda r: r["d_me"] > 0.0005 and r["d_x"] > 0.0005, lambda r: min(r["d_me"], r["d_x"])),
    ("Grösster Gewinn für dich (die Siegchance des Managers sinkt um höchstens 1 Pp)",
     lambda r: r["d_me"] > 0.0005 and r["d_x"] >= -0.01, lambda r: r["d_me"]),
    ("Wirkt für den Manager fair (Wert laut ESPN-Liste/Draft-Preis, Siegchance sinkt um höchstens 0.5 Pp)",
     lambda r: r["d_me"] > 0.0005 and r["d_x"] >= -0.005 and fair_value(r), lambda r: r["d_me"]),
]


def drop_redundant(results) -> list:
    """Remove trades that only add pieces without changing anything: if a smaller trade (subset on both sides, wish players
    are on both sides anyway) has the same effect for both teams, the bigger one just gives away or asks for more for nothing."""
    bins: dict = {}
    for item in results:
        bins.setdefault((round(item[2]["d_me"], 3), round(item[2]["d_x"], 3)), []).append(item)
    keep = []
    for items in bins.values():
        sets = [(set(g), set(t), (g, t, r)) for g, t, r in items]
        for g, t, orig in sets:
            if not any(g2 <= g and t2 <= t and (g2, t2) != (g, t) for g2, t2, _ in sets):
                keep.append(orig)
    return keep


def pick_options(results, top=2) -> list:
    """results: [(give, get, r)] -> [(title, [(give, get, r)])]; each trade appears under the first option that takes it."""
    seen, out = set(), []
    for title, ok, key in WANT_OPTIONS:
        chosen = []
        for give, get, r in sorted((x for x in results if ok(x[2])), key=lambda x: -key(x[2])):
            k = (tuple(sorted(give)), tuple(sorted(get)))
            if k in seen:
                continue
            seen.add(k); chosen.append((give, get, r))
            if len(chosen) >= top:
                break
        out.append((title, chosen))
    return out


def side_text(L: League, ids, indent="      ") -> str:
    return "\n".join(f"{indent}{stat_line(L, i)}" for i in ids)


def describe(L: League, partner, give, get, r) -> str:
    me = L.you
    bm, bx = L.base[me], L.base[partner]
    return "\n".join([
        f"    Du gibst ({len(give)}):\n{side_text(L, give)}",
        f"    Du bekommst ({len(get)}):\n{side_text(L, get)}",
        f"    Du:  Siegchance pro Woche {100 * bm:.1f}% -> {100 * (bm + r['d_me']):.1f}% ({100 * r['d_me']:+.1f} Pp) · Kategorien in Pp: {cat_delta_text(r['cat_me'], 4)}",
        f"    {partner[:22]}: {100 * bx:.1f}% -> {100 * (bx + r['d_x']):.1f}% ({100 * r['d_x']:+.1f} Pp) · Kategorien in Pp: {cat_delta_text(r['cat_x'], 4)}",
        f"    Wert aus Sicht des Managers (ESPN-Liste/Draft-Preis): er bekommt ${r['v_give']:.0f}, gibt ${r['v_get']:.0f}"
        + ("  -> wirkt fair oder besser für ihn" if fair_value(r) else "  -> er verliert Wert, braucht ein Argument")])


def want_candidates(L: League, x, wishes, give_ns, get_ns, keep, skip):
    """All trades 'I give n of my players, I get the wish players + (m - wishes) more of the partner's' as (give, get)."""
    mine = [p for p in L.rosters[L.you] if p not in keep]
    theirs = [p for p in L.rosters[x] if p not in wishes and p not in skip]
    out = []
    for n in give_ns:
        for give in itertools.combinations(mine, n):
            for m in get_ns:
                for extra in itertools.combinations(theirs, m - len(wishes)):
                    out.append((list(give), list(wishes) + list(extra)))
    return out


def cmd_want(L: League, args):
    me = L.you
    wishes = [L.find_player(p.strip()) for p in args.player.split(";") if p.strip()]
    owners = {L.owner(w) for w in wishes}
    if None in owners:
        raise SystemExit("Mindestens ein Wunschspieler gehört keinem Team (frei).")
    if len(owners) > 1:
        raise SystemExit("Die Wunschspieler gehören verschiedenen Teams: ein Trade geht nur mit einem Partner.")
    x = owners.pop()
    if x == me:
        raise SystemExit("Der Spieler gehört schon dir.")
    keep = {L.find_player(p.strip()) for p in (args.keep or "").split(";") if p.strip()}
    skip = {L.find_player(p.strip()) for p in (args.skip or "").split(";") if p.strip()}
    if not keep <= set(L.rosters[me]):
        raise SystemExit("--keep: nur Spieler aus deinem Kader.")
    if not skip <= set(L.rosters[x]):
        raise SystemExit(f"--skip: nur Spieler aus dem Kader von {x}.")
    give_ns = parse_count(args.give, "1-2")
    get_ns = [m for m in parse_count(args.get, str(len(wishes))) if m >= len(wishes)]
    if not get_ns:
        raise SystemExit(f"--get muss mindestens die {len(wishes)} Wunschspieler enthalten.")

    print(f"\nWunschspieler von {x}:")
    for w in wishes:
        print("  " + stat_line(L, w))
    cands = want_candidates(L, x, wishes, give_ns, get_ns, keep, skip)
    print(f"Suche: du gibst {'-'.join(map(str, [give_ns[0], give_ns[-1]])) if len(give_ns) > 1 else give_ns[0]}, "
          f"du bekommst {'-'.join(map(str, [get_ns[0], get_ns[-1]])) if len(get_ns) > 1 else get_ns[0]} Spieler (Wunschspieler eingerechnet) · {len(cands)} mögliche Trades"
          + (f" · behalten: {', '.join(L.model.names[i] for i in keep)}" if keep else "")
          + (f" · nicht verlangt: {', '.join(L.model.names[i] for i in skip)}" if skip else ""))
    if not cands:
        raise SystemExit("Keine möglichen Trades mit diesen Einschränkungen.")
    if len(cands) > MAX_EXACT:                                        # cheap approximation first, exact evaluation for the promising ones
        quick = [(c, *L.quick_trade(me, x, c[0], c[1])) for c in cands]
        pick = {}
        fair_q = lambda c: sum(L.value(p) for p in c[0]) >= sum(L.value(p) for p in c[1]) - FAIR_TOL
        for rank_key, ok in ((lambda q: min(q[1], q[2]), lambda q: True),
                             (lambda q: q[1], lambda q: q[2] >= -0.015),
                             (lambda q: q[1], lambda q: q[2] >= -0.01 and fair_q(q[0]))):
            for q in sorted((q for q in quick if ok(q)), key=lambda q: -rank_key(q))[:MAX_EXACT // 3]:
                pick[(tuple(q[0][0]), tuple(q[0][1]))] = q[0]
        print(f"  (grosse Suche: {len(cands)} Trades grob vorgefiltert, {len(pick)} davon genau durchgerechnet)")
        cands = list(pick.values())
    results = [(give, get, L.trade(me, x, give, get)) for give, get in cands]

    shown = []
    for title, chosen in pick_options(drop_redundant(results), top=args.top):
        print(f"\n== {title} ==")
        if not chosen:
            print("  (keine Option erfüllt das)")
        for n, (give, get, r) in enumerate(chosen, 1):
            print(f"  [{len(shown) + 1}] {' + '.join(L.model.names[i] for i in give)}  gegen  {' + '.join(L.model.names[i] for i in get)}")
            print(describe(L, x, give, get, r)); print()
            shown.append((give, get, r))
    if not shown:
        print("\nKein Trade mit diesen Einschränkungen verbessert dein Team. Mehr Spieler zulassen (--give 1-3 --get 1-2) oder weniger behalten (--keep).")
        return
    give, get, r = shown[0]
    gs, ts = " + ".join(L.model.names[i] for i in give), " + ".join(L.model.names[i] for i in get)
    print("== Nachricht an den Manager (Vorschlag zu Option [1]) ==")
    print(f"  Hallo, ich würde dir {gs} für {ts} anbieten.")
    print(f"  Für dich: {gs} bringt dir in den Kategorien {cat_delta_text(r['cat_x'], 3)} (Veränderung deiner Wochen-Gewinnchance je Kategorie in Prozentpunkten),")
    print(f"  insgesamt {100 * r['d_x']:+.1f} Prozentpunkte Siegchance pro Woche nach meiner Rechnung.")
    print("  Ist das etwas für dich?")
    print("\n== So ändern sich eure Werte bei Option [1] (erwartete Wochensummen) ==")
    print(f"  {'':8}{'du jetzt':>12}{'du danach':>12}{x[:14]:>16}{'danach':>10}")
    for j, c in enumerate(CATN):
        a0, a1, b0, b1 = L.mom[me][0][j], r["mom_me"][0][j], L.mom[x][0][j], r["mom_x"][0][j]
        f = "{:>12.3f}{:>12.3f}{:>16.3f}{:>10.3f}" if c in ("FG_PCT", "FT_PCT", "PTS_MIN") else "{:>12.1f}{:>12.1f}{:>16.1f}{:>10.1f}"
        print(f"  {engine.LABEL[c]:<8}" + f.format(a0, a1, b0, b1))
    print(f"  Siegchance/Woche: du {100 * L.base[me]:.1f}% -> {100 * (L.base[me] + r['d_me']):.1f}%   ·   {x} {100 * L.base[x]:.1f}% -> {100 * (L.base[x] + r['d_x']):.1f}%")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--projection", action="store_true", help="Per-Spiel-Werte aus der Projektion vor der Saison statt aus den Schnitten der laufenden Saison")
    common.add_argument("--min-games", type=int, default=1, help="Saison-Schnitte erst ab so vielen Spielen verwenden (Standard 1)")
    common.add_argument("--shrink", type=float, default=0.0, help="Schnitt und Projektion nach Stichprobe mischen: Gewicht = Spiele/(Spiele+K); 0 = reiner Schnitt (Standard), 10 = vorsichtig")
    r = sub.add_parser("refresh"); r.add_argument("--file", help="nur diese gespeicherte Datei lesen (kein Live-Abruf)")
    r.add_argument("--brief", action="store_true", help="knappe Ausgabe: eine Zeile, dazu Veränderungen und Warnungen")
    sub.add_parser("login", help="einmalig: ESPN-Zugang für den automatischen Kader-Abruf einrichten")
    sub.add_parser("rank", parents=[common]); sub.add_parser("suggest", parents=[common]); sub.add_parser("all", parents=[common])
    o = sub.add_parser("offer", parents=[common]); o.add_argument("--team", required=True); o.add_argument("--asks", required=True, help='deine Spieler, nach denen gefragt wird, mit ; getrennt')
    w = sub.add_parser("want", parents=[common]); w.add_argument("player", help='Wunschspieler, mehrere vom selben Team mit ; getrennt')
    w.add_argument("--give", help="wie viele deiner Spieler im Trade (Zahl oder Bereich, 1-3; Standard 1-2)")
    w.add_argument("--get", help="wie viele Spieler du bekommst, Wunschspieler eingerechnet (Standard: nur die Wunschspieler; bis 3)")
    w.add_argument("--keep", help="deine Spieler, die nicht angeboten werden, mit ; getrennt")
    w.add_argument("--skip", help="Spieler des Partners, die nicht mitverlangt werden, mit ; getrennt")
    w.add_argument("--top", type=int, default=2, help="Optionen je Kategorie (Standard 2)")
    args = ap.parse_args()
    if args.cmd == "login":
        cmd_login(args); return 0
    if args.cmd == "refresh":
        refresh(args); return 0
    if args.cmd in ("all", "rank", "suggest", "offer", "want"):          # every analysis starts from the current rosters
        try:
            refresh(args, brief=args.cmd != "all")
        except SystemExit as e:
            print(f"(Kader nicht aktualisiert: {e}) rechne mit dem letzten Stand in league_rosters.json")
    L = League(season=not args.projection, min_games=args.min_games, shrink=args.shrink)
    print(wl.season_text(L.pool))
    if args.cmd in ("rank", "all"):
        cmd_rank(L)
    if args.cmd in ("suggest", "all"):
        cmd_suggest(L)
    if args.cmd == "offer":
        cmd_offer(L, args)
    if args.cmd == "want":
        cmd_want(L, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
