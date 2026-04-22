from __future__ import annotations

import json
import subprocess
from pathlib import Path

from hermes_tasklane import cli
from hermes_tasklane.cli import command_init, command_reconcile, command_sync, load_config, load_state
from hermes_tasklane.dashboard import dashboard_state, job_detail


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


def init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tasklane@example.test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Tasklane Test"], cwd=path, check=True)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "tasklane/demo"], cwd=path, check=True, capture_output=True)


def failed_provider_job(repo: Path, *, allowed_paths: list[str] | None = None) -> dict:
    return {
        "attempt": 2,
        "last_error": "An error occurred while processing your request. Please include the request ID abc.",
        "spec": {
            "project": "Demo",
            "repo": {"key": f"repo://{repo}", "path": str(repo)},
            "request": {"title": "salvage me", "body": "Implement the demo task."},
            "branch": {
                "mode": "new-branch",
                "base_branch": "main",
                "work_branch": "tasklane/demo",
                "pr_target": "main",
            },
            "delivery_mode": "pull-request",
            "scope": {
                "allowed_paths": allowed_paths or ["README.md"],
                "denied_paths": [],
                "allow_unlisted_paths": False,
            },
        },
    }


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


def test_sync_preflight_blocks_unbounded_large_task(tmp_path: Path, capsys) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))
    capsys.readouterr()

    repo = tmp_path / "repo"
    repo.mkdir()
    task_file = task_root / "inbox" / "big.md"
    task_file.write_text(
        f"---\nrepo_path: {repo}\nbase_branch: development\nrequest_type: feature\n---\nImplement the whole season.\n",
        encoding="utf-8",
    )

    command_sync(cfg)

    output = json.loads(capsys.readouterr().out)
    assert output["actions"][0]["status"] == "preflight-blocked"
    assert output["actions"][0]["findings"][0]["code"] == "large-task-unbounded-scope"
    assert task_file.exists()
    assert (task_root / "inbox" / "big.md.preflight.json").exists()
    assert not list((hermes_home / "jobs" / "ready").glob("*.json"))


def test_sync_preflight_blocks_same_branch_multiple_roots(tmp_path: Path, capsys) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))
    capsys.readouterr()

    repo = tmp_path / "repo"
    repo.mkdir()
    (task_root / "inbox" / "one.md").write_text(
        f"---\nid: one\nrepo_path: {repo}\nbase_branch: development\ndelivery_group: s3-wave\nallowed_paths: apps/client\nrequest_type: feature\n---\nBuild one.\n",
        encoding="utf-8",
    )
    (task_root / "inbox" / "two.md").write_text(
        f"---\nid: two\nrepo_path: {repo}\nbase_branch: development\ndelivery_group: s3-wave\nallowed_paths: apps/server\nrequest_type: feature\n---\nBuild two.\n",
        encoding="utf-8",
    )

    command_sync(cfg)

    output = json.loads(capsys.readouterr().out)
    blocked = [action for action in output["actions"] if action["status"] == "preflight-blocked"]
    assert len(blocked) == 2
    assert {finding["code"] for action in blocked for finding in action["findings"]} == {"same-branch-multiple-roots"}
    assert not list((hermes_home / "jobs" / "ready").glob("*.json"))


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
    assert report["counts"]["blocked"] == 0
    assert report["counts_all"]["blocked"] == 1
    assert report["blocked"] == []
    assert report["ignored_blocked"][0]["id"] == "tasklane_obsolete"
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


