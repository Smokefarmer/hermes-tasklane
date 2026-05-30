# hermes-tasklane

A polished, file-based task inbox for Hermes v10 JobStore runs.

`hermes-tasklane` gives you the useful parts of a production Hermes queue setup without forcing your team onto Nextcloud. Instead of CalDAV tasks, your teammates drop plain text/Markdown task files into an inbox folder. The package:

- turns inbox files into Hermes JobStore records
- gates submission so only one active coding run per repo is launched by default
- tracks submitted tasks locally
- reconciles tasks back out of JobStore and governed run state
- repairs common delivery-state drift by checking GitHub PR + CI reality

It is designed for teams that already run Hermes and want a simple, shareable “task lane” on top.

## Operating Model

Tasklane is an orchestration layer for Hermes coding work. It is built around a few durable files and commands:

- `inbox/`: task files waiting to become Hermes jobs
- `submitted/`: task files already handed to Hermes
- `completed/`, `failed/`, `cancelled/`: reconciled task history
- `~/.hermes/jobs/ready/`: JobStore records Hermes can claim
- `hermes-tasklane reconcile`: moves completed job reality back into Tasklane state
- `hermes-tasklane watch --mode guarded`: safe unattended health and recovery loop
- `hermes-tasklane wave-runner`: issue-to-wave planner and queue manager

The normal production loop is:

```bash
hermes-tasklane status
hermes-tasklane inspect <job-id> --json
hermes-tasklane watch --mode guarded --json
hermes-tasklane reconcile
hermes-tasklane wave-runner --repo /path/to/repo --project "Project Name" --base development --enqueue --notify --json
```

Use `status` for the default machine-readable queue summary, `inspect <job-id>` for a read-only operator drill-down, `watch` to detect and safely recover queue problems, `reconcile` to clean up completed work, and `wave-runner` to queue the next issue wave only when project PR caps allow it.

## Features At A Glance

- Manual task files for one-off work.
- Delivery groups for many implementation jobs that land in one final PR.
- Codex review gates on final PR jobs.
- Review-fix jobs that push to the existing PR branch.
- Merge gates for projects that allow automated merge.
- Issue wave planning from GitHub issues.
- Active PR caps per project.
- Contract/large-feature/docs lane risk caps.
- Duplicate issue protection using active PRs and merged PR references.
- Guarded watchdog recovery for stale worktrees and narrow transient failures.
- Telegram/Hermes notifications for real blockers.
- Read-only dashboard for queue inspection.
- Shared operator liveness summaries for status, watch, inspect, and dashboard views.
- Optional wave lane-plan artifacts that map issues, lanes, branches, tasks, and jobs.

For a teammate-facing walkthrough, start with [docs/onboarding.md](docs/onboarding.md).

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


## Operator inspection workflow

`hermes-tasklane status` remains JSON by default and is the fastest overview for automation. Active, waiting, blocked, and needs-human job summaries include shared liveness fields such as `derived_state`, `waiting_for`, `claimant_pid`, `claimant_alive`, `runtime_seconds`, `recovery_eligible`, `last_watchdog_action`, and `historical_last_error`. Derived states include `ready`, `waiting-on-dependency`, `running-alive`, `dead-claimant`, `running-unknown-claimant`, `blocked`, `needs-human`, `failed`, `completed`, and `unknown`.

Use `hermes-tasklane inspect <job-id>` when an operator needs detail for one job. `inspect` is read-only and does not take the Tasklane lock. It supports human output by default and `--json` for tooling, plus `--events N` to limit recent JobStore events. The report includes the job summary, liveness, dependency states, recent events, branch/delivery info, PR visibility, lane-plan context when available, and a recommended next action.

`watch --mode observe --json` is read-only by default. Config-level `watch.notify` is ignored in observe mode unless the CLI explicitly passes `--notify`; guarded mode preserves the existing explicit/config notification behavior.

If `status`, `inspect`, or `watch` reports `derived_state: dead-claimant`, use `hermes-tasklane recover-dead-claims --dry-run --json` first to verify the claimant process is gone and the claim is older than the grace period. Re-run without `--dry-run` to move eligible dead-claim jobs from `running/` back to `ready/`; live claimants, recent claims, unknown claimant formats, and missing timestamps are left untouched for manual review.

PR visibility is normalized as `found`, `not-found`, `unknown-auth-missing`, `branch-pushed-no-pr`, or `query-failed` depending on cached PR metadata, GitHub auth, API lookup, and remote branch checks.

Wave enqueue writes a best-effort lane-plan artifact after sync at `~/.local/share/hermes-tasklane/lane-plans/<wave_id>.json`. Artifacts include `schema_version`, wave/project/repo/base metadata, `artifact_status`, and per-lane issue numbers, branch, delivery group, task UIDs, job IDs, implementation job IDs, and final PR job ID. Missing or partial artifacts do not block enqueue; the enqueue result reports `written`, `partial`, `skipped`, or `failed`.

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

For a trusted internal network dashboard, install with:

