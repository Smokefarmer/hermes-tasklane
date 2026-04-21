from __future__ import annotations

import json
from pathlib import Path

from hermes_tasklane import cli
from hermes_tasklane.cli import command_init, command_reconcile, command_sync, load_config, load_state


def write_job_record(hermes_home: Path, state: str, job_id: str, payload: dict) -> Path:
    path = hermes_home / "jobs" / state / f"{job_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"id": job_id, "state": state, **payload}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_config(path: Path, *, hermes_home: Path, task_root: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "hermes_home": str(hermes_home),
                "task_root": str(task_root),
                "poll_repo_idle": True,
                "max_pending_per_repo": 1,
            }
        )
    )


def test_init_writes_example_outside_inbox(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))

    command_init(cfg, str(config_path))

    assert (task_root / "examples" / "example-task.md").exists()
    assert not (task_root / "inbox" / "example-task.md").exists()


def test_sync_moves_inbox_task_to_submitted_and_writes_jobstore_record(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    repo = tmp_path / "repo"
    repo.mkdir()
    task_file = task_root / "inbox" / "demo.md"
    task_file.write_text(
        f"---\nrepo_path: {repo}\nbranch_base: main\nproject: Demo\n---\nImplement the demo task.\n",
        encoding="utf-8",
    )

    command_sync(cfg)

    submitted_task = task_root / "submitted" / "demo.md"
    assert submitted_task.exists()
    ready = list((hermes_home / "jobs" / "ready").glob("*.json"))
    assert len(ready) == 1
    payload = json.loads(ready[0].read_text(encoding="utf-8"))
    assert payload["state"] == "ready"
    assert payload["spec"]["request"]["body"] == "Implement the demo task."
    assert payload["spec"]["request"]["type"] == "task-small"
    assert payload["spec"]["branch"]["mode"] == "new-branch"
    assert payload["spec"]["branch"]["base_branch"] == "main"
    assert payload["spec"]["branch"]["pr_target"] == "main"
    assert payload["spec"]["delivery_mode"] == "pull-request"
    state = load_state(cfg)
    assert len(state["submitted"]) == 1
    entry = next(iter(state["submitted"].values()))
    assert entry["job_id"] == payload["id"]


def test_sync_supports_scope_and_mode_frontmatter(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    repo = tmp_path / "repo"
    repo.mkdir()
    task_file = task_root / "inbox" / "demo.md"
    task_file.write_text(
        f"---\nrepo_path: {repo}\nbase_branch: development\nwork_branch: tasklane/demo\nrequest_type: feature\ndelivery_mode: pr\nallowed_paths: README.md, docs/usage.md\nallow_unlisted_paths: false\nreview_loops: 2\nsecurity_review: false\n---\nImplement the demo task.\n",
        encoding="utf-8",
    )

    command_sync(cfg)

    ready = list((hermes_home / "jobs" / "ready").glob("*.json"))
    payload = json.loads(ready[0].read_text(encoding="utf-8"))
    spec = payload["spec"]
    assert spec["request"]["type"] == "feature-large"
    assert spec["branch"]["work_branch"] == "tasklane/demo"
    assert spec["scope"]["allowed_paths"] == ["README.md", "docs/usage.md"]
    assert spec["scope"]["allow_unlisted_paths"] is False
    assert spec["pipeline"]["budgets"]["review_loops"] == 2
    assert spec["pipeline"]["security_review"] is False


def test_sync_applies_default_telegram_source_from_config(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "hermes_home": str(hermes_home),
                "task_root": str(task_root),
                "poll_repo_idle": True,
                "max_pending_per_repo": 1,
                "default_platform": "telegram",
                "default_chat_id": "-1001234567890",
                "default_thread_id": "42",
            }
        )
    )
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    repo = tmp_path / "repo"
    repo.mkdir()
    task_file = task_root / "inbox" / "demo.md"
    task_file.write_text(
        f"---\nrepo_path: {repo}\nbranch_base: main\nproject: Demo\n---\nImplement the demo task.\n",
        encoding="utf-8",
    )

    command_sync(cfg)

    ready = list((hermes_home / "jobs" / "ready").glob("*.json"))
    payload = json.loads(ready[0].read_text(encoding="utf-8"))
    assert payload["spec"]["source"]["type"] == "telegram"
    assert payload["spec"]["source"]["chat_id"] == "-1001234567890"
    assert payload["spec"]["source"]["thread_id"] == "42"


