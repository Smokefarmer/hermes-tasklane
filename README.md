# hermes-tasklane

A polished, file-based task inbox for Hermes v10 JobStore runs.

`hermes-tasklane` gives you the useful parts of a production Hermes queue setup without forcing your team onto Nextcloud. Instead of CalDAV tasks, your teammates drop plain text/Markdown task files into an inbox folder. The package:

- turns inbox files into Hermes JobStore records
- gates submission so only one active coding run per repo is launched by default
- tracks submitted tasks locally
- reconciles tasks back out of JobStore and governed run state
- repairs common delivery-state drift by checking GitHub PR + CI reality

It is designed for teams that already run Hermes and want a simple, shareable “task lane” on top.

## What this package includes

1. File-based task source
- human-editable Markdown task files
- no Nextcloud dependency
- one task file = one launchable governed run

2. Safe JobStore bridging
- writes Hermes job records into `~/.hermes/jobs/ready/`
- checks active JobStore jobs and governed runs
- checks repo locks
- avoids duplicate launches per repo by default

3. Reconciliation helpers
- watches JobStore records in `~/.hermes/jobs/`
- keeps compatibility with governed run records in `~/.hermes/runs/`
- moves submitted task files into `completed/`, `failed/`, or `cancelled/`
- normalizes stale delivery blockers by checking GitHub PR + CI state

4. Queue watchdog
- reviews queue health on a timer
- detects failed, blocked, stale, and dead-gateway jobs
- checks expected base-branch policy per project or repo
- defaults to observe-only and can optionally run guarded safe retries
- can auto-salvage failed pull-request jobs that already produced scoped, verified code changes

## What it does not replace

This is not a full replacement for Hermes itself.

You still need a Hermes install with:
- the Hermes v10 JobStore watcher active in the gateway
- Git + GitHub access configured for the repos you want Hermes to operate on

## Install

### Quick install

```bash
git clone https://github.com/Smokefarmer/hermes-tasklane.git
cd hermes-tasklane
./scripts/install.sh --systemd
hermes-tasklane doctor
hermes-tasklane status
hermes-tasklane watch
```

That installs the package, initializes config and folders, and enables user-level systemd timers when available. If the machine has no usable user systemd session, the installer skips timers cleanly and you can use the cron fallback instead.

Bundled Hermes skills, including `hermes-tasklane-intake`, are installed to:

```text
~/.hermes/skills/software-development/
```

Use `--no-skills` if you only want the CLI.

### Quick use

1. Put a task file in:

```text
~/.local/share/hermes-tasklane/inbox/
```

2. Submit it:

```bash
hermes-tasklane sync
```

3. Check progress:

```bash
hermes-tasklane status
hermes jobs list --json
```

### Telegram updates

Task files can include Telegram metadata so Hermes can send start/completion/failure updates:

```yaml
platform: telegram
chat_id: -1001234567890
thread_id: 42
```

You can also set defaults in `~/.config/hermes-tasklane/config.json`:

```json
{
  "default_platform": "telegram",
  "default_chat_id": "-1001234567890",
  "default_thread_id": null
}
```

Use per-task `chat_id` values when one machine serves multiple project groups.

## Group Master Prompt

Paste this into the Hermes group chat where the agent should create tasklane jobs from voice or text requests:

