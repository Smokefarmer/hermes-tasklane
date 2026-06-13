# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Client pairing flow for registering and authenticating MCP clients.
- End-to-end coverage for the inspect-and-fix tools (`get_diff`, `read_file`,
  `write_file`, `apply_patch`, `exec`, `git`, `run_tests`).
- `tasklane doctor` command for diagnosing environment and configuration issues.

## [0.2.0] - 2026-06-12

### Added

- **Reliability layer.** Orphan recovery requeues `running` jobs whose claimant
  process died (crash/reboot); transient failures (workspace prep, agent errors)
  auto-retry with exponential backoff up to `max_attempts` before parking in
  `blocked`; stale claim locks are swept on a configurable cadence. Recovery runs
  at startup and on `reconcile_interval_seconds`, and on demand via the
  `tasklane reconcile` CLI command and the `reconcile_jobs` MCP tool.
- **End-to-end test suite.** A fake `claude` CLI drives full-pipeline, worker-loop,
  and MCP control-plane coverage without invoking the real agent.
- **Observability.** Per-job cost/token telemetry (cost, tokens, turns, runs, wall
  time, summed across repair passes and retries) is captured from the CLI's JSON
  output onto each record. Store-wide metrics are exposed via the `metrics` MCP
  tool and the `tasklane stats` CLI command. A `daily_budget_usd` gate pauses
  claiming of new jobs once 24h spend reaches the budget (running jobs finish;
  claiming resumes when the window rolls; `0` = unlimited).
- **Assembly-line pipelines.** `create_pipeline` builds dependency-chained
  plan → implement → review stage jobs from a single call. Each stage's prompt
  includes the previous stage's final response (`context_from`), and a stage only
  becomes claimable once its predecessor completes (`dependencies`). Per-repo claim
  serialization (`serialize_per_repo`, default on) ensures at most one job per
  repository at a time, so parallel jobs across different repos are safe while
  same-repo jobs queue.

### Fixed

- Direct-push delivery validation no longer rejects valid pushes.

## [0.1.0] - 2026-06-12

### Added

- Standalone rewrite of TaskLane with no Hermes dependency: an autonomous
  coding-job control plane that runs `claude -p` in isolated git worktrees.
- File-backed, atomically locked job store shared between the worker and the
  control plane.
- `tasklane-worker` runner that claims ready jobs, runs the agent in a worktree,
  and validates delivery (`report-only`, `direct-push`, `pull-request`).
- FastMCP control plane (`tasklane-mcp`) with bearer-token auth and audit logging.
- src-layout package with console-script entry points (`tasklane`,
  `tasklane-worker`, `tasklane-mcp`) and smoke tests.

[Unreleased]: https://github.com/Smokefarmer/tasklane/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Smokefarmer/tasklane/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Smokefarmer/tasklane/releases/tag/v0.1.0
