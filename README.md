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

- **Lifecycle:** `create_task`, `create_pipeline`, `analyze_project`, `security_audit`,
  `test_deployment`, `list_tasks`, `get_task`, `task_events`, `task_logs`, `retry_task`,
  `cancel_task`, `run_task_now`
- **Inspect & fix** (operate on the job's worktree; `exec`/`git`/`run_tests` are cwd-scoped
  shells, not a sandbox — see Security): `get_diff`, `list_dir`, `read_file`,
  `write_file`, `apply_patch`, `exec`, `git`, `run_tests`
- **Pairing:** `pairing_requests`, `approve_client`, `reject_client`, `list_clients`,
  `revoke_client`
- **Ops:** `worker_status`, `restart_worker`, `prune_worktrees`, `reconcile_jobs`, `metrics`,
  `admin_exec` (gated by config)
- **Project registry & secrets:** `register_project`, `list_projects`, `set_project_secrets`,
  `list_project_secret_keys`, `delete_project_secret`

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

## Architecture analysis

`analyze_project(repo, base_branch="main", id=None)` creates a single architecture-audit
job: role `analyze`, a fresh review branch (`tasklane/architecture-review`, or
`tasklane/architecture-review-<id>` when `id` is given), delivered `direct-push` so the
**review document itself arrives as a reviewable branch** rather than mutating your code.

The audit agent reviews the *whole* repo against best practices **and the project's own
documented intent** — the antidote to AI-era code accretion. It runs four explicit passes:

1. **Map** — directory/module structure, dependency direction, entry points; bounded
   contexts and context bleed (domain importing infrastructure, shared mutable models,
   circular imports).
2. **Intent** — reads `CLAUDE.md`, `docs/adr/**`, and rules files; lists where code
   contradicts documented decisions and which de-facto decisions deserve a new ADR.
3. **Patterns** — right pattern in the right place vs. cargo-culted ceremony.
4. **Accretion hotspots** — oversized files, god-modules, duplication, dead code, and
   test deserts, ranked by risk.

It writes `docs/architecture-review.md` (context map, findings by severity, ADR drafts under
`docs/adr/proposed/`, and a suggested `CLAUDE.md` if absent) and ends its final response with
a ```` ```proposed_tasks` ```` block of ranked remediation tasks. **Those remediation tasks land
as drafts requiring approval** — the draft fan-out parses the block; nothing is acted on
automatically. Review the branch, then approve the remediation tasks you want to run.

## Project registry

A job's prompt is generic, but every repo has its own test/build commands and
authoritative rule files (`CLAUDE.md`, ADRs, `rules/` dirs). Register a **project
profile** once and every job targeting that repo is told its commands and which
docs it MUST read before planning:

```bash
# via the register_project MCP tool (or tasklane.projects.register_project)
register_project(
  path="/abs/repo/path", name="my-service",
  test_command=".venv/bin/python -m pytest -q", build_command="make build",
  docs=["CLAUDE.md", "docs/adr/", "rules/"], base_branch="main",
  default_model=null, merge_policy="manual")
```