```text
You are the Hermes Tasklane Intake agent for this group.

When I describe work by voice or text, turn it into safe Hermes tasklane jobs. Do not start coding directly in the chat.

Always use the hermes-tasklane-intake skill when I ask for features, bugs, fixes, refactors, reviews, audits, or batches of coding work.

Default workflow:
1. Create tasklane Markdown files in:
   ~/.local/share/hermes-tasklane/inbox/
   If this request came from Telegram and the chat ID is available, include:
   platform: telegram
   chat_id: <current chat id>
   thread_id: <current topic/thread id if available>
2. Run:
   hermes-tasklane sync
   hermes-tasklane status
   hermes jobs list --json
3. Report task IDs, job IDs, delivery groups, dependencies, and anything deferred or invalid.

Required before creating jobs:
- repo path
- base branch
- goal
- delivery shape: one PR, multiple PRs, direct push, or report-only
- acceptance criteria
- Telegram chat ID if the group expects job updates and no tasklane default_chat_id is configured

If required information is missing, ask one short clarification question.

Safe defaults:
- request_type: task-small
- branch_mode: new-branch
- delivery_mode: pull-request
- review_loops: 3
- security_review: true
- allow_unlisted_paths: false when allowed_paths can be identified

Never use direct-push unless I explicitly allow it, or unless you are creating implementation subtasks inside one delivery_group where one final task will open the PR.

For one small independent task:
- create one tasklane task
- use delivery_mode: pull-request
- use branch_mode: new-branch
- use allowed_paths if you can infer them safely

For a batch that should become one PR:
- use one shared delivery_group
- implementation tasks use delivery_mode: direct-push
- serial tasks use depends_on
- parallelizable tasks omit depends_on
- create exactly one final integration task with:
  - branch_mode: existing-branch
  - delivery_mode: pull-request
  - same delivery_group
  - depends_on all implementation task IDs
- the final task reviews the grouped branch, runs verification, and opens one PR

For two big features:
- use two different delivery_group values
- each delivery_group gets its own final PR task
- do not mix unrelated features into the same PR

For audits:
- use delivery_mode: report-only unless I explicitly ask for code changes

After sync, answer in this format:

Tasklane queued:
- task_id:
- job_id:
- repo:
- base_branch:
- delivery_group:
- depends_on:
- delivery:
- allowed_paths:

Deferred or invalid:
- none, or list exact reason

Next review point:
- PR expected, report expected, or waiting for dependency chain
```

### Manual path

### 1. Clone the repo

```bash
git clone https://github.com/Smokefarmer/hermes-tasklane.git
cd hermes-tasklane
```

### 2. Install the CLI

```bash
python3 -m pip install .
```

Or editable while iterating:

```bash
python3 -m pip install -e .
```

### 3. Initialize local folders

```bash
hermes-tasklane init
```

This creates:
- `~/.config/hermes-tasklane/config.json`
- `~/.local/share/hermes-tasklane/inbox/`
- `~/.local/share/hermes-tasklane/submitted/`
- `~/.local/share/hermes-tasklane/completed/`
- `~/.local/share/hermes-tasklane/failed/`
- `~/.local/share/hermes-tasklane/cancelled/`
- `~/.local/share/hermes-tasklane/examples/`

### 4. Verify Hermes wiring

```bash
hermes-tasklane doctor
```

## Quick start

### Create a task file

Drop a Markdown file into your inbox, for example:

`~/.local/share/hermes-tasklane/inbox/fix-login-flow.md`

```md
---
repo_path: /mnt/data/workspace/Alvin
branch_base: develop
branch_mode: new-branch
delivery_mode: pull-request
request_type: task-small
platform: telegram
chat_id: -5246337506
project: Alvin
---
Fix the login flow regression, add or update focused tests, run the strongest relevant verification, and prepare a concise review-ready result.
```

### Submit inbox tasks into Hermes

```bash
hermes-tasklane sync
```

If the repo is idle, the task is converted into a JobStore record under:

```text
~/.hermes/jobs/ready/
```

Before submission, `sync` runs a production preflight. Risky tasks stay in
`inbox/` and get a sibling report such as `fix-login-flow.md.preflight.json`
instead of entering Hermes. Current blockers include unbounded large tasks,
unsafe direct-push large tasks, missing dependencies, dependency cycles, mixed
base branches inside one delivery group, and multiple independent roots trying
to mutate the same integration branch.

If the repo is busy, the task stays deferred in the inbox and will be eligible on the next sync.

### Reconcile finished work back into tasklane state

```bash
hermes-tasklane reconcile
```

This:
- checks JobStore state
- checks legacy governed run state when needed
- moves task files out of `submitted/`
- writes a small `.result.json` note beside the moved task file
- attempts delivery reconciliation for stale PR/CI blockers

### Inspect status

```bash
hermes-tasklane status
```

### Night watchdog

```bash
hermes-tasklane watch
```

`watch` is the production queue supervisor. It reads Hermes JobStore state and reports:

- gateway health
- running/ready/failed/blocked/needs-human counts
- stale running jobs
- dead `gateway-<pid>` claimants
- branch policy mismatches
- blocked jobs that are not explicitly ignored

Configure branch policy and known expected blocked jobs in `~/.config/hermes-tasklane/config.json`:

