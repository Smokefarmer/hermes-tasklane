# Recipe: PR review bot

Use TaskLane as an automated reviewer. A report-only job inspects a branch,
checks correctness / security / test coverage, and reports findings **without
touching any files** — so you get a second pair of eyes before you merge.

## When to use

- You (or another job) just pushed a branch and want a structured review.
- You want a review gate in front of a stack of dependent branches.
- You explicitly do NOT want the reviewer to edit code — only to report.

A report-only job uses `branch_mode="detached-review"` and
`delivery_mode="report-only"`: a dirty worktree fails the job, which guarantees
the reviewer made no changes.

## Review a single branch

```
create_task(
  repo="/home/me/dev/api",
  title="Review feat/payment-retry before merge",
  body="""
Review branch feat/payment-retry relative to develop:
  git diff develop...HEAD

Check, with severity (BLOCKER / MAJOR / MINOR / NIT):
- Correctness: does retry/backoff behave as specified? edge cases?
- Security: any secrets, injection, unsafe error handling, SSRF?
- Tests: do they actually cover the new behavior? backoff injectable?
- Style: matches the surrounding module?

Report findings as a list. DO NOT modify any files.
""",
  branch_mode="detached-review",
  base_branch="feat/payment-retry",
  delivery_mode="report-only",
)
```

Read the verdict when it finishes:

```
get_task(job_id)      # result holds the reviewer's final report
task_logs(job_id)     # full reasoning if you want the detail
```

## Stacked-branch review

For a stack (`base → A → B → C`), queue one review per branch so each is judged
against its own parent, not the whole stack:

```
# review A vs base, B vs A, C vs B
create_task(repo=R, title="Review A", branch_mode="detached-review",
            base_branch="feature/A", delivery_mode="report-only",
            body="Review feature/A relative to develop (git diff develop...HEAD). …")
create_task(repo=R, title="Review B", branch_mode="detached-review",
            base_branch="feature/B", delivery_mode="report-only",
            body="Review feature/B relative to feature/A (git diff feature/A...HEAD). …")
create_task(repo=R, title="Review C", branch_mode="detached-review",
            base_branch="feature/C", delivery_mode="report-only",
            body="Review feature/C relative to feature/B (git diff feature/B...HEAD). …")
```

Each review only sees its own branch's delta, which keeps findings focused.

## What to expect

- The job completes with a findings report in its result — **no commits, no PR**.
- If the worktree ends up dirty (the reviewer tried to edit), the job fails by
  design; recreate it with a firmer "DO NOT modify any files" instruction.

## Morning / merge-time review

```
list_tasks(state="completed")
get_task(<review_job_id>)     # read findings; triage BLOCKER/MAJOR first
```

Address BLOCKER and MAJOR findings (yourself, or via a
`fire-and-forget-bugfix.md` job), then merge the stack from the bottom up. Prune
the review worktrees with `prune_worktrees()` when done.
