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


def complete_ready_job(hermes_home: Path, job_id: str, result: dict) -> None:
    ready_path = hermes_home / "jobs" / "ready" / f"{job_id}.json"
    payload = json.loads(ready_path.read_text(encoding="utf-8"))
    ready_path.unlink()
    payload["state"] = "completed"
    payload["updated_at"] = "2026-05-08T00:00:00+00:00"
    payload["completed_at"] = "2026-05-08T00:00:00+00:00"
    payload["result"] = result
    completed_path = hermes_home / "jobs" / "completed" / f"{job_id}.json"
    completed_path.parent.mkdir(parents=True, exist_ok=True)
    completed_path.write_text(json.dumps(payload), encoding="utf-8")


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


def wave_issue(number: int, title: str, body: str = "", labels: list[str] | None = None, milestone: str | None = None) -> dict:
    return {
        "number": number,
        "url": f"https://github.com/example/demo/issues/{number}",
        "title": title,
        "body": body,
        "labels": labels or [],
        "milestone": milestone,
    }


def wave_pr(number: int, branch: str, title: str = "Tasklane PR", body: str = "") -> dict:
    return {
        "number": number,
        "url": f"https://github.com/example/demo/pull/{number}",
        "title": title,
        "body": body,
        "state": "open",
        "head_branch": branch,
        "base_branch": "development",
        "draft": False,
    }


def stub_wave_github(monkeypatch, *, issues: list[dict], prs: list[dict] | None = None) -> None:
    monkeypatch.setattr(cli, "git_remote_for_repo", lambda repo_path: ("example", "demo"))
    monkeypatch.setattr(cli, "github_open_pull_requests", lambda owner, repo: prs or [])
    monkeypatch.setattr(cli, "github_open_issues", lambda owner, repo, limit: issues[:limit])




def demo_job(repo: Path, *, title: str = "demo", deps: list[str] | None = None) -> dict:
    return {
        "spec": {
            "project": "Demo",
            "repo": {"key": f"repo://{repo}", "path": str(repo)},
            "request": {"title": title, "body": "Implement demo."},
            "branch": {"mode": "new-branch", "base_branch": "main", "work_branch": "tasklane/demo", "pr_target": "main"},
            "delivery_mode": "pull-request",
            "dependencies": deps or [],
        }
    }


def test_job_liveness_summary_classifies_claimants_and_dependencies(tmp_path: Path) -> None:
    live = cli.job_liveness_summary(
        {"id": "run1", "state": "running", "claimed_by": "gateway-123", "claimed_at": "2026-05-30T10:00:00+00:00"},
        now=cli.datetime.fromisoformat("2026-05-30T10:02:00+00:00"),
        process_alive=lambda pid: True,
    )
    assert live["derived_state"] == "running-alive"
    assert live["claimant_pid"] == 123
    assert live["claimant_alive"] is True
    assert live["runtime_seconds"] == 120

    dead = cli.job_liveness_summary({"id": "run2", "state": "running", "claimed_by": "gateway-999"}, process_alive=lambda pid: False)
    assert dead["derived_state"] == "dead-claimant"
    assert dead["recovery_eligible"] is True

    unknown = cli.job_liveness_summary({"id": "run3", "state": "running", "claimed_by": "worker-x"})
    assert unknown["derived_state"] == "running-unknown-claimant"

    waiting = cli.job_liveness_summary({"id": "ready1", "state": "ready", "spec": {"dependencies": ["dep1"]}}, completed_ids=set())
    assert waiting["derived_state"] == "waiting-on-dependency"
    assert waiting["waiting_for"] == ["dep1"]

    retried = cli.job_liveness_summary({"id": "ready2", "state": "ready", "last_error": "old watchdog error", "metadata": {"watchdog_retry": {"at": "now", "reason": "safe-transient-failure"}}})
    assert retried["derived_state"] == "ready"
    assert retried["last_error"] is None
    assert retried["historical_last_error"] == "old watchdog error"
    assert retried["last_watchdog_action"]["reason"] == "safe-transient-failure"


def test_status_watch_inspect_and_dashboard_include_liveness(tmp_path: Path, monkeypatch, capsys) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))
    capsys.readouterr()
    repo = tmp_path / "repo"
    repo.mkdir()
    write_job_record(hermes_home, "ready", "dep_job", demo_job(repo, title="dep"))
    write_job_record(hermes_home, "ready", "wait_job", demo_job(repo, title="waiting", deps=["dep_job"]))
    write_job_record(hermes_home, "running", "run_job", {**demo_job(repo, title="running"), "claimed_by": "gateway-555", "claimed_at": "2026-05-30T10:00:00+00:00"})
    monkeypatch.setattr(cli, "process_is_alive", lambda pid: True)

    cli.command_status(cfg)
    status = json.loads(capsys.readouterr().out)
    active = {item["id"]: item for item in status["active_jobs"]}
    waiting = {item["id"]: item for item in status["waiting_jobs"]}
    assert active["run_job"]["derived_state"] == "running-alive"
    assert waiting["wait_job"]["derived_state"] == "waiting-on-dependency"
    assert waiting["wait_job"]["waiting_for"] == ["dep_job"]

    ready_after_dep = cli.operator_job_summary(
        {**demo_job(repo, title="ready", deps=["dep_job"]), "id": "ready_after_dep", "state": "ready"},
        completed_ids={"dep_job"},
    )
    assert ready_after_dep["derived_state"] == "ready"
    assert ready_after_dep["waiting_for"] == []

    watch = cli.build_watch_report(cfg, check_gateway=False)
    assert watch["running"][0]["derived_state"] == "running-alive"
    assert watch["waiting"][0]["derived_state"] == "waiting-on-dependency"

    inspection = cli.build_job_inspection(cfg, "wait_job")
    assert inspection["liveness"]["derived_state"] == "waiting-on-dependency"
    assert inspection["dependencies"][0]["job_id"] == "dep_job"
    assert "Wait for dependency" in inspection["recommended_action"]

    detail = job_detail(cfg, "wait_job")
    assert detail["liveness"]["derived_state"] == "waiting-on-dependency"
    dashboard = dashboard_state(cfg)
    assert dashboard["jobs"]["waiting"][0]["derived_state"] == "waiting-on-dependency"


