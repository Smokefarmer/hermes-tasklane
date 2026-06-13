# Development Branch Review — 2026-06-13

**Reviewer:** `zz-zzz-01-big-review` (autonomous final-reviewer job)
**Scope:** `git diff main...development` — 45 files, +4710/-48. HEAD `5a798a6`.
**Method:** README re-read, architecture/drift pass, correctness pass over every new
module, security pass (pairing, middleware, fan-out, fix tools), test-quality pass
(full suite executed).

---

## Executive summary

`development` is **broadly sound and well-engineered** — consistent spec normalisation,
atomic file writes, timing-safe token compares, SHA-256-only token storage, isolated
worktrees, a clean reconciler, and a real test suite (155 tests, **all green** once the
environment leak below is removed). No deleted or weakened tests; no obvious secret
leakage; no circular imports.

It is **not ready to ship unqualified**, for two reasons:

1. **One headline feature is broken end-to-end.** `analyze_project`'s remediation
   pipeline cannot create drafts: the `analyze` role prompt instructs the agent to emit
   the `proposed_tasks` block as a **markdown bullet list**, but the fan-out parser only
   accepts a **JSON array**. A faithful analyze run yields a `JSONDecodeError`, logs
   `job_fanout_parse_failed`, and creates **zero** drafts. The unit/e2e tests miss it
   because they feed the parser hand-written JSON that the real prompt never produces.
   This is the exact "integration seam between independently-built features" risk the
   review was commissioned to catch. (Finding **F1**, high.)

2. **The README overstates the fix-tool sandbox.** It claims `exec` is "confined to a
   job's worktree (path traversal rejected)". `exec`/`git` run `bash -lc` with no
   filesystem confinement — only `cwd` is set — so they trivially escape via absolute
   paths or `cd`. Combined with the fix tools' silent fall-back to the **live repo
   checkout** when a job has no worktree (F3), an operator can mutate the real repo while
   believing they are sandboxed. (Findings **F2/F3**, medium.)

Everything else is drift/duplication and hardening: a 853-line god-module
(`mcp_server.py`), an unlocked `store.transition` that can duplicate records under the
newly-increased concurrency, and several re-implemented helpers (atomic-write, timestamp
parsing, prompt assembly) that will drift apart.

### Top risks (ranked)

| # | Risk | Severity |
|---|------|----------|
| F1 | `analyze` → draft remediation flow is dead on arrival (prompt/parser format mismatch); tests mask it | **High** |
| F2 | README security claim false: `exec`/`git` are unconfined arbitrary shell | Medium |
| F3 | Fix tools silently operate on the **live repo** when a job has no worktree | Medium |
| F5 | `store.transition` is unlocked → duplicate records across state dirs under concurrent transitions | Medium |
| F4 | `mcp_server.py` is 853 lines (> 800 hard cap, > 500 project rule) | Medium |

### Environment note (not a code defect)

The job harness leaks `PYTHONPATH=/home/server/tasklane` into the worktree, which shadows
`src/` and makes the suite import the **stale live install** (collection errors:
`No module named 'tasklane.reconcile'`, missing `parse_timestamp`). Run tests with
`env -u PYTHONPATH .venv/bin/python -m pytest -q` → **155 passed**. Worth a worker-side
fix so isolated jobs never inherit the operator's `PYTHONPATH`.

---

## Findings

