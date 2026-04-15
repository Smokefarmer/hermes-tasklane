# hermes-tasklane

A polished, file-based task inbox for Hermes governed runs.

`hermes-tasklane` gives you the useful parts of a production Hermes queue setup without forcing your team onto Nextcloud. Instead of CalDAV tasks, your teammates drop plain text/Markdown task files into an inbox folder. The package:

- turns inbox files into Hermes autocode queue items
- gates submission so only one active coding run per repo is launched by default
- tracks submitted tasks locally
- reconciles tasks back out of governed run state
- repairs common delivery-state drift by checking GitHub PR + CI reality

It is designed for teams that already run Hermes and want a simple, shareable “task lane” on top.

## What this package includes

1. File-based task source
- human-editable Markdown task files
- no Nextcloud dependency
- one task file = one launchable governed run

2. Safe queue bridging
- writes Hermes queue payloads into `~/.hermes/autocode_queue/incoming/`
- checks active governed runs
- checks repo locks
- checks pending queue items
- avoids duplicate launches per repo by default

3. Reconciliation helpers
- watches governed run records in `~/.hermes/runs/`
- moves submitted task files into `completed/`, `failed/`, or `cancelled/`
- normalizes stale delivery blockers by checking GitHub PR + CI state

## What it does not replace

This is not a full replacement for Hermes itself.

You still need a Hermes install with:
- governed runs enabled
- the autocode queue watcher active
- Git + GitHub access configured for the repos you want Hermes to operate on

## Install

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

If the repo is idle, the task is converted into a queue payload under:

```text
~/.hermes/autocode_queue/incoming/
```

If the repo is busy, the task stays deferred in the inbox and will be eligible on the next sync.

### Reconcile finished work back into tasklane state

```bash
hermes-tasklane reconcile
```

This:
- checks governed run state
- moves task files out of `submitted/`
- writes a small `.result.json` note beside the moved task file
- attempts delivery reconciliation for stale PR/CI blockers

### Inspect status

```bash
hermes-tasklane status
```

## Recommended cron setup

Run both of these periodically:

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
- `platform`
- `chat_id`
- `thread_id`
- `project`
- `id`
- any extra metadata fields you want copied into the queue payload metadata block

Example:

```md
---
id: invoice-export-hardening
repo_path: /srv/work/finance-app
branch_base: main
project: Finance App
platform: telegram
chat_id: -1001234567890
thread_id: 55
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
Read inbox files and write eligible Hermes queue payloads.

### `hermes-tasklane reconcile`
Reconcile submitted tasks from governed run state and attempt PR/CI normalization.

### `hermes-tasklane status`
Show inbox/submitted/completed counts and current governed run states.

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
~/.hermes/autocode_queue/incoming/*.json
      ↓
Hermes queue watcher
      ↓
Governed run + PR + CI
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

## Development

```bash
python3 -m pip install -e .
hermes-tasklane init
hermes-tasklane doctor
```

## Safety note

This package writes queue payloads for Hermes to execute. Only point it at repos and branches you actually want Hermes to work on.

## License

MIT
