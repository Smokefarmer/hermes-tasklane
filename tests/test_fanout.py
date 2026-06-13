"""Unit tests for fan-out parsing: proposed_tasks block -> capped draft jobs."""

import pytest

from tasklane.fanout import (
    PROPOSED_TASKS_CAP,
    create_proposed_drafts,
    extract_proposed_tasks_block,
    parse_proposed_tasks,
)
from tasklane.store import JobStore


@pytest.fixture()
def store(tmp_path):
    s = JobStore(root=tmp_path / "jobs")
    s.ensure_dirs()
    return s


def parent_record(job_id: str = "parent-1") -> dict:
    return {
        "id": job_id,
        "state": "completed",
        "spec": {
            "repo": {"path": "/tmp/example-repo", "key": "repo://example"},
            "branch": {"mode": "new-branch", "base_branch": "main", "work_branch": "feat/x"},
        },
    }


def _block(payload: str) -> str:
    return f"All done.\n\n```proposed_tasks\n{payload}\n```\n\nThanks."


# --------------------------------------------------------------------------- #
# extraction / parsing
# --------------------------------------------------------------------------- #
def test_extract_returns_none_without_block():
    assert extract_proposed_tasks_block("just a normal response") is None
    assert extract_proposed_tasks_block("") is None
    assert extract_proposed_tasks_block(None) is None


def test_parse_well_formed_list():
    entries = parse_proposed_tasks(_block('[{"title": "a", "body": "b"}]'))
    assert entries == [{"title": "a", "body": "b"}]


def test_parse_accepts_single_object():
    entries = parse_proposed_tasks(_block('{"title": "solo", "body": "b"}'))
    assert entries == [{"title": "solo", "body": "b"}]


def test_parse_no_block_returns_empty():
    assert parse_proposed_tasks("no fenced block here") == []


def test_parse_malformed_json_raises():
    with pytest.raises(ValueError):
        parse_proposed_tasks(_block("[{not valid json"))


def test_parse_drops_non_mapping_entries():
    entries = parse_proposed_tasks(_block('["string", 5, {"title": "ok", "body": "b"}]'))
    assert entries == [{"title": "ok", "body": "b"}]


# --------------------------------------------------------------------------- #
# draft creation
# --------------------------------------------------------------------------- #
def test_well_formed_block_creates_linked_drafts(store):
    parent = parent_record()
    payload = (
        '[{"title": "Investigate X", "body": "look at x", "type": "task-small", '
        '"allowed_paths": ["src/x"], "severity": "high"}, '
        '{"title": "Investigate Y", "body": "look at y"}]'
    )
    result = create_proposed_drafts(store, parent, _block(payload))

    assert result["created"] == ["parent-1-p01", "parent-1-p02"]
    assert result["error"] is None and result["skipped"] == 0

    first = store.get("parent-1-p01")
    assert first["state"] == "draft"
    assert first["spec"]["source"] == {"proposed_by": "parent-1", "severity": "high"}
    assert first["spec"]["repo"]["path"] == "/tmp/example-repo"
    assert first["spec"]["branch"]["mode"] == "detached-review"
    assert first["spec"]["delivery_mode"] == "report-only"
    assert first["spec"]["scope"]["allowed_paths"] == ["src/x"]

    # parent records the created draft ids as an event
    reasons = [e["event_type"] for e in store.events("parent-1")]
    assert "job_fanout_drafts_created" in reasons


def test_drafts_are_not_claimable(store):
    create_proposed_drafts(store, parent_record(), _block('[{"title": "t", "body": "b"}]'))
    # draft jobs must never be picked up by the worker
    assert store.claim_next(owner="worker-1") is None


def test_cap_enforced_and_extras_ignored(store):
    entries = ",".join(f'{{"title": "t{i}", "body": "b{i}"}}' for i in range(PROPOSED_TASKS_CAP + 5))
    result = create_proposed_drafts(store, parent_record(), _block(f"[{entries}]"))
    assert len(result["created"]) == PROPOSED_TASKS_CAP
    assert result["skipped"] == 5
    assert store.get(f"parent-1-p{PROPOSED_TASKS_CAP + 1:02d}") is None


def test_malformed_json_is_safe(store):
    result = create_proposed_drafts(store, parent_record(), _block("[{broken"))
    assert result["created"] == [] and result["error"] is not None
    reasons = [e["event_type"] for e in store.events("parent-1")]
    assert "job_fanout_parse_failed" in reasons


def test_no_block_creates_nothing(store):
    result = create_proposed_drafts(store, parent_record(), "completed normally, nothing proposed")
    assert result == {"created": [], "skipped": 0, "rejected": 0, "error": None}
    assert store.events("parent-1") == []


def test_bad_entry_skipped_without_aborting_batch(store):
    # the first entry has no body (invalid spec) -> rejected; the second still lands
    payload = '[{"title": "missing body"}, {"title": "good", "body": "b"}]'
    result = create_proposed_drafts(store, parent_record(), _block(payload))
    assert result["rejected"] == 1
    assert result["created"] == ["parent-1-p02"]
    assert store.get("parent-1-p01") is None
    assert store.get("parent-1-p02") is not None
    reasons = [e["event_type"] for e in store.events("parent-1")]
    assert "job_fanout_draft_rejected" in reasons
