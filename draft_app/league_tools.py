"""
League tools: current rosters, rankings, trade ideas, offers and wish players
=============================================================================
    python draft_app/league_tools.py refresh                                  # rosters of ALL teams as they are now + trades since last time
    python draft_app/league_tools.py rank                                     # rankings with win chances (simulated season)
    python draft_app/league_tools.py suggest                                  # trade ideas for you
    python draft_app/league_tools.py offer --team "Brick City" --asks "Jamal Murray"        # somebody asks for YOUR player(s): what to ask back
    python draft_app/league_tools.py want "Alperen Sengun"                    # you want a player: what to offer, how the stats change, a message for the manager
    python draft_app/league_tools.py all                                      # refresh + rank + suggest

Where the rosters come from (`refresh`), in this order:
  1. draft_app/espn_league.json  - the league's roster data saved from YOUR browser (no password or cookie ever touches this tool):
     open  https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/2027/segments/0/leagues/<leagueId>?view=mRoster&view=mTeam
     while logged in to ESPN, save the page (Cmd+S) as draft_app/espn_league.json. The league id is in local_config.json.
  2. optional: ESPN login cookies in the environment (ESPN_S2, ESPN_SWID) or in draft_app/espn_auth.json (gitignored), to fetch it directly.
Players the model has no line for (rookies, new signings) are evaluated with ESPN's own projection (espn_extra.py); players ESPN does not project either are ignored and listed.
All numbers are model estimates with the same assumptions as the rest of this project (ESPN blend, injuries, season-average weeks).
"""

import argparse
import itertools
import json
import os
import sys
import time
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


