# Task Format

Each task is a `.md` or `.txt` file inside the inbox directory.

## Required frontmatter

- `repo_path`
- `branch_base` or `base_branch`

## Optional frontmatter

- `id`
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
- `bootstrap_commands`: comma-separated commands to run before salvage verification
- `bootstrap_profile`: named profile from `watch.bootstrap_profiles`
- `verification_commands`: comma-separated commands for this task's salvage verification
- `verification_profile`: named profile from `watch.verification_profiles`
- `baseline_verification`: `true` or `false`
- `allow_matching_baseline_failures`: `true` or `false`
- `platform`
- `chat_id`
- `thread_id`
- `project`
- any extra metadata you want kept with the tasklane submission

## Example

```md
---
id: auth-cleanup
repo_path: /mnt/data/workspace/app
branch_base: main
branch_mode: new-branch
delivery_mode: pull-request
request_type: task-small
delivery_group: auth-cleanup
project: App
platform: telegram
chat_id: -1001234567890
thread_id: 99
allowed_paths: src/auth/, tests/auth/
allow_unlisted_paths: false
priority: high
---
Clean up the auth flow, add regression tests, run verification, and prepare a concise review-ready handoff.
```

## Notes

- The file body becomes the Hermes task prompt.
- The filename is used as the default summary.
- If no explicit `id` is set, tasklane generates one from the file path.
- New-branch pull-request delivery is the safest default for group-chat usage.
- Use `direct-push` only for low-risk fixes on a pre-agreed branch.
- Use the same `delivery_group` when multiple task files should land in one final PR.
- Use different `delivery_group` values when two big features need separate PRs.
- Use `depends_on` for serial work; omit it for work that can be scheduled independently.
- For a many-task single-PR batch, use `direct-push` for implementation tasks and one final `pull-request` task that depends on all implementation tasks.
- Add `platform: telegram` and `chat_id` when the group should receive start/completion/failure updates.
- `default_platform`, `default_chat_id`, and `default_thread_id` in config are used when a task file omits these fields.
- Use `bootstrap_profile` for dependency install or generated-client setup before verification.
- Use `verification_commands` only for simple per-task overrides. Prefer named `verification_profile` values for larger repos.

## Preflight Blockers

`hermes-tasklane sync` validates task files before it writes Hermes JobStore
records. A blocked task stays in `inbox/` and receives a sibling
`*.preflight.json` report with concrete findings.

Current blockers:

- `repo_path` does not exist.
- `feature-large` or `refactor-large` uses `direct-push`.
- `detached-review` uses a mutating delivery mode.
- A large task has no `allowed_paths` while `allow_unlisted_paths` is true.
- `allow_unlisted_paths: false` is set without `allowed_paths`.
- `review_loops` is outside `1..3`.
- `depends_on` references an unknown task ID.
- Dependencies contain a cycle.
- One `delivery_group` mixes multiple base branches.
- Multiple tasks mutate the same `work_branch` without an ordering dependency.

## Auto-Salvage

When `watch.auto_salvage` is enabled, failed pull-request jobs can still be
delivered if they produced safe code changes. This is meant for provider/API
failures or dirty-worktree failures that happen after useful work exists.

Auto-salvage only proceeds when all of these are true:

- delivery mode is `pull-request`
- the job failure matches a safe transient/provider pattern
- the worktree exists and is on the expected task branch
- changed files stay inside `allowed_paths` and outside `denied_paths`
- configured verification commands pass
- if baseline comparison is enabled, any accepted failed command also fails the base branch with matching output
- the branch can be pushed and a PR can be found or created

If a task sets `allow_unlisted_paths: false`, keep `allowed_paths` precise.
That scope is the hard guard that prevents an overnight run from pushing broad
or unrelated changes.

## Final PR Review Artifact

For a many-task batch, the final integration task should require the PR body to
include:

- full changed-file list from `git diff --name-only <base_branch>...HEAD`
- diffstat from `git diff --stat <base_branch>...HEAD`
- high-risk file summary for auth, money, contracts, migrations, schemas, and public APIs
- verification command results
- residual risks or skipped issues

This keeps review focused even when a batch spans dozens of files.