```bash
./scripts/install.sh --systemd --enable-dashboard --dashboard-host 0.0.0.0 --dashboard-port 8765
```

Then open:

```text
http://<server-lan-ip>:8765
```

Do not expose the dashboard directly to the public internet without an authenticating reverse proxy.

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
- codex_review: true for pull-request tasks that should pass the independent Codex review gate
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
- require the final PR body to include:
  - full changed-file list from `git diff --name-only <base_branch>...HEAD`
  - diffstat from `git diff --stat <base_branch>...HEAD`
  - high-risk file summary for auth, money, contracts, migrations, schemas, and public APIs
  - verification command results
  - residual risks or skipped issues
- set `codex_review: true` on the final pull-request task so Tasklane creates a separate report-only review job. The reviewer must return `TASKLANE_REVIEW_DECISION: pass` or `TASKLANE_REVIEW_DECISION: needs-fix`; needs-fix queues one same-branch fix job and another review until `review_loops` is exhausted.

For two big features:
- use two different delivery_group values
- each delivery_group gets its own final PR task
- do not mix unrelated features into the same PR

For audits:
- use delivery_mode: report-only unless I explicitly ask for code changes

Command safety:
- do not put raw shell commands such as `verification_commands` or `bootstrap_commands` in task files
- use configured `verification_profile` and `bootstrap_profile` names only

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
    "allow_task_command_overrides": false,
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


## Wave Runner

`wave-runner` is the high-level autonomous coding entrypoint. It inspects GitHub issues, active Tasklane PRs, current JobStore state, and project policy, then proposes or queues a small set of PR lanes.

Dry-run a wave plan:

```bash
hermes-tasklane plan-wave --repo /mnt/data/workspace/Example --project "Example" --base development --json
```

Queue the next safe wave:

```bash
hermes-tasklane wave-runner --repo /mnt/data/workspace/Example --project "Example" --base development --enqueue --notify --json
```

The planner favors merge shape over raw issue count:

- same domain/files -> same PR lane
- likely conflicts -> serial `depends_on` chain
- contract or IDL work -> isolated lane
- frontend/docs/ops work -> group only when low conflict
- active project PR cap reached -> review/fix/merge mode instead of new work
- already-covered issue -> skipped when an active or merged PR references it

Use project config under `wave_planner.projects.<Project Name>` to set:

- `max_active_prs`
- `max_pr_lanes`
- `max_issues_per_wave`
- `issue_include_terms`, `issue_exclude_terms`
- `issue_labels_any`, `issue_labels_all`, `issue_milestone`
- `max_active_contract_prs`
- `max_active_large_feature_prs`
- `max_active_docs_prs`
- `disable_contract_lane`
- `review_docs`
- `verification_profile` and `bootstrap_profile`
- `merge_gate` and `auto_merge`
- `extra_review_fix_loops`

## Review And Merge Gates

Set `review_gate.enabled: true` globally or `codex_review: true` on final PR task files to create an independent review job after the PR job completes. A review gate returns one of:

```text
TASKLANE_REVIEW_DECISION: pass
TASKLANE_REVIEW_DECISION: needs-fix
```

When review says `needs-fix`, Tasklane queues a fix job on the same PR branch and then another review, up to the configured loop budget. `extra_review_fix_loops` lets a project continue autonomously for actionable findings after the normal review-loop cap. Human attention should be reserved for product/design decisions, secrets, deploy approval, contract upgrade approval, or ambiguous acceptance criteria.

Set `merge_gate: true` for a final merge check. Set `auto_merge: true` only for projects where passing gates should merge without human approval. The merge gate should close linked GitHub issues after merge; PR bodies should include `Closes #123` for every issue the PR completes.

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

### `hermes-tasklane plan-wave`
Dry-run the next GitHub Issues wave without creating task files or queueing jobs. It reports open `tasklane/` PRs, the active PR cap, issue scope, issue candidates, proposed PR lanes, serial dependencies for likely conflicts, and blocker notification payloads.

Use issue scope filters to avoid planning against the wrong backlog slice:

```bash
hermes-tasklane plan-wave --repo /path/to/repo --project "Treasure Hunter" --base development \
  --issue-include "[S3-" --issue-include "Season 3" --issue-scan-limit 50
```

When the dry-run output is acceptable, `--enqueue` creates guarded task files for the proposed lanes and immediately syncs them into Hermes JobStore. Enqueue mode refuses to run when the inbox already contains task files and caps new lanes to the remaining active PR slots.

### `hermes-tasklane wave-runner`
Run one guarded rolling wave cycle for a project. It reconciles finished jobs, applies guarded watchdog recovery, plans the next wave using project-specific settings, and optionally enqueues enough lanes to fill the remaining PR slots.

```bash
hermes-tasklane wave-runner --repo /path/to/repo --project "PeerPay" --base development --enqueue --notify
```

Use a timer/cron to make it rolling. Merge-gate jobs are only queued after the Codex review gate passes, and Telegram notifications are intended for blockers or human decisions.

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