Profiles live in `$TASKLANE_HOME/projects.yaml` (keyed by the repo's git toplevel):

```yaml
projects:
  "/abs/repo/path":
    name: my-service
    test_command: ".venv/bin/python -m pytest -q"
    build_command: "make build"
    docs: [CLAUDE.md, docs/adr/, rules/]      # the repo's authoritative rules
    base_branch: main
    default_model: null
    merge_policy: manual                      # manual|auto — informational for now
```

When the worker runs a job it looks up the original repo path; if a profile is
found it injects a **"Project profile:"** prompt section listing the commands
("verify with exactly this command") and, for each doc that actually exists in the
worktree, a `MANDATORY: read <path>` line. `merge_policy` is informational for now.

## Security audits

`security_audit(repo, base_branch="main", scope=None, id=None)` creates ONE report-only
job (role `audit`, `branch_mode: detached-review`) whose primary output is **decomposition**.
The agent first **surveys the attack surface** — entry points, trust boundaries, authn/authz
flows, data stores, third-party calls, secrets/config handling — using the **OWASP Top 10** as
its checklist spine. Then it either:

- **reports findings directly** when the codebase is small enough to read every relevant line, or
- **drafts focused child audits** — a fenced ` ```proposed_tasks ` block of scoped,
  report-only investigations (auth, session handling, database/query layer, frontend/XSS surface,
  API input validation, secrets/config handling, dependency CVEs). Each child carries a precise
  title, `allowed_paths` tight enough that an agent can read every line in scope, what to look
  for, and a severity rationale.

The recommended flow is **survey → draft children → approve → focused audits**: run
`security_audit` (optionally narrowing with `scope`, e.g. `"backend auth only"`), read the
parent's survey and `proposed_tasks` block, then approve the children you want by creating them
(`create_task` with `branch_mode: detached-review`, `delivery_mode: report-only`) — each scoped
to its `allowed_paths`. Findings from both parent and children use the same structured JSON
contract as the **review** role (`severity` / `file` / `line` / `issue` / `suggestion`), so they
flow into the same downstream machinery. Audits are strictly **report-only**: the agent never
exploits, never exfiltrates, and never modifies code, and flags any hardcoded secret as
**CRITICAL** with rotation advice.

## Tester roles

TESTER jobs verify an implementation by **actually running it**, not by reading the diff.
Two report-only roles, each with its own prompt template:

- **`test-local`** — boots the app in the worktree (the project's `local_test_command`) and
  exercises the changed behaviour end-to-end.
- **`test-staging`** — drives the deployed staging frontend from the outside with
  [Playwright](https://playwright.dev/) (`npx playwright`): discovers the URL (`staging_url`
  or `staging_url_command`), logs in with the test account, and walks the named user flows.

Create one ad-hoc with `test_deployment(repo, mode="staging"|"local", flows="…")`, or add the
optional `test` stage to a pipeline (`stages="plan,implement,review,test"`).

**Secret contract.** Credentials never appear in a job spec, prompt, log, or final response.
A project profile's `env_file` (a mode-600 `KEY=VALUE` file — refused if not 600 / not owned by
you) is the only place they live. The worker loads it and injects the **values** straight into
the `claude` subprocess (after the usual `ANTHROPIC_*` stripping, never logged); the prompt only
ever receives the sorted **KEY NAMES**. Store credentials conversationally with
`set_project_secrets(repo, {"TEST_USER": "...", "TEST_PASSWORD": "..."})` — the audit log records
only repo + key names. When a client asks for a pipeline with a `test` stage and
`list_project_secret_keys` shows no login, it should ask you for a demo/test user, store it via
`set_project_secrets`, and never echo the values back.

**Prerequisites:** the `railway` CLI (config/local boot) and `npx playwright` (staging flows)
available on the server. Hard rules in both templates: staging/test resources and test accounts
only, nothing destructive, never print a secret value, and FAIL with a diagnosis rather than
skip verification.

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
- `write_file`/`apply_patch`/`read_file`/`list_dir` are path-confined to a job's worktree
  (traversal outside it is rejected), and mutating tools refuse to run on a job with no live
  worktree (they never touch your original checkout).
- `exec`/`git`/`run_tests` set their **cwd** to the worktree but run a full `bash -lc` shell —
  they are **not a sandbox**: an absolute path or `cd ..` can reach the rest of the filesystem
  as the server user. They are gated only by the bearer token. Treat any paired/token-holding
  client as able to run code on the host. Real confinement would need an OS sandbox
  (bubblewrap / firejail / unshare / a container) — see the roadmap.
- `admin_exec` (explicitly unconfined server shell, not even cwd-scoped) is disabled unless
  `enable_admin_exec: true`.
- Jobs run via `claude -p` on your Claude subscription (legitimate Claude Code usage).

> ⚠️ This is a remote code-execution control plane by design. Keep `app_token` secret and rotate
> it if exposed.

## License

See [LICENSE](LICENSE).