def test_observe_json_ignores_config_notify_without_cli_notify(tmp_path: Path, monkeypatch, capsys) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"hermes_home": str(hermes_home), "task_root": str(task_root), "watch": {"notify": True}}), encoding="utf-8")
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))
    capsys.readouterr()
    write_job_record(hermes_home, "blocked", "blocked1", {"spec": {"project": "Demo", "request": {"title": "blocked"}}})
    sent: list[dict] = []
    monkeypatch.setattr(cli, "systemd_gateway_status", lambda: {"available": False, "state": "unchecked", "ok": None})
    monkeypatch.setattr(cli, "maybe_send_tasklane_notification", lambda *a, **k: sent.append(k) or {"status": "sent"})

    rc = cli.main(["--config", str(config_path), "watch", "--mode", "observe", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["health"] == "warning"
    assert sent == []
    assert not (task_root / "notification-state.json").exists()

    rc = cli.main(["--config", str(config_path), "watch", "--mode", "observe", "--json", "--notify"])
    assert rc == 0
    assert sent


def test_watch_problem_job_preserves_completed_dependency_context(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    cfg = cli.Config(
        hermes_home,
        task_root,
        True,
        1,
        None,
        None,
        None,
        None,
        {"expected_base_branches": {"Demo": "develop"}},
        {},
        {},
    )
    cli.ensure_layout(cfg)
    repo = tmp_path / "repo"
    write_job_record(hermes_home, "completed", "dep", demo_job(repo, title="dependency"))
    ready_job = demo_job(repo, title="ready with completed dependency", deps=["dep"])
    ready_job["spec"]["branch"]["base_branch"] = "main"
    write_job_record(hermes_home, "ready", "ready1", ready_job)

    report = cli.build_watch_report(cfg, check_gateway=False)

    assert report["ready"][0]["derived_state"] == "ready"
    problem = next(item for item in report["problems"] if item["code"] == "base-branch-mismatch")
    assert problem["job"]["derived_state"] == "ready"
    assert problem["job"]["waiting_for"] == []


def test_pr_visibility_distinguishes_auth_missing_branch_no_pr_and_found(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = cli.Config(tmp_path / "hermes", tmp_path / "tasklane", True, 1, None, None, None, None, {}, {}, {})
    job = {"id": "job1", "state": "completed", **demo_job(repo)}
    monkeypatch.setattr(cli, "git_remote_for_repo", lambda path: ("example", "demo"))
    monkeypatch.setattr(cli, "remote_branch_exists", lambda path, branch: (True, None))
    monkeypatch.setattr(cli, "github_auth_header", lambda: None)
    assert cli.pr_visibility_status(cfg, job)["status"] == "unknown-auth-missing"

    monkeypatch.setattr(cli, "github_auth_header", lambda: "token x")
    monkeypatch.setattr(cli, "find_pr", lambda owner, repo_name, branch: None)
    assert cli.pr_visibility_status(cfg, job)["status"] == "branch-pushed-no-pr"

    monkeypatch.setattr(cli, "find_pr", lambda owner, repo_name, branch: {"number": 7, "url": "https://github.com/example/demo/pull/7", "branch": branch, "base_branch": "main"})
    found = cli.pr_visibility_status(cfg, job)
    assert found["status"] == "found"
    assert found["number"] == 7


def test_plan_wave_enqueue_writes_lane_plan_artifact(tmp_path: Path, monkeypatch, capsys) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))
    capsys.readouterr()
    repo = tmp_path / "repo"
    repo.mkdir()
    stub_wave_github(monkeypatch, issues=[wave_issue(41, "Client UI polish", "`apps/client/src/App.tsx`")], prs=[])
    monkeypatch.setattr(cli, "github_merged_prs_for_issue", lambda owner, repo_name, issue: [])

    rc = cli.command_plan_wave(
        cfg,
        repo_path=repo,
        project="Demo",
        base_branch="development",
        max_active_prs=None,
        branch_prefix=None,
        issue_limit=None,
        issue_scan_limit=None,
        max_lanes=None,
        issue_includes=None,
        issue_excludes=None,
        issue_labels_any=None,
        issue_labels_all=None,
        issue_milestone=None,
        enqueue=True,
        json_output=True,
    )
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    lane_plan = report["enqueue_result"]["lane_plan"]
    assert lane_plan["status"] == "written"
    artifact = json.loads(Path(lane_plan["path"]).read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 1
    assert artifact["wave_id"] == report["enqueue_result"]["wave_id"]
    assert artifact["artifact_status"] == "complete"
    lane = artifact["lanes"][0]
    assert lane["issue_numbers"] == [41]
    assert lane["implementation_job_ids"]
    assert lane["final_pr_job_id"]


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


def test_plan_wave_blocks_new_work_at_active_pr_cap(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    stub_wave_github(
        monkeypatch,
        issues=[wave_issue(1, "Client UI polish", "`apps/client/src/App.tsx`")],
        prs=[wave_pr(10, "tasklane/a"), wave_pr(11, "tasklane/b"), wave_pr(12, "tasklane/c"), wave_pr(13, "feature/manual")],
    )

    report = cli.plan_wave_report(cfg, repo_path=repo, project="Demo", base_branch="development")

    assert report["mode"] == "review-fix-unblock"
    assert report["may_start_new_work"] is False
    assert len(report["active_tasklane_prs"]) == 3
    assert report["proposed_lanes"] == []
    assert report["notification_payloads"][0]["reason"] == "max_active_prs=3 reached"
    assert report["notification_payloads"][0]["safe_secret_names_only"] is True


def test_plan_wave_ignores_non_tasklane_prs_for_cap(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    stub_wave_github(
        monkeypatch,
        issues=[wave_issue(1, "Client UI polish", "`apps/client/src/App.tsx`")],
        prs=[wave_pr(10, "feature/manual"), wave_pr(11, "codex/manual")],
    )

    report = cli.plan_wave_report(cfg, repo_path=repo, project="Demo", base_branch="development")

    assert report["may_start_new_work"] is True
    assert report["active_tasklane_prs"] == []
    assert report["proposed_lanes"][0]["lane_id"] == "ux"


def test_plan_wave_limits_new_lanes_to_remaining_pr_slots(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    stub_wave_github(
        monkeypatch,
        issues=[
            wave_issue(1, "Contract PDA claim", "`contracts/fogo/programs/treasure-hunter-programm/src/lib.rs`"),
            wave_issue(2, "Client UI polish", "`apps/client/src/App.tsx`"),
        ],
        prs=[wave_pr(10, "tasklane/a"), wave_pr(11, "tasklane/b")],
    )

    report = cli.plan_wave_report(cfg, repo_path=repo, project="Demo", base_branch="development")

    assert report["remaining_pr_slots"] == 1
    assert len(report["proposed_lanes"]) == 1
    assert report["proposed_lanes"][0]["lane_id"] == "contract"
    assert any(item["reason"] == "lane-cap" and item["issue"]["number"] == 2 for item in report["blocked_items"])


def test_plan_wave_serializes_conflicting_file_ownership(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    stub_wave_github(
        monkeypatch,
        issues=[
            wave_issue(1, "Island panel UI", "`apps/client/src/components/game-ui/GameUI.tsx`"),
            wave_issue(2, "Relic gate UI", "`apps/client/src/components/game-ui/GameUI.tsx`"),
        ],
    )

    report = cli.plan_wave_report(cfg, repo_path=repo, project="Demo", base_branch="development")

    lane = report["proposed_lanes"][0]
    assert lane["lane_id"] == "ux"
    assert lane["implementation_tasks"][0]["depends_on"] == []
    assert lane["implementation_tasks"][1]["depends_on"] == ["issue-1"]
    assert lane["final_pr_task"]["depends_on"] == ["issue-1", "issue-2"]
    assert lane["final_pr_task"]["review_loops"] == 2


def test_plan_wave_isolates_contract_tasks(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    stub_wave_github(
        monkeypatch,
        issues=[
            wave_issue(1, "Contract PDA claim", "`contracts/fogo/programs/treasure-hunter-programm/src/lib.rs`"),
            wave_issue(2, "IDL account update", "Regenerate IDL and client types"),
            wave_issue(3, "Rollout checklist", "`docs/season-3/rollout.md`"),
        ],
    )

    report = cli.plan_wave_report(cfg, repo_path=repo, project="Demo", base_branch="development")

    lanes = {lane["lane_id"]: lane for lane in report["proposed_lanes"]}
    assert set(lanes) == {"contract", "ops-readiness"}
    assert [issue["number"] for issue in lanes["contract"]["issues"]] == [1]
    assert any(item["reason"] == "contract-pr-cap" and item["issue"]["number"] == 2 for item in report["blocked_items"])


def test_plan_wave_command_is_dry_run_non_mutating(tmp_path: Path, monkeypatch, capsys) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))
    capsys.readouterr()
    repo = tmp_path / "repo"
    repo.mkdir()
    stub_wave_github(
        monkeypatch,
        issues=[wave_issue(1, "Telemetry events", "`apps/server/src/game/game.gateway.ts` telemetry")],
    )

    before_inbox = sorted(path.name for path in (task_root / "inbox").glob("*"))
    before_jobs = sorted(path.name for path in (hermes_home / "jobs" / "ready").glob("*.json"))
    cli.command_plan_wave(
        cfg,
        repo_path=repo,
        project="Demo",
        base_branch="development",
        max_active_prs=None,
        branch_prefix=None,
        issue_limit=None,
        issue_scan_limit=None,
        max_lanes=None,
        issue_includes=None,
        issue_excludes=None,
        issue_labels_any=None,
        issue_labels_all=None,
        issue_milestone=None,
        enqueue=False,
        json_output=True,
    )

    output = json.loads(capsys.readouterr().out)
    assert output["dry_run"] is True
    assert sorted(path.name for path in (task_root / "inbox").glob("*")) == before_inbox
    assert sorted(path.name for path in (hermes_home / "jobs" / "ready").glob("*.json")) == before_jobs


def test_plan_wave_filters_issue_scope_after_wider_scan(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    cfg.wave_planner.update(
        {
            "max_issues_per_wave": 2,
            "issue_scan_limit": 20,
            "issue_include_terms": ["[S3-"],
        }
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    old_issues = [wave_issue(number, f"Marketplace backlog {number}", "`apps/client/src/marketplace/Page.tsx`") for number in range(1, 12)]
    stub_wave_github(
        monkeypatch,
        issues=[
            *old_issues,
            wave_issue(376, "[S3-T024] Tool-Gated Wood And Stone Nodes", "`contracts/fogo/programs/treasure-hunter-programm/src/lib.rs`"),
            wave_issue(377, "[S3-T025] Tool Repair Kit", "`apps/client/src/components/panels/CraftsmanPanel.tsx`"),
            wave_issue(378, "[S3-T026] Later Season 3 Issue", "`apps/client/src/App.tsx`"),
        ],
    )

    report = cli.plan_wave_report(cfg, repo_path=repo, project="Demo", base_branch="development")

    assert report["issue_scope"]["scanned_count"] == 14
    assert report["issue_scope"]["matched_count"] == 3
    assert report["issue_scope"]["selected_count"] == 2
    assert [candidate["number"] for candidate in report["issue_candidates"]] == [376, 377]
    assert all(item["reason"] == "missing-include-term" for item in report["scoped_out_items"])
    assert [item["number"] for item in report["deferred_items"]] == [378]


def test_plan_wave_filters_by_label_and_milestone(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    cfg.wave_planner.update(
        {
            "issue_labels_any": ["season-3"],
            "issue_milestone": "Season 3",
        }
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    stub_wave_github(
        monkeypatch,
        issues=[
            wave_issue(1, "Season 2 client cleanup", "`apps/client/src/App.tsx`", labels=["season-2"], milestone="Season 2"),
            wave_issue(2, "Season 3 client cleanup", "`apps/client/src/App.tsx`", labels=["season-3"], milestone="Season 3"),
            wave_issue(3, "Season 3 wrong milestone", "`apps/client/src/App.tsx`", labels=["season-3"], milestone="Backlog"),
        ],
    )

    report = cli.plan_wave_report(cfg, repo_path=repo, project="Demo", base_branch="development")

    assert [candidate["number"] for candidate in report["issue_candidates"]] == [2]
    assert {item["number"]: item["reason"] for item in report["scoped_out_items"]} == {
        1: "missing-any-label",
        3: "milestone-mismatch",
    }


def test_plan_wave_skips_issues_already_referenced_by_active_tasklane_pr(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    cfg.wave_planner.update({"issue_include_terms": ["[S3-"]})
    repo = tmp_path / "repo"
    repo.mkdir()
    stub_wave_github(
        monkeypatch,
        issues=[
            wave_issue(381, "[S3-T033] Island UI Shell", "`apps/client/src/components/game-ui/GameUI.tsx`"),
            wave_issue(410, "[S3-T080] Telemetry Events", "`apps/server/src/game/game.gateway.ts` telemetry"),
        ],
        prs=[wave_pr(433, "tasklane/th-s3-ux-gates", "S3 UX Gates Final PR (#381, #400, #403)")],
    )

    report = cli.plan_wave_report(cfg, repo_path=repo, project="Demo", base_branch="development")

    assert [candidate["number"] for candidate in report["issue_candidates"]] == [410]
    assert report["issue_scope"]["already_covered_count"] == 1
    assert report["already_covered_items"] == [
        {
            "number": 381,
            "url": "https://github.com/example/demo/issues/381",
            "title": "[S3-T033] Island UI Shell",
            "reason": "active-tasklane-pr",
        }
    ]


def test_plan_wave_enqueue_creates_and_syncs_guarded_tasks(tmp_path: Path, monkeypatch, capsys) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))
    capsys.readouterr()
    repo = tmp_path / "repo"
    repo.mkdir()
    stub_wave_github(
        monkeypatch,
        issues=[wave_issue(376, "[S3-T024] Tool-Gated Wood And Stone Nodes", "`contracts/fogo/programs/treasure-hunter-programm/src/lib.rs`")],
    )

    cli.command_plan_wave(
        cfg,
        repo_path=repo,
        project="Demo",
        base_branch="development",
        max_active_prs=None,
        branch_prefix=None,
        issue_limit=None,
        issue_scan_limit=None,
        max_lanes=None,
        issue_includes=["[S3-"],
        issue_excludes=None,
        issue_labels_any=None,
        issue_labels_all=None,
        issue_milestone=None,
        enqueue=True,
        json_output=True,
    )

    output = json.loads(capsys.readouterr().out)
    assert output["enqueue_result"]["status"] == "enqueued"
    assert len(output["enqueue_result"]["created_task_files"]) == 2
    assert sorted(path.name for path in (task_root / "inbox").glob("*.md")) == []
    submitted_files = sorted(path.name for path in (task_root / "submitted").glob("*.md"))
    assert len([name for name in submitted_files if "issue-376" in name or "final-pr" in name]) == 2
    ready_jobs = [json.loads(path.read_text(encoding="utf-8")) for path in (hermes_home / "jobs" / "ready").glob("*.json")]
    assert len(ready_jobs) == 3
    assert any(job["spec"]["delivery_mode"] == "direct-push" for job in ready_jobs)
    final_jobs = [job for job in ready_jobs if job["spec"]["delivery_mode"] == "pull-request"]
    assert len(final_jobs) == 1
    assert final_jobs[0]["spec"]["pipeline"]["codex_review"] is True


def test_plan_wave_uses_project_specific_settings_and_review_docs(tmp_path: Path, monkeypatch, capsys) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))
    cfg.wave_planner["projects"] = {
        "PeerPay": {
            "max_active_prs": 2,
            "issue_include_terms": ["[PP-"],
            "review_docs": ["admin.md", "docs/"],
            "merge_gate": True,
            "auto_merge": True,
            "verification_profile": "PeerPay",
        }
    }
    cfg.watch["verification_profiles"] = {"PeerPay": ["npm run typecheck", "npm run test"]}
    capsys.readouterr()
    repo = tmp_path / "repo"
    repo.mkdir()
    stub_wave_github(
        monkeypatch,
        issues=[
            wave_issue(1, "[S3-OLD] Wrong project", "`src/old.ts`"),
            wave_issue(2, "[PP-001] PeerPay task", "`src/payments.ts`"),
        ],
    )

    cli.command_plan_wave(
        cfg,
        repo_path=repo,
        project="PeerPay",
        base_branch="development",
        max_active_prs=None,
        branch_prefix=None,
        issue_limit=None,
        issue_scan_limit=None,
        max_lanes=None,
        issue_includes=None,
        issue_excludes=None,
        issue_labels_any=None,
        issue_labels_all=None,
        issue_milestone=None,
        enqueue=True,
        json_output=True,
    )

    output = json.loads(capsys.readouterr().out)
    assert output["settings"]["max_active_prs"] == 2
    assert output["issue_candidates"][0]["number"] == 2
    ready_jobs = [json.loads(path.read_text(encoding="utf-8")) for path in (hermes_home / "jobs" / "ready").glob("*.json")]
    final_job = next(job for job in ready_jobs if job["spec"]["delivery_mode"] == "pull-request")
    assert final_job["spec"]["metadata"]["merge_gate"] == "true"
    assert final_job["spec"]["metadata"]["auto_merge"] == "true"
    assert final_job["spec"]["metadata"]["review_docs"] == "admin.md, docs/"
    assert final_job["spec"]["metadata"]["required_verification_commands"] == "npm run typecheck, npm run test"
    assert "admin.md" in final_job["spec"]["request"]["body"]
    assert "`npm run typecheck`" in final_job["spec"]["request"]["body"]
    assert "`npm run test`" in final_job["spec"]["request"]["body"]
    assert "Open or update one PR for this lane" in final_job["spec"]["request"]["body"]
    assert "Mark the PR ready for review" in final_job["spec"]["request"]["body"]
    assert "Open or update one draft PR" not in final_job["spec"]["request"]["body"]


def test_plan_wave_can_disable_contract_lane_for_non_contract_projects(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    cfg.wave_planner["projects"] = {
        "PeerPay": {
            "disable_contract_lane": True,
            "max_issues_per_wave": 2,
        }
    }
    repo = tmp_path / "repo"
    repo.mkdir()
    stub_wave_github(
        monkeypatch,
        issues=[
            wave_issue(1, "Database schema and payment contract rules", "`src/lib/db/schema.ts` `src/features/payments/model.ts`"),
            wave_issue(2, "Wallet UI", "`src/components/Wallet.tsx`"),
        ],
    )

    report = cli.plan_wave_report(cfg, repo_path=repo, project="PeerPay", base_branch="development")

    lanes = {lane["lane_id"]: lane for lane in report["proposed_lanes"]}
    assert "contract" not in lanes
    assert any(issue["number"] == 1 for issue in lanes["backend"]["issues"])


def test_reconcile_queues_merge_gate_after_review_pass(tmp_path: Path, capsys) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))
    capsys.readouterr()
    repo = tmp_path / "repo"
    repo.mkdir()
    root_uid = "wave-demo-final-pr"
    root_job_id = "tasklane_root"
    review_job_id = cli.review_job_id(root_uid, 1)
    cli.save_state(
        cfg,
        {
            "submitted": {
                root_uid: {
                    "job_id": root_job_id,
                    "run_id": root_job_id,
                    "repo_key": f"repo://{repo}",
                    "review_gate": {"enabled": True, "status": "pending", "max_loops": 2},
                },
                f"{root_uid}:codex-review:1": {
                    "synthetic": True,
                    "kind": "codex-review",
                    "root_uid": root_uid,
                    "job_id": review_job_id,
                    "run_id": review_job_id,
                    "repo_key": f"repo://{repo}",
                    "review_iteration": 1,
                },
            }
        },
    )
    write_job_record(
        hermes_home,
        "completed",
        root_job_id,
        {
            "spec": {
                "project": "PeerPay",
                "repo": {"key": f"repo://{repo}", "path": str(repo)},
                "request": {"title": "PeerPay Final PR", "body": "Finalize PR"},
                "branch": {"base_branch": "development", "work_branch": "tasklane/peerpay-demo"},
                "delivery_mode": "pull-request",
                "pipeline": {"budgets": {"review_loops": 2}},
                "scope": {"allowed_paths": [], "denied_paths": [], "allow_unlisted_paths": True},
                "metadata": {"uid": root_uid, "merge_gate": "true", "auto_merge": "true", "review_docs": "admin.md, docs/", "required_verification_commands": "npm run typecheck, npm run test"},
            },
            "result": {"final_response": "PR opened"},
        },
    )
    write_job_record(
        hermes_home,
        "completed",
        review_job_id,
        {
            "spec": {"metadata": {"uid": f"{root_uid}:codex-review:1"}},
            "result": {"final_response": "TASKLANE_REVIEW_DECISION: pass\nLooks good."},
        },
    )

    command_reconcile(cfg)

    output = json.loads(capsys.readouterr().out)
    assert any(action["status"] == "merge-gate-queued" for action in output["actions"])
    merge_id = cli.merge_job_id(root_uid)
    merge_job = json.loads((hermes_home / "jobs" / "ready" / f"{merge_id}.json").read_text(encoding="utf-8"))
    assert merge_job["spec"]["pipeline"]["role"] == "tasklane-merge-gate"
    assert "TASKLANE_MERGE_DECISION: merged" in merge_job["spec"]["request"]["body"]
    assert "authoritative Tasklane evidence" in merge_job["spec"]["request"]["body"]
    assert "mark it ready for review before merging" in merge_job["spec"]["request"]["body"]
    assert "`npm run typecheck`" in merge_job["spec"]["request"]["body"]
    state = load_state(cfg)
    assert state["submitted"][root_uid]["merge_gate"]["status"] == "queued"
    assert state["submitted"][f"{root_uid}:merge-gate"]["kind"] == "tasklane-merge-gate"


def test_status_and_watch_surface_submitted_gate_attention(tmp_path: Path, capsys) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    cfg.watch["require_notifications"] = True
    command_init(cfg, str(config_path))
    capsys.readouterr()
    submitted_task = task_root / "submitted" / "final.md"
    submitted_task.parent.mkdir(parents=True, exist_ok=True)
    submitted_task.write_text("final task\n", encoding="utf-8")
    cli.save_state(
        cfg,
        {
            "submitted": {
                "wave-demo-final": {
                    "source_path": str(submitted_task),
                    "job_id": "tasklane_root",
                    "repo_key": "repo:///tmp/demo",
                    "review_gate": {
                        "enabled": True,
                        "status": "needs-human",
                        "reason": "max-review-loops-reached",
                        "decision_job_id": "tasklane_review",
                        "current_iteration": 2,
                        "max_loops": 2,
                    },
                }
            }
        },
    )

    cli.command_status(cfg)

    status = json.loads(capsys.readouterr().out)
    assert status["gate_attention"][0]["task_uid"] == "wave-demo-final"
    assert status["gate_attention"][0]["reason"] == "max-review-loops-reached"
    report = cli.build_watch_report(cfg, check_gateway=False)
    assert report["gate_attention"][0]["job_id"] == "tasklane_review"
    assert any(problem["code"] == "review-gate-needs-human" for problem in report["problems"])
    assert any(problem["code"] == "notification-misconfigured" for problem in report["problems"])
    assert report["notification_config"]["configured"] is False
    assert report["notification_config"]["provider"] == "hermes"
    assert "What you need to do:" in cli.format_watch_report(report)
    assert "Do not merge yet" in cli.format_watch_report(report)
    assert "Hermes/project-chat delivery is not fully configured" in cli.format_watch_report(report)


def test_tasklane_notification_uses_hermes_relay_first(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    agent_path = hermes_home / "hermes-agent"
    python_path = agent_path / "venv" / "bin" / "python"
    (agent_path / "tools").mkdir(parents=True)
    python_path.parent.mkdir(parents=True)
    (agent_path / "tools" / "send_message_tool.py").write_text("# marker\n", encoding="utf-8")
    python_path.write_text("# marker\n", encoding="utf-8")
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    cfg.watch["hermes_target"] = "telegram:Treasure Hunter Dev"

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        assert kwargs["input"] == "hello"
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"success": True, "message_id": "1"}), stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = cli.send_tasklane_notification(cfg, "hello")

    assert result["status"] == "sent"
    assert result["provider"] == "hermes"
    assert result["target"] == "telegram:Treasure Hunter Dev"
    assert calls[0][0] == str(python_path)


def test_tasklane_notification_dedupes_same_fingerprint_during_cooldown(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    cfg.watch["notification_cooldown_minutes"] = 240
    command_init(cfg, str(config_path))
    sent: list[str] = []

    def fake_send(_cfg, text):
        sent.append(text)
        return {"status": "sent", "provider": "test"}

    monkeypatch.setattr(cli, "send_tasklane_notification", fake_send)
    payload = {"problems": [{"code": "review-gate-needs-human", "job_id": "tasklane_review"}]}

    first = cli.maybe_send_tasklane_notification(cfg, channel="watch", text="hello", payload=payload)
    second = cli.maybe_send_tasklane_notification(cfg, channel="watch", text="hello", payload=payload)

    assert first["status"] == "sent"
    assert second["status"] == "skipped"
    assert second["reason"] == "duplicate-notification-cooldown"
    assert sent == ["hello"]


def test_tasklane_notification_sends_when_fingerprint_changes(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    cfg.watch["notification_cooldown_minutes"] = 240
    command_init(cfg, str(config_path))
    sent: list[str] = []
    monkeypatch.setattr(cli, "send_tasklane_notification", lambda _cfg, text: sent.append(text) or {"status": "sent", "provider": "test"})

    cli.maybe_send_tasklane_notification(cfg, channel="watch", text="first", payload={"jobs": ["a"]})
    second = cli.maybe_send_tasklane_notification(cfg, channel="watch", text="second", payload={"jobs": ["a", "b"]})

    assert second["status"] == "sent"
    assert sent == ["first", "second"]


def test_reconcile_finalizes_manual_merged_tasklane_pr(tmp_path: Path, monkeypatch, capsys) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))
    capsys.readouterr()
    repo = tmp_path / "repo"
    repo.mkdir()
    submitted_task = task_root / "submitted" / "final.md"
    submitted_task.parent.mkdir(parents=True, exist_ok=True)
    submitted_task.write_text("final task\n", encoding="utf-8")
    cli.save_state(
        cfg,
        {
            "submitted": {
                "wave-demo-final": {
                    "source_path": str(submitted_task),
                    "job_id": "tasklane_root",
                    "run_id": "tasklane_root",
                    "repo_key": f"repo://{repo}",
                    "review_gate": {"enabled": True, "status": "passed", "decision_job_id": "tasklane_review"},
                    "merge_gate": {"enabled": True, "status": "needs-human", "merge_job_id": "tasklane_merge", "reason": "needs-human"},
                }
            }
        },
    )
    write_job_record(
        hermes_home,
        "completed",
        "tasklane_root",
        {
            "spec": {
                "project": "Demo",
                "repo": {"key": f"repo://{repo}", "path": str(repo)},
                "request": {"title": "Demo Final PR"},
                "branch": {"base_branch": "development", "work_branch": "tasklane/demo"},
                "delivery_mode": "pull-request",
            },
            "result": {
                "delivery_validation": {
                    "pr": {"number": 42, "url": "https://github.com/example/demo/pull/42"},
                }
            },
        },
    )
    monkeypatch.setattr(
        cli,
        "github_pull_request",
        lambda owner, repo_name, number: {
            "number": number,
            "state": "closed",
            "merged_at": "2026-05-09T08:00:00Z",
            "html_url": f"https://github.com/{owner}/{repo_name}/pull/{number}",
            "draft": False,
        },
    )

    command_reconcile(cfg)

    output = json.loads(capsys.readouterr().out)
    assert output["actions"][0]["status"] == "completed-manual-merge-detected"
    assert not load_state(cfg)["submitted"]
    assert (task_root / "completed" / "final.md").exists()
    result = json.loads((task_root / "completed" / "final.md.result.json").read_text(encoding="utf-8"))
    assert result["result"]["merge_gate"]["status"] == "merged"
    assert result["result"]["merge_gate"]["reason"] == "manual-merge-detected"


def test_wave_runner_enqueues_when_project_has_free_slots(tmp_path: Path, monkeypatch, capsys) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))
    cfg.wave_planner["projects"] = {"PeerPay": {"issue_include_terms": ["[PP-"], "review_docs": ["admin.md", "docs/"]}}
    capsys.readouterr()
    repo = tmp_path / "repo"
    repo.mkdir()
    stub_wave_github(monkeypatch, issues=[wave_issue(2, "[PP-001] PeerPay task", "`src/payments.ts`")])
    monkeypatch.setattr(
        cli,
        "build_watch_report",
        lambda *args, **kwargs: {"health": "ok", "problems": [], "counts": {}, "gateway": {"state": "ok"}, "running": [], "ready": [], "waiting": [], "blocked": [], "needs_human": [], "failed": [], "notices": []},
    )
    monkeypatch.setattr(cli, "apply_guarded_watch_actions", lambda cfg, report: [])

    rc = cli.command_wave_runner(
        cfg,
        repo_path=repo,
        project="PeerPay",
        base_branch="development",
        enqueue=True,
        notify=False,
        json_output=True,
    )

    output = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert output["enqueue_result"]["status"] == "enqueued"
    assert output["plan"]["project"] == "PeerPay"
    assert len(list((hermes_home / "jobs" / "ready").glob("*.json"))) == 3


def test_sync_prunes_loaded_task_repos_once(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ["one.md", "two.md"]:
        (task_root / "inbox" / name).write_text(
            f"---\nrepo_path: {repo}\nbranch_base: main\nproject: Demo\n---\nImplement {name}.\n",
            encoding="utf-8",
        )

    calls: list[Path] = []

    def fake_prune(repo_path: Path) -> dict:
        calls.append(repo_path)
        return {"ok": True, "reason": "pruned", "repo_path": str(repo_path)}

    monkeypatch.setattr(cli, "git_worktree_prune", fake_prune)

    command_sync(cfg)

    assert calls == [repo]


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


def test_sync_respects_max_pending_per_repo_for_same_batch(tmp_path: Path, capsys) -> None:
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
        f"---\nid: one\nrepo_path: {repo}\nbase_branch: main\n---\nBuild one.\n",
        encoding="utf-8",
    )
    (task_root / "inbox" / "two.md").write_text(
        f"---\nid: two\nrepo_path: {repo}\nbase_branch: main\n---\nBuild two.\n",
        encoding="utf-8",
    )

    command_sync(cfg)

    output = json.loads(capsys.readouterr().out)
    assert [action["status"] for action in output["actions"]] == ["job-ready", "deferred"]
    assert output["actions"][1]["reason"] == "repo-pending-limit"
    assert len(list((hermes_home / "jobs" / "ready").glob("*.json"))) == 1
    assert (task_root / "submitted" / "one.md").exists()
    assert (task_root / "inbox" / "two.md").exists()


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


def test_task_metadata_cannot_override_verification_commands_by_default(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    cfg.watch["verification_commands"] = ["git diff --check"]

    repo = tmp_path / "repo"
    repo.mkdir()
    job = {
        "spec": {
            "project": "Demo",
            "repo": {"key": f"repo://{repo}", "path": str(repo)},
            "metadata": {"verification_commands": "touch SHOULD_NOT_RUN"},
        }
    }

    assert cli.configured_commands_for_job(cfg, job, "verification") == ["git diff --check"]


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


def test_sync_creates_codex_review_gate_job_when_enabled(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "hermes_home": str(hermes_home),
                "task_root": str(task_root),
                "poll_repo_idle": True,
                "review_gate": {"enabled": True},
            }
        )
    )
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    repo = tmp_path / "repo"
    repo.mkdir()
    (task_root / "inbox" / "feature.md").write_text(
        f"---\nid: feature-review\nrepo_path: {repo}\nbase_branch: development\nwork_branch: tasklane/feature-review\nreview_loops: 2\n---\nBuild the feature with tests.\n",
        encoding="utf-8",
    )

    command_sync(cfg)

    records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((hermes_home / "jobs" / "ready").glob("*.json"))]
    impl = next(item for item in records if item["spec"]["metadata"]["source"] == "tasklane-file-bridge")
    review = next(item for item in records if item["spec"]["metadata"]["source"] == "tasklane-review-gate")
    assert review["spec"]["delivery_mode"] == "report-only"
    assert review["spec"]["branch"]["mode"] == "existing-branch"
    assert review["spec"]["branch"]["work_branch"] == "tasklane/feature-review"
    assert review["spec"]["dependencies"] == [impl["id"]]
    assert "TASKLANE_REVIEW_DECISION: pass" in review["spec"]["request"]["body"]
    state = load_state(cfg)
    assert state["submitted"]["feature-review"]["review_gate"]["review_job_id"] == review["id"]
    assert state["submitted"]["feature-review:codex-review:1"]["kind"] == "codex-review"


def test_sync_review_gate_does_not_defer_same_batch_tasks(tmp_path: Path, capsys) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "hermes_home": str(hermes_home),
                "task_root": str(task_root),
                "poll_repo_idle": True,
                "max_pending_per_repo": 2,
                "review_gate": {"enabled": True},
            }
        )
    )
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))
    capsys.readouterr()

    repo = tmp_path / "repo"
    repo.mkdir()
    (task_root / "inbox" / "a-final.md").write_text(
        f"---\nid: group-final\nrepo_path: {repo}\nbase_branch: development\nbranch_mode: existing-branch\nwork_branch: tasklane/group\ndelivery_group: group\ndelivery_mode: pull-request\ndepends_on: group-impl\n---\nOpen the grouped PR.\n",
        encoding="utf-8",
    )
    (task_root / "inbox" / "b-impl.md").write_text(
        f"---\nid: group-impl\nrepo_path: {repo}\nbase_branch: development\ndelivery_group: group\ndelivery_mode: direct-push\n---\nImplement the grouped work.\n",
        encoding="utf-8",
    )

    command_sync(cfg)

    output = json.loads(capsys.readouterr().out)
    assert [action["status"] for action in output["actions"]] == ["job-ready", "job-ready"]
    assert all(action.get("reason") != "repo-active-job" for action in output["actions"])
    ready = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((hermes_home / "jobs" / "ready").glob("*.json"))]
    assert len(ready) == 3
    assert sum(1 for item in ready if item["spec"]["metadata"]["source"] == "tasklane-review-gate") == 1


