"""Name matching shared by the ESPN bridge and the tests.

ESPN writes "Nikola Jokic", the pool says "Nikola Jokić"; suffixes ("Jr.") and punctuation differ as well. Everything is
compared on an accent-free, punctuation-free, suffix-free key (the same key the v4 notebook uses to join ESPN's list).
"""

import difflib
import unicodedata

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