def test_guarded_watch_restores_reconciled_failed_task_to_submitted(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    repo = tmp_path / "repo"
    repo.mkdir()
    failed_task = task_root / "failed" / "demo.md"
    failed_task.write_text(
        f"---\nid: demo-task\nrepo_path: {repo}\nbase_branch: main\n---\nDo the retryable task.\n",
        encoding="utf-8",
    )
    (task_root / "failed" / "demo.md.result.json").write_text(
        json.dumps({"job_id": "tasklane_retry", "state": "failed"}),
        encoding="utf-8",
    )
    write_job_record(
        hermes_home,
        "failed",
        "tasklane_retry",
        {
            "attempt": 1,
            "last_error": "An error occurred while processing your request. Please include the request ID abc.",
            "spec": {
                "project": "Demo",
                "repo": {"key": f"repo://{repo}"},
                "request": {"title": "retry me"},
                "branch": {"mode": "new-branch", "base_branch": "main"},
                "metadata": {"uid": "demo-task"},
            },
        },
    )

    report = cli.build_watch_report(cfg, mode="guarded", ignored_blocked=set(), check_gateway=False)
    actions = cli.apply_guarded_watch_actions(cfg, report)

    assert actions[0]["status"] == "retried"
    assert actions[0]["submitted_restored"]["task_uid"] == "demo-task"
    assert not failed_task.exists()
    assert not (task_root / "failed" / "demo.md.result.json").exists()
    restored_path = task_root / "submitted" / "demo.md"
    assert restored_path.exists()
    state = load_state(cfg)
    assert state["submitted"]["demo-task"]["source_path"] == str(restored_path)
    assert state["submitted"]["demo-task"]["job_id"] == "tasklane_retry"


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
            "last_error": "An error occurred while processing your request. You can retry your request, or contact us through our help center at help.openai.com if the error persists. Please include the request ID abc.",
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


def test_failed_provider_dirty_worktree_is_salvage_needed_not_retried(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    repo = tmp_path / "repo"
    init_git_repo(repo)
    (repo / "README.md").write_text("base\nchanged\n", encoding="utf-8")
    write_job_record(hermes_home, "failed", "tasklane_salvage", failed_provider_job(repo))
    cli.append_jsonl(
        cli.job_event_log_path(cfg, "tasklane_salvage"),
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "job_id": "tasklane_salvage",
            "event_type": "job_workspace_prepared",
            "state": "running",
            "metadata": {"worktree_path": str(repo)},
        },
    )

    report = cli.build_watch_report(cfg, mode="guarded", ignored_blocked=set(), check_gateway=False)
    actions = cli.apply_guarded_watch_actions(cfg, report)

    assert [problem["code"] for problem in report["problems"]] == ["salvage-needed"]
    assert actions[0]["status"] == "salvage-needed"
    assert actions[0]["inspection"]["dirty_files"] == ["README.md"]
    assert (hermes_home / "jobs" / "failed" / "tasklane_salvage.json").exists()
    assert not (hermes_home / "jobs" / "ready" / "tasklane_salvage.json").exists()


def test_guarded_watch_auto_salvage_commits_pushes_and_marks_completed(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    cfg.watch["auto_salvage"] = True
    cfg.watch["verification_commands"] = ["git diff --check"]
    command_init(cfg, str(config_path))

    repo = tmp_path / "repo"
    init_git_repo(repo)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/example/demo.git"], cwd=repo, check=True)
    (repo / "README.md").write_text("base\nchanged\n", encoding="utf-8")
    failed_task = task_root / "failed" / "demo.md"
    failed_task.write_text("---\nid: demo-task\n---\nImplement the demo task.\n", encoding="utf-8")
    (task_root / "failed" / "demo.md.result.json").write_text(
        json.dumps({"job_id": "tasklane_salvage", "state": "failed"}),
        encoding="utf-8",
    )
    write_job_record(hermes_home, "failed", "tasklane_salvage", failed_provider_job(repo))
    cli.append_jsonl(
        cli.job_event_log_path(cfg, "tasklane_salvage"),
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "job_id": "tasklane_salvage",
            "event_type": "job_workspace_prepared",
            "state": "running",
            "metadata": {"worktree_path": str(repo)},
        },
    )
    monkeypatch.setattr(cli, "push_branch", lambda worktree, branch: {"ok": True, "branch": branch})
    monkeypatch.setattr(cli, "find_pr", lambda owner, repo_name, branch: None)
    monkeypatch.setattr(
        cli,
        "create_pr",
        lambda owner, repo_name, **kwargs: {
            "number": 123,
            "url": "https://github.com/example/demo/pull/123",
            "title": kwargs["title"],
            "state": "open",
            "merged_at": None,
            "head_sha": "abc123",
            "branch": kwargs["branch"],
            "base_branch": kwargs["base_branch"],
        },
    )

    report = cli.build_watch_report(cfg, mode="guarded", ignored_blocked=set(), check_gateway=False)
    actions = cli.apply_guarded_watch_actions(cfg, report)

    assert actions[0]["status"] == "salvaged"
    assert actions[0]["pr"]["url"] == "https://github.com/example/demo/pull/123"
    assert not (hermes_home / "jobs" / "failed" / "tasklane_salvage.json").exists()
    completed_job = json.loads((hermes_home / "jobs" / "completed" / "tasklane_salvage.json").read_text(encoding="utf-8"))
    assert completed_job["state"] == "completed"
    assert completed_job["result"]["delivery_validation"]["pr"]["number"] == 123
    assert (task_root / "completed" / "demo.md").exists()
    completed_note = json.loads((task_root / "completed" / "demo.md.result.json").read_text(encoding="utf-8"))
    assert completed_note["state"] == "completed"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True)
    assert status.stdout == ""