def test_sync_allows_waiting_task_while_repo_has_active_job(tmp_path: Path, capsys) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))
    capsys.readouterr()

    repo = tmp_path / "repo"
    repo.mkdir()
    write_job_record(
        hermes_home,
        "running",
        "tasklane_active",
        {
            "spec": {
                "project": "Demo",
                "repo": {"key": f"repo://{repo}", "path": str(repo)},
                "request": {"title": "active"},
                "branch": {"mode": "new-branch", "base_branch": "development"},
                "dependencies": [],
            }
        },
    )
    (task_root / "inbox" / "waiting.md").write_text(
        f"---\nid: waiting-task\nrepo_path: {repo}\nbase_branch: development\ndepends_on: tasklane_missing_dependency\n---\nQueue me as waiting.\n",
        encoding="utf-8",
    )

    command_sync(cfg)

    output = json.loads(capsys.readouterr().out)
    assert output["actions"][0]["status"] == "job-ready"
    ready = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((hermes_home / "jobs" / "ready").glob("*.json"))]
    assert ready[0]["spec"]["dependencies"] == ["tasklane_missing_dependency"]


def test_reconcile_review_gate_needs_fix_queues_fix_and_next_review(tmp_path: Path, capsys) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "hermes_home": str(hermes_home),
                "task_root": str(task_root),
                "poll_repo_idle": True,
                "review_gate": {"enabled": True},
            }
        )
    )
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))
    capsys.readouterr()

    repo = tmp_path / "repo"
    repo.mkdir()
    (task_root / "inbox" / "feature.md").write_text(
        f"---\nid: feature-review\nrepo_path: {repo}\nbase_branch: development\nwork_branch: tasklane/feature-review\nreview_loops: 2\n---\nBuild the feature with tests.\n",
        encoding="utf-8",
    )
    command_sync(cfg)
    capsys.readouterr()
    records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((hermes_home / "jobs" / "ready").glob("*.json"))]
    impl = next(item for item in records if item["spec"]["metadata"]["source"] == "tasklane-file-bridge")
    review = next(item for item in records if item["spec"]["metadata"]["source"] == "tasklane-review-gate")
    complete_ready_job(hermes_home, impl["id"], {"final_response": "implementation done"})
    complete_ready_job(hermes_home, review["id"], {"final_response": "TASKLANE_REVIEW_DECISION: needs-fix\n- Missing regression test."})

    command_reconcile(cfg)

    output = json.loads(capsys.readouterr().out)
    assert any(action["status"] == "review-gate-fix-queued" for action in output["actions"])
    ready = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((hermes_home / "jobs" / "ready").glob("*.json"))]
    fix = next(item for item in ready if item["spec"]["metadata"]["source"] == "tasklane-review-fix")
    next_review = next(item for item in ready if item["spec"]["metadata"]["source"] == "tasklane-review-gate")
    assert fix["spec"]["delivery_mode"] == "direct-push"
    assert fix["spec"]["branch"]["work_branch"] == "tasklane/feature-review"
    assert "Missing regression test" in fix["spec"]["request"]["body"]
    assert next_review["spec"]["dependencies"] == [fix["id"]]
    state = load_state(cfg)
    assert state["submitted"]["feature-review"]["review_gate"]["status"] == "fixing"


