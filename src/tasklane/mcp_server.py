"""TaskLane MCP control plane (Streamable HTTP) — wiring + entrypoint.

The implementation is split across the ``tasklane.mcp`` package:
  - ``mcp.core``          — the shared FastMCP instance, the ``audited`` decorator,
                            config/store accessors, and worktree/path helpers.
  - ``mcp.lifecycle``     — create_task / create_pipeline / analyze_project /
                            security_audit and the task lifecycle tools.
  - ``mcp.drafts``        — draft-approval tools (propose → approve → run).
  - ``mcp.fixtools``      — worktree inspect & fix tools.
  - ``mcp.pairing_tools`` — client pairing (connection approval) tools.
  - ``mcp.ops``           — worker ops, metrics, project registry & secret intake.
  - ``mcp.status``        — the browser status page.
  - ``mcp.auth``          — the ASGI auth middleware and the public /pair endpoint.

Importing the tool submodules below registers their ``@mcp.tool()`` functions on
the shared instance. This module then re-exports the public surface (so
``tasklane.mcp_server.<tool>`` keeps resolving) and provides ``build_app`` / ``main``.
"""

from __future__ import annotations

from tasklane.config import Config, load_config

# Shared core: the FastMCP instance, decorator, accessors, helpers, doctrine.
from tasklane.mcp.core import (  # noqa: F401 — re-exported public surface
    USAGE,
    audited,
    logger,
    mcp,
    _cfg,
    _get_record,
    _readable_dir,
    _run,
    _safe_path,
    _store,
    _summary,
    _worktree_dir,
)

# Tool modules — imported for their @mcp.tool() registration side effects, and
# re-exported so existing call sites (tests, ops) keep using tasklane.mcp_server.X.
from tasklane.mcp.lifecycle import (  # noqa: F401
    analyze_project,
    cancel_task,
    create_pipeline,
    create_task,
    get_task,
    list_tasks,
    run_task_now,
    retry_task,
    security_audit,
    task_events,
    task_logs,
    _analyze_job_spec,
    _pipeline_stage_specs,
)
from tasklane.mcp.drafts import (  # noqa: F401
    approve_all_drafts,
    approve_draft,
    list_drafts,
    reject_draft,
)
from tasklane.mcp.fixtools import (  # noqa: F401
    apply_patch,
    exec,
    get_diff,
    git,
    list_dir,
    read_file,
    run_tests,
    write_file,
)
from tasklane.mcp.pairing_tools import (  # noqa: F401
    approve_client,
    list_clients,
    pairing_requests,
    reject_client,
    revoke_client,
)
from tasklane.mcp.ops import (  # noqa: F401
    admin_exec,
    delete_project_secret,
    list_project_secret_keys,
    list_projects,
    metrics,
    prune_worktrees,
    reconcile_jobs,
    register_project,
    restart_worker,
    set_project_secrets,
    test_deployment,
    worker_status,
)
from tasklane.mcp.status import _status_data, _status_html  # noqa: F401
from tasklane.mcp.auth import AuthMiddleware


def build_app(cfg: Config):
    from mcp.server.transport_security import TransportSecuritySettings

    if not cfg.app_token:
        raise RuntimeError("config.app_token is empty; refusing to start without an app token")
    mcp.settings.host = cfg.mcp_host
    mcp.settings.port = cfg.mcp_port
    # DNS-rebinding protection: allow localhost plus the configured public hostnames
    # (the Host header arriving via the Cloudflare tunnel, e.g. tasklane.example.com).
    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    for h in cfg.public_hostnames:
        allowed_hosts += [h, f"{h}:*"]
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=cfg.allowed_origins or [f"https://{h}" for h in cfg.public_hostnames],
    )
    # FastMCP caches its StreamableHTTP session manager, but a session manager's
    # run() works only once per instance — a second build_app (tests, in-process
    # restarts) would fail at startup. Reset the cache so every built app gets
    # its own manager.
    mcp._session_manager = None
    app = mcp.streamable_http_app()
    allowed = set(cfg.allowed_origins)
    return AuthMiddleware(app, token=cfg.app_token, allowed_origins=allowed,
                          pairing_enabled=cfg.pairing_enabled,
                          oauth_enabled=cfg.oauth_enabled)


def main() -> None:
    import uvicorn

    cfg = load_config()
    logger.info("TaskLane MCP on %s:%s (mcp path /mcp) admin_exec=%s", cfg.mcp_host, cfg.mcp_port, cfg.enable_admin_exec)
    uvicorn.run(build_app(cfg), host=cfg.mcp_host, port=cfg.mcp_port, log_level="info")


if __name__ == "__main__":
    main()
