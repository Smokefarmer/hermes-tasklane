# TaskLane

**A standalone, autonomous coding-job control plane.** Drop in a coding task; a server-side
worker runs it with the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) (`claude -p`)
in an isolated git worktree, then commits / pushes / opens a PR. Drive and **fix** jobs remotely
from any MCP client (e.g. Claude Code on your laptop) over an authenticated HTTP endpoint.

No external services, no database — just files on disk, git worktrees, and the `claude` CLI.

> Previously this repo was a file-inbox layer on top of the Hermes agent. It has been rewritten
> as a **fully standalone** runner + MCP control plane with no Hermes dependency. (The old
> implementation remains in git history.)

## Architecture

```
MCP client (e.g. laptop Claude Code)
   │  MCP over HTTPS, Authorization: Bearer <app_token>
   ▼
reverse proxy / tunnel  →  tasklane-mcp   (127.0.0.1:8788, FastMCP, auth + audit)
                                  │  shares the file-backed job store
                                  ▼
                          $TASKLANE_HOME/jobs/   (records, events, logs, locks)
                                  ▲
                          tasklane-worker  → claims ready jobs, runs `claude -p`
                                             in an isolated git worktree, validates delivery
```

Two processes share an atomic, file-locked job store, so the worker keeps running jobs even if
the control plane is down (and vice-versa). Job lifecycle:

```
draft → ready → running → { completed | blocked | failed | needs-human }
```

Blocked/failed jobs **keep their worktree** so an operator (you, or a remote agent) can inspect
and fix them, then `retry`.

## Requirements

- Python 3.10+
- `git`, and the `claude` CLI authenticated with your Claude subscription
- `gh` (only for `pull-request` delivery)

## Install

```bash
git clone https://github.com/Smokefarmer/tasklane.git && cd tasklane
python3 -m venv venv && ./venv/bin/pip install -e .
./venv/bin/tasklane-worker   # the worker
./venv/bin/tasklane-mcp      # the MCP control plane
```

For a service install see `scripts/install.sh` and the `scripts/tasklane-*.service` unit templates.
Data lives under `$TASKLANE_HOME` (default `~/.tasklane`).

## Configure — `$TASKLANE_HOME/config.yaml` (mode 600)

Auto-generated on first run with a random `app_token`. See [`config.example.yaml`](config.example.yaml).
Key fields:

- `app_token` — bearer token every MCP client must send.
- `permission_mode: bypassPermissions` — **required** so headless jobs can `git commit`/`push`
  (`acceptEdits` only auto-approves file edits). Safe because each job is confined to an isolated worktree.
- `repos_allowlist` — `[]` allows any path; list absolute prefixes to restrict (recommended for remote use).
- `public_hostnames` — set to your external host (e.g. `tasklane.example.com`) when fronting it with a
  proxy/tunnel, or the MCP transport rejects the `Host` header.
- `enable_admin_exec` — server-level shell tool, off by default.
- `max_attempts` / `retry_backoff_seconds` — transient failures (workspace prep, agent errors)
  auto-retry with exponential backoff until the attempt cap, then park in `blocked`.
  Delivery-validation failures go straight to `blocked` (the in-job repair pass already ran
  and the worktree is kept for the fix tools).
- `reconcile_interval_seconds` / `stale_lock_seconds` — the worker recovers orphaned `running`
  jobs (claimant process died, e.g. crash/reboot) and sweeps stale claim locks on this cadence,
  and once at startup. Also available on demand: `tasklane reconcile` / the `reconcile_jobs` MCP tool.
- `daily_budget_usd` — per-job cost/token telemetry is captured from the CLI's JSON output onto
  each record (`metrics`: cost, tokens, turns, runs, wall time, summed across repair passes and
  retries). When 24h spend reaches this budget the worker pauses claiming new jobs (running jobs
  finish; claiming resumes when the window rolls). `0` = unlimited. Inspect via `tasklane stats`,
  the `metrics` MCP tool, or the status page.

## Connect an MCP client

Expose `127.0.0.1:8788` via a reverse proxy or a tunnel (e.g. Cloudflare Tunnel) at
`https://tasklane.<your-domain>`, then:

