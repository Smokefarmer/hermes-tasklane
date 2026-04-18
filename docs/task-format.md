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
- `allowed_paths`: comma-separated path allowlist
- `denied_paths`: comma-separated path denylist
- `allow_unlisted_paths`: `true` or `false`
- `review_loops`: max self-review loops, default `3`
- `security_review`: `true` or `false`
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
