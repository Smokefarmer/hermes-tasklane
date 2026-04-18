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

## What it does not replace

This is not a full replacement for Hermes itself.

You still need a Hermes install with:
- the Hermes v10 JobStore watcher active in the gateway
- Git + GitHub access configured for the repos you want Hermes to operate on

## Install

### Fast path

```bash
git clone https://github.com/Smokefarmer/hermes-tasklane.git
cd hermes-tasklane
./scripts/install.sh --systemd
```

That installs the package, initializes config and folders, and enables user-level systemd timers when available. If the machine has no usable user systemd session, the installer skips timers cleanly and you can use the cron fallback instead.

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

## Recommended automation

### systemd user timers (recommended)

```bash
./scripts/install.sh --systemd
systemctl --user status hermes-tasklane-sync.timer
systemctl --user status hermes-tasklane-reconcile.timer
```

### cron fallback

If you do not use systemd user timers, run both of these periodically:

```bash
*/5 * * * * /usr/bin/env hermes-tasklane sync
*/5 * * * * /usr/bin/env hermes-tasklane reconcile
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
