# Recipe: fire-and-forget bugfix

A single, well-scoped bug you don't want to context-switch into. Hand it to
TaskLane, keep working, and come back when it's done (or when it pings you by
landing in `blocked`).

## When to use

- A reproducible bug with a clear fix and a test that would prove it.
- The fix is >a couple of minutes of work but doesn't need live iteration.
- You can name the verify command and the files likely involved.

For a one-line edit, just make it yourself — writing the brief costs more than
the fix.

## Fire it off

```
create_task(
  repo="/home/me/dev/api",
  title="Fix off-by-one in pagination page count",
  body="""
GOAL: /items?page=N returns the correct items; the last page is no longer empty.

ACCEPTANCE CRITERIA:
- total_pages = ceil(total / page_size), not floor.
- A regression test covers a total that is an exact multiple of page_size and one
  that is not.

VERIFY: `pytest tests/test_pagination.py -q` is green.

NON-GOALS: do not change the response schema or default page_size.

FILES IN SCOPE: src/api/pagination.py and tests/test_pagination.py.
""",
  base_branch="develop", work_branch="fix/pagination-off-by-one",
  delivery_mode="pull-request", pr_target="develop",
)
```

Note the returned `id` and `state` (`ready`), then go back to what you were
doing. **Do not poll.**

## Check on it later

```
get_task(job_id)     # state + result + metrics
task_logs(job_id)    # tail of the claude -p run, if you want detail
```

- `completed` → review the PR / diff and merge.
- `running` → leave it; come back later.
- `failed` (transient) → it auto-retries; nothing to do.
- `blocked` → it needs you (next section).

## If it blocks

`blocked` means a human is required. The worktree is kept so you can fix in place:

1. **Diagnose**
   ```
   get_diff(job_id)                       # what it changed so far
   task_logs(job_id, tail=120)            # why it stopped
   exec(job_id, "pytest tests/test_pagination.py -q")   # reproduce
   ```
2. **Fix** — edit inside the worktree:
   ```
   read_file(job_id, "src/api/pagination.py")
   write_file(job_id, "src/api/pagination.py", "<corrected content>")
   # or apply_patch(job_id, unified_diff="…") / git(job_id, "checkout -- <f>")
   run_tests(job_id)                      # confirm green before re-running
   ```
3. **Re-run** with guidance appended to the brief:
   ```
   retry_task(job_id, extra_instructions="ceil division was the bug; keep the new
   test and just push.")
   ```

If the job is fundamentally mis-scoped, `cancel_task(job_id)` and recreate with a
sharper brief — that's cheaper than many retries.

## Done

After merge, `prune_worktrees()` reclaims the kept worktree. Check `metrics()` if
you want to know what the fix cost.
