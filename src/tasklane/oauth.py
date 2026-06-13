"""OAuth 2.1 authorization + resource server for the MCP control plane.

This is what lets claude.ai / the Claude mobile + desktop apps connect to
TaskLane as a *custom connector*. Those clients can't hold a static bearer
token; they speak the MCP authorization flow (OAuth 2.1 + PKCE + Dynamic Client
Registration), per the MCP spec (2025-06-18): RFC 9728 protected-resource
metadata, RFC 8414 authorization-server metadata, RFC 7591 DCR, RFC 8707
resource indicators.

TaskLane is both the resource server (it validates access tokens on ``/mcp``)
and the authorization server (it registers clients, runs the authorize/consent
step, and issues tokens). The consent step is the operator gate: the user proves
they hold the ``app_token`` once in the browser, and from then on the app stores
its own OAuth token — no manual header. Tokens are opaque, stored only as
SHA-256 hashes, audience-bound to this server, short-lived, with rotating
refresh tokens (required for public clients).

State lives in ``$TASKLANE_HOME/oauth.json`` (mode 600, atomic writes), guarded
by the same O_EXCL lock idiom as the rest of the store.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from tasklane.atomicio import atomic_write_json
from tasklane.paths import tasklane_home
from tasklane.store import utc_now

# Lifetimes. Access tokens are short-lived (theft mitigation); the client
# refreshes silently. Authorization codes are single-use and expire fast.
ACCESS_TOKEN_TTL_SECONDS = 3600          # 1 hour
REFRESH_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days
AUTH_CODE_TTL_SECONDS = 120              # 2 minutes
_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_POLL = 0.02

# Caps (flood guards on a public endpoint).
MAX_CLIENTS = 100
MAX_CODES = 200
MAX_REDIRECT_URIS = 10

SUPPORTED_SCOPES = ("tasklane",)


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
def oauth_path() -> Path:
    return tasklane_home() / "oauth.json"


def _empty() -> dict[str, Any]:
    return {"clients": {}, "codes": {}, "tokens": {}, "refresh": {}}


def _load() -> dict[str, Any]:
    path = oauth_path()
    if not path.exists():
        return _empty()
    import json

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Expected JSON object in {path}")
    data = _empty()
    for key in data:
        if isinstance(raw.get(key), dict):
            data[key] = dict(raw[key])
    return data


def _save(data: dict[str, Any]) -> None:
    atomic_write_json(oauth_path(), data, mode=0o600)


@contextmanager
def _locked(timeout: float = _LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    path = oauth_path().with_suffix(".json.lock")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(path, flags, 0o600)
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > timeout:
                    path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"oauth lock busy: {path}")
            time.sleep(_LOCK_POLL)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _now() -> float:
    return time.time()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _b64url_nopad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def verify_pkce(code_challenge: str, code_challenge_method: str, code_verifier: str) -> bool:
    """RFC 7636. We require S256 (OAuth 2.1 disallows plain for public clients)."""
    if code_challenge_method != "S256" or not code_verifier or not code_challenge:
        return False
    expected = _b64url_nopad(hashlib.sha256(code_verifier.encode("ascii")).digest())
    return hmac.compare_digest(expected, code_challenge)


def _expired(data: dict[str, Any], *, prune: bool = True) -> None:
    """Drop expired codes/tokens/refresh entries (best-effort housekeeping)."""
    now = _now()

    def _exp(entry: Any) -> float:
        try:
            return float(entry.get("expires_at") or 0)
        except (TypeError, ValueError, AttributeError):
            return 0.0

    if prune:
        for bucket in ("codes", "tokens", "refresh"):
            data[bucket] = {k: v for k, v in data[bucket].items() if _exp(v) > now}


# --------------------------------------------------------------------------- #
# metadata (RFC 9728 / RFC 8414)
# --------------------------------------------------------------------------- #
def protected_resource_metadata(issuer: str, resource: str) -> dict[str, Any]:
    """RFC 9728 — tells the client where the authorization server lives."""
    return {
        "resource": resource,
        "authorization_servers": [issuer],
        "scopes_supported": list(SUPPORTED_SCOPES),
        "bearer_methods_supported": ["header"],
    }


def authorization_server_metadata(issuer: str) -> dict[str, Any]:
    """RFC 8414 — the authorization server's endpoints and capabilities."""
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "registration_endpoint": f"{issuer}/register",
        "scopes_supported": list(SUPPORTED_SCOPES),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


# --------------------------------------------------------------------------- #
# dynamic client registration (RFC 7591)
# --------------------------------------------------------------------------- #
def register_client(redirect_uris: list[str], client_name: str | None = None,
                    token_endpoint_auth_method: str = "none") -> dict[str, Any]:
    """Register a public client. Returns the client metadata incl. ``client_id``.

    Redirect URIs are validated up front (HTTPS or localhost only, per OAuth 2.1)
    and matched EXACTLY at authorize time — no wildcards, no prefixes.
    """
    uris = [str(u).strip() for u in (redirect_uris or []) if str(u).strip()]
    if not uris:
        raise ValueError("at least one redirect_uri is required")
    if len(uris) > MAX_REDIRECT_URIS:
        raise ValueError(f"too many redirect_uris (max {MAX_REDIRECT_URIS})")
    for uri in uris:
        if not _valid_redirect_uri(uri):
            raise ValueError(f"redirect_uri must be https:// or http://localhost: {uri}")
    with _locked():
        data = _load()
        if len(data["clients"]) >= MAX_CLIENTS:
            _expired(data)
            if len(data["clients"]) >= MAX_CLIENTS:
                raise ValueError(f"too many registered clients (max {MAX_CLIENTS})")
        client_id = "tlc_" + secrets.token_urlsafe(16)
        record = {
            "client_id": client_id,
            "client_name": str(client_name or "mcp-client").strip()[:120],
            "redirect_uris": uris,
            "token_endpoint_auth_method": "none",
            "created_at": utc_now(),
        }
        data["clients"][client_id] = record
        _save(data)
    return dict(record)


