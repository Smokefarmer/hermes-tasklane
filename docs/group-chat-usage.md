# Group Chat Usage

Use this workflow while the pipeline is useful but not fully production hardened.

## Safe Default

Ask for small, scoped tasks that produce a pull request:

```text
Create a tasklane job for Treasure Hunter.
Repo: /mnt/data/workspace/Treasure-Hunter
Base branch: development
Branch mode: new-branch
Delivery: pull-request
Allowed paths: README.md
Do: update the README with one short note.
Do not touch product code.
```

Preferred defaults:

- `request_type: task-small`
- `branch_mode: new-branch`
- `delivery_mode: pull-request`
- `allow_unlisted_paths: false` when the change can be scoped
- small `allowed_paths` lists
- one `delivery_group` per desired final PR

## Avoid For Now

- broad feature requests without path scope
- direct pushes to shared branches
- multi-repo jobs
- ambiguous tasks like "fix everything"
- production secrets, deploy keys, or credential changes
- large refactors with no acceptance criteria

## Before Submitting

Make sure the task says:

- repo path
- base branch
- delivery mode
- exact goal
- acceptance criteria
- allowed paths or a reason why broad scope is needed
- whether tests should be added, updated, or only run
- whether the task belongs to a larger `delivery_group`
- whether it depends on another task ID

## After Submitting

Check:

```bash
hermes-tasklane status
hermes jobs list --json
```

For pull-request jobs, review the PR before merging. Treat the autonomous output as a draft from a junior developer until the pipeline has more production burn-in.

## Good Task Template

```md
---
repo_path: /mnt/data/workspace/Treasure-Hunter
base_branch: development
branch_mode: new-branch
delivery_mode: pull-request
request_type: task-small
project: Treasure Hunter
allowed_paths: README.md
allow_unlisted_paths: false
---
Update README.md with one short note explaining the current smoke-test status.

Acceptance criteria:
- only README.md changes
- no product code changes
- open a PR against development
```

## Ten-Task Batches

For ten tasks that should become one PR:

- give all ten the same `delivery_group`
- give serial tasks stable IDs and `depends_on`
- omit `depends_on` for tasks that do not require ordering

For two big features:

- use two `delivery_group` values
- each group becomes its own branch/PR target
- keep dependencies inside the feature group unless one feature truly depends on the other

Example:

```md
---
id: checkout-schema
delivery_group: checkout-v2
---
```

```md
---
id: checkout-api
delivery_group: checkout-v2
depends_on: checkout-schema
---
```

```md
---
id: inventory-ui
delivery_group: inventory-v1
---
```