```json
{
  "watch": {
    "mode": "observe",
    "auto_salvage": false,
    "baseline_verification": false,
    "allow_matching_baseline_failures": false,
    "bootstrap_timeout_seconds": 1800,
    "verification_timeout_seconds": 1800,
    "stale_running_minutes": 180,
    "max_retry_attempts": 3,
    "bootstrap_commands": [],
    "bootstrap_profiles": {
      "Treasure Hunter": [
        "npm ci"
      ]
    },
    "verification_commands": [
      "git diff --check"
    ],
    "verification_profiles": {
      "Treasure Hunter": [
        "git diff --check",
        "npm run client:typecheck",
        "npm run server:typecheck"
      ]
    },
    "expected_base_branches": {
      "Alvin": "develop",
      "Treasure Hunter": "development"
    },
    "ignored_blocked_jobs": [
      "tasklane_d08a145fbccc"
    ]
  }
}
```

Use observe mode for dry runs and first installs:

```bash
hermes-tasklane watch --mode observe --quiet-ok
```

Use guarded mode for unattended production after the repo policies and verification commands are configured:

```bash
hermes-tasklane watch --mode guarded
```

Guarded mode retries narrowly classified transient failures such as provider HTTP 500/502/503/504, APIError, timeouts, and rate-limit transport failures only when the failed job worktree is clean. It never retries blocked jobs, planning/schema errors, or no-code-change results.

If `auto_salvage` is enabled, guarded mode treats dirty failed pull-request jobs differently:

- inspect the job worktree and current branch
- require the job failure to match a safe provider/job failure pattern
- require all changed files to stay inside the task scope
- run configured bootstrap commands, such as dependency install or code generation
- run configured verification commands
- optionally compare failed verification commands against the base branch
- commit remaining dirty changes
- push the task branch
- find or create the pull request
- mark the job and tasklane file completed

If any guard fails, the job stays failed or needs-human and is not pushed.

Baseline-aware verification is intentionally strict. With
`baseline_verification: true`, Tasklane re-runs failed verification commands on
the base ref in a temporary detached worktree. With
`allow_matching_baseline_failures: true`, a failed command can be accepted only
when the base branch fails with matching output. If the base passes or the
output differs, delivery stays `needs-human`.

Manual salvage commands:

```bash
hermes-tasklane salvage <job-id>
hermes-tasklane salvage <job-id> --verify
hermes-tasklane salvage <job-id> --auto
```

For Telegram watchdog messages, set `TELEGRAM_BOT_TOKEN` and configure `watch.telegram_chat_id` or `default_chat_id`, then run:

```bash
hermes-tasklane watch --notify --quiet-ok
```

### Pipeline dashboard

```bash
hermes-tasklane dashboard --host 127.0.0.1 --port 8765
```

The dashboard is a read-only web view over the same JobStore files used by `watch`. It shows queue health, active work, failed/blocked jobs, completed jobs, and per-job event details. It does not create, retry, cancel, or edit jobs.

For a trusted internal network, bind it to all interfaces:

```bash
hermes-tasklane dashboard --host 0.0.0.0 --port 8765
```

Then open:

```text
http://<server-lan-ip>:8765
```

Do not expose this dashboard directly to the public internet without an authenticating reverse proxy.

## Recommended automation

### systemd user timers (recommended)

```bash
./scripts/install.sh --systemd
systemctl --user status hermes-tasklane-sync.timer
systemctl --user status hermes-tasklane-reconcile.timer
systemctl --user status hermes-tasklane-watch.timer
```

The installer also copies `hermes-tasklane-dashboard.service` but does not enable it automatically because binding a dashboard is a deployment choice.

### cron fallback

If you do not use systemd user timers, run both of these periodically:

```bash
*/5 * * * * /usr/bin/env hermes-tasklane sync
*/5 * * * * /usr/bin/env hermes-tasklane reconcile
*/15 * * * * /usr/bin/env hermes-tasklane watch --mode observe --quiet-ok
```

That gives you a lightweight, always-on text-file task queue on top of Hermes.

## Task file format

Task files are plain text or Markdown with frontmatter.

Required fields:
- `repo_path`
- `branch_base` or `base_branch`

