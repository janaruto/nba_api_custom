"""Name matching shared by the ESPN bridge and the tests.

ESPN writes "Nikola Jokic", the pool says "Nikola Jokić"; suffixes ("Jr.") and punctuation differ as well. Everything is
compared on an accent-free, punctuation-free, suffix-free key (the same key the v4 notebook uses to join ESPN's list).
"""

import difflib
import json
import unicodedata
from pathlib import Path

SUFFIXES = (" jr", " sr", " ii", " iii", " iv", " v")


def name_key(s: str) -> str:
    """'Nikola Jokić' -> 'nikolajokic'."""
    s = "".join(c for c in unicodedata.normalize("NFKD", str(s)) if not unicodedata.combining(c))
    s = s.lower().replace(".", " ").replace("-", " ").replace("'", " ")
    s = " ".join(s.split())
    for suf in SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    return "".join(ch for ch in s if ch.isalnum())


def build_index(player_ids, player_names) -> dict:
    """key -> (player_id, pool name). Keys shared by two different players are dropped (never guess)."""
    index, dupes = {}, set()
    for pid, nm in zip(player_ids, player_names):
        k = name_key(nm)
        if k in index and index[k][0] != int(pid):
            dupes.add(k)
        index[k] = (int(pid), str(nm))
    for k in dupes:
        index.pop(k, None)
    return index


def resolve_player(index: dict, name: str):
    """(player_id, pool name) or None."""
    return index.get(name_key(name))


def suggest_players(index: dict, name: str, n: int = 3) -> list:
    """Closest pool names for a name that did not resolve (shown to the user, never applied automatically)."""
    keys = difflib.get_close_matches(name_key(name), list(index), n=n, cutoff=0.75)
    return [index[k][1] for k in keys]


def _team_key(s: str) -> str:
    return "".join(ch for ch in str(s).casefold() if ch.isalnum())


def resolve_team(teams: list, name: str):
    """League team name for whatever ESPN shows (exact after normalising, else a unique containment), or None."""
    k = _team_key(name)
    if not k:
        return None
    for t in teams:
        if _team_key(t) == k:
            return t
    hits = [t for t in teams if _team_key(t) in k or k in _team_key(t)]
    return hits[0] if len(hits) == 1 else None


# ── teams get renamed in ESPN: follow them by ESPN's team id ────────────────────────────────────────
TEAMS_FILE = Path(__file__).parent / "league_teams.json"      # local, git-ignored: {"<espn team id>": {"name": current, "aliases": [earlier names]}}
LOCAL_FILE = Path(__file__).parent / "local_config.json"      # optional "team_aliases": {"old name": "new name"} for renames the registry never saw


def espn_team_name(t: dict) -> str:
    return (t.get("name") or f"{t.get('location', '')} {t.get('nickname', '')}".strip() or f"Team {t.get('id')}").strip()


def team_aliases(path=None) -> dict:
    """{earlier name: current name} from the registry (and the optional manual list in local_config.json)."""
    out = {}
    try:
        for rec in json.loads(Path(path or TEAMS_FILE).read_text()).values():
            for old in rec.get("aliases", []):
                if old != rec["name"]:
                    out[old] = rec["name"]
    except (OSError, ValueError, KeyError, AttributeError):
        pass
    try:
        manual = json.loads(LOCAL_FILE.read_text()).get("team_aliases", {})
        out.update({k: v for k, v in manual.items() if isinstance(v, str)})
    except (OSError, ValueError, AttributeError):
        pass
    return out


def canonical_team(name: str, aliases: dict | None = None) -> str:
    """The team's current name for any name it ever had (follows chains of renames, ignores case and punctuation);
    unknown names are returned as they are."""
    aliases = team_aliases() if aliases is None else aliases
    norm = {_team_key(k): v for k, v in aliases.items()}
    seen, cur = set(), name
    while _team_key(cur) in norm and _team_key(cur) not in seen:
        seen.add(_team_key(cur))
        cur = norm[_team_key(cur)]
    return cur


def same_team(a: str, b: str, aliases: dict | None = None) -> bool:
    aliases = team_aliases() if aliases is None else aliases
    return _team_key(canonical_team(a, aliases)) == _team_key(canonical_team(b, aliases))


def update_team_registry(teams: list, seed_names: list | None = None, path=None) -> list:
    """Record ESPN's teams {id: current name + earlier names}. Returns the renames found as [(old, new)].
    First run: `seed_names` (the draft-time team names, in ESPN's team order) become the earlier names of teams whose name differs now."""
    path = Path(path or TEAMS_FILE)
    try:
        reg = json.loads(path.read_text())
    except (OSError, ValueError):
        reg = {}
    ordered = sorted(teams, key=lambda t: int(t.get("id", 0)))
    seed = list(seed_names) if (not reg and seed_names and len(seed_names) == len(ordered)) else None
    renames = []
    for k, t in enumerate(ordered):
        tid, now = str(t.get("id")), espn_team_name(t)
        rec = reg.setdefault(tid, {"name": now, "aliases": []})
        if seed and seed[k] != now and seed[k] not in rec["aliases"]:
            rec["aliases"].append(seed[k])
            renames.append((seed[k], now))
        if rec["name"] != now:
            if rec["name"] not in rec["aliases"]:
                rec["aliases"].append(rec["name"])
            renames.append((rec["name"], now))
            rec["name"] = now
    path.write_text(json.dumps(reg, indent=1, ensure_ascii=False))
    return renames