def fetch_with_cookies() -> dict | None:
    s2, swid = os.environ.get("ESPN_S2"), os.environ.get("ESPN_SWID")
    if not (s2 and swid) and AUTH_JSON.exists():
        a = json.loads(AUTH_JSON.read_text()); s2, swid = a.get("espn_s2"), a.get("swid")
    if not (s2 and swid):
        return None
    url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/2027/segments/0/leagues/{league_id()}"
           "?view=mRoster&view=mTeam")
    req = urllib.request.Request(url, headers={"Cookie": f"espn_s2={s2}; SWID={swid}", "Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=60, context=_ssl()) as r:
            return json.loads(r.read().decode())
    except Exception as e:                                       # never print the request (it carries the cookies)
        raise SystemExit(f"ESPN hat die Anfrage abgelehnt ({type(e).__name__}). Cookies abgelaufen? Alternative: espn_league.json aus dem Browser speichern.")


def _is_league_json(path: Path) -> dict | None:
    """The parsed file if it is ESPN's league JSON (has a 'teams' list), else None. Never prints anything from the file."""
    try:
        j = json.loads(path.read_text())
    except Exception:
        return None
    return j if isinstance(j, dict) and isinstance(j.get("teams"), list) and j["teams"] else None


def find_league_file(explicit=None):
    """(parsed json, path) of the saved league data. Tolerates the usual mistakes: wrong file name (e.g. saved as
    espn_auth.json) or still sitting in ~/Downloads."""
    cands = [Path(explicit)] if explicit else [LEAGUE_JSON, AUTH_JSON]
    if not explicit:
        dl = Path.home() / "Downloads"
        if dl.exists():
            cands += sorted([p for p in dl.glob("*.json") if "espn" in p.name.lower() or league_id_or_none() in p.name],
                            key=lambda p: -p.stat().st_mtime)[:5]
    for c in cands:
        if c.exists() and (j := _is_league_json(c)) is not None:
            return j, c
    return None, None


def league_id_or_none() -> str:
    try:
        return str(json.loads(LOCAL_JSON.read_text())["league_id"])
    except Exception:
        return "\0"


def refresh(args=None) -> dict:
    explicit = getattr(args, "file", None)
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
        src = f"Datei {path.name} (Stand {time.strftime('%d.%m. %H:%M', time.localtime(path.stat().st_mtime))})"
        if path != LEAGUE_JSON and not explicit:
            print(f"Hinweis: die Liga-Daten lagen in {path} - am besten umbenennen in draft_app/espn_league.json.")
    else:
        j = fetch_with_cookies()
        src = "ESPN mit deinen Cookies" if j else None
    if j is None:
        raise SystemExit("Keine Roster-Quelle gefunden. Geprüft: draft_app/espn_league.json, draft_app/espn_auth.json, ~/Downloads/*espn*.json\n"
                         "(keine davon enthält eine ESPN-Teamliste). Speichere die ESPN-Liga-Daten aus deinem Browser als draft_app/espn_league.json\n"
                         "(Anleitung ganz oben in league_tools.py: URL im eingeloggten Chrome öffnen, Cmd+S) und starte noch einmal.")
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
    print(f"Quelle: {src} · {len(new)} Teams, {sum(len(v) for v in new.values())} Spieler -> league_rosters.json")
    if old:
        where_old = {names.name_key(p): t for t, ps in old.items() for p in ps}
        where_new = {names.name_key(p): (t, p) for t, ps in new.items() for p in ps}
        moves = [(p, where_old[k], t) for k, (t, p) in where_new.items() if k in where_old and where_old[k] != t]
        adds = [(p, t) for k, (t, p) in where_new.items() if k not in where_old]
        drops = [(p, where_old[k]) for k in where_old if k not in where_new for p in [next((x for ps in old.values() for x in ps if names.name_key(x) == k), k)]]
        print(f"\nTrades / Wechsel seit dem letzten Stand: {len(moves)}")
        for p, a, b in sorted(moves, key=lambda m: (m[1], m[2])):
            print(f"  {p}: {a} -> {b}")
        if adds or drops:
            print(f"Neu verpflichtet: {len(adds)}  ·  entlassen: {len(drops)}")
            for p, t in adds[:12]:
                print(f"  + {p} ({t})")
            for p, t in drops[:12]:
                print(f"  - {p} ({t})")
    return new


# ── the league model ─────────────────────────────────────────────────────────────────────────────────
class League:
    def __init__(self):
        if not ROSTERS_JSON.exists():
            wl.init_rosters()
        raw = json.loads(ROSTERS_JSON.read_text())
        self.pool, self.meta = wl.load_pool_and_meta([n for ns in raw.values() for n in ns])   # rookies/new signings: ESPN's projection
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
        z = ((self.model.E - self.model.E.mean(0)) / np.maximum(self.model.E.std(0), 1e-9)).sum(1)
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
            raise SystemExit(f"Spieler '{text}' nicht im Pool (Schreibweise prüfen, Rookies fehlen).")
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
        tot, k = 0.0, 0
        for u in self.T:
            if u == t:
                continue
            om = (others or {}).get(u, self.mom[u])
            pc = engine.phi((mom[0] - om[0]) / np.sqrt(mom[1] ** 2 + om[1] ** 2))
            tot += float(engine.matchup_prob(pc, self.rules)); k += 1
        return tot / k

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
    return f"{L.model.names[i]} ({p.POS_ELIGIBLE_STR.iat[i]}, {p.CURRENT_TEAM_ABBR.iat[i]}; {p.EXP_GP.iat[i]:.0f} Spiele erw.): " + ", ".join(parts)


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


def cmd_want(L: League, args):
    me = L.you
    w = L.find_player(args.player)
    x = L.owner(w)
    if x is None:
        raise SystemExit(f"{L.model.names[w]} gehört keinem Team (frei).")
    if x == me:
        raise SystemExit("Der Spieler gehört schon dir.")
    print(f"\nWunschspieler: {stat_line(L, w)}\nBesitzer: {x}")
    cands = []
    mine = L.rosters[me]
    for k in (1, 2):
        for give in itertools.combinations(mine, k):
            cands.append((give, L.trade(me, x, list(give), [w])))
    fair = [c for c in cands if c[1]["v_give"] >= c[1]["v_get"] - 3 and c[1]["d_x"] > -0.01]
    fair.sort(key=lambda c: -c[1]["d_me"])
    print("\n== Angebote, die für den Manager fair wirken und dir am meisten bringen ==")
    shown = fair[:4] or sorted(cands, key=lambda c: -(c[1]["d_me"] + c[1]["d_x"]))[:4]
    if not fair:
        print("  (Kein Angebot erfüllt beide Kriterien; hier die besten Kompromisse)")
    for give, r in shown:
        print(reason(L, x, list(give), [w], r)); print()
    if shown:
        give, r = shown[0]
        gs = " + ".join(L.model.names[i] for i in give)
        print("== Nachricht an den Manager (Vorschlag) ==")
        print(f"  Hallo, ich würde dir {gs} für {L.model.names[w]} anbieten.")
        print(f"  Für dich: {gs} bringt dir in den Kategorien {cat_delta_text(r['cat_x'], 3)} (Veränderung deiner Wochen-Gewinnchance je Kategorie in Prozentpunkten),")
        print(f"  insgesamt {100 * r['d_x']:+.1f} Prozentpunkte Siegchance pro Woche nach meiner Rechnung.")
        print("  Ist das etwas für dich?")
        print("\n== So ändern sich eure Werte (erwartete Wochensummen) ==")
        print(f"  {'':8}{'du jetzt':>12}{'du danach':>12}{x[:14]:>16}{'danach':>10}")
        for j, c in enumerate(CATN):
            a0, a1, b0, b1 = L.mom[me][0][j], r["mom_me"][0][j], L.mom[x][0][j], r["mom_x"][0][j]
            f = "{:>12.3f}{:>12.3f}{:>16.3f}{:>10.3f}" if c in ("FG_PCT", "FT_PCT", "PTS_MIN") else "{:>12.1f}{:>12.1f}{:>16.1f}{:>10.1f}"
            print(f"  {engine.LABEL[c]:<8}" + f.format(a0, a1, b0, b1))
        print(f"  Siegchance/Woche: du {100 * L.base[me]:.1f}% -> {100 * (L.base[me] + r['d_me']):.1f}%   ·   {x} {100 * L.base[x]:.1f}% -> {100 * (L.base[x] + r['d_x']):.1f}%")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refresh"); r.add_argument("--file")
    sub.add_parser("rank"); sub.add_parser("suggest"); sub.add_parser("all")
    o = sub.add_parser("offer"); o.add_argument("--team", required=True); o.add_argument("--asks", required=True, help='deine Spieler, nach denen gefragt wird, mit ; getrennt')
    w = sub.add_parser("want"); w.add_argument("player")
    args = ap.parse_args()
    if args.cmd == "refresh":
        refresh(args); return 0
    if args.cmd == "all":
        try:
            refresh(args)
        except SystemExit as e:
            print(f"(Roster nicht aktualisiert: {e}) rechne mit dem letzten Stand in league_rosters.json")
    L = League()
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
