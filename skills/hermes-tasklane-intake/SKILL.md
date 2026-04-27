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

### Important sizing rule

Do **not** bundle a broad multi-area implementation into one `task-small` job just because the user wants one final PR. If the work spans multiple subsystems (for example server auth, frontend rendering, RPC/backend behavior, contracts, config, and tests), split by subsystem into smaller serial tasks under one shared `delivery_group`, then use one final PR-opening task.

A good default for cross-cutting fix batches is:

1. server/backend auth or domain logic
2. client/UI hardening
3. API/proxy/backend boundary fixes
4. contract/economy/config work
5. final integration + strongest practical verification + PR open

This is more reliable than one huge implementation job and avoids blocked runs caused by dirty worktrees, failing tests, or missing PR delivery.

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
- Use comma-separated `depends_on` values in frontmatter for multi-dependency tasks unless Tasklane explicitly documents another format.
- Parallelizable tasks in the same PR group should omit `depends_on`.
- Serial tasks in one PR group should all share `delivery_group` and form a dependency chain.
- One-PR batches should have exactly one final PR-opening task.

## Queue Hygiene Before Batch Intake

Before queuing a batch of Tasklane audit/review jobs for a repo, check these first:

- repo worktree is clean
- intended source branch exists locally or remotely
- branch naming is consistent across `base_branch`, prompt text, and requested issue metadata
- old blocked jobs for that repo are reviewed so stale failures do not confuse the new batch
- `depends_on` references resolve to real upstream task IDs in the same batch/program
- final synthesis/integration task depends on every upstream task it needs

If the repo is dirty, do not queue the audit batch yet unless the user explicitly accepts that risk.

For Alvin specifically, keep branch naming consistent across the prompt and issue metadata. Use `feat/anlageverzeichnis` if that is the intended audit target; otherwise use `develop` consistently. Do not write `development` unless that branch actually exists.

If the user wants issue metadata such as `source branch: ...`, verify the real branch first and keep that value consistent everywhere. Do not mix `feat/anlageverzeichnis` as the audit target with `source branch: development` unless the user explicitly wants that mismatch and understands it is metadata-only.

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

## Prompt Authoring Style

When the user is asking for the *task prompt wording itself* instead of immediate inbox creation, format the body as a compact operator-ready prompt with explicit metadata first, then the task, then constraints, then output/acceptance requirements.

Preferred structure:

```text
create a tasklane <type> job for <Project>.

Repo: /absolute/path
Base branch: development
Branch mode: <new-branch|existing-branch|detached-review>
Delivery mode: <pull-request|direct-push|report-only>
Request type: task-small
Project: <Project>

<clear task goal>

<constraints such as no code changes / no commits / no PR>

Focus areas:
- ...

Output requirements:
- ...

GitHub issue behavior:
- ...

Acceptance criteria:
- ...
```

Authoring rules:

- Keep labels exactly spelled and easy to scan.
- Prefer short imperative sentences.
- Put hard prohibitions on their own lines: `No code changes.`, `No commits.`, `No PR.`
- For review-only work, explicitly require separation of real vulnerabilities vs code-quality concerns.
- If issue creation is desired, state title format, body fields, and caps such as `top 8 issues only`.
- Include `return the report and list all created issue URLs` when the user wants verifiable delivery.

## Review-Only Jobs

When the user wants a deep review with no implementation, prefer:

- `branch_mode: detached-review`
- `delivery_mode: report-only`
- explicit lines: `No code changes.`, `No commits.`, `No PR.`

For security/code-quality review prompts, encourage these sections when relevant:

- Focus areas
- Output requirements
- GitHub issue behavior
- Acceptance criteria

If the user wants GitHub issues created from findings, keep the prompt explicit that only actionable findings should become issues and vague/stylistic concerns should stay in the report.

## Structured Audit Programs

When the user wants a broad audit across multiple domains, do **not** create one giant audit task. Split it into bounded review tasks with clear domain ownership and one shared `delivery_group`.

Recommended pattern:

1. **Source pack / rubric task first**
   - creates the official source baseline
   - defines checklist, interpretation risks, and downstream audit domains
   - should usually be the dependency root for later audit tasks
2. **Parallel domain audits** with `depends_on` the source-pack task
   - tax compliance / forms / asset-register audit
   - booking architecture audit
   - backend security / data-integrity audit
   - other domains only if the user asks for them
3. **Final synthesis task** after the domain audits
   - deduplicates overlapping findings
   - turns accepted findings into one implementation roadmap

Authoring rules for these audit programs:

- Give every task a stable explicit `id`.
- Use one shared `delivery_group` for the whole audit program.
- Keep each task report-only unless the user explicitly asks for fixes.
- Put `depends_on` on later tasks when the source pack or synthesis must run first.
- For tax/bookkeeping audits, explicitly require official sources only, preferably BMF, USP, RIS, or FinanzOnline/BMF forms.
- If legal interpretation is uncertain, require the label `needs tax/legal verification`.
- Make issue-title prefixes domain-specific, for example:
  - `[Tax][Severity] ...`
  - `[Booking Architecture][Severity] ...`
  - `[Security][Severity] ...`
- Require every issue to cite affected files/functions and evidence.
- Require each task to leave the worktree clean.

Operational note:

- `hermes-tasklane sync` may defer newly written tasks with reason `repo-active-job` when another job for the same repo is already active. In that case, keep the task files in inbox and tell the user they are queued behind the active repo job rather than lost.

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
