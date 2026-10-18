"""
Local bridge between the ESPN draft room and the draft assistant.
=================================================================
A small userscript (draft_app/espn_bridge.user.js) runs in YOUR logged-in browser tab, reads the ESPN auction draft room
and posts what it sees to this process on 127.0.0.1. The bridge turns the events into the files the Streamlit app reads:

    nominated  -> current_nomination.json   who is up, the current bid and who leads (the app shows BID / DROP at once)
    sold       -> draft_log.json            the pick, exactly as if you had tapped it (deduplicated by player)
    clear      -> removes current_nomination.json
    /dump      -> draft_room_dump_<label>.html   page snapshots to fit the userscript's selectors to ESPN's real page

Run it next to the app:

    python draft_app/bridge.py            # prints the URL and where the token lives

Nothing leaves your machine: it listens on 127.0.0.1 only and every POST needs the token in draft_app/bridge_token.txt
(so another web page you happen to have open cannot write picks into your draft). Names that cannot be matched to the
pool or the league's teams are NOT logged; they go to bridge_unresolved.json and the app asks you to enter them by hand.
"""

import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).parent
sys.path.insert(0, str(APP_DIR))
import names  # noqa: E402

HOST, PORT = "127.0.0.1", int(os.environ.get("BRIDGE_PORT", "8765"))
MAX_EVENT_BYTES, MAX_DUMP_BYTES = 64 * 1024, 6 * 1024 * 1024


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


class Bridge:
    """All the logic, independent of HTTP so it can be tested directly."""

    def __init__(self, player_ids, player_names, teams, log_json: Path, nom_json: Path, unresolved_json: Path, dump_dir: Path):
        self.index = names.build_index(player_ids, player_names)
        self.teams = list(teams)
        self.log_json, self.nom_json, self.unresolved_json, self.dump_dir = log_json, nom_json, unresolved_json, dump_dir
        self.lock = threading.Lock()

    # ── events ────────────────────────────────────────────────────────────────────
    def handle(self, ev: dict) -> dict:
        kind = ev.get("type")
        with self.lock:
            if kind == "nominated":
                return self._nominated(ev)
            if kind == "sold":
                return self._sold(ev)
            if kind == "clear":
                self.nom_json.unlink(missing_ok=True)
                return {"ok": True, "status": "cleared"}
        return {"ok": False, "status": f"unknown event type {kind!r}"}

    @staticmethod
    def _int(v):
        try:
            return int(round(float(str(v).replace("$", "").replace(",", "").strip())))
        except (TypeError, ValueError):
            return None

    def _nominated(self, ev: dict) -> dict:
        raw = str(ev.get("player") or "").strip()
        if not raw:
            return {"ok": False, "status": "nominated without a player name"}
        hit = names.resolve_player(self.index, raw)
        team_raw = ev.get("team")
        team = names.resolve_team(self.teams, team_raw) if team_raw else None
        _atomic_write(self.nom_json, json.dumps({
            "player_id": hit[0] if hit else None, "player_name": hit[1] if hit else raw, "espn_name": raw,
            "bid": self._int(ev.get("bid")), "team": team or (str(team_raw) if team_raw else None), "ts": time.time()}))
        return {"ok": True, "status": "nominated", "resolved": bool(hit)}

    def _unresolved(self, ev: dict, why: str) -> None:
        items = _read_json(self.unresolved_json, [])
        raw = str(ev.get("player") or "")
        if any(i.get("espn_name") == raw and i.get("team") == ev.get("team") for i in items):
            return
        items.append({"espn_name": raw, "team": ev.get("team"), "price": self._int(ev.get("price")), "why": why,
                      "suggestions": names.suggest_players(self.index, raw), "ts": time.time()})
        _atomic_write(self.unresolved_json, json.dumps(items, indent=2))

    def _sold(self, ev: dict) -> dict:
        raw = str(ev.get("player") or "").strip()
        price, hit = self._int(ev.get("price")), names.resolve_player(self.index, raw)
        team = names.resolve_team(self.teams, ev.get("team") or "")
        if hit is None or team is None or price is None or price < 1:
            why = ("Spieler nicht im Pool" if hit is None else "Team nicht erkannt" if team is None else "Preis nicht lesbar")
            self._unresolved(ev, why)
            return {"ok": False, "status": f"unresolved: {why}"}
        log = _read_json(self.log_json, [])
        if any(int(e["player_id"]) == hit[0] for e in log):
            return {"ok": True, "status": "duplicate (already logged)"}
        log.append({"player_id": hit[0], "player_name": hit[1], "team": team, "price": price, "source": "espn"})
        _atomic_write(self.log_json, json.dumps(log, indent=2))
        nom = _read_json(self.nom_json, None)
        if nom and nom.get("player_id") == hit[0]:
            self.nom_json.unlink(missing_ok=True)
        return {"ok": True, "status": "logged", "player": hit[1], "team": team, "price": price}

    def dump(self, label: str, html: str) -> Path:
        safe = "".join(c for c in label if c.isalnum() or c in "-_")[:40] or "dump"
        self.dump_dir.mkdir(exist_ok=True)
        path = self.dump_dir / f"draft_room_dump_{safe}.html"
        path.write_text(html)
        return path


