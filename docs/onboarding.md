# Tasklane Operator Onboarding

This guide is for a teammate or another Hermes session that needs to operate an existing Tasklane installation.

## What Tasklane Does

Tasklane keeps autonomous coding work structured around GitHub issues and reviewable PRs. It does not replace Hermes. It prepares Hermes jobs, watches the queue, reconciles finished jobs, and creates the next safe issue wave.

A healthy Tasklane run looks like this:

1. GitHub issues are selected and grouped into PR lanes.
2. Implementation jobs push to lane branches.
3. One final PR job opens or updates the PR.
4. A review gate checks the PR against the issue, docs, architecture, tests, and evidence.
5. Fix jobs address review findings on the same branch.
6. A merge gate merges only when project policy allows it.
7. Linked issues close after merge.

## Important Paths

```text
~/.config/hermes-tasklane/config.json
~/.local/share/hermes-tasklane/inbox/
~/.local/share/hermes-tasklane/submitted/
~/.local/share/hermes-tasklane/completed/
~/.local/share/hermes-tasklane/failed/
~/.local/share/hermes-tasklane/cancelled/
~/.local/share/hermes-tasklane/state.json
~/.hermes/jobs/ready/
~/.hermes/jobs/running/
~/.hermes/jobs/waiting/
~/.hermes/jobs/blocked/
~/.hermes/jobs/completed/
~/.hermes/jobs/failed/
~/.hermes/jobs/needs-human/
```

If you are debugging a live install, treat `state.json` and `~/.hermes/jobs/` as production state. Prefer Tasklane commands over manual file edits.

## First Commands To Run

Always inspect and reconcile before queueing new work:

```bash
hermes-tasklane status
hermes-tasklane inspect <job-id> --json
hermes-tasklane watch --mode guarded --json
hermes-tasklane reconcile
```

Interpretation:

- `active_jobs`: ready/running jobs that Hermes may claim or is already running. Items include `derived_state`, claimant, runtime, watchdog, and PR summary fields.
- `waiting_jobs`: dependency chain is not complete yet. Inspect `waiting_for` or run `hermes-tasklane inspect <job-id>` for dependency detail.
- `blocked_jobs`: Tasklane or Hermes needs recovery. Run guarded watch before manual action.
- `gate_attention`: review or merge gates that require either a fix, a merge decision, or human input.
- `failed`: failures that were not safely salvaged.


## Inspecting One Job

Use `hermes-tasklane inspect <job-id>` for an operator drill-down. It is read-only and does not require the Tasklane lock. Human output is the default; add `--json` for automation and `--events N` to change the recent event count.

The inspection report combines the same shared liveness summary used by `status`, `watch`, and the dashboard with dependency rows, recent events, branch and delivery details, normalized PR visibility, lane-plan context, and a recommended next action. Treat watchdog retry metadata on ready/running jobs as historical context, not as proof of an active failure.

Derived states you will see include `running-alive`, `dead-claimant`, `running-unknown-claimant`, `waiting-on-dependency`, `ready`, `blocked`, `needs-human`, `failed`, `completed`, and `unknown`.

## When To Queue A Wave

Queue a new wave only when the project has room under its PR caps and existing work is not waiting for fix/review/merge.

Dry-run first when you are unsure:

```bash
hermes-tasklane plan-wave --repo /path/to/repo --project "Project Name" --base development --json
```

Queue when safe:

```bash
hermes-tasklane wave-runner --repo /path/to/repo --project "Project Name" --base development --enqueue --notify --json
```

The wave runner should tell you:

- current open Tasklane PRs
- issue candidates
- already-covered issues
- proposed PR lanes
- blocked/deferred issues and why
- whether new work may start
- PRs needing review/fix/merge attention


After a successful enqueue, Tasklane also writes a best-effort lane-plan artifact under `~/.local/share/hermes-tasklane/lane-plans/<wave_id>.json`. Use it to map lane IDs and delivery groups back to branches, issue numbers, task UIDs, implementation job IDs, and final PR job IDs. A `partial` or `failed` artifact status is an operator visibility warning, not an enqueue blocker.