def test_reconcile_allows_configured_extra_fix_after_review_loop_cap(tmp_path: Path, capsys) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "hermes_home": str(hermes_home),
                "task_root": str(task_root),
                "poll_repo_idle": True,
                "wave_planner": {"projects": {"PeerPay": {"extra_review_fix_loops": 1}}},
            }
        )
    )
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))
    capsys.readouterr()
    repo = tmp_path / "repo"
    repo.mkdir()
    root_uid = "wave-peerpay-final-pr"
    root_job_id = "tasklane_root"
    review_job_id = cli.review_job_id(root_uid, 2)
    cli.save_state(
        cfg,
        {
            "submitted": {
                root_uid: {
                    "job_id": root_job_id,
                    "run_id": root_job_id,
                    "repo_key": f"repo://{repo}",
                    "review_gate": {"enabled": True, "status": "pending", "max_loops": 2, "current_iteration": 2},
                },
                f"{root_uid}:codex-review:2": {
                    "synthetic": True,
                    "kind": "codex-review",
                    "root_uid": root_uid,
                    "job_id": review_job_id,
                    "run_id": review_job_id,
                    "repo_key": f"repo://{repo}",
                    "review_iteration": 2,
                },
            }
        },
    )
    write_job_record(
        hermes_home,
        "completed",
        root_job_id,
        {
            "spec": {
                "project": "PeerPay",
                "repo": {"key": f"repo://{repo}", "path": str(repo)},
                "request": {"title": "PeerPay Final PR", "body": "Finalize PR"},
                "branch": {"base_branch": "development", "work_branch": "tasklane/peerpay-demo"},
                "delivery_mode": "pull-request",
                "pipeline": {"budgets": {"review_loops": 2}},
                "scope": {"allowed_paths": [], "denied_paths": [], "allow_unlisted_paths": True},
                "metadata": {"uid": root_uid},
            },
            "result": {"final_response": "PR opened"},
        },
    )
    write_job_record(
        hermes_home,
        "completed",
        review_job_id,
        {
            "spec": {"metadata": {"uid": f"{root_uid}:codex-review:2"}},
            "result": {"final_response": "TASKLANE_REVIEW_DECISION: needs-fix\n- Actionable bug remains."},
        },
    )

    command_reconcile(cfg)

    output = json.loads(capsys.readouterr().out)
    assert any(action["status"] == "review-gate-extra-fix-queued" for action in output["actions"])
    ready = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((hermes_home / "jobs" / "ready").glob("*.json"))]
    assert any(item["spec"]["metadata"]["source"] == "tasklane-review-fix" for item in ready)
    assert any(item["spec"]["metadata"]["review_iteration"] == 3 for item in ready)
    state = load_state(cfg)
    assert state["submitted"][root_uid]["review_gate"]["current_iteration"] == 3
    assert state["submitted"][root_uid]["review_gate"]["extra_fix_count"] == 1


