"""Tests for the draft approval flow: drafts are not runnable until approved.

Covers the store-level guarantee (claim_next skips drafts) and the audited MCP
tools (list_drafts / approve_draft / approve_all_drafts / reject_draft).
"""

import pytest

from tasklane import mcp_server
from tasklane.store import JobStore


def make_spec(job_id: str = "draft-1", **overrides):
    spec = {
        "id": job_id,
        "repo": {"path": "/tmp/example-repo"},
        "request": {"type": "task", "title": "t", "body": "b"},
        "branch": {"mode": "detached-review"},
        "delivery_mode": "report-only",
    }
    spec.update(overrides)
    return spec


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A JobStore whose TASKLANE_HOME is isolated, so the MCP tools' own _store()
    resolves to the same root."""
    monkeypatch.setenv("TASKLANE_HOME", str(tmp_path / "home"))
    s = JobStore()
    s.ensure_dirs()
    return s


# --------------------------------------------------------------------------- #
# store-level: drafts are not claimable
# --------------------------------------------------------------------------- #
def test_draft_is_not_claimable(store):
    record = store.put(make_spec(), state="draft")
    assert record["state"] == "draft"
    assert store.claim_next(owner="worker-1") is None


def test_approved_draft_becomes_claimable(store):
    store.put(make_spec(), state="draft")
    result = mcp_server.approve_draft(job_id="draft-1")
    assert result == {"id": "draft-1", "state": "ready"}

    claimed = store.claim_next(owner="worker-1")
    assert claimed is not None and claimed["id"] == "draft-1"
    assert claimed["state"] == "running"

    events = [e for e in store.events("draft-1") if e["reason"] == "draft-approved"]
    assert events, "approval should be audited as a draft-approved event"


# --------------------------------------------------------------------------- #
# MCP tools
# --------------------------------------------------------------------------- #
def test_list_drafts_only_returns_drafts(store):
    store.put(make_spec("draft-a"), state="draft")
    store.put(make_spec("ready-b"))  # default state=ready
    ids = {row["id"] for row in mcp_server.list_drafts()}
    assert ids == {"draft-a"}


def test_reject_draft_goes_failed(store):
    store.put(make_spec(), state="draft")
    result = mcp_server.reject_draft(job_id="draft-1", reason="not worth it")
    assert result == {"id": "draft-1", "state": "failed"}
    record = store.get("draft-1")
    assert record["state"] == "failed"
    assert record["last_error"] == "not worth it"
    assert store.claim_next(owner="worker-1") is None
    events = [e for e in store.events("draft-1") if e["reason"] == "draft-rejected"]
    assert events


def test_approve_all_drafts(store):
    store.put(make_spec("d1"), state="draft")
    store.put(make_spec("d2"), state="draft")
    store.put(make_spec("r1"))  # ready, must be untouched
    result = mcp_server.approve_all_drafts()
    assert set(result["approved"]) == {"d1", "d2"}
    assert result["count"] == 2
    assert store.get("d1")["state"] == "ready"
    assert store.get("d2")["state"] == "ready"
    assert mcp_server.list_drafts() == []


def test_approve_non_draft_errors(store):
    store.put(make_spec("ready-x"))  # state=ready, not a draft
    result = mcp_server.approve_draft(job_id="ready-x")
    assert "error" in result and "not a draft" in result["error"]
    assert store.get("ready-x")["state"] == "ready"


def test_reject_non_draft_errors(store):
    store.put(make_spec("ready-y"))
    result = mcp_server.reject_draft(job_id="ready-y")
    assert "error" in result and "not a draft" in result["error"]
    assert store.get("ready-y")["state"] == "ready"


def test_approve_missing_job_errors(store):
    result = mcp_server.approve_draft(job_id="nope")
    assert "error" in result and "not found" in result["error"]
