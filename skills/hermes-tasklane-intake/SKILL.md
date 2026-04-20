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
- Telegram `chat_id` when the group expects updates and tasklane config does not provide `default_chat_id`

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

Use `delivery_group` to define final branch/PR shape:

- One final PR for many tasks: use one shared `delivery_group`.
- Two big features: two different `delivery_group` values.
- Independent small fixes with separate PRs: omit `delivery_group` or give each a different group.

Current gateway execution is conservative and may run jobs serially even when tasks have no dependencies. Still model the dependency graph correctly so later concurrency can use it safely.

For a many-task single-PR batch, prefer:

1. Implementation tasks use the same `delivery_group` and `delivery_mode: direct-push`.
2. The final integration/review task uses the same `delivery_group`, `branch_mode: existing-branch`, `delivery_mode: pull-request`, and `depends_on` all implementation task IDs.

This avoids multiple jobs fighting to create or update the same PR.

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
- One-PR batches should have exactly one final PR-opening task.

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
platform: telegram
chat_id: optional-chat-id
thread_id: optional-topic-id
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
5. When the request came from Telegram and chat metadata is available, include `platform: telegram`, `chat_id`, and `thread_id` in each task file so Hermes can send start/completion/failure updates.
6. Write the files into the inbox.
7. Run:

```bash
hermes-tasklane sync
hermes-tasklane status
hermes jobs list --json
```

8. Report the created job IDs and any deferred/invalid tasks.
