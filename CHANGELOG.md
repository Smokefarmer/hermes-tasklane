# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-06-13

### Added

- **Role-based stage prompts.** Per-role prompt templates (`plan`, `implement`,
  `review`, `fix`, `analyze`, `audit`, `test-local`, `test-staging`) shipped in the
  package and overridable at `$TASKLANE_HOME/prompts/<role>.md`. The `role` spec
  field selects one; without it the generic prompt is used.
- **Project registry.** Per-repo profiles (`projects.yaml`, keyed by repo path):
  test/build commands, authoritative docs the job MUST read (CLAUDE.md, ADRs), and
  tester fields (`env_file`, `local_test_command`, `staging_url`,
  `staging_url_command`, `test_notes`). Managed via `register_project` /
  `list_projects`; injected as a "Project profile" prompt section.
- **Client pairing (connection approval).** `POST /pair` issues a one-time token +
  pairing code; an operator approves via `approve_client` (or rejects/revokes).
  Only the SHA-256 of each token is stored. Auth is read-only, fails closed, and
  throttles its `last_seen` write so it never blocks on lock contention.
- **Architecture-audit role.** `analyze_project` reviews a whole repo against best
  practices and its own documented intent (bounded contexts, ADR drift, patterns,
  accretion hotspots), delivering a review doc and proposing remediation drafts.
- **Security-audit role.** `security_audit` surveys the attack surface (OWASP Top
  10) and decomposes it into scoped child audits proposed as drafts.
- **Tester roles.** `test-local` (boot + exercise in the worktree) and
  `test-staging` (Playwright against the deployed frontend), plus `test_deployment`
  and an optional pipeline `test` stage. Secrets live only in a mode-600 `env_file`,
  injected into the job subprocess; the prompt sees only KEY NAMES. Conversational
  intake via `set_project_secrets` / `list_project_secret_keys` /
  `delete_project_secret`, with secret values redacted from the audit log.
- **Draft fan-out.** Agents may PROPOSE follow-up jobs via a `proposed_tasks` block;
  they are created as inert `draft` jobs that a human approves (`approve_draft` /
  `approve_all_drafts` / `reject_draft`) before they can run.
- **Self-teaching client surface.** FastMCP `instructions` doctrine, a `tasklane`
  Claude Code skill, and copy-paste workflow recipes.
- **`tasklane doctor`** diagnostics for environment and configuration issues.
- **End-to-end coverage** for the inspect-and-fix tools and the tester flow.

### Changed

- `JobStore.transition` is serialized under a per-job lock, so concurrent
  transitions (reconciler vs worker vs MCP ops) can no longer leave a record in
  two state dirs.
- Single source of truth for atomic file writes (`tasklane.atomicio`) and for the
  prompt block builders (`tasklane.prompts.render`); the MCP control plane is split
  into a `tasklane.mcp` package (every module under 500 lines).

### Fixed

- `analyze`/`audit` fan-out now emits the canonical JSON the parser expects (the
  prior markdown-bullet form produced zero drafts); the fence parser no longer
  matches an inline mention of the block in prose.
- Mutating fix tools refuse to operate on a job with no live worktree instead of
  silently editing the original checkout; read-only tools label the fallback.
- Honest docs: `exec`/`git`/`run_tests` are cwd-scoped shells, not a sandbox.
- Removed a `PYTHONPATH` leak in the worker service unit that shadowed each job's
  worktree install.

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
