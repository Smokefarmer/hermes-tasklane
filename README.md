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
git clone https://github.com/Smokefarmer/hermes-tasklane.git tasklane && cd tasklane
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
# run directly (PYTHONPATH=.), or `pip install -e .` for the `tasklane*` entry points
PYTHONPATH=. ./venv/bin/python -m tasklane.worker      # the worker
PYTHONPATH=. ./venv/bin/python -m tasklane.mcp_server  # the MCP control plane
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

## Connect an MCP client

Expose `127.0.0.1:8788` via a reverse proxy or a tunnel (e.g. Cloudflare Tunnel) at
`https://tasklane.<your-domain>`, then:

```bash
claude mcp add --transport http tasklane https://tasklane.<your-domain>/mcp \
  --header "Authorization: Bearer <app_token>" --scope user
```

A browser status page is available at `https://tasklane.<your-domain>/status?token=<app_token>`
(worker health + recent jobs, auto-refresh).

## MCP tools

- **Lifecycle:** `create_task`, `list_tasks`, `get_task`, `task_events`, `task_logs`, `retry_task`,
  `cancel_task`, `run_task_now`
- **Inspect & fix** (confined to the job's worktree): `get_diff`, `list_dir`, `read_file`,
  `write_file`, `apply_patch`, `exec`, `git`, `run_tests`
- **Ops:** `worker_status`, `restart_worker`, `prune_worktrees`, `admin_exec` (gated by config)

A typical operator flow: `create_task` → watch with `get_task`/`task_logs` → if it blocks,
`get_diff` + `exec` to find the problem, `write_file`/`apply_patch` to fix, `retry_task`.

## Delivery modes

- `report-only` (`branch_mode: detached-review`) — analysis only; **no edits** (a dirty worktree fails).
- `direct-push` — commit + push `work_branch` (needs a pushable remote).
- `pull-request` — commit + push + open a PR via `gh` (needs `pr_target` and `gh` auth).

## CLI (local ops without MCP)

```bash
PYTHONPATH=. python -m tasklane.cli {submit|list|show|events|logs|retry|cancel}
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