Optional fields:
- `request_type`: `bug-small`, `task-small`, `feature-large`, or `refactor-large`
- `branch_mode`: `new-branch`, `existing-branch`, or `detached-review`
- `work_branch`: required for `existing-branch`, generated for `new-branch` when omitted
- `delivery_mode`: `pull-request`, `direct-push`, or `report-only`
- `delivery_group`: shared branch/PR group for multiple task files
- `depends_on`: comma-separated task IDs that must complete first
- `allowed_paths`: comma-separated path allowlist
- `denied_paths`: comma-separated path denylist
- `allow_unlisted_paths`: `true` or `false`
- `review_loops`: max self-review loops, default `3`
- `security_review`: `true` or `false`
- `platform`
- `chat_id`
- `thread_id`
- `project`
- `id`
- any extra metadata fields you want copied into local task metadata

Example:

```md
---
id: invoice-export-hardening
repo_path: /srv/work/finance-app
branch_base: main
branch_mode: new-branch
delivery_mode: pull-request
request_type: task-small
delivery_group: invoice-export
project: Finance App
platform: telegram
chat_id: -1001234567890
thread_id: 55
allowed_paths: README.md, docs/
allow_unlisted_paths: false
priority: high
owner: alice
---
Harden the invoice export edge cases, add regression coverage, run verification, and prepare a review-ready summary.
```

## Commands

### `hermes-tasklane init`
Create config, folders, and an example task.

### `hermes-tasklane doctor`
Check whether the expected Hermes and tasklane directories exist.

### `hermes-tasklane sync`
Read inbox files and write eligible Hermes JobStore records.

### `hermes-tasklane reconcile`
Reconcile submitted tasks from JobStore/governed run state and attempt PR/CI normalization.

### `hermes-tasklane status`
Show inbox/submitted/completed counts and current JobStore/governed run states.

### `hermes-tasklane watch`
Review queue health for unattended operation. Defaults to observe-only. Use `--expected-base Project=branch` for one-off branch policy checks, `--ignore-blocked JOB_ID` for known obsolete jobs, `--json` for machine-readable output, and `--mode guarded` for narrowly safe transient retries and configured auto-salvage.

### `hermes-tasklane salvage`
Inspect or deliver a failed pull-request job that produced code changes. `--verify` runs the configured checks without committing or pushing. `--auto` runs the full guarded salvage path.

### `hermes-tasklane dashboard`
Run a read-only web dashboard. Defaults to `127.0.0.1:8765`; use `--host 0.0.0.0` only on a trusted internal network.

## Delivery reconciliation behavior

One of the painful real-world failure modes in governed coding systems is stale delivery state:
- the run says `github-no-write-permission`
- but the branch actually exists remotely
- and a PR may already exist
- or CI may simply still be queued/pending

`hermes-tasklane reconcile` handles that by:
- reading the run’s repo metadata
- checking the GitHub remote from the repo/worktree
- finding the PR for the delivery branch
- checking CI status using GitHub REST API
- normalizing the run to:
  - `completed` if PR exists and CI is green
  - `blocked` + `ci-pending` if PR exists and CI is still pending

It falls back to credentials from:
- `GITHUB_TOKEN` / `GH_TOKEN`
- or `~/.git-credentials`

This was added specifically because real Hermes deployments can end up with:
- unauthenticated `gh`
- misleading token-path failures
- private repos where branch/PR reality is healthier than the stored blocker

## Example architecture

```text
Markdown task file
      ↓
hermes-tasklane sync
      ↓
~/.hermes/jobs/ready/*.json
      ↓
Hermes JobStore watcher
      ↓
Agent run + PR + CI
      ↓
hermes-tasklane reconcile
      ↓
completed/ failed/ cancelled/
```

## Why files instead of Nextcloud?

Because teams often want:
- something dead simple
- zero external dependency
- easy Git-friendly examples
- a setup colleagues can install in minutes

An inbox directory of text files is enough.

## Suggested team workflow

1. Install Hermes.
2. Install `hermes-tasklane`.
3. Point each teammate at their local repos.
4. Drop tasks into `inbox/`.
5. Run `sync` + `reconcile` on a timer.
6. Let Hermes do the implementation work.

For group-chat usage before full production hardening, use:

- `docs/group-chat-usage.md`

## Tell another Hermes to install it

A ready-to-paste install prompt for another Hermes instance lives in:

- `docs/hermes-install-prompt.md`

## Development

```bash
python3 -m pip install -e .
hermes-tasklane init
hermes-tasklane doctor
```

## Safety note

This package writes JobStore records for Hermes to execute. Only point it at repos and branches you actually want Hermes to work on.

## License

MIT