def test_reconcile_review_gate_pass_finalizes_original_task(tmp_path: Path, capsys) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "hermes_home": str(hermes_home),
                "task_root": str(task_root),
                "poll_repo_idle": True,
                "review_gate": {"enabled": True},
            }
        )
    )
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))
    capsys.readouterr()

    repo = tmp_path / "repo"
    repo.mkdir()
    (task_root / "inbox" / "feature.md").write_text(
        f"---\nid: feature-review\nrepo_path: {repo}\nbase_branch: development\nwork_branch: tasklane/feature-review\n---\nBuild the feature with tests.\n",
        encoding="utf-8",
    )
    command_sync(cfg)
    capsys.readouterr()
    records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((hermes_home / "jobs" / "ready").glob("*.json"))]
    impl = next(item for item in records if item["spec"]["metadata"]["source"] == "tasklane-file-bridge")
    review = next(item for item in records if item["spec"]["metadata"]["source"] == "tasklane-review-gate")
    complete_ready_job(hermes_home, impl["id"], {"final_response": "implementation done"})
    complete_ready_job(hermes_home, review["id"], {"final_response": "TASKLANE_REVIEW_DECISION: pass\nVerification passed."})

    command_reconcile(cfg)
    capsys.readouterr()
    command_reconcile(cfg)

    assert not load_state(cfg)["submitted"]
    assert (task_root / "completed" / "feature.md").exists()
    result = json.loads((task_root / "completed" / "feature.md.result.json").read_text(encoding="utf-8"))
    assert result["result"]["review_gate"]["status"] == "passed"


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


