"""Unit tests for the OAuth 2.1 authorization/resource server (tasklane.oauth)."""

import base64
import hashlib
import os
import stat
import time

import pytest

from tasklane import oauth


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKLANE_HOME", str(tmp_path / "home"))
    return tmp_path


REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _register(home) -> str:
    return oauth.register_client([REDIRECT], "claude.ai")["client_id"]


def _code(client_id, challenge, **over) -> str:
    kw = dict(client_id=client_id, redirect_uri=REDIRECT, code_challenge=challenge,
              code_challenge_method="S256", resource="https://x/mcp", scope="tasklane", state="s1")
    kw.update(over)
    return oauth.issue_code(**kw)


# --------------------------------------------------------------------------- #
# metadata
# --------------------------------------------------------------------------- #
def test_protected_resource_metadata_shape():
    m = oauth.protected_resource_metadata("https://h", "https://h/mcp")
    assert m["resource"] == "https://h/mcp"
    assert m["authorization_servers"] == ["https://h"]


def test_authorization_server_metadata_shape():
    m = oauth.authorization_server_metadata("https://h")
    assert m["issuer"] == "https://h"
    assert m["authorization_endpoint"] == "https://h/authorize"
    assert m["token_endpoint"] == "https://h/token"
    assert m["registration_endpoint"] == "https://h/register"
    assert m["code_challenge_methods_supported"] == ["S256"]


# --------------------------------------------------------------------------- #
# DCR
# --------------------------------------------------------------------------- #
def test_register_requires_redirect_uri(home):
    with pytest.raises(ValueError, match="redirect_uri is required"):
        oauth.register_client([], "x")


def test_register_rejects_non_https_non_localhost(home):
    with pytest.raises(ValueError, match="https"):
        oauth.register_client(["http://evil.example.com/cb"], "x")


def test_register_allows_localhost_http(home):
    rec = oauth.register_client(["http://localhost:8976/callback"], "desktop")
    assert rec["client_id"].startswith("tlc_")
    assert rec["token_endpoint_auth_method"] == "none"


def test_register_no_secret_issued(home):
    rec = oauth.register_client([REDIRECT], "x")
    assert "client_secret" not in rec  # public client


def test_register_persists_mode_600(home):
    _register(home)
    assert stat.S_IMODE(os.stat(oauth.oauth_path()).st_mode) == 0o600


# --------------------------------------------------------------------------- #
# authorize validation
# --------------------------------------------------------------------------- #
def test_validate_unknown_client(home):
    with pytest.raises(ValueError, match="unknown client_id"):
        oauth.validate_authorization_request("nope", REDIRECT)


def test_validate_redirect_must_match_exactly(home):
    cid = _register(home)
    with pytest.raises(ValueError, match="redirect_uri"):
        oauth.validate_authorization_request(cid, REDIRECT + "/extra")


# --------------------------------------------------------------------------- #
# PKCE + code exchange
# --------------------------------------------------------------------------- #
def test_full_happy_path(home):
    cid = _register(home)
    verifier, challenge = _pkce()
    code = _code(cid, challenge)
    tokens = oauth.exchange_code(code=code, code_verifier=verifier, client_id=cid, redirect_uri=REDIRECT)
    assert tokens["token_type"] == "Bearer"
    assert tokens["access_token"] and tokens["refresh_token"]
    assert oauth.authenticate(tokens["access_token"])["client_id"] == cid


def test_code_is_single_use(home):
    cid = _register(home)
    verifier, challenge = _pkce()
    code = _code(cid, challenge)
    oauth.exchange_code(code=code, code_verifier=verifier, client_id=cid, redirect_uri=REDIRECT)
    with pytest.raises(ValueError, match="invalid or expired"):
        oauth.exchange_code(code=code, code_verifier=verifier, client_id=cid, redirect_uri=REDIRECT)


def test_wrong_verifier_rejected(home):
    cid = _register(home)
    _, challenge = _pkce()
    code = _code(cid, challenge)
    with pytest.raises(ValueError, match="PKCE"):
        oauth.exchange_code(code=code, code_verifier="wrong-verifier", client_id=cid, redirect_uri=REDIRECT)


