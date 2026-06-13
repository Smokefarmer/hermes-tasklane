"""Fan-out parsing: turn a completed job's ``proposed_tasks`` block into drafts.

Agents may PROPOSE follow-up work without being able to CREATE runnable jobs
(a queue/budget explosion guard). When a job completes, the worker scans its
final response for a fenced ``proposed_tasks`` block, e.g.::

    ```proposed_tasks
    [{"title": "...", "body": "...", "type": "task-small",
      "allowed_paths": ["src/x"], "severity": "high"}]
    ```

Each well-formed entry becomes a job in the ``draft`` state — targeting the same
repo as the parent, branch mode ``detached-review`` + delivery ``report-only`` by
default (proposals are investigations unless a human edits them) — with
``spec.source = {"proposed_by": <parent_id>}`` and id ``<parent_id>-p<NN>``.
A human approves drafts (draft -> ready) before any of them can run.

Malformed JSON, oversize batches, and bad individual entries never crash the
worker: they are logged and recorded as job events on the parent.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping

from tasklane.store import JobStore

logger = logging.getLogger("tasklane.fanout")

# Hard cap on drafts created from a single completion (budget/queue guard).
PROPOSED_TASKS_CAP = 20

# Matches a ```proposed_tasks fenced block; the opening fence may carry trailing
# text (ignored) and the payload is captured up to the closing fence.
_FENCE_RE = re.compile(r"```proposed_tasks[^\n]*\n(.*?)```", re.DOTALL)


def extract_proposed_tasks_block(text: str | None) -> str | None:
    """Return the raw payload of the first ``proposed_tasks`` fenced block, or None."""
    if not text:
        return None
    match = _FENCE_RE.search(text)
    return match.group(1).strip() if match else None


def parse_proposed_tasks(text: str | None) -> list[dict[str, Any]]:
    """Parse ``proposed_tasks`` entries from a final response.

    Returns the list of well-formed entry mappings (uncapped — the caller caps).
    Returns ``[]`` when no block is present. Raises ``ValueError`` (which includes
    :class:`json.JSONDecodeError`) on malformed JSON so the caller can record it.
    """
    raw = extract_proposed_tasks_block(text)
    if raw is None or not raw:
        return []
    data = json.loads(raw)
    if isinstance(data, Mapping):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("proposed_tasks block must be a JSON array or object")
    return [dict(item) for item in data if isinstance(item, Mapping)]


def _draft_spec(parent: Mapping[str, Any], entry: Mapping[str, Any], draft_id: str) -> dict[str, Any]:
    """Build a draft job spec from a single proposal entry, targeting the parent's repo."""
    parent_spec = parent.get("spec") if isinstance(parent.get("spec"), dict) else {}
    repo = parent_spec.get("repo") if isinstance(parent_spec.get("repo"), dict) else {}
    parent_branch = parent_spec.get("branch") if isinstance(parent_spec.get("branch"), dict) else {}
    repo_path = str(repo.get("path") or "").strip()
    if not repo_path:
        raise ValueError("parent job has no repo.path; cannot target the same repo")

    title = str(entry.get("title") or "").strip()
    body = str(entry.get("body") or entry.get("description") or entry.get("prompt") or "").strip()
    request_type = str(entry.get("type") or "task-small").strip() or "task-small"

    source: dict[str, Any] = {"proposed_by": str(parent.get("id") or "")}
    severity = str(entry.get("severity") or "").strip()
    if severity:
        source["severity"] = severity

    spec: dict[str, Any] = {
        "id": draft_id,
        "repo": {"path": repo_path, "key": repo.get("key")},
        "request": {"type": request_type, "title": title, "body": body},
        # proposals are investigations: read-only review unless a human edits them
        "branch": {"mode": "detached-review", "base_branch": parent_branch.get("base_branch")},
        "delivery_mode": "report-only",
        "source": source,
    }
    allowed_paths = entry.get("allowed_paths")
    if allowed_paths:
        spec["scope"] = {"allowed_paths": allowed_paths}
    return spec


def create_proposed_drafts(
    store: JobStore,
    parent: Mapping[str, Any],
    final_response: str | None,
) -> dict[str, Any]:
    """Scan a completed job's final response and create draft jobs for its proposals.

    Never raises: malformed JSON and bad entries are recorded as parent job events.
    Returns ``{"created": [ids], "skipped": int, "rejected": int, "error": str|None}``.
    """
    parent_id = str(parent.get("id") or "")
    try:
        entries = parse_proposed_tasks(final_response)
    except ValueError as exc:
        store.append_event(parent_id, "job_fanout_parse_failed", reason=f"malformed proposed_tasks: {exc}")
        logger.warning("Job %s proposed_tasks block is malformed: %s", parent_id, exc)
        return {"created": [], "skipped": 0, "rejected": 0, "error": str(exc)}

    if not entries:
        return {"created": [], "skipped": 0, "rejected": 0, "error": None}

    capped = entries[:PROPOSED_TASKS_CAP]
    skipped = len(entries) - len(capped)
    if skipped:
        logger.warning(
            "Job %s proposed %d tasks; capping at %d (ignoring %d)",
            parent_id, len(entries), PROPOSED_TASKS_CAP, skipped,
        )

    created: list[str] = []
    rejected = 0
    for index, entry in enumerate(capped, start=1):
        draft_id = f"{parent_id}-p{index:02d}"
        try:
            record = store.put(_draft_spec(parent, entry, draft_id), state="draft", reason="proposed-by-agent")
            created.append(str(record["id"]))
        except Exception as exc:  # noqa: BLE001 — one bad proposal must not abort the rest
            rejected += 1
            store.append_event(parent_id, "job_fanout_draft_rejected", reason=str(exc),
                               metadata={"draft_id": draft_id})
            logger.warning("Job %s draft %s rejected: %s", parent_id, draft_id, exc)

    if created:
        store.append_event(parent_id, "job_fanout_drafts_created", reason="draft-proposals-created",
                           metadata={"draft_ids": created})
        logger.info("Job %s proposed %d draft(s): %s", parent_id, len(created), ", ".join(created))
    return {"created": created, "skipped": skipped, "rejected": rejected, "error": None}
