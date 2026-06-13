"""Unit tests for the client-pairing (connection approval) module."""

import json
import stat
from datetime import datetime, timedelta, timezone

import pytest

from tasklane import pairing


@pytest.fixture()
def pairing_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKLANE_HOME", str(tmp_path))
    return tmp_path


def read_store(pairing_home) -> dict:
    return json.loads((pairing_home / "clients.json").read_text(encoding="utf-8"))


def backdate_pending(pairing_home, seconds: float) -> None:
    data = read_store(pairing_home)
    old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    for entry in data["pending"].values():
        entry["requested_at"] = old
    (pairing_home / "clients.json").write_text(json.dumps(data), encoding="utf-8")


def test_request_approve_authenticate_happy_path(pairing_home):
    result = pairing.request_pairing("My Laptop")
    assert result["status"] == "pending"
    assert result["client_id"].startswith("my-laptop-")
    assert len(result["pairing_code"]) == 8
    assert set(result["pairing_code"]) <= set(pairing.PAIRING_CODE_ALPHABET)

    # Not authenticated while still pending.
    assert pairing.authenticate(result["token"]) is None

    approved = pairing.approve_client(result["pairing_code"])
    assert approved["client_id"] == result["client_id"]
    assert approved["approved_at"] is not None
    assert "token_sha256" not in approved

    record = pairing.authenticate(result["token"])
    assert record is not None
    assert record["client_id"] == result["client_id"]
    assert record["name"] == "My Laptop"
    assert record["last_seen_at"] is not None
    assert "token_sha256" not in record


def test_approve_unknown_code_raises(pairing_home):
    with pytest.raises(ValueError):
        pairing.approve_client("ZZZZZZZZ")


def test_reject_removes_pending_and_client(pairing_home):
    result = pairing.request_pairing("intruder")
    rejected = pairing.reject_client(result["pairing_code"])
    assert rejected["client_id"] == result["client_id"]
    assert pairing.pending_requests() == []
    assert pairing.list_clients() == []
    assert pairing.authenticate(result["token"]) is None
    with pytest.raises(ValueError):
        pairing.reject_client(result["pairing_code"])


def test_revoke_blocks_authentication(pairing_home):
    result = pairing.request_pairing("phone")
    pairing.approve_client(result["pairing_code"])
    assert pairing.authenticate(result["token"]) is not None

    revoked = pairing.revoke_client(result["client_id"])
    assert revoked["revoked_at"] is not None
    assert pairing.authenticate(result["token"]) is None
    with pytest.raises(ValueError):
        pairing.revoke_client("no-such-client")


def test_authenticate_wrong_or_garbage_token(pairing_home):
    result = pairing.request_pairing("desk")
    pairing.approve_client(result["pairing_code"])
    assert pairing.authenticate("not-a-real-token") is None
    assert pairing.authenticate("") is None
    assert pairing.authenticate(result["token"] + "x") is None


def test_pending_ttl_expiry(pairing_home):
    result = pairing.request_pairing("stale")
    backdate_pending(pairing_home, seconds=901)

    pruned = pairing.prune_pending()
    assert pruned == [result["pairing_code"]]
    assert pairing.pending_requests() == []
    # The unapproved client record is dropped with its pending request.
    assert pairing.list_clients() == []
    with pytest.raises(ValueError):
        pairing.approve_client(result["pairing_code"])


def test_pending_requests_prunes_expired(pairing_home):
    pairing.request_pairing("stale")
    backdate_pending(pairing_home, seconds=901)
    assert pairing.pending_requests() == []


def test_request_pairing_prunes_expired_before_cap(pairing_home):
    for index in range(pairing.PENDING_CAP):
        pairing.request_pairing(f"old-{index}")
    backdate_pending(pairing_home, seconds=901)
    # Expired entries no longer count against the cap.
    result = pairing.request_pairing("fresh")
    assert result["status"] == "pending"


def test_pending_cap(pairing_home):
    for index in range(pairing.PENDING_CAP):
        pairing.request_pairing(f"client-{index}")
    with pytest.raises(ValueError):
        pairing.request_pairing("one-too-many")


def test_cleartext_token_never_persisted(pairing_home):
    import hashlib

    result = pairing.request_pairing("secret-holder")
    pairing.approve_client(result["pairing_code"])
    pairing.authenticate(result["token"])

    raw = (pairing_home / "clients.json").read_text(encoding="utf-8")
    assert result["token"] not in raw
    expected_hash = hashlib.sha256(result["token"].encode("utf-8")).hexdigest()
    assert expected_hash in raw


def test_clients_file_mode_600(pairing_home):
    pairing.request_pairing("perms")
    mode = stat.S_IMODE((pairing_home / "clients.json").stat().st_mode)
    assert mode == 0o600


def test_list_clients_and_pending_have_no_hashes(pairing_home):
    result = pairing.request_pairing("listed")
    pendings = pairing.pending_requests()
    assert len(pendings) == 1
    assert pendings[0]["pairing_code"] == result["pairing_code"]
    assert pendings[0]["client_id"] == result["client_id"]

    pairing.approve_client(result["pairing_code"])
    clients = pairing.list_clients()
    assert len(clients) == 1
    assert clients[0]["client_id"] == result["client_id"]
    assert all("token_sha256" not in client for client in clients)


def test_request_pairing_rejects_blank_name(pairing_home):
    with pytest.raises(ValueError):
        pairing.request_pairing("   ")