def test_redirect_mismatch_rejected(home):
    cid = _register(home)
    verifier, challenge = _pkce()
    code = _code(cid, challenge)
    with pytest.raises(ValueError, match="redirect_uri mismatch"):
        oauth.exchange_code(code=code, code_verifier=verifier, client_id=cid, redirect_uri="https://claude.ai/other")


def test_client_mismatch_rejected(home):
    cid = _register(home)
    other = oauth.register_client([REDIRECT], "other")["client_id"]
    verifier, challenge = _pkce()
    code = _code(cid, challenge)
    with pytest.raises(ValueError, match="client_id mismatch"):
        oauth.exchange_code(code=code, code_verifier=verifier, client_id=other, redirect_uri=REDIRECT)


def test_issue_code_requires_s256(home):
    cid = _register(home)
    with pytest.raises(ValueError, match="S256"):
        _code(cid, "challenge", code_challenge_method="plain")


def test_expired_code_rejected(home, monkeypatch):
    cid = _register(home)
    verifier, challenge = _pkce()
    code = _code(cid, challenge)
    monkeypatch.setattr(oauth, "_now", lambda: time.time() + oauth.AUTH_CODE_TTL_SECONDS + 1)
    with pytest.raises(ValueError, match="invalid or expired"):
        oauth.exchange_code(code=code, code_verifier=verifier, client_id=cid, redirect_uri=REDIRECT)


# --------------------------------------------------------------------------- #
# refresh rotation
# --------------------------------------------------------------------------- #
def test_refresh_rotates_and_invalidates_old(home):
    cid = _register(home)
    verifier, challenge = _pkce()
    tokens = oauth.exchange_code(code=_code(cid, challenge), code_verifier=verifier, client_id=cid, redirect_uri=REDIRECT)
    rotated = oauth.refresh_tokens(refresh_token=tokens["refresh_token"], client_id=cid)
    assert rotated["refresh_token"] != tokens["refresh_token"]
    assert oauth.authenticate(rotated["access_token"])["client_id"] == cid
    with pytest.raises(ValueError, match="invalid or expired"):
        oauth.refresh_tokens(refresh_token=tokens["refresh_token"], client_id=cid)  # reuse denied


# --------------------------------------------------------------------------- #
# authenticate (resource server)
# --------------------------------------------------------------------------- #
def test_authenticate_denies_unknown_and_empty(home):
    assert oauth.authenticate("nope") is None
    assert oauth.authenticate("") is None


def test_authenticate_denies_expired_access(home, monkeypatch):
    cid = _register(home)
    verifier, challenge = _pkce()
    tokens = oauth.exchange_code(code=_code(cid, challenge), code_verifier=verifier, client_id=cid, redirect_uri=REDIRECT)
    monkeypatch.setattr(oauth, "_now", lambda: time.time() + oauth.ACCESS_TOKEN_TTL_SECONDS + 1)
    assert oauth.authenticate(tokens["access_token"]) is None


def test_authenticate_fails_closed_on_unreadable(home, monkeypatch):
    def boom():
        raise OSError("disk gone")
    monkeypatch.setattr(oauth, "_load", boom)
    assert oauth.authenticate("anything") is None


def test_revoke_client_tokens(home):
    cid = _register(home)
    verifier, challenge = _pkce()
    tokens = oauth.exchange_code(code=_code(cid, challenge), code_verifier=verifier, client_id=cid, redirect_uri=REDIRECT)
    assert oauth.authenticate(tokens["access_token"]) is not None
    removed = oauth.revoke_client_tokens(cid)
    assert removed >= 1
    assert oauth.authenticate(tokens["access_token"]) is None


def test_verify_pkce_helper():
    verifier, challenge = _pkce()
    assert oauth.verify_pkce(challenge, "S256", verifier) is True
    assert oauth.verify_pkce(challenge, "S256", "other") is False
    assert oauth.verify_pkce(challenge, "plain", verifier) is False
