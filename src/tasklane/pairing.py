"""Client pairing (connection approval) for TaskLane.

OpenClaw-style flow: a client requests pairing and receives a one-time
cleartext token plus a short human-readable pairing code. An operator approves
or rejects the code; only approved, non-revoked clients authenticate. Only the
SHA-256 hex of the token is ever persisted.

State lives in ``$TASKLANE_HOME/clients.json`` (mode 600, atomic writes) and
read-modify-write cycles are serialised with an ``O_EXCL`` lock file, mirroring
the patterns in :mod:`tasklane.store`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from tasklane.atomicio import atomic_write_json
from tasklane.paths import tasklane_home
from tasklane.store import parse_timestamp, utc_now

# A-Z minus I/O plus 2-9: no characters that read ambiguously when spoken or typed.
PAIRING_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIRING_CODE_LENGTH = 8
PENDING_CAP = 10
DEFAULT_PENDING_TTL_SECONDS = 900.0
_LOCK_TIMEOUT_SECONDS = 5.0


def clients_path() -> Path:
    return tasklane_home() / "clients.json"


def request_pairing(name: str) -> dict[str, Any]:
    """Register a pairing request; returns the cleartext token exactly once."""
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("client name is required")
    token = secrets.token_urlsafe(32)
    with _locked():
        data = _load()
        _prune(data, DEFAULT_PENDING_TTL_SECONDS)
        if len(data["pending"]) >= PENDING_CAP:
            raise ValueError(f"too many pending pairing requests (cap {PENDING_CAP})")
        client_id = _new_client_id(clean_name, data)
        pairing_code = _new_pairing_code(data)
        now = utc_now()
        data["clients"][client_id] = {
            "name": clean_name,
            "token_sha256": _token_digest(token),
            "created_at": now,
            "approved_at": None,
            "last_seen_at": None,
            "revoked_at": None,
        }
        data["pending"][pairing_code] = {
            "client_id": client_id,
            "name": clean_name,
            "requested_at": now,
        }
        _save(data)
    return {
        "client_id": client_id,
        "pairing_code": pairing_code,
        "token": token,
        "status": "pending",
    }


def approve_client(pairing_code: str) -> dict[str, Any]:
    """Approve a pending pairing request; unknown or expired codes raise ValueError."""
    code = str(pairing_code or "").strip().upper()
    with _locked():
        data = _load()
        _prune(data, DEFAULT_PENDING_TTL_SECONDS)
        entry = data["pending"].pop(code, None)
        if entry is None:
            raise ValueError("unknown or expired pairing code")
        client_id = str(entry.get("client_id") or "")
        record = data["clients"].get(client_id)
        if record is None:
            raise ValueError("unknown or expired pairing code")
        record["approved_at"] = utc_now()
        _save(data)
        return _public(client_id, record)


def reject_client(pairing_code: str) -> dict[str, Any]:
    """Reject a pending pairing request, discarding the client record."""
    code = str(pairing_code or "").strip().upper()
    with _locked():
        data = _load()
        entry = data["pending"].pop(code, None)
        if entry is None:
            raise ValueError("unknown pairing code")
        client_id = str(entry.get("client_id") or "")
        record = data["clients"].pop(client_id, None)
        _save(data)
        return {
            "client_id": client_id,
            "name": str(entry.get("name") or ""),
            "status": "rejected",
            "record": _public(client_id, record) if record else None,
        }


def authenticate(token: str) -> dict[str, Any] | None:
    """Return the matching approved, non-revoked client record, or None.

    The auth DECISION is read-only and lock-free (``clients.json`` is written
    atomically, so a plain read is consistent) and fails CLOSED — any read error
    denies rather than propagating. ``last_seen_at`` is stamped best-effort and
    throttled (see :func:`_touch_last_seen`), so a read/auth never mutates state
    on the hot path nor serializes behind the write lock.
    """
    text = str(token or "")
    if not text:
        return None
    digest = _token_digest(text)
    try:
        data = _load()
    except Exception:  # noqa: BLE001 — fail closed: an unreadable store denies
        return None
    matched_id: str | None = None
    matched_record: dict[str, Any] | None = None
    for client_id, record in data["clients"].items():
        if not hmac.compare_digest(str(record.get("token_sha256") or ""), digest):
            continue
        if not record.get("approved_at") or record.get("revoked_at"):
            return None
        matched_id, matched_record = client_id, record
        break
    if matched_id is None or matched_record is None:
        return None
    # The client is being seen right now, so the reported last_seen_at is `now`;
    # the throttle below only governs how often we PERSIST it, not what we report.
    seen = dict(matched_record)
    seen["last_seen_at"] = utc_now()
    _touch_last_seen(matched_id)
    return _public(matched_id, seen)


def _touch_last_seen(client_id: str, *, throttle_seconds: float = 60.0) -> None:
    """Best-effort, throttled ``last_seen_at`` stamp. Never raises, never blocks
    auth: skips the write if last_seen is fresh, and swallows lock contention /
    IO errors (a missed timestamp is harmless; failing auth over it is not)."""
    try:
        with _locked(timeout=0.5):
            data = _load()
            record = data["clients"].get(client_id)
            if record is None or record.get("revoked_at"):
                return
            last = parse_timestamp(record.get("last_seen_at"))
            if last is not None and (datetime.now(timezone.utc) - last).total_seconds() < throttle_seconds:
                return
            record["last_seen_at"] = utc_now()
            _save(data)
    except (TimeoutError, OSError, ValueError):
        return  # best-effort only


def list_clients() -> list[dict[str, Any]]:
    """All client records (pending and approved), without token hashes."""
    data = _load()
    return [_public(client_id, record) for client_id, record in sorted(data["clients"].items())]


def pending_requests(ttl_seconds: float = DEFAULT_PENDING_TTL_SECONDS) -> list[dict[str, Any]]:
    """Unexpired pending pairing requests; prunes expired entries first."""
    with _locked():
        data = _load()
        if _prune(data, ttl_seconds):
            _save(data)
        return [
            {"pairing_code": code, **entry}
            for code, entry in sorted(data["pending"].items(), key=lambda item: str(item[1].get("requested_at") or ""))
        ]


def revoke_client(client_id: str) -> dict[str, Any]:
    """Mark a client as revoked; revoked clients can no longer authenticate."""
    safe_id = str(client_id or "").strip()
    with _locked():
        data = _load()
        record = data["clients"].get(safe_id)
        if record is None:
            raise ValueError(f"unknown client: {safe_id!r}")
        record["revoked_at"] = utc_now()
        _save(data)
        return _public(safe_id, record)


def prune_pending(ttl_seconds: float = DEFAULT_PENDING_TTL_SECONDS) -> list[str]:
    """Drop pending requests older than the TTL; returns the pruned pairing codes."""
    with _locked():
        data = _load()
        pruned = _prune(data, ttl_seconds)
        if pruned:
            _save(data)
        return pruned


def _prune(data: dict[str, Any], ttl_seconds: float) -> list[str]:
    """Remove expired pending entries (and their never-approved client records) in place."""
    now = datetime.now(timezone.utc)
    pruned: list[str] = []
    for code, entry in list(data["pending"].items()):
        requested_at = parse_timestamp(entry.get("requested_at"))
        if requested_at is not None and (now - requested_at).total_seconds() <= ttl_seconds:
            continue
        data["pending"].pop(code)
        pruned.append(code)
        client_id = str(entry.get("client_id") or "")
        record = data["clients"].get(client_id)
        if record is not None and not record.get("approved_at"):
            data["clients"].pop(client_id)
    return pruned


def _new_client_id(name: str, data: dict[str, Any]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "client"
    while True:
        client_id = f"{slug}-{secrets.token_hex(3)}"
        if client_id not in data["clients"]:
            return client_id


def _new_pairing_code(data: dict[str, Any]) -> str:
    while True:
        code = "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))
        if code not in data["pending"]:
            return code


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public(client_id: str, record: dict[str, Any]) -> dict[str, Any]:
    public = {key: value for key, value in record.items() if key != "token_sha256"}
    return {"client_id": client_id, **public}


def _load() -> dict[str, Any]:
    path = clients_path()
    if not path.exists():
        return {"clients": {}, "pending": {}}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Expected JSON object in {path}")
    clients = raw.get("clients")
    pending = raw.get("pending")
    return {
        "clients": dict(clients) if isinstance(clients, dict) else {},
        "pending": dict(pending) if isinstance(pending, dict) else {},
    }


def _save(data: dict[str, Any]) -> None:
    # mode 0600: the client registry holds token hashes — never world-readable.
    atomic_write_json(clients_path(), data, mode=0o600)


@contextmanager
def _locked(timeout: float = _LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Serialise read-modify-write cycles with an O_EXCL lock file."""
    lock_path = clients_path().with_suffix(".json.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"could not acquire pairing lock: {lock_path}")
            time.sleep(0.01)
    try:
        os.close(fd)
        yield
    finally:
        lock_path.unlink(missing_ok=True)
