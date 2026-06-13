"""TaskLane MCP drafts tools (registered on the shared FastMCP instance)."""

from __future__ import annotations

from tasklane.mcp.core import *  # noqa: F401,F403 — shared hub (mcp, audited, helpers)
from tasklane.mcp.core import (  # explicit for linters / clarity
    mcp, audited, _cfg, _store, _get_record, _worktree_dir, _readable_dir,
    _safe_path, _run, _summary, _live_worktree, _audit, logger, repo_path_allowed,
    pairing, cleanup_worktree, worktrees_root, _WORKER_SERVICE, _MAX_READ_BYTES, _MAX_OUT,
)


# --------------------------------------------------------------------------- #
# draft approval (agent-proposed follow-up jobs)
# --------------------------------------------------------------------------- #
def _require_draft(store: JobStore, job_id: str) -> dict[str, Any]:
    """Return the record iff it exists and is in state draft, else raise a clear error."""
    record = store.get(job_id)
    if record is None:
        raise FileNotFoundError(f"job not found: {job_id}")
    state = str(record.get("state") or "")
    if state != "draft":
        raise ValueError(f"job {job_id} is not a draft (state={state}); only drafts can be approved or rejected")
    return record


@mcp.tool()
@audited
def list_drafts() -> list[dict[str, Any]]:
    """List agent-proposed draft jobs awaiting approval (state=draft) as summaries.
    Drafts are NOT runnable until approved — the worker never claims them."""
    return [_summary(r) for r in _store().list(states=["draft"])]


@mcp.tool()
@audited
def approve_draft(job_id: str) -> dict[str, Any]:
    """Approve an agent-proposed draft (draft -> ready) so the worker can claim it.
    Errors if the job is not currently a draft."""
    store = _store()
    _require_draft(store, job_id)
    updated = store.transition(job_id, "ready", reason="draft-approved")
    return {"id": job_id, "state": updated["state"]}


@mcp.tool()
@audited
def approve_all_drafts() -> dict[str, Any]:
    """Approve every draft job (draft -> ready). Returns the approved ids."""
    store = _store()
    approved: list[str] = []
    for record in store.list(states=["draft"]):
        job_id = record.get("id")
        if not job_id:
            continue
        store.transition(job_id, "ready", reason="draft-approved")
        approved.append(job_id)
    return {"approved": approved, "count": len(approved)}


@mcp.tool()
@audited
def reject_draft(job_id: str, reason: str = "") -> dict[str, Any]:
    """Reject an agent-proposed draft (draft -> failed). Errors if not a draft."""
    store = _store()
    _require_draft(store, job_id)
    note = (reason or "").strip() or "draft-rejected"
    updated = store.transition(job_id, "failed", reason="draft-rejected", updates={"last_error": note})
    return {"id": job_id, "state": updated["state"]}


