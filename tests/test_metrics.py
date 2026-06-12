"""Unit tests for telemetry: CLI metrics extraction, merging, aggregation, budget."""

from datetime import datetime, timedelta, timezone

import pytest

from tasklane.metrics import aggregate, merge_metrics, run_metrics, spend_last_24h
from tasklane.runner import _parse_result
from tasklane.store import JobStore


def make_spec(job_id: str = "job-1"):
    return {
        "id": job_id,
        "repo": {"path": "/tmp/example-repo"},
        "request": {"type": "task", "title": "t", "body": "b"},
        "branch": {"mode": "detached-review"},
        "delivery_mode": "report-only",
    }


@pytest.fixture()
def store(tmp_path):
    s = JobStore(root=tmp_path / "jobs")
    s.ensure_dirs()
    return s


CLI_JSON = (
    '{"type":"result","is_error":false,"result":"done","total_cost_usd":0.12,'
    '"duration_ms":4000,"num_turns":5,"session_id":"s1",'
    '"usage":{"input_tokens":2000,"output_tokens":500,'
    '"cache_read_input_tokens":100,"cache_creation_input_tokens":50}}'
)


def test_parse_result_extracts_metrics():
    parsed = _parse_result(CLI_JSON, "", 0)
    assert parsed["error"] is None
    m = parsed["metrics"]
    assert m["cost_usd"] == 0.12
    assert m["input_tokens"] == 2000
    assert m["output_tokens"] == 500
    assert m["num_turns"] == 5
    assert m["session_id"] == "s1"


def test_parse_result_metrics_survive_error():
    err_json = CLI_JSON.replace('"is_error":false', '"is_error":true')
    parsed = _parse_result(err_json, "", 0)
    assert parsed["error"]
    assert parsed["metrics"]["cost_usd"] == 0.12


def test_parse_result_without_telemetry_fields():
    parsed = _parse_result('{"result":"ok","is_error":false}', "", 0)
    assert parsed["metrics"] == {}


def test_merge_metrics_sums_runs_and_attempts():
    first = {"cost_usd": 0.10, "input_tokens": 100, "runs": 1, "wall_seconds": 30}
    second = {"cost_usd": 0.05, "input_tokens": 50, "runs": 1, "session_id": "s2"}
    merged = merge_metrics(first, second)
    assert merged["cost_usd"] == 0.15
    assert merged["input_tokens"] == 150
    assert merged["runs"] == 2
    assert merged["wall_seconds"] == 30
    assert merged["session_id"] == "s2"
    assert merged["recorded_at"]


def test_merge_metrics_handles_garbage():
    merged = merge_metrics(None, {}, {"cost_usd": "not-a-number", "runs": True})
    assert merged["cost_usd"] == 0
    assert merged["runs"] == 0


def test_run_metrics_counts_one_run():
    assert run_metrics({"metrics": {"cost_usd": 0.01}})["runs"] == 1
    assert run_metrics(None)["runs"] == 1


def test_spend_last_24h_windows_correctly(store):
    now = datetime.now(timezone.utc)
    recent = store.put(make_spec("recent"))
    old = store.put(make_spec("old"))
    store.transition(recent["id"], "completed", reason="t",
                     updates={"metrics": {"cost_usd": 1.5, "recorded_at": now.isoformat()}})
    store.transition(old["id"], "completed", reason="t",
                     updates={"metrics": {"cost_usd": 9.0, "recorded_at": (now - timedelta(hours=30)).isoformat()}})
    assert spend_last_24h(store, now=now) == 1.5


def test_aggregate_report(store):
    now = datetime.now(timezone.utc)
    record = store.put(make_spec("agg-1"))
    store.transition(record["id"], "completed", reason="t", updates={
        "metrics": {"cost_usd": 0.25, "input_tokens": 100, "output_tokens": 20,
                    "runs": 2, "wall_seconds": 45, "recorded_at": now.isoformat()}})
    store.put(make_spec("agg-2"))  # ready, no metrics
    report = aggregate(store, now=now)
    assert report["counts"] == {"completed": 1, "ready": 1}
    assert report["jobs_with_metrics"] == 1
    assert report["totals"]["cost_usd"] == 0.25
    assert report["spend_24h_usd"] == 0.25
    assert report["avg_wall_seconds"] == 45.0
