"""
Refresh ESPN's injury status for the draft pool (takes a few seconds, no notebook run needed).

    python draft_app/refresh_injuries.py

Reads ESPN's public player list, matches it to draft_app/player_pool.csv by name and writes draft_app/injuries.json:
status (ACTIVE / DAY_TO_DAY / OUT ...), whether ESPN flags the player as injured, and ESPN's outlook text. The app reads
that file: OUT players get fewer expected games (see OUT_GAMES_LOST in app.py) and every injured player shows ESPN's note
next to his name. Run it again right before the draft; the app picks the change up without a restart.
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).parent
sys.path.insert(0, str(APP_DIR))
import names  # noqa: E402

ESPN_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/2027/players?view=kona_player_info"
ESPN_FILTER = '{"players":{"limit":1500,"sortPercOwned":{"sortPriority":1,"sortAsc":false}}}'


def _ssl_context():
    """python.org builds of Python on macOS ship without root certificates: use certifi's (installed with requests)."""
    try:
        import ssl

        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def fetch() -> list:
    req = urllib.request.Request(ESPN_URL, headers={"X-Fantasy-Filter": ESPN_FILTER, "Accept": "application/json",
                                                    "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=90, context=_ssl_context()) as r:
        return json.loads(r.read().decode())


def build(espn: list, pool: pd.DataFrame) -> dict:
    index = names.build_index(pool["PLAYER_ID"], pool["PLAYER_NAME"])
    out = {}
    for p in espn:
        hit = names.resolve_player(index, p.get("fullName", ""))
        status = p.get("injuryStatus")
        if hit is None or (status in (None, "ACTIVE") and not p.get("injured")):
            continue
        out[str(hit[0])] = {"name": hit[1], "status": status or "UNKNOWN", "injured": bool(p.get("injured")),
                            "outlook": (p.get("seasonOutlook") or "")[:600], "news_ts": p.get("lastNewsDate")}
    return out


def main() -> None:
    pool = pd.read_csv(APP_DIR / "player_pool.csv", usecols=["PLAYER_ID", "PLAYER_NAME"])
    inj = build(fetch(), pool)
    payload = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "players": inj}
    (APP_DIR / "injuries.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    out = sorted((v["name"] for v in inj.values() if v["status"] == "OUT" or v["injured"]))
    dtd = sum(1 for v in inj.values() if v["status"] == "DAY_TO_DAY")
    print(f"injuries.json: {len(inj)} pool players not fully healthy · {len(out)} OUT/injured · {dtd} day-to-day")
    print("OUT / injured:", ", ".join(out) if out else "none")


if __name__ == "__main__":
    main()