def test_sync_resolves_dependencies_and_delivery_group_branch(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    repo = tmp_path / "repo"
    repo.mkdir()
    (task_root / "inbox" / "one.md").write_text(
        f"---\nid: feature-one\nrepo_path: {repo}\nbase_branch: development\ndelivery_group: checkout-v2\n---\nBuild part one.\n",
        encoding="utf-8",
    )
    (task_root / "inbox" / "two.md").write_text(
        f"---\nid: feature-two\nrepo_path: {repo}\nbase_branch: development\ndelivery_group: checkout-v2\ndepends_on: feature-one\n---\nBuild part two.\n",
        encoding="utf-8",
    )

    command_sync(cfg)

    records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((hermes_home / "jobs" / "ready").glob("*.json"))]
    by_title = {item["spec"]["request"]["title"]: item for item in records}
    first = by_title["one"]
    second = by_title["two"]
    assert first["spec"]["branch"]["work_branch"] == "tasklane/checkout-v2"
    assert second["spec"]["branch"]["work_branch"] == "tasklane/checkout-v2"
    assert second["spec"]["dependencies"] == [first["id"]]
    assert second["spec"]["metadata"]["delivery_group"] == "checkout-v2"


def test_sync_resolves_dependencies_from_previously_submitted_active_tasks(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    repo = tmp_path / "repo"
    repo.mkdir()
    (task_root / "inbox" / "source.md").write_text(
        f"---\nid: source-pack\nrepo_path: {repo}\nbase_branch: development\n---\nCreate source pack.\n",
        encoding="utf-8",
    )

    command_sync(cfg)

    ready_records = list((hermes_home / "jobs" / "ready").glob("*.json"))
    assert len(ready_records) == 1
    source_record = json.loads(ready_records[0].read_text(encoding="utf-8"))

    (task_root / "inbox" / "audit.md").write_text(
        f"---\nid: domain-audit\nrepo_path: {repo}\nbase_branch: development\ndepends_on: source-pack\n---\nAudit one domain.\n",
        encoding="utf-8",
    )

    command_sync(cfg)

    child_records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (hermes_home / "jobs" / "ready").glob("*.json")
    ]
    by_title = {item["spec"]["request"]["title"]: item for item in child_records}
    assert set(by_title) == {"source", "audit"}
    assert by_title["audit"]["spec"]["dependencies"] == [source_record["id"]]


