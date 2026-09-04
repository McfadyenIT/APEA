"""Remember a captured card token, so losing the file does not lose the token.

WHY THIS EXISTS
---------------
A browser never tells a page where an uploaded file came from -- it reports a
fake path on purpose -- so LT Metrics edits its own COPY and the operator's file stays
as it was. Enrolment therefore writes tokens somewhere the next upload does not
read, and the two drift apart. That cost two runs in one day: a page showing
tokens, a file on disk without them, and warnings that were correct about a copy
nobody was looking at.

Offering the updated file back helped, but only if someone remembers to save it.
This removes the footgun instead: a token captured for an account is remembered,
and re-applied to any later upload that has that account with an empty cell.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It never overwrites a token already in the file -- an explicit value is a choice,
and a remembered one must not silently replace it. It is keyed by STORE as well
as account, because a token belongs to one store and applying yesterday's to a
different environment would fail in a way that looks like a code bug. And every
restore is reported; a file that quietly gains values nobody typed is worse than
one that is missing them.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

# Beside the code, not in a temp dir: it has to survive a reboot to be useful.
_ROOT = Path(__file__).resolve().parent.parent.parent
_STORE = _ROOT / ".ltmetrics-token-memory.json"
_LEGACY_STORE = _ROOT / ".apea-token-memory.json"


def _migrate_legacy() -> None:
    """Carry a pre-rename store across, once.

    A token costs a real order to create. Losing the file because the product
    was renamed would mean re-enrolling every account, so the old name is
    honoured if the new one is not there yet.
    """
    try:
        if _STORE.exists() or not _LEGACY_STORE.exists():
            return
        _LEGACY_STORE.replace(_STORE)
    except Exception:
        pass                      # a memory that cannot migrate is not fatal


_migrate_legacy()
_MAX_ENTRIES = 500          # bounded, so a long-lived install cannot grow forever


def _store_key(base_url: str) -> str:
    """A token belongs to one store. Key on the host so a token captured against
    staging is never applied to a different environment."""
    try:
        return (urlparse(base_url or "").hostname or "unknown").lower()
    except Exception:
        return "unknown"


def _load() -> dict:
    try:
        with open(_STORE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    """Write atomically. A half-written memory file would be worse than none:
    it fails to parse, silently returns empty, and the tokens look lost."""
    try:
        _STORE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_STORE.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, _STORE)
    except Exception:
        pass                # memory is a convenience; never break a run for it


def remember(base_url: str, tokens: dict) -> int:
    """Record {username: token} for this store. Returns how many were stored."""
    if not tokens:
        return 0
    data = _load()
    key = _store_key(base_url)
    slot = data.setdefault(key, {})
    n = 0
    for user, tok in tokens.items():
        u, t = str(user or "").strip().lower(), str(tok or "").strip()
        if u and t:
            slot[u] = t
            n += 1
    # Trim oldest stores first if this ever grows unreasonably.
    while sum(len(v) for v in data.values()) > _MAX_ENTRIES and len(data) > 1:
        data.pop(next(iter(data)))
    _save(data)
    return n


def recall(base_url: str) -> dict:
    """Every remembered {username: token} for this store.

    With no store to key on -- an upload before anything named a target -- fall
    back ONLY when the memory holds exactly one store. That case is unambiguous.
    With several, guessing which one an account belongs to would apply a token
    from the wrong environment, which fails in a way that looks like a code bug.
    """
    data = _load()
    if base_url:
        return dict(data.get(_store_key(base_url)) or {})
    if len(data) == 1:
        return dict(next(iter(data.values())) or {})
    return {}


def apply_to_rows(rows: list, base_url: str) -> list:
    """Fill BLANK payment_token cells from memory. Returns the usernames filled.

    Blank only. A token already in the file was put there deliberately -- by a
    person or by an earlier enrolment -- and replacing it would make the file
    say something its author did not.
    """
    known = recall(base_url)
    filled = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        if (r.get("payment_token") or "").strip():
            continue
        u = (r.get("username") or r.get("email") or "").strip().lower()
        if not u:
            continue
        tok = known.get(u)
        if not tok and not (base_url or "").strip():
            # ONLY when the tool does not know which store it is working with --
            # _LAST_TARGET lives in the process and is empty after a restart.
            # A NAMED store that lacks the account is a different matter: the
            # answer there is no, or a token would cross between stores.
            tok = _sole_token_for(u)
        if tok:
            r["payment_token"] = tok
            filled.append(r.get("username") or u)
    return filled


def _sole_token_for(user: str) -> str:
    """A remembered token for this account when exactly one store has one.

    Two stores holding the same email is genuinely ambiguous -- the tokens are
    different and picking one would put the wrong card against an account -- so
    that case is left for the operator.
    """
    hits = []
    for _host, accts in (_load() or {}).items():
        tok = (accts or {}).get(user)
        if tok:
            hits.append(tok)
    return hits[0] if len(hits) == 1 else ""


def forget(base_url: str = "", user: str = "") -> int:
    """Drop remembered tokens. No argument clears everything; a store clears that
    store; a store and user clears one account. Needed because 'capture a fresh
    token' must be possible without hunting for a file."""
    data = _load()
    if not base_url:
        n = sum(len(v) for v in data.values())
        _save({})
        return n
    key = _store_key(base_url)
    slot = data.get(key) or {}
    if not user:
        n = len(slot)
        data.pop(key, None)
    else:
        n = 1 if slot.pop(str(user).strip().lower(), None) else 0
        data[key] = slot
    _save(data)
    return n