| id | sev | file:line | issue | suggested fix |
|----|-----|-----------|-------|---------------|
| F1 | high | `src/tasklane/prompts/analyze.md:88-93` vs `src/tasklane/fanout.py:59` | Prompt instructs `proposed_tasks` as markdown bullets (`- [high] ...`); parser does `json.loads` and requires a JSON array of objects. Real analyze runs create zero drafts (`job_fanout_parse_failed`). Tests pass only because `test_fanout.py`/`fake_claude.py` feed JSON the real prompt never emits, and `test_analyze.py` only greps for the fence string. | Pick one contract and make prompt, parser, and tests agree. Recommended: change `analyze.md` to emit a JSON array (`[{"title","body","type","allowed_paths","severity"}]`) matching `_draft_spec`; add an e2e test that runs the `analyze` scenario through `fake_claude` and asserts drafts are created. |
| F2 | medium | `src/tasklane/mcp_server.py:502-516`, `README.md:196` | `exec`/`git` run `bash -lc` with `cwd=worktree` but no confinement; absolute paths / `cd ..` escape freely. README claims "`exec`/`write_file`/`apply_patch` are confined to a job's worktree (path traversal rejected)" — true only for `read/write_file`/`apply_patch` (`_safe_path`), false for `exec`/`git`. Misleads the threat model that distinguishes `exec` (confined) from `admin_exec` (unconfined). | Correct the README to state `exec`/`git` are full shells scoped only by `cwd`; or actually confine (bubblewrap/`firejail`/`unshare`) if the claim is to hold. |
| F3 | medium | `src/tasklane/mcp_server.py:85-92` | `_worktree_dir` falls back to `spec.repo.path` (the **original checkout**) when no worktree exists (draft/completed/cleaned-up). `write_file`/`exec`/`apply_patch` then mutate the live repo, breaking the "isolated worktree" guarantee silently. | Reject fix-tool calls on jobs without a live worktree (raise), or clearly label the fall-back in the tool result; never write to the original checkout without explicit opt-in. |
| F4 | medium | `src/tasklane/mcp_server.py:1-854` | 853-line god-module — over the 800-line common limit and far over the project's "files under 500 lines" rule. Mixes lifecycle tools, fix tools, pairing tools, ops tools, status HTML, and the ASGI auth middleware. | Split: `mcp/lifecycle.py`, `mcp/fixtools.py`, `mcp/pairing_tools.py`, `mcp/status.py`, `mcp/auth.py`, leaving `mcp_server.py` as wiring. |
| F5 | medium | `src/tasklane/store.py:95-118` | `transition` is read-modify-write-then-unlink with **no lock**. Two concurrent transitions on one job (now plausible: reconciler + worker `complete`/`fail` + MCP `retry`/`cancel`, none of which hold the claim lock) each write to their own state dir and unlink the old → the record can exist in **two** state dirs; `get()` returns whichever `JOB_STATES` set-iteration hits first (nondeterministic). | Hold a per-job lock around the full transition, or write-new-then-atomically-remove-old only after re-reading current state under lock. At minimum, have `get()` detect and reconcile duplicates. |
| F6 | medium | `src/tasklane/pairing.py:250-267` & `src/tasklane/store.py:292-308`; `pairing.py:218-232` & `store.py:32-33,338-349` | Re-implemented helpers: atomic-write (tempfile+fsync+`os.replace`) exists in both `store._write_record` and `pairing._save`; `utc_now`/timestamp-parse exist in both modules; two O_EXCL lock idioms (`store._acquire_claim_lock`, `pairing._locked`). Independent copies will drift (e.g. `pairing._save` chmods 600, `store._write_record` does not). | Extract a shared `tasklane/atomicio.py` (atomic write + 600 option) and reuse `store.parse_timestamp`/`utc_now` in `pairing`/`metrics`/`reconcile`. |
| F7 | medium | `src/tasklane/worktree.py:300-363` vs `src/tasklane/prompts/render.py:23-90` | The branch/workspace/scope/delivery/upstream prompt blocks are rendered **twice** — once in `_generic_job_prompt`, once in `render.py`. The operational-rules text and upstream-context loop are copy-pasted; they will diverge. | Have `_generic_job_prompt` build its blocks from the same `render.py` helpers (or vice-versa); single source of truth for the contract text. |
| F8 | medium | `src/tasklane/pairing.py:115-133` | `authenticate` takes the 5s O_EXCL file lock and **rewrites all of `clients.json`** on every authenticated request (to stamp `last_seen_at`). A read/auth path mutates state, serialises all paired-client auth through one lock, and on contention raises `TimeoutError` that `_authorized` (`mcp_server.py:772-778`) does not catch → unhandled 500. | Make `last_seen_at` best-effort/throttled (e.g. update at most once per N seconds) or skip it; wrap `authenticate` so lock timeout fails closed (deny) rather than 500. |
| F9 | low | `src/tasklane/doctor.py:60,87` | Doctor is documented "read-only … never mutates state" (README:185-189, module docstring), but `check_config_parse`→`load_config()` **creates `config.yaml` with a fresh token** if missing, and `check_job_store_dirs`→`ensure_dirs()` **creates directories** (even reports "created missing …"). Running `doctor` as a different user can create root-owned state. | Make doctor strictly read-only: detect missing config/dirs and report WARN without creating them. |
| F10 | low | `src/tasklane/mcp_server.py:761-764,839-841` | Origin allow-set is inconsistent: middleware uses raw `set(cfg.allowed_origins)` (empty by default) while `TransportSecuritySettings` falls back to `https://{public_hostname}`. A client sending a legit `Origin` for the public host gets 403 from the middleware though the transport would allow it. No test covers the 403/Origin path. | Derive the middleware's allowed origins from the same fallback as the transport; add an Origin-rejection test. |
| F11 | low | `src/tasklane/mcp_server.py:64-75` | `audited` writes **all kwargs** to `audit.log`, including full `write_file` `content`, `exec` `command`, and `apply_patch` `unified_diff`. Unbounded growth and a sink for any sensitive payload an operator passes. | Redact/elide large or sensitive args (log sizes/hashes, not bodies). |
| F12 | low | `src/tasklane/mcp_server.py:743` | `client_ip` is read from client-controllable `cf-connecting-ip` / `x-forwarded-for`; audit-log IPs are spoofable unless strictly fronted by a trusted proxy that overwrites them. | Document the trusted-proxy assumption; prefer the socket peer when not behind the known proxy. |
| F13 | low | `src/tasklane/worktree.py:122-124` | `.worktreeinclude` directory entries are added as **symlinks into the original repo**; a job writing through them mutates the live checkout (isolation breach). Opt-in, operator-controlled. | Copy directories (as for files) instead of symlinking, or document the breach clearly. |
| F14 | low | `src/tasklane/worker.py`, `src/tasklane/config.py:136` | `repos_allowlist` is enforced only at MCP creation (`create_task`/`create_pipeline`/`analyze_project`); the worker never re-checks `repo_path_allowed` before running a claimed job. A record inserted by any other path runs unrestricted. | Re-validate `repo_path_allowed` in the worker before workspace prep (defense in depth). |
| F15 | low | `tests/test_fanout.py`, `tests/e2e/test_pairing_e2e.py`, `tests/e2e/test_mcp_e2e.py` | Test-quality gaps on new security/integration surfaces: no test exercises the **real** analyze→fan-out format (F1); no Origin/403 rebind test; `write_file` path-escape is untested (only `read_file`). | Add the negative cases listed; make the fan-out e2e drive `fake_claude`'s analyze output, not synthetic JSON. |