def test_ci_status_classifies_startup_failure_as_unavailable(monkeypatch) -> None:
    def fake_github_get(url: str) -> dict:
        if url.endswith("/status"):
            return {"state": "failure", "statuses": []}
        if url.endswith("/check-suites"):
            return {
                "check_suites": [
                    {
                        "app": {"slug": "github-actions"},
                        "status": "completed",
                        "conclusion": "startup_failure",
                    }
                ]
            }
        raise AssertionError(url)

    monkeypatch.setattr(cli, "github_get", fake_github_get)

    status = cli.ci_status("example", "demo", "abc123")

    assert status["status"] == "unavailable"
    assert status["check_suites"][0]["conclusion"] == "startup_failure"


def test_ci_status_classifies_billing_failure_as_unavailable(monkeypatch) -> None:
    check_runs_url = "https://api.github.com/repos/example/demo/check-suites/1/check-runs"

    def fake_github_get(url: str) -> dict:
        if url.endswith("/status"):
            return {"state": "failure", "statuses": []}
        if url.endswith("/check-suites"):
            return {
                "check_suites": [
                    {
                        "app": {"slug": "github-actions"},
                        "status": "completed",
                        "conclusion": "failure",
                        "check_runs_url": check_runs_url,
                    }
                ]
            }
        if url == check_runs_url:
            return {
                "check_runs": [
                    {
                        "name": "build",
                        "status": "completed",
                        "conclusion": "failure",
                        "output": {
                            "title": "GitHub Actions unavailable",
                            "summary": "Workflow was not run because the spending limit has been reached.",
                        },
                    }
                ]
            }
        raise AssertionError(url)

    monkeypatch.setattr(cli, "github_get", fake_github_get)

    status = cli.ci_status("example", "demo", "abc123")

    assert status["status"] == "unavailable"
    assert "CI unavailable" in status["output"]