## Wave Design Rules

Design by merge shape, not by maximum issue count.

Good lanes:

- one clear review theme
- shared domain or files
- bounded verification scope
- one final PR task

Avoid risky mixing:

- contract + UI polish + docs + economy in one PR unless they are one coherent feature
- multiple contract/IDL tasks in parallel
- two workers editing the same files without `depends_on`
- starting new waves while old PRs need fixes

Recommended caps:

- `max_active_prs`: 3 per project
- `max_pr_lanes`: 2 or 3
- `max_active_contract_prs`: 1
- `max_active_large_feature_prs`: 1
- `review_loops`: 2
- `extra_review_fix_loops`: 1 or 2 for mature projects

## Handling Blockers

Use this decision table:

| Situation | Action |
|---|---|
| Evidence-only issue, stale PR body, missing exact command output | Queue a focused evidence fix; do not ask the user first. |
| Concrete code bug found by review | Queue one focused fix on the PR branch, then another review. |
| Stale worktree or stale branch checkout | Run `hermes-tasklane watch --mode guarded --json` and let the watchdog retry if safe. |
| CI pending | Wait or let reconcile/watch observe it. |
| CI failed | Inspect logs, queue a fix if clear, ask user only if ambiguous. |
| Product/design/economy ambiguity | Ask the user one exact question. |
| Missing secret/API key | Ask for the config location/name, never ask to paste secrets into chat unless unavoidable. |
| Contract deploy/upgrade required | Stop and ask for explicit approval. |
| Merge approval only on `auto_merge: true` project | Do not ask; merge when gates are green. |

## Review Gates

Review gates are independent jobs. They should not edit files. They should inspect the PR, docs, architecture, tests, acceptance criteria, and evidence.

A passing review says:

```text
TASKLANE_REVIEW_DECISION: pass
```

A failing review says:

```text
TASKLANE_REVIEW_DECISION: needs-fix
```

A `needs-fix` review should include actionable findings with files/lines where possible. Tasklane can then create a fix job.

## Merge Gates

Use merge gates when you want an automated final check before merge.

`merge_gate: true` means a merge-check job is created.

`auto_merge: true` means the merge gate is allowed to merge without asking the user when all gates pass. Use this only on projects where unattended merge is acceptable.

Do not auto-merge if:

- checks or required verification failed
- review gate did not pass
- branch has conflicts
- PR is not targeting the configured base branch
- product/design ambiguity remains
- secrets/config are missing
- deploy or contract upgrade approval is required

After merge, close linked issues. Prefer PR bodies with explicit `Closes #123` lines so GitHub handles this automatically.

## Keeping Issues Clean

Before queueing work, check whether open issues are already covered by:

- an active Tasklane PR
- a merged PR body with `Closes #123`
- a merged PR title/body containing the issue number
- a ticket ID such as `S3-T034`
- the same cleaned title phrase

Already-implemented issues should be closed with a short comment linking the merged PR. Do not delete issues.

## Project Policy Examples

Full-auto product/API project:

```json
{
  "merge_gate": true,
  "auto_merge": true,
  "extra_review_fix_loops": 2,
  "disable_contract_lane": true
}
```

Guarded game/contract project:

```json
{
  "merge_gate": false,
  "auto_merge": false,
  "max_active_contract_prs": 1,
  "extra_review_fix_loops": 2,
  "review_docs": [
    "AGENTS.md",
    "apps/server/DEVELOPER_GUIDE.md",
    "apps/client/DEVELOPER_GUIDE.md",
    "docs/"
  ]
}
```

## Operator Report Format

Keep reports short:

```text
Health: ok/warning
Running: ...
Ready: ...
Waiting: ...
PRs: ...
Issues closed: ...
Blockers needing user: none / exact question
Next queued wave: ...
```

If there is no real human decision, do not send a human blocker. Fix, retry, or wait.