def test_auto_salvage_runs_bootstrap_before_verification(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    cfg.watch["auto_salvage"] = True
    cfg.watch["bootstrap_commands"] = ["git config tasklane.bootstrapped true"]
    cfg.watch["verification_commands"] = ["test \"$(git config --get tasklane.bootstrapped)\" = true"]
    command_init(cfg, str(config_path))

    repo = tmp_path / "repo"
    init_git_repo(repo)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/example/demo.git"], cwd=repo, check=True)
    (repo / "README.md").write_text("base\nchanged\n", encoding="utf-8")
    write_job_record(hermes_home, "failed", "tasklane_salvage", failed_provider_job(repo))
    monkeypatch.setattr(cli, "push_branch", lambda worktree, branch: {"ok": True, "branch": branch})
    monkeypatch.setattr(cli, "find_pr", lambda owner, repo_name, branch: None)
    monkeypatch.setattr(
        cli,
        "create_pr",
        lambda owner, repo_name, **kwargs: {
            "number": 124,
            "url": "https://github.com/example/demo/pull/124",
            "title": kwargs["title"],
            "state": "open",
            "merged_at": None,
            "head_sha": "def456",
            "branch": kwargs["branch"],
            "base_branch": kwargs["base_branch"],
        },
    )

    report = cli.build_watch_report(cfg, mode="guarded", ignored_blocked=set(), check_gateway=False)
    actions = cli.apply_guarded_watch_actions(cfg, report)

    assert actions[0]["status"] == "salvaged"
    completed_job = json.loads((hermes_home / "jobs" / "completed" / "tasklane_salvage.json").read_text(encoding="utf-8"))
    assert completed_job["result"]["auto_salvage"]["bootstrap"][0]["ok"] is True
    assert completed_job["result"]["auto_salvage"]["verification"][1]["ok"] is True


def test_auto_salvage_accepts_matching_baseline_failure(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    cfg.watch["auto_salvage"] = True
    cfg.watch["baseline_verification"] = True
    cfg.watch["allow_matching_baseline_failures"] = True
    cfg.watch["verification_commands"] = ["printf 'shared failure\\n' >&2; exit 2"]
    command_init(cfg, str(config_path))

    repo = tmp_path / "repo"
    init_git_repo(repo)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/example/demo.git"], cwd=repo, check=True)
    (repo / "README.md").write_text("base\nchanged\n", encoding="utf-8")
    write_job_record(hermes_home, "failed", "tasklane_salvage", failed_provider_job(repo))
    monkeypatch.setattr(cli, "push_branch", lambda worktree, branch: {"ok": True, "branch": branch})
    monkeypatch.setattr(cli, "find_pr", lambda owner, repo_name, branch: None)
    monkeypatch.setattr(
        cli,
        "create_pr",
        lambda owner, repo_name, **kwargs: {
            "number": 125,
            "url": "https://github.com/example/demo/pull/125",
            "title": kwargs["title"],
            "state": "open",
            "merged_at": None,
            "head_sha": "fed789",
            "branch": kwargs["branch"],
            "base_branch": kwargs["base_branch"],
        },
    )

    report = cli.build_watch_report(cfg, mode="guarded", ignored_blocked=set(), check_gateway=False)
    actions = cli.apply_guarded_watch_actions(cfg, report)

    assert actions[0]["status"] == "salvaged"
    completed_job = json.loads((hermes_home / "jobs" / "completed" / "tasklane_salvage.json").read_text(encoding="utf-8"))
    accepted = [item for item in completed_job["result"]["auto_salvage"]["verification"] if item.get("accepted_baseline_failure")]
    assert accepted[0]["status"] == "accepted-baseline-failure"
    assert accepted[0]["baseline"]["matches_branch_output"] is True


def test_auto_salvage_blocks_scope_violations(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    cfg.watch["auto_salvage"] = True
    command_init(cfg, str(config_path))

    repo = tmp_path / "repo"
    init_git_repo(repo)
    (repo / "README.md").write_text("base\nchanged\n", encoding="utf-8")
    write_job_record(
        hermes_home,
        "failed",
        "tasklane_salvage",
        failed_provider_job(repo, allowed_paths=["docs"]),
    )

    report = cli.build_watch_report(cfg, mode="guarded", ignored_blocked=set(), check_gateway=False)
    actions = cli.apply_guarded_watch_actions(cfg, report)

    assert actions[0]["status"] == "needs-human"
    assert actions[0]["reason"] == "scope-violations"
    assert actions[0]["inspection"]["scope_violations"] == [{"code": "unlisted-path-changed", "path": "README.md"}]
    assert not (hermes_home / "jobs" / "failed" / "tasklane_salvage.json").exists()
    needs_human = json.loads((hermes_home / "jobs" / "needs-human" / "tasklane_salvage.json").read_text(encoding="utf-8"))
    assert needs_human["state"] == "needs-human"
    assert needs_human["needs_human_reason"] == "scope-violations"


def test_dashboard_state_groups_jobs_and_exposes_watch_health(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    write_job_record(
        hermes_home,
        "running",
        "tasklane_running",
        {
            "claimed_at": "2026-01-01T00:00:00+00:00",
            "spec": {
                "project": "Demo",
                "repo": {"key": "repo:///repo/demo"},
                "request": {"title": "running job"},
                "branch": {"mode": "new-branch", "base_branch": "main"},
            },
        },
    )
    cli.append_jsonl(
        cli.job_event_log_path(cfg, "tasklane_running"),
        {
            "timestamp": "2026-01-01T00:00:01+00:00",
            "event_type": "job_workspace_prepared",
            "state": "running",
            "reason": "isolated-worktree-ready",
            "metadata": {"worktree_path": "/tmp/tasklane_running"},
        },
    )
    session_dir = hermes_home / "sessions"
    session_dir.mkdir(parents=True)
    (session_dir / "session_job_tasklane_running.json").write_text(
        json.dumps(
            {
                "last_updated": "2026-01-01T00:00:02+00:00",
                "message_count": 2,
                "messages": [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "terminal",
                                    "arguments": json.dumps({"command": "npm test"}),
                                }
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "content": json.dumps({"output": "1 failed\npassword=secret", "exit_code": 1, "error": None}),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    write_job_record(
        hermes_home,
        "completed",
        "tasklane_completed",
        {
            "spec": {
                "project": "Demo",
                "repo": {"key": "repo:///repo/demo"},
                "request": {"title": "completed job"},
                "branch": {"mode": "new-branch", "base_branch": "main"},
            },
        },
    )

    state = dashboard_state(cfg)

    assert state["watch"]["counts"]["running"] == 1
    assert state["watch"]["counts"]["completed"] == 1
    assert state["jobs"]["running"][0]["id"] == "tasklane_running"
    assert state["jobs"]["completed"][0]["id"] == "tasklane_completed"
    assert state["current_runs"][0]["id"] == "tasklane_running"
    assert state["current_runs"][0]["workspace"]["path"] == "/tmp/tasklane_running"
    assert state["current_runs"][0]["latest_event"]["event_type"] == "job_workspace_prepared"
    assert state["current_runs"][0]["session"]["latest"]["exit_code"] == 1
    assert "password=<redacted>" in state["current_runs"][0]["session"]["latest"]["summary"]
    assert state["resources"]["cpu"]["cores"] >= 1
    assert "memory" in state["resources"]
    assert "task_root" in state["resources"]["disk"]
    assert state["tasklane"]["task_root"] == str(task_root)


def test_dashboard_hides_ignored_blocked_jobs_from_active_counts(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    cfg.watch["ignored_blocked_jobs"] = ["tasklane_obsolete"]
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

    state = dashboard_state(cfg)

    assert state["totals"]["blocked"] == 0
    assert state["watch"]["counts_all"]["blocked"] == 1
    assert state["watch"]["ignored_blocked"][0]["id"] == "tasklane_obsolete"
    assert state["jobs"]["blocked"] == []


def test_dashboard_exposes_needs_human_verification_summary(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    write_job_record(
        hermes_home,
        "needs-human",
        "tasklane_review",
        {
            "needs_human_reason": "verification-failed",
            "spec": {
                "project": "Demo",
                "repo": {"key": "repo:///repo/demo"},
                "request": {"title": "review job"},
                "branch": {"mode": "new-branch", "base_branch": "main"},
            },
            "metadata": {"tasklane_salvage": {"status": "needs-human", "reason": "verification-failed"}},
            "result": {
                "auto_salvage": {
                    "status": "needs-human",
                    "reason": "verification-failed",
                    "verification": [
                        {
                            "command": "npm run typecheck",
                            "phase": "verification",
                            "exit_code": 2,
                            "ok": False,
                            "output_tail": "typecheck failed",
                        }
                    ],
                    "changed_files": ["README.md"],
                }
            },
        },
    )

    state = dashboard_state(cfg)
    job = state["jobs"]["needs-human"][0]

    assert job["needs_human_reason"] == "verification-failed"
    assert job["verification"]["failed_verification"][0]["command"] == "npm run typecheck"
    assert job["verification"]["changed_files"] == ["README.md"]


def test_dashboard_job_detail_includes_event_log(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    write_job_record(
        hermes_home,
        "completed",
        "tasklane_detail",
        {
            "spec": {
                "project": "Demo",
                "repo": {"key": "repo:///repo/demo"},
                "request": {"title": "detail job"},
                "branch": {"mode": "new-branch", "base_branch": "main"},
            },
        },
    )
    cli.append_jsonl(
        cli.job_event_log_path(cfg, "tasklane_detail"),
        {"timestamp": "2026-01-01T00:00:00+00:00", "event_type": "job_completed", "state": "completed"},
    )

    detail = job_detail(cfg, "tasklane_detail")

    assert detail["job"]["id"] == "tasklane_detail"
    assert detail["events"][0]["event_type"] == "job_completed"
