# Recipe: feature pipeline (plan → implement → review)

For a feature meaty enough that an up-front plan and an independent review pay
off, use `create_pipeline`. It creates three **dependency-chained** jobs in one
call: **plan** (report-only), **implement** (delivers on the work branch), and
**review** (report-only on the work-branch tip). Each stage receives the previous
stage's final response as context, and a stage only becomes claimable once its
predecessor completes.

## When to use

- A multi-file feature or refactor where a wrong approach is expensive.
- You want a written plan to sanity-check before code is written.
- You want a review pass baked into the flow, not bolted on later.

For small, obvious changes, a single `create_task` is plenty — skip the ceremony.

## Create the pipeline

```
create_pipeline(
  repo="/home/me/dev/api",
  title="Add idempotency keys to the orders endpoint",
  body="""
GOAL: POST /orders accepts an Idempotency-Key header; duplicate keys within 24h
return the original response instead of creating a second order.

ACCEPTANCE CRITERIA:
- Key stored with the order; replay returns the stored response + 200.
- Missing key → current behavior (create), unchanged.
- Concurrent duplicates resolve to one order (row lock or unique constraint).

VERIFY: `pytest tests/test_orders_idempotency.py -q` is green.

NON-GOALS: no new infra; reuse the existing Postgres connection.

FILES IN SCOPE: src/api/orders.py, src/api/idempotency.py (new), migrations/, tests/.
""",
  work_branch="feat/order-idempotency",
  base_branch="develop",
  delivery_mode="pull-request",
  stages="plan,implement,review",   # implement is required; you may drop plan or review
)
```

The call returns the three job ids in execution order, e.g.
`add-idempotency-keys-…-plan`, `-implement`, `-review`.

## What to expect

1. **plan** runs first (report-only, no edits) and writes a step-by-step plan.
2. **implement** waits for plan, then writes code on `feat/order-idempotency` and
   delivers per `delivery_mode` (here: opens a PR against develop). It sees the
   plan's output as context.
3. **review** waits for implement, then reviews the work-branch tip against the
   base (report-only) and reports findings. It sees the implement output as context.

Each stage only starts after its dependency completes — so a failed plan stops
the chain before any code is written.

## Monitor

```
list_tasks()                          # see all three stages and their states
get_task("<base>-plan")               # read the plan once it completes
get_task("<base>-implement")          # result + PR link + metrics
get_task("<base>-review")             # the review findings
task_logs("<base>-implement")         # detail if a stage misbehaves
```

Don't poll tightly — stages gate on each other automatically.

## Morning / completion review

- Read the **plan** first if you want to sanity-check the approach early; you can
  `cancel_task` the implement/review stages if the plan reveals a wrong turn.
- When **implement** completes, review its PR/diff.
- Read **review** findings and address BLOCKER/MAJOR items (directly, or via a
  `fire-and-forget-bugfix.md` job) before merging.
- If **implement** blocked, repair it in its worktree and `retry_task` — the
  downstream review re-runs once it completes. See `fire-and-forget-bugfix.md`.

Finish with `prune_worktrees()` and a glance at `metrics()` for the total cost.
