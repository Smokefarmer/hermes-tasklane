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

### Connecting a new client (pairing)

Instead of sharing the `app_token`, a new client can request its own credential
(OpenClaw-style connection approval; on by default, `pairing_enabled: false` disables it):

1. The client requests pairing — public endpoint, no auth:

   ```bash
   curl -X POST https://tasklane.<your-domain>/pair -d '{"name": "my-laptop"}'
   # -> {"client_id": "my-laptop-a1b2c3", "pairing_code": "ABCD2345",
   #     "token": "<save this — shown exactly once>", "status": "pending"}
   ```

2. The operator approves the pairing code — either from an **already-trusted MCP client**
   (`pairing_requests` lists pending codes, then `approve_client`), or directly on the server:

   ```bash
   python -c 'from tasklane import pairing; print(pairing.approve_client("ABCD2345"))'
   ```

3. The client's token now authenticates against `/mcp` exactly like the app token.

Pending requests expire after 15 minutes and are capped at 10 (`/pair` returns 429 beyond
that). `list_clients` shows every client; `revoke_client(client_id)` cuts one off immediately.
Only the SHA-256 of each token is stored (`$TASKLANE_HOME/clients.json`). The legacy
`app_token` keeps working regardless of pairing.

## MCP tools

- **Lifecycle:** `create_task`, `create_pipeline`, `test_deployment`, `list_tasks`, `get_task`,
  `task_events`, `task_logs`, `retry_task`, `cancel_task`, `run_task_now`
- **Inspect & fix** (confined to the job's worktree): `get_diff`, `list_dir`, `read_file`,
  `write_file`, `apply_patch`, `exec`, `git`, `run_tests`
- **Pairing:** `pairing_requests`, `approve_client`, `reject_client`, `list_clients`,
  `revoke_client`
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

The default is `plan,implement,review`. Pass `stages="plan,implement,review,test"` to opt into
an extra **test** stage (report-only, detached on the work branch, chained onto review) that
verifies the change by actually running it — see **Tester roles** below.

Parallelism: raise `max_in_progress` to run multiple jobs concurrently; `serialize_per_repo`
(default true) ensures at most one job per repository at a time, so parallel jobs across
different repos are safe while same-repo jobs queue behind each other.

### Tester roles

TESTER jobs verify an implementation by **actually running it**, not by reading the diff. Two
modes, each a report-only role with its own prompt template:

- **`test-local`** — boots the app in the worktree (via the project's `local_test_command` and
  the pre-authenticated `railway` CLI), then exercises the changed behaviour end-to-end (API
  calls, the app's e2e suite).
- **`test-staging`** — drives the **deployed** staging frontend from the outside with
  [Playwright](https://playwright.dev/) (`npx playwright`): discovers the URL (`staging_url` or
  by running `staging_url_command`), logs in with the test account, and walks the named user flows.

Create one ad-hoc with the `test_deployment` MCP tool
(`test_deployment(repo, mode="staging"|"local", flows="...", project="...")`), or wire it into a
pipeline with the `test` stage.

**Secret env contract.** Credentials are never written into a job spec, prompt, log, or final
response (all are persisted as plain files). Instead, per-project profiles in `config.yaml`
point at a dotenv-style **secret file**:

```yaml
projects:
  acme:
    env_file: /home/me/.secrets/acme.staging.env   # mode 600, owned by you, KEY=VALUE lines
    local_test_command: "npm run dev"
    staging_url: "https://staging.acme.example.com"
    staging_url_command: "railway domain --json"
    test_notes: "Seeded test tenant only; never touch tenant prod-*."
```

The worker loads `env_file` (refusing it unless it is **mode 600 and owned by the current user**)
and injects the values straight into the `claude` subprocess environment — *after* the usual
`ANTHROPIC_*` stripping, and never logged. The prompt only ever receives the sorted **KEY NAMES**
(e.g. `Environment variables available (NAMES ONLY ...): STAGING_URL, TEST_PASSWORD, TEST_USER`),
so the agent reads them from `os.environ` / `$VAR` but no value can leak into a stored artifact.

**Prerequisites:** the `railway` CLI authenticated on the server (config + local boot) and
`npx playwright` available (staging browser flows). Hard rules baked into both templates:
staging/test resources and test accounts only, no destructive operations against shared data,
never print a secret value, and FAIL with a diagnosis rather than skip verification.

**Conversational credential intake.** Credentials don't have to be hand-edited into
`config.yaml` ahead of time — a connected client can capture them in the conversation.
Three audited MCP tools manage a project's secret env file, and they only ever expose
**KEY NAMES**, never values:

- `list_project_secret_keys(repo)` — the names currently stored (e.g. `["TEST_PASSWORD", "TEST_USER"]`).
- `set_project_secrets(repo, secrets)` — merge `{KEY: VALUE}` pairs into the project's
  `env_file`. Keys must be `SCREAMING_SNAKE_CASE`; values non-empty, ≤4096 chars, ≤50 per call.
  If the project has no `env_file` yet, one is created at `~/.tasklane/secrets/<project>.env`
  (directory `0700`, file `0600`) and the registry entry is auto-linked to it.
- `delete_project_secret(repo, key)` — drop one key (others untouched).

The repo must be allowlisted **and** registered in the project registry — i.e. a `projects:`
entry whose `repo:` matches the path. The typical flow: before launching a tester or a
`…,test` pipeline, the client runs `list_project_secret_keys`; if `TEST_USER` / `TEST_PASSWORD`
are missing it asks the human *"Is there a login? Please give me a demo/test user so I can
verify the changes for you"*, stores the answer via `set_project_secrets`, and **never repeats
the values back** in chat or job text.

Security: `set_project_secrets` is exempt from the usual kwargs audit-logging — the audit
entry records only the repo and the sorted **key names**, so a secret value never lands in
`audit.log`. The values reach the secret file (mode 600) and, at run time, the tester
subprocess env — nothing else.

## Delivery modes

- `report-only` (`branch_mode: detached-review`) — analysis only; **no edits** (a dirty worktree fails).
- `direct-push` — commit + push `work_branch` (needs a pushable remote).
- `pull-request` — commit + push + open a PR via `gh` (needs `pr_target` and `gh` auth).

## CLI (local ops without MCP)

```bash
tasklane {submit|list|show|events|logs|retry|cancel|reconcile|stats|doctor}   # after `pip install -e .`
```

`tasklane doctor` runs read-only environment diagnostics — TASKLANE_HOME writability,
`config.yaml` parse/`app_token`/mode 600, job store dirs, `claude`/`git`/`gh` on PATH,
stale claim locks, orphaned running jobs, free disk space, and worktrees-root size — printing
an aligned `OK`/`WARN`/`FAIL` table (exit 1 if any check FAILs) or `--json` for the raw report.
It never mutates state; run `tasklane reconcile` to act on stale locks / orphaned jobs.

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