```bash
claude mcp add --transport http tasklane https://tasklane.<your-domain>/mcp \
  --header "Authorization: Bearer <app_token>" --scope user
```

A browser status page is available at `https://tasklane.<your-domain>/status?token=<app_token>`
(worker health + recent jobs, auto-refresh).

## Teach your Claude

TaskLane ships its own operating doctrine so any connecting client becomes a
competent operator:

- **Built-in:** the MCP server advertises a compact usage doctrine via FastMCP
  `instructions` (see `USAGE` in [`src/tasklane/mcp_server.py`](src/tasklane/mcp_server.py)) —
  loaded into the client's context on connect.
- **Skill:** [`skills/tasklane/SKILL.md`](skills/tasklane/SKILL.md) is a Claude Code
  skill with the full playbook (delegation decision, good-vs-bad briefs, pipelines,
  monitoring, the blocked-job repair flow, stacked reviews). Install it:

  ```bash
  cp -r skills/tasklane ~/.claude/skills/
  ```

- **Recipes:** copy-pasteable, end-to-end flows in [`docs/recipes/`](docs/recipes/):
  - [overnight-batch.md](docs/recipes/overnight-batch.md) — queue a backlog, review in the morning.
  - [fire-and-forget-bugfix.md](docs/recipes/fire-and-forget-bugfix.md) — hand off one bug, repair it if it blocks.
  - [pr-review-bot.md](docs/recipes/pr-review-bot.md) — report-only review gate, single or stacked branches.
  - [feature-pipeline.md](docs/recipes/feature-pipeline.md) — plan → implement → review for meatier features.

## MCP tools

- **Lifecycle:** `create_task`, `create_pipeline`, `list_tasks`, `get_task`, `task_events`,
  `task_logs`, `retry_task`, `cancel_task`, `run_task_now`
- **Inspect & fix** (confined to the job's worktree): `get_diff`, `list_dir`, `read_file`,
  `write_file`, `apply_patch`, `exec`, `git`, `run_tests`
- **Ops:** `worker_status`, `restart_worker`, `prune_worktrees`, `reconcile_jobs`, `metrics`,
  `admin_exec` (gated by config)

A typical operator flow: `create_task` → watch with `get_task`/`task_logs` → if it blocks,
`get_diff` + `exec` to find the problem, `write_file`/`apply_patch` to fix, `retry_task`.

### Pipelines (assembly line)

`create_pipeline` creates dependency-chained stage jobs from one call:
**plan** (report-only on the base branch) → **implement** (delivers on `work_branch`) →
**review** (report-only on the work-branch tip). Each stage's prompt includes the previous
stage's final response (`context_from`), and a stage only becomes claimable once its
predecessor completed (`dependencies`). `stages` selects a subset (implement is required).

Parallelism: raise `max_in_progress` to run multiple jobs concurrently; `serialize_per_repo`
(default true) ensures at most one job per repository at a time, so parallel jobs across
different repos are safe while same-repo jobs queue behind each other.

## Delivery modes

- `report-only` (`branch_mode: detached-review`) — analysis only; **no edits** (a dirty worktree fails).
- `direct-push` — commit + push `work_branch` (needs a pushable remote).
- `pull-request` — commit + push + open a PR via `gh` (needs `pr_target` and `gh` auth).

## CLI (local ops without MCP)

```bash
tasklane {submit|list|show|events|logs|retry|cancel|reconcile|stats}   # after `pip install -e .`
```

## Security

- Bound to `127.0.0.1`; expose only through a proxy/tunnel. Every request needs the bearer token
  (constant-time compare); unauthorized attempts are audit-logged with client IP. DNS-rebinding
  protection is on. Add a second gate (e.g. Cloudflare Access) for defense in depth.
- `exec`/`write_file`/`apply_patch` are confined to a job's worktree (path traversal rejected).
- `admin_exec` (unconfined server shell) is disabled unless explicitly enabled.
- Jobs run via `claude -p` on your Claude subscription (legitimate Claude Code usage).

> ⚠️ This is a remote code-execution control plane by design. Keep `app_token` secret and rotate
> it if exposed.

## License

See [LICENSE](LICENSE).