---

## Drift & duplication observations

- **Format contract split between features (F1)** is the most damaging drift: the
  `analyze` role and the draft fan-out were built to different `proposed_tasks` schemas
  and never integration-tested together.
- **Atomic-write / timestamp / lock helpers (F6)** are re-implemented in `store` and
  `pairing`; `metrics` and `reconcile` already correctly *import* `store`'s versions,
  so `pairing`'s private copies are the outlier. Consolidate.
- **Prompt assembly (F7)** lives in two places (`worktree._generic_job_prompt` and
  `prompts/render.py`); the operational-rules block is duplicated verbatim.
- **Module size:** only `mcp_server.py` (853) exceeds limits; `specs.py` (433) and
  `worktree.py` (435) are within the 500 guideline.
- **Patterns are otherwise consistent:** spec coercion via `specs.py`, config coercion
  via `config._coerce`, `O_EXCL` locks, `tempfile`+`fsync`+`os.replace`, and the
  `audited` decorator are applied uniformly. No circular imports. `repo_path_allowed`,
  `validate_job_id`, `_safe_path`, and role/state whitelists give consistent boundary
  validation.
- **No new runtime dependencies** (only dev extras `pytest`, `httpx`). `claude` env is
  correctly stripped of `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` (`runner.py:27`).

### Features advertised in the review brief but NOT on `development`

Only `analyze-project` landed of the "roles" wave. These branches exist but are **not
merged** into `development`, so there was nothing to review (and, notably, **no secrets
handling code exists yet** — so the "secrets reaching prompts/logs/audit" surface is
currently empty): `secrets-intake`, `project-registry`, `client-skill`, `security-audit`,
`tester-role`. Reconcile the brief with the actual merge set before relying on them.

---

## Security pass verdict

- **Pairing:** correct. Pending cap enforced under lock (`request_pairing` prunes then
  checks `>= PENDING_CAP`); **no cap bypass found**. Tokens stored as SHA-256 only and
  verified with `hmac.compare_digest`; cleartext returned once; `clients.json` chmod 600.
  Only hardening items: F8 (write-on-auth) and minor.
- **Middleware:** app-token and paired-token compares are timing-safe; DNS-rebind via
  `TransportSecuritySettings` Host allow-list. Issues: F10 (origin set inconsistency),
  F12 (spoofable IP in audit).
- **Fan-out:** drafts are forced to `detached-review`/`report-only` and require human
  approval, so a malicious job output cannot create a runnable or pushing job — good
  blast-radius control. `allowed_paths` is passed through unvalidated (cosmetic, since
  drafts are read-only until a human edits). The functional bug is F1, not a security
  hole.
- **Fix tools:** `_safe_path` correctly rejects traversal (incl. symlink escape) for
  `read/write_file`/`apply_patch`. The gaps are F2 (exec unconfined, doc inaccurate) and
  F3 (live-repo fall-back).

## Test-quality verdict

**Adequate, with real masking gaps.** Strengths: 155 tests, no deletions/weakening/skips,
genuine e2e machinery (real git, real worker/MCP, fake `claude`), and good negative
coverage on pairing (cap, TTL, revoke, garbage token, no-hash leakage, mode 600) and on
auth (missing/wrong token, status-page token gate, `read_file` path-escape). Weakness:
the fan-out tests feed the parser JSON that the **real** `analyze` prompt never produces,
masking F1; no Origin/403 test; no `write_file` escape test. Recommend the additions in
F15 and treating "the fixture must use the feature's real output format" as a rule for
new integration tests.

---

## Verification performed

- `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"` — clean.
- `env -u PYTHONPATH .venv/bin/python -m pytest -q` → **155 passed, 5 warnings**
  (warnings: MCP `streamable_http_client` deprecation — pre-existing, harmless).
- Read every new/changed `src/` module in full; scoped via `git diff main...development`.
- `development` and `main` left untouched; work confined to `tasklane/big-review`.