def make_handler(bridge: Bridge, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):                      # one quiet line per request
            sys.stderr.write("  bridge: " + (fmt % args) + "\n")

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Bridge-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")

        def _reply(self, code: int, obj: dict):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            if self.path == "/health":
                self._reply(200, {"ok": True, "picks": len(_read_json(bridge.log_json, []))})
            else:
                self._reply(404, {"ok": False})

        def do_POST(self):
            if self.headers.get("X-Bridge-Token") != token:
                return self._reply(403, {"ok": False, "status": "bad token"})
            limit = MAX_DUMP_BYTES if self.path == "/dump" else MAX_EVENT_BYTES
            n = int(self.headers.get("Content-Length") or 0)
            if n > limit:
                return self._reply(413, {"ok": False, "status": "too large"})
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._reply(400, {"ok": False, "status": "invalid json"})
            if self.path == "/event":
                res = bridge.handle(data)
                print(f"  event {data.get('type')}: {data.get('player', '')!s:<28} -> {res.get('status')}", flush=True)
                self._reply(200 if res.get("ok") else 422, res)
            elif self.path == "/dump":
                path = bridge.dump(str(data.get("label", "dump")), str(data.get("html", "")))
                print(f"  dump saved: {path.name} ({path.stat().st_size // 1024} KB)", flush=True)
                self._reply(200, {"ok": True, "file": path.name})
            else:
                self._reply(404, {"ok": False})

    return Handler


def load_token(path: Path) -> str:
    if not path.exists():
        path.write_text(secrets.token_urlsafe(16))
    return path.read_text().strip()


def main() -> None:
    import pandas as pd
    pool = pd.read_csv(APP_DIR / "player_pool.csv", usecols=["PLAYER_ID", "PLAYER_NAME"])
    meta = json.loads((APP_DIR / "league_meta.json").read_text())
    bridge = Bridge(pool["PLAYER_ID"], pool["PLAYER_NAME"], meta["team_names"], APP_DIR / "draft_log.json",
                    APP_DIR / "current_nomination.json", APP_DIR / "bridge_unresolved.json", APP_DIR)
    token_path = APP_DIR / "bridge_token.txt"
    token = os.environ.get("BRIDGE_TOKEN") or load_token(token_path)       # BRIDGE_TOKEN: throwaway token for tests
    server = ThreadingHTTPServer((HOST, PORT), make_handler(bridge, token))
    print(f"ESPN bridge listening on http://{HOST}:{PORT}  ({len(bridge.index)} pool names, {len(bridge.teams)} teams)")
    print(f"Token (put it into the userscript's CONFIG): {token_path}")
    print("Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
