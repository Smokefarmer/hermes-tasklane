# Architecture

`hermes-tasklane` sits in front of Hermes v10's file-backed JobStore.

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

JobStore input:

```text
~/.hermes/jobs/ready/*.json
```

JobStore state:

```text
~/.hermes/jobs/{ready,running,blocked,completed,failed,needs-human}/*.json
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
   - check active jobs, active runs, and repo locks
   - write a JobStore record
   - move task file to `submitted/`

2. Hermes gateway claims and executes the ready job

3. `reconcile`
   - inspect matching JobStore record
   - fall back to governed run records for older submissions
   - normalize stale delivery blockers from GitHub reality when possible
   - move task file to `completed/`, `failed/`, or `cancelled/`
   - write a `.result.json` sidecar

## Why this approach works

It keeps all source-of-truth task editing simple while reusing Hermes' gateway execution model instead of rebuilding it.
