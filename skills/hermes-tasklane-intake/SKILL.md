---
name: hermes-tasklane-intake
description: Create safe Hermes tasklane Markdown tasks from user requests. Use when a user wants to enqueue coding tasks, split work into serial/parallel tasks, create one PR or multiple PRs from a batch, or asks what information is needed to start a Hermes tasklane run.
---

# Hermes Tasklane Intake

Use this skill to turn a user request into one or more `.md` files in the tasklane inbox, then run `hermes-tasklane sync`.

## Required Inputs

Do not create a task until these are known:

- `repo_path`
- `base_branch`
- clear task goal
- delivery shape: one PR, multiple PRs, direct push, or report-only
- acceptance criteria

Ask a short follow-up if any required input is missing.

## Safe Defaults

Use these unless the user explicitly chooses otherwise:

- `request_type: task-small`
- `branch_mode: new-branch`
- `delivery_mode: pull-request`
- `review_loops: 3`
- `security_review: true`
- `allow_unlisted_paths: false` when a practical allowlist is known

Never choose `direct-push` unless the user explicitly says direct push is acceptable for that repo/branch.

## PR Grouping

Use `delivery_group` to define final PR shape:

- One final PR for many tasks: same `delivery_group`, same `base_branch`, same generated branch.
- Two big features: two different `delivery_group` values.
- Independent small fixes with separate PRs: omit `delivery_group` or give each a different group.

Current gateway execution is conservative and may run jobs serially even when tasks have no dependencies. Still model the dependency graph correctly so later concurrency can use it safely.

## Dependencies

Use stable task IDs and `depends_on`:

```md
---
id: checkout-api
depends_on: checkout-schema
---
```

Rules:

- A task that depends on another task must list that task's `id`.
- Parallelizable tasks in the same PR group should omit `depends_on`.
- Serial tasks in one PR group should all share `delivery_group` and form a dependency chain.

## Task File Template

Write files under:

```text
/home/server/.local/share/hermes-tasklane/inbox/
```

Template:

```md
---
id: short-stable-id
repo_path: /absolute/path/to/repo
base_branch: development
branch_mode: new-branch
delivery_mode: pull-request
request_type: task-small
delivery_group: optional-group-name
depends_on: optional-task-id
project: Project Name
allowed_paths: path/one, path/two
allow_unlisted_paths: false
review_loops: 3
security_review: true
---
Task body.

Acceptance criteria:
- concrete criterion
- concrete criterion
```

## Workflow

1. Normalize the user's request into task files.
2. Keep IDs lowercase, stable, and readable.
3. Use one `delivery_group` per desired final PR.
4. Use `depends_on` only for real order dependencies.
5. Write the files into the inbox.
6. Run:

```bash
hermes-tasklane sync
hermes-tasklane status
hermes jobs list --json
```

7. Report the created job IDs and any deferred/invalid tasks.

