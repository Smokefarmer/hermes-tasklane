---
name: tasklane
description: Delegate, queue, and supervise autonomous coding jobs through the TaskLane MCP control plane. Use when the user wants to delegate or queue a coding job, run work overnight, fire off an autonomous bugfix, set up a plan→implement→review pipeline, or repair a blocked TaskLane job.
---

# TaskLane operator playbook

TaskLane runs coding tasks for you: you hand it a self-contained brief, a
server-side worker runs `claude -p` in an isolated git worktree, then it
commits / pushes / opens a PR. Your job is to **brief well, monitor lightly, and
unblock the few jobs that get stuck.** All actions go through the `tasklane`
MCP tools.

## 1. Should I delegate this at all?

| Delegate ✅ | Do it yourself ❌ |
|-------------|-------------------|
| >30 min of grind | A one-line edit you can make now |
| Parallelizable across repos | Anything needing live back-and-forth |
| Good to run overnight | Exploratory work with no clear "done" |
| A queue of small, well-scoped tasks | A task you can't write acceptance criteria for |

If you cannot write crisp acceptance criteria, the job will block. Sharpen the
brief first, or just do it yourself.

## 2. Write a good brief

Everything the worker knows comes from `body`. A strong brief always contains:

- **Goal** — one or two sentences of intent.
- **Acceptance criteria** — concrete, checkable outcomes.
- **How to verify** — the exact test/build command (`pytest -q`, `npm test`, …).
- **Non-goals** — what NOT to touch, so it doesn't wander.
- **Files in scope** — the modules/paths to change.

### GOOD brief

```
create_task(
  repo="/home/me/dev/api",
  title="Add retry with backoff to PaymentClient",
  body="""
GOAL: PaymentClient.charge() should retry transient 5xx/network errors.

ACCEPTANCE CRITERIA:
- Retries up to 3 times with exponential backoff (0.5s, 1s, 2s).
- Only retries on 5xx and connection errors; 4xx fails immediately.
- Backoff is injectable so tests run instantly.

VERIFY: `pytest tests/test_payment_client.py -q` is green.

NON-GOALS: do not change the public signature of charge(); no new deps.

FILES IN SCOPE: src/api/payment_client.py and its test module.
""",
  base_branch="develop", work_branch="feat/payment-retry",
  delivery_mode="pull-request", pr_target="develop",
)
```

### BAD brief (will block)

```
create_task(repo="/home/me/dev/api", title="make payments better",
            body="payments are flaky, please fix")
```

No acceptance criteria, no verify command, no scope. The worker has nothing to
aim at and no way to know it's done.

## 3. Prefer a pipeline for non-trivial work

`create_pipeline` builds three dependency-chained jobs — **plan → implement →
review** — where each stage receives the previous stage's final response as
context. Plan and review are report-only (no edits); implement delivers on the
work branch.

```
create_pipeline(
  repo="/home/me/dev/api",
  title="Extract billing into its own module",
  body="<same anatomy as a good brief>",
  work_branch="refactor/billing-module",
  base_branch="develop",
  delivery_mode="pull-request",
  stages="plan,implement,review",   # implement is required; subset is allowed
)
```

Use a bare `create_task` for small, obvious changes; reach for a pipeline when a
plan or an independent review pass adds real value.

## 4. Monitor without polling

- `list_tasks(state="running")` / `list_tasks(state="blocked")` for a sweep.
- `get_task(job_id)` for the full record (state, attempt, result, metrics).
- `task_events(job_id)` for the timeline; `task_logs(job_id)` for the `claude -p` tail.

Do **not** poll in a tight loop. Transient failures auto-retry with backoff; only
`blocked` / `needs-human` actually need you. Check back on your own cadence or
open the `/status` page.

## 5. Repair a blocked job (fix-tools)

`blocked` means "needs a human." The worktree is kept so you can fix it in place:

1. **Diagnose** — `get_diff(job_id)`, `read_file`, `exec(job_id, "…")`, `run_tests`.
2. **Fix** — `write_file`, `apply_patch`, or `git(job_id, "…")` — all confined to
   the job's worktree.
3. **Re-run** — `retry_task(job_id, extra_instructions="…")`; the follow-up text
   is appended to the brief so the next run sees your guidance.

If a job is fundamentally mis-scoped, `cancel_task` and recreate it with a
sharper brief rather than nursing it through many retries.

## 6. Stacked-branch review workflow

For a stack of dependent changes, queue an implement job per branch, each based
on the previous one's `work_branch`, then queue a report-only review per branch
(`branch_mode="detached-review"`, `delivery_mode="report-only"`). Read the review
output via `get_task`/`task_logs` before merging from the bottom of the stack up.

## 7. Cost awareness

Each job records cost/tokens/turns (`get_task` → `metrics`). Store-wide and 24h
spend come from the `metrics` tool. A configurable daily budget pauses new claims
when reached — so queue deliberately and check spend before a big overnight batch.

## Installation

```bash
cp -r skills/tasklane ~/.claude/skills/
```

Then connect the MCP server (see the repo README) so the `tasklane` tools are
available alongside this skill.