def test_ci_status_keeps_real_failures_as_failed(monkeypatch) -> None:
    def fake_github_get(url: str) -> dict:
        if url.endswith("/status"):
            return {"state": "failure", "statuses": []}
        if url.endswith("/check-suites"):
            return {
                "check_suites": [
                    {
                        "app": {"slug": "github-actions"},
                        "status": "completed",
                        "conclusion": "failure",
                    }
                ]
            }
        raise AssertionError(url)

    monkeypatch.setattr(cli, "github_get", fake_github_get)

    status = cli.ci_status("example", "demo", "abc123")

    assert status["status"] == "fail"


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


def test_watch_splits_dependency_waiting_jobs(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    write_job_record(
        hermes_home,
        "ready",
        "tasklane_base",
        {
            "spec": {
                "project": "Demo",
                "repo": {"key": "repo:///repo/demo"},
                "request": {"title": "base work"},
                "branch": {"mode": "new-branch", "base_branch": "main"},
            },
        },
    )
    write_job_record(
        hermes_home,
        "ready",
        "tasklane_final",
        {
            "spec": {
                "project": "Demo",
                "repo": {"key": "repo:///repo/demo"},
                "request": {"title": "final PR"},
                "branch": {"mode": "existing-branch", "base_branch": "main"},
                "dependencies": ["tasklane_base"],
            },
        },
    )

    report = cli.build_watch_report(cfg, ignored_blocked=set(), check_gateway=False)

    assert report["health"] == "ok"
    assert report["counts"]["ready"] == 1
    assert report["counts"]["waiting"] == 1
    assert report["counts_all"]["ready"] == 2
    assert report["counts_all"]["ready_physical"] == 2
    assert report["ready"][0]["id"] == "tasklane_base"
    assert report["waiting"][0]["id"] == "tasklane_final"
    assert report["waiting"][0]["state"] == "waiting"
    assert report["waiting"][0]["waiting_for"] == ["tasklane_base"]


def test_status_reports_dependency_waiting_jobs(tmp_path: Path, capsys) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))
    capsys.readouterr()

    write_job_record(
        hermes_home,
        "ready",
        "tasklane_base",
        {
            "spec": {
                "project": "Demo",
                "repo": {"key": "repo:///repo/demo"},
                "request": {"title": "base work"},
                "branch": {"mode": "new-branch", "base_branch": "main"},
            },
        },
    )
    write_job_record(
        hermes_home,
        "ready",
        "tasklane_final",
        {
            "spec": {
                "project": "Demo",
                "repo": {"key": "repo:///repo/demo"},
                "request": {"title": "final PR"},
                "branch": {"mode": "existing-branch", "base_branch": "main"},
                "dependencies": ["tasklane_base"],
            },
        },
    )

    assert cli.command_status(cfg) == 0
    payload = json.loads(capsys.readouterr().out)

    assert [job["id"] for job in payload["active_jobs"]] == ["tasklane_base"]
    assert payload["waiting_jobs"][0]["id"] == "tasklane_final"
    assert payload["waiting_jobs"][0]["state"] == "waiting"
    assert payload["waiting_jobs"][0]["waiting_for"] == ["tasklane_base"]


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


