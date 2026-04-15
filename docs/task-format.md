# Task Format

Each task is a `.md` or `.txt` file inside the inbox directory.

## Required frontmatter

- `repo_path`
- `branch_base` or `base_branch`

## Optional frontmatter

- `id`
- `platform`
- `chat_id`
- `thread_id`
- `project`
- any extra metadata you want copied into the queue payload

## Example

```md
---
id: auth-cleanup
repo_path: /mnt/data/workspace/app
branch_base: main
project: App
platform: telegram
chat_id: -1001234567890
thread_id: 99
priority: high
---
Clean up the auth flow, add regression tests, run verification, and prepare a concise review-ready handoff.
```

## Notes

- The file body becomes the Hermes task prompt.
- The filename is used as the default summary.
- If no explicit `id` is set, tasklane generates one from the file path.
