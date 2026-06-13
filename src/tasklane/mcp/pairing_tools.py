"""TaskLane MCP pairing_tools tools (registered on the shared FastMCP instance)."""

from __future__ import annotations

from tasklane.mcp.core import *  # noqa: F401,F403 — shared hub (mcp, audited, helpers)
from tasklane.mcp.core import (  # explicit for linters / clarity
    mcp, audited, _cfg, _store, _get_record, _worktree_dir, _readable_dir,
    _safe_path, _run, _summary, _live_worktree, _audit, logger, repo_path_allowed,
    pairing, cleanup_worktree, worktrees_root, _WORKER_SERVICE, _MAX_READ_BYTES, _MAX_OUT,
)


# --------------------------------------------------------------------------- #
# client pairing (connection approval)
# --------------------------------------------------------------------------- #
@mcp.tool()
@audited
def pairing_requests() -> list[dict[str, Any]]:
    """List unexpired pending pairing requests (pairing_code, client name, requested_at)."""
    return pairing.pending_requests()


@mcp.tool()
@audited
def approve_client(pairing_code: str) -> dict[str, Any]:
    """Approve a pending pairing request; the client's token starts authenticating immediately."""
    return pairing.approve_client(pairing_code)


@mcp.tool()
@audited
def reject_client(pairing_code: str) -> dict[str, Any]:
    """Reject a pending pairing request and discard its client record."""
    return pairing.reject_client(pairing_code)


@mcp.tool()
@audited
def list_clients() -> list[dict[str, Any]]:
    """List all paired clients (pending, approved, revoked); token hashes are never returned."""
    return pairing.list_clients()


@mcp.tool()
@audited
def revoke_client(client_id: str) -> dict[str, Any]:
    """Revoke a paired client; its token stops authenticating immediately."""
    return pairing.revoke_client(client_id)