def test_guarded_watch_recovers_stale_worktree_blocker(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    repo = tmp_path / "repo"
    repo.mkdir()
    write_job_record(
        hermes_home,
        "blocked",
        "tasklane_stale_worktree",
        {
            "attempt": 1,
            "claimed_at": "2026-05-08T00:00:00+00:00",
            "claimed_by": "dead-worker",
            "failed_at": "2026-05-08T00:01:00+00:00",
            "last_error": "work_branch is already checked out in another worktree: /tmp/missing",
            "spec": {
                "project": "Demo",
                "repo": {"key": f"repo://{repo}", "path": str(repo)},
                "request": {"title": "recover me"},
                "branch": {"mode": "existing-branch", "base_branch": "main", "work_branch": "tasklane/demo"},
            },
        },
    )
    monkeypatch.setattr(cli, "git_worktree_prune", lambda repo_path: {"ok": True, "reason": "pruned", "repo_path": str(repo_path)})
    monkeypatch.setattr(cli, "git_worktree_branch_entries", lambda repo_path, branch: {"ok": True, "repo_path": str(repo_path), "branch": branch, "entries": []})
    report = cli.build_watch_report(cfg, mode="guarded", ignored_blocked=set(), check_gateway=False)

    actions = cli.apply_guarded_watch_actions(cfg, report)

    assert actions == [
        {
            "job_id": "tasklane_stale_worktree",
            "status": "retried",
            "reason": "stale-worktree-pruned",
            "from": str(hermes_home / "jobs" / "blocked" / "tasklane_stale_worktree.json"),
            "to": str(hermes_home / "jobs" / "ready" / "tasklane_stale_worktree.json"),
        }
    ]
    assert not (hermes_home / "jobs" / "blocked" / "tasklane_stale_worktree.json").exists()
    ready = json.loads((hermes_home / "jobs" / "ready" / "tasklane_stale_worktree.json").read_text(encoding="utf-8"))
    assert ready["state"] == "ready"
    assert ready["last_error"] is None
    assert "claimed_at" not in ready
    assert ready["metadata"]["watchdog_retry"]["reason"] == "stale-worktree-pruned"


def test_guarded_watch_does_not_recover_live_checked_out_worktree(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    repo = tmp_path / "repo"
    repo.mkdir()
    write_job_record(
        hermes_home,
        "blocked",
        "tasklane_live_worktree",
        {
            "last_error": "work_branch is already checked out in another worktree: /tmp/live",
            "spec": {
                "project": "Demo",
                "repo": {"key": f"repo://{repo}", "path": str(repo)},
                "request": {"title": "do not recover"},
                "branch": {"mode": "existing-branch", "base_branch": "main", "work_branch": "tasklane/demo"},
            },
        },
    )
    monkeypatch.setattr(cli, "git_worktree_prune", lambda repo_path: {"ok": True, "reason": "pruned", "repo_path": str(repo_path)})
    monkeypatch.setattr(
        cli,
        "git_worktree_branch_entries",
        lambda repo_path, branch: {"ok": True, "repo_path": str(repo_path), "branch": branch, "entries": [{"worktree": "/tmp/live", "branch": branch}]},
    )
    report = cli.build_watch_report(cfg, mode="guarded", ignored_blocked=set(), check_gateway=False)

    actions = cli.apply_guarded_watch_actions(cfg, report)

    assert actions[0]["status"] == "skipped"
    assert actions[0]["reason"] == "work-branch-still-checked-out"
    assert (hermes_home / "jobs" / "blocked" / "tasklane_live_worktree.json").exists()
    assert not (hermes_home / "jobs" / "ready" / "tasklane_live_worktree.json").exists()


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


def test_dashboard_exposes_dependency_waiting_jobs(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    write_job_record(
        hermes_home,
        "ready",
        "tasklane_base",
        {
            "spec": {
                "project": "Demo",
                "repo": {"key": "repo:///repo/demo"},
                "request": {"title": "base work"},
                "branch": {"mode": "new-branch", "base_branch": "main"},
            },
        },
    )
    write_job_record(
        hermes_home,
        "ready",
        "tasklane_final",
        {
            "spec": {
                "project": "Demo",
                "repo": {"key": "repo:///repo/demo"},
                "request": {"title": "final PR"},
                "branch": {"mode": "existing-branch", "base_branch": "main"},
                "dependencies": ["tasklane_base"],
            },
        },
    )

    state = dashboard_state(cfg)

    assert state["totals"]["ready"] == 1
    assert state["totals"]["waiting"] == 1
    assert state["jobs"]["ready"][0]["id"] == "tasklane_base"
    assert state["jobs"]["waiting"][0]["id"] == "tasklane_final"
    assert state["jobs"]["waiting"][0]["waiting_for"] == ["tasklane_base"]


def test_dashboard_preserves_completed_dependency_context_for_ready_jobs(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    write_job_record(
        hermes_home,
        "completed",
        "tasklane_base",
        {
            "spec": {
                "project": "Demo",
                "repo": {"key": "repo:///repo/demo"},
                "request": {"title": "base work"},
                "branch": {"mode": "new-branch", "base_branch": "main"},
            },
        },
    )
    write_job_record(
        hermes_home,
        "ready",
        "tasklane_final",
        {
            "spec": {
                "project": "Demo",
                "repo": {"key": "repo:///repo/demo"},
                "request": {"title": "final PR"},
                "branch": {"mode": "existing-branch", "base_branch": "main"},
                "dependencies": ["tasklane_base"],
            },
        },
    )

    state = dashboard_state(cfg)
    ready = state["jobs"]["ready"][0]

    assert ready["id"] == "tasklane_final"
    assert ready["derived_state"] == "ready"
    assert ready["waiting_for"] == []
    assert ready["recovery_blocked_reason"] is None


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


def test_review_actionable_lines_skip_review_plan_before_findings() -> None:
    payload = {
        "result": {
            "final_response": "\n".join(
                [
                    "TASKLANE_REVIEW_DECISION: needs-fix",
                    "",
                    "Review plan executed:",
                    "1. Read AGENTS.md and project docs.",
                    "2. Ran verification.",
                    "",
                    "Blocking finding:",
                    "",
                    "1. Datenschutzerklaerung acceptance criterion is still unmet while production Clarity can be enabled.",
                    "   - The fix should update the relevant legal/privacy text before production activation is possible.",
                    "",
                    "Verification evidence:",
                    "- typecheck passed.",
                ]
            )
        }
    }

    assert cli.first_actionable_finding(payload) == "1. Datenschutzerklaerung acceptance criterion is still unmet while production Clarity can be enabled."
    assert cli.review_blocker_classification(payload)["class"] == "concrete-fix"


def test_review_blocker_classification_ignores_secret_scan_boilerplate() -> None:
    payload = {
        "result": {
            "final_response": "\n".join(
                [
                    "TASKLANE_REVIEW_DECISION: needs-fix",
                    "",
                    "Findings:",
                    "- The consent masking docs still omit the exact legal/privacy disclosure text and env-doc update required for PR #61.",
                    "",
                    "Security scan:",
                    "- Secret scan was reviewed; no leaked secrets were found.",
                    "",
                    "Verification:",
                    "- npm test passed.",
                ]
            )
        }
    }

    assert cli.review_blocker_classification(payload)["class"] == "concrete-fix"
    assert "consent masking docs" in (cli.first_actionable_finding(payload) or "")


def test_review_blocker_classification_scope_narrowing_is_human_decision() -> None:
    payload = {
        "result": {
            "final_response": "\n".join(
                [
                    "TASKLANE_REVIEW_DECISION: needs-fix",
                    "Findings:",
                    "1. Production activation acceptance criterion is not implemented.",
                    "Expected fix:",
                    "- Either update the relevant legal copy in an allowed scope, or",
                    "- explicitly narrow this PR/task as staging/testable consent only and do not present it as completing ALV-33 production Clarity.",
                ]
            )
        }
    }

    assert cli.review_blocker_classification(payload)["class"] == "needs-human"


def test_review_blocker_classification_true_human_blockers() -> None:
    findings = [
        "- Missing API key for the staging payment provider.",
        "- This requires manual approval before continuing.",
        "- Missing product decision on whether to mask names or initials.",
        "- Needs design decision for the public consent banner layout.",
    ]
    for finding in findings:
        payload = {"result": {"final_response": f"TASKLANE_REVIEW_DECISION: needs-fix\nFindings:\n{finding}"}}
        assert cli.review_blocker_classification(payload)["class"] == "needs-human"


def test_recover_dead_running_claim_requires_gateway_pid(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    job = {"id": "tasklane_manual", "state": "running", "claimed_by": "worker-x", "claimed_at": "2000-01-01T00:00:00+00:00"}

    action = cli.recover_dead_running_claim(cfg, job)

    assert action["status"] == "manual-recovery-required"
    assert action["reason"] == "claimed-by-not-gateway-pid"


def test_recover_dead_running_claim_skips_recent_dead_claim(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    job = {
        "id": "tasklane_recent",
        "state": "running",
        "claimed_by": "gateway-12345",
        "claimed_at": cli.now_iso(),
    }
    write_job_record(hermes_home, "running", "tasklane_recent", job)
    monkeypatch.setattr(cli, "process_is_alive", lambda pid: False)

    action = cli.recover_dead_running_claim(cfg, job)

    assert action["status"] == "skipped"
    assert action["reason"] == "too-recent"
    assert (hermes_home / "jobs" / "running" / "tasklane_recent.json").exists()


def test_recover_dead_running_claim_requeues_old_dead_gateway_claim(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    write_job_record(
        hermes_home,
        "running",
        "tasklane_dead",
        {
            "claimed_by": "gateway-12345",
            "claimed_at": "2000-01-01T00:00:00+00:00",
            "attempt": 1,
            "spec": {"request": {"title": "dead"}, "repo": {"key": "repo:///tmp/demo"}},
        },
    )
    monkeypatch.setattr(cli, "process_is_alive", lambda pid: False)

    job = json.loads((hermes_home / "jobs" / "running" / "tasklane_dead.json").read_text(encoding="utf-8"))
    action = cli.recover_dead_running_claim(cfg, job)

    assert action["status"] == "dead-claim-requeued"
    assert not (hermes_home / "jobs" / "running" / "tasklane_dead.json").exists()
    ready = json.loads((hermes_home / "jobs" / "ready" / "tasklane_dead.json").read_text(encoding="utf-8"))
    assert ready["state"] == "ready"
    assert "claimed_by" not in ready
    assert "claimed_at" not in ready
    assert ready["metadata"]["watchdog_retry"]["reason"] == "dead-gateway-claimant"


def test_recover_dead_claims_command_dry_run_leaves_job_running(tmp_path: Path, monkeypatch, capsys) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    write_job_record(
        hermes_home,
        "running",
        "tasklane_dead",
        {"claimed_by": "gateway-12345", "claimed_at": "2000-01-01T00:00:00+00:00"},
    )
    monkeypatch.setattr(cli, "process_is_alive", lambda pid: False)

    exit_code = cli.command_recover_dead_claims(cfg, job_id=None, dry_run=True, json_output=True)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert json.loads(captured.out)["actions"][0]["status"] == "would-requeue"
    assert (hermes_home / "jobs" / "running" / "tasklane_dead.json").exists()
