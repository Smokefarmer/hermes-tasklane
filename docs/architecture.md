# Architecture

`hermes-tasklane` sits in front of Hermes' existing autocode queue.

## Inputs

Human-editable task files:

```text
~/.local/share/hermes-tasklane/inbox/*.md
```

## Internal state

Tasklane stores local submission state in:

```text
~/.local/share/hermes-tasklane/state.json
```

## Hermes integration points

Queue input:

```text
~/.hermes/autocode_queue/incoming/*.json
```

Governed run state:

```text
~/.hermes/runs/*.json
```

Repo locks:

```text
~/.hermes/runs/repo-locks/*.json
```

## Main loop

1. `sync`
   - parse inbox files
   - derive repo key
   - check active runs, locks, pending queue items
   - write queue payload
   - move task file to `submitted/`

2. Hermes executes the queue item

3. `reconcile`
   - inspect matching governed run
   - normalize stale delivery blockers from GitHub reality when possible
   - move task file to `completed/`, `failed/`, or `cancelled/`
   - write a `.result.json` sidecar

## Why this approach works

It keeps all source-of-truth task editing simple while reusing Hermes' governed execution model instead of rebuilding it.