def _valid_redirect_uri(uri: str) -> bool:
    from urllib.parse import urlparse

    try:
        parsed = urlparse(uri)
    except ValueError:
        return False
    if parsed.scheme == "https":
        return bool(parsed.netloc) and not parsed.fragment
    if parsed.scheme == "http":
        host = (parsed.hostname or "").lower()
        return host in {"localhost", "127.0.0.1", "::1"} and not parsed.fragment
    return False


def get_client(client_id: str) -> dict[str, Any] | None:
    return _load()["clients"].get(str(client_id or ""))


# --------------------------------------------------------------------------- #
# authorization code + token issuance
# --------------------------------------------------------------------------- #
def validate_authorization_request(client_id: str, redirect_uri: str) -> dict[str, Any]:
    """Pre-consent validation. Returns the client, or raises ValueError.

    Per OAuth 2.1 the redirect_uri must match a registered value EXACTLY; an
    invalid client_id/redirect_uri must NOT redirect (it could be an attacker) —
    the caller renders an error page instead.
    """
    client = get_client(client_id)
    if client is None:
        raise ValueError("unknown client_id")
    if redirect_uri not in client.get("redirect_uris", []):
        raise ValueError("redirect_uri does not match a registered value")
    return client


def issue_code(*, client_id: str, redirect_uri: str, code_challenge: str,
               code_challenge_method: str, resource: str, scope: str | None,
               state: str | None) -> str:
    """Create a single-use authorization code (called only after consent)."""
    if code_challenge_method != "S256" or not code_challenge:
        raise ValueError("PKCE S256 code_challenge is required")
    with _locked():
        data = _load()
        _expired(data)
        if len(data["codes"]) >= MAX_CODES:
            raise ValueError("too many outstanding authorization codes")
        code = secrets.token_urlsafe(32)
        data["codes"][_digest(code)] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "resource": resource,
            "scope": scope or "tasklane",
            "state": state,
            "expires_at": _now() + AUTH_CODE_TTL_SECONDS,
        }
        _save(data)
    return code


def _mint_tokens(data: dict[str, Any], client_id: str, scope: str) -> dict[str, Any]:
    access = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(32)
    now = _now()
    data["tokens"][_digest(access)] = {
        "client_id": client_id, "scope": scope,
        "issued_at": now, "expires_at": now + ACCESS_TOKEN_TTL_SECONDS,
    }
    data["refresh"][_digest(refresh)] = {
        "client_id": client_id, "scope": scope,
        "issued_at": now, "expires_at": now + REFRESH_TOKEN_TTL_SECONDS,
    }
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL_SECONDS,
        "scope": scope,
    }


def exchange_code(*, code: str, code_verifier: str, client_id: str, redirect_uri: str) -> dict[str, Any]:
    """Authorization-code grant. Verifies PKCE, single-use, exact redirect match."""
    with _locked():
        data = _load()
        _expired(data)
        entry = data["codes"].pop(_digest(str(code or "")), None)
        if entry is None:
            raise ValueError("invalid or expired authorization code")
        if entry["client_id"] != client_id:
            raise ValueError("client_id mismatch")
        if entry["redirect_uri"] != redirect_uri:
            raise ValueError("redirect_uri mismatch")
        if not verify_pkce(entry["code_challenge"], entry["code_challenge_method"], str(code_verifier or "")):
            raise ValueError("PKCE verification failed")
        tokens = _mint_tokens(data, client_id, entry.get("scope") or "tasklane")
        _save(data)
    return tokens


def refresh_tokens(*, refresh_token: str, client_id: str) -> dict[str, Any]:
    """Refresh-token grant with rotation (public clients MUST rotate)."""
    with _locked():
        data = _load()
        _expired(data)
        old = data["refresh"].pop(_digest(str(refresh_token or "")), None)
        if old is None:
            raise ValueError("invalid or expired refresh token")
        if old["client_id"] != client_id:
            raise ValueError("client_id mismatch")
        tokens = _mint_tokens(data, client_id, old.get("scope") or "tasklane")
        _save(data)
    return tokens


def authenticate(access_token: str) -> dict[str, Any] | None:
    """Resource-server check: return the token's client record, or None.

    Read-only and fails closed. Expired tokens deny. (Pruning of the expired
    entry is left to the next write path so auth never blocks on the lock.)
    """
    text = str(access_token or "")
    if not text:
        return None
    try:
        data = _load()
    except Exception:  # noqa: BLE001 — unreadable store denies
        return None
    entry = data["tokens"].get(_digest(text))
    if entry is None:
        return None
    try:
        if float(entry.get("expires_at") or 0) <= _now():
            return None
    except (TypeError, ValueError):
        return None
    return {"client_id": entry.get("client_id"), "scope": entry.get("scope")}


def revoke_client_tokens(client_id: str) -> int:
    """Drop all tokens + codes for a client (e.g. on connector removal). Returns count."""
    with _locked():
        data = _load()
        removed = 0
        for bucket in ("tokens", "refresh", "codes"):
            keep = {k: v for k, v in data[bucket].items() if v.get("client_id") != client_id}
            removed += len(data[bucket]) - len(keep)
            data[bucket] = keep
        _save(data)
    return removed


def list_clients() -> list[dict[str, Any]]:
    return [dict(c) for c in _load()["clients"].values()]