def test_existing_branch_delivery_group_derives_work_branch(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    repo = tmp_path / "repo"
    repo.mkdir()
    task_file = task_root / "inbox" / "final.md"
    task_file.write_text(
        f"---\nid: checkout-final\nrepo_path: {repo}\nbase_branch: development\nbranch_mode: existing-branch\ndelivery_group: checkout-v2\ndelivery_mode: pull-request\n---\nOpen the final PR.\n",
        encoding="utf-8",
    )

    command_sync(cfg)

    ready = list((hermes_home / "jobs" / "ready").glob("*.json"))
    payload = json.loads(ready[0].read_text(encoding="utf-8"))
    assert payload["spec"]["branch"]["mode"] == "existing-branch"
    assert payload["spec"]["branch"]["base_branch"] == "development"
    assert payload["spec"]["branch"]["work_branch"] == "tasklane/checkout-v2"
    assert payload["spec"]["branch"]["pr_target"] == "development"


def test_detached_review_preserves_base_branch_for_runner_worktree(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    repo = tmp_path / "repo"
    repo.mkdir()
    task_file = task_root / "inbox" / "audit.md"
    task_file.write_text(
        f"---\nrepo_path: {repo}\nbase_branch: feat/audit-source\nbranch_mode: detached-review\ndelivery_mode: report-only\n---\nAudit only.\n",
        encoding="utf-8",
    )

    command_sync(cfg)

    ready = list((hermes_home / "jobs" / "ready").glob("*.json"))
    payload = json.loads(ready[0].read_text(encoding="utf-8"))
    assert payload["spec"]["branch"]["mode"] == "detached-review"
    assert payload["spec"]["branch"]["base_branch"] == "feat/audit-source"
    assert payload["spec"]["delivery_mode"] == "report-only"


def test_reconcile_moves_completed_job_to_completed(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    submitted_task = task_root / "submitted" / "demo.md"
    submitted_task.parent.mkdir(parents=True, exist_ok=True)
    submitted_task.write_text("demo task\n", encoding="utf-8")

    job_id = "tasklane_done"
    job_payload = {
        "id": job_id,
        "state": "completed",
        "spec": {"repo": {"key": "repo:///tmp/demo"}, "request": {"title": "Demo"}},
        "result": {"final_response": "done"},
    }
    (hermes_home / "jobs" / "completed").mkdir(parents=True, exist_ok=True)
    (hermes_home / "jobs" / "completed" / f"{job_id}.json").write_text(json.dumps(job_payload), encoding="utf-8")

    state = {
        "submitted": {
            "demo-uid": {
                "source_path": str(submitted_task),
                "original_name": "demo.md",
                "job_id": job_id,
                "repo_key": "repo:///tmp/demo",
                "submitted_at": "2026-01-01T00:00:00+00:00",
            }
        }
    }
    (task_root / "state.json").write_text(json.dumps(state), encoding="utf-8")

    command_reconcile(cfg)

    assert not load_state(cfg)["submitted"]
    assert (task_root / "completed" / "demo.md").exists()
    result = json.loads((task_root / "completed" / "demo.md.result.json").read_text(encoding="utf-8"))
    assert result["job_id"] == job_id
    assert result["state"] == "completed"


def test_reconcile_keeps_pending_delivery_run_submitted(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    submitted_task = task_root / "submitted" / "demo.md"
    submitted_task.parent.mkdir(parents=True, exist_ok=True)
    submitted_task.write_text("demo task\n", encoding="utf-8")

    run_id = "tasklane_demo_pending"
    run_payload = {
        "id": run_id,
        "kind": "coding_task",
        "state": "blocked",
        "repo": {"key": "repo:///tmp/demo", "path": "/tmp/demo", "working_branch": "feat/demo"},
        "workflow": {"current_stage": "monitoring_ci", "issue_id": None, "stage_history": []},
        "blocked_reason": "ci-pending",
        "metadata": {},
    }
    (hermes_home / "runs").mkdir(parents=True, exist_ok=True)
    (hermes_home / "runs" / f"{run_id}.json").write_text(json.dumps(run_payload), encoding="utf-8")

    state = {
        "submitted": {
            "demo-uid": {
                "source_path": str(submitted_task),
                "original_name": "demo.md",
                "run_id": run_id,
                "queue_file": "/tmp/demo.json",
                "repo_key": "repo:///tmp/demo",
                "submitted_at": "2026-01-01T00:00:00+00:00",
            }
        }
    }
    (task_root / "state.json").write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr(cli, "reconcile_delivery", lambda cfg, rid, payload: {"status": "blocked", "ci": {"status": "pending"}})

    command_reconcile(cfg)

    post_state = load_state(cfg)
    assert "demo-uid" in post_state["submitted"]
    assert submitted_task.exists()
    assert not list((task_root / "failed").glob("demo.md"))


def test_watch_flags_base_branch_policy_mismatch(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    write_job_record(
        hermes_home,
        "ready",
        "tasklane_alvin",
        {
            "spec": {
                "project": "Alvin",
                "repo": {"key": "repo:///repo/alvin", "path": "/repo/alvin"},
                "request": {"title": "audit"},
                "branch": {"mode": "detached-review", "base_branch": "feat/anlageverzeichnis"},
                "delivery_mode": "report-only",
            }
        },
    )

    report = cli.build_watch_report(cfg, expected_base={"Alvin": "develop"}, ignored_blocked=set(), check_gateway=False)

    assert report["health"] == "warning"
    assert [problem["code"] for problem in report["problems"]] == ["base-branch-mismatch"]


def test_watch_ignores_known_blocked_job(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    write_job_record(
        hermes_home,
        "blocked",
        "tasklane_obsolete",
        {
            "last_error": "superseded",
            "spec": {
                "project": "Treasure Hunter",
                "repo": {"key": "repo:///repo/th"},
                "request": {"title": "obsolete"},
                "branch": {"mode": "new-branch", "base_branch": "development"},
            },
        },
    )

    report = cli.build_watch_report(cfg, ignored_blocked={"tasklane_obsolete"}, check_gateway=False)

    assert report["health"] == "ok"
    assert report["problems"] == []
    assert report["notices"][0]["code"] == "blocked-ignored"


def test_watch_flags_stale_running_dead_claimant(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))
    monkeypatch.setattr(cli, "process_is_alive", lambda pid: False)

    write_job_record(
        hermes_home,
        "running",
        "tasklane_stale",
        {
            "claimed_at": "2000-01-01T00:00:00+00:00",
            "claimed_by": "gateway-999999",
            "spec": {
                "project": "Demo",
                "repo": {"key": "repo:///repo/demo"},
                "request": {"title": "long run"},
                "branch": {"mode": "new-branch", "base_branch": "main"},
            },
        },
    )

    report = cli.build_watch_report(cfg, stale_running_minutes=1, ignored_blocked=set(), check_gateway=False)
    codes = {problem["code"] for problem in report["problems"]}

    assert report["health"] == "critical"
    assert {"running-stale", "running-dead-claimant"} <= codes


def test_guarded_watch_retries_safe_transient_failed_job(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    write_job_record(
        hermes_home,
        "failed",
        "tasklane_retry",
        {
            "attempt": 1,
            "last_error": "temporary timeout while connecting",
            "spec": {
                "project": "Demo",
                "repo": {"key": "repo:///repo/demo"},
                "request": {"title": "retry me"},
                "branch": {"mode": "new-branch", "base_branch": "main"},
            },
        },
    )
    report = cli.build_watch_report(cfg, mode="guarded", ignored_blocked=set(), check_gateway=False)

    actions = cli.apply_guarded_watch_actions(cfg, report)

    assert actions == [{"job_id": "tasklane_retry", "status": "retried", "from": str(hermes_home / "jobs" / "failed" / "tasklane_retry.json"), "to": str(hermes_home / "jobs" / "ready" / "tasklane_retry.json")}]
    assert not (hermes_home / "jobs" / "failed" / "tasklane_retry.json").exists()
    ready = json.loads((hermes_home / "jobs" / "ready" / "tasklane_retry.json").read_text(encoding="utf-8"))
    assert ready["state"] == "ready"
    assert ready["last_error"] is None


def test_safe_retry_classifier_accepts_provider_500_and_rejects_dirty_worktree() -> None:
    ok, reason = cli.safe_to_retry(
        {
            "attempt": 1,
            "last_error": "HTTP 500: The server had an error processing your request. Please include the request ID abc.",
        },
        3,
    )
    assert ok is True
    assert reason == "safe-transient-error"

    ok, reason = cli.safe_to_retry(
        {
            "attempt": 1,
            "last_error": "worktree has uncommitted changes after agent run",
        },
        3,
    )
    assert ok is False
    assert reason == "unsafe-error"
