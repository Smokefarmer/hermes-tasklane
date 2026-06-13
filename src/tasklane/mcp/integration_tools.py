"""MCP tools for the GitHub + Linear integrations.

These give a connected Claude (e.g. the mobile app, over the OAuth connector)
GitHub and Linear access *through* TaskLane — and the bridge tools turn a Linear
ticket into a GitHub issue and a GitHub issue into a TaskLane job, so the whole
"Linear → GitHub → TaskLane" flow runs from one connection.
"""

from __future__ import annotations

from tasklane import integrations
from tasklane.config import repo_path_allowed
from tasklane.mcp.core import _cfg, _store, audited, mcp


# --------------------------------------------------------------------------- #
# connection management
# --------------------------------------------------------------------------- #
@mcp.tool()
@audited
def set_integration_token(service: str, token: str, default_repo: str | None = None) -> dict:
    """Connect GitHub or Linear by storing its API token (mode-600, never logged).

    service: "github" (a fine-grained PAT with Issues read/write) or "linear"
    (a personal API key). default_repo (GitHub only, "owner/name") is used when a
    tool call omits the repo. The token value is redacted from the audit log;
    only a masked hint is ever returned."""
    return integrations.set_token(service, token, default_repo=default_repo)


@mcp.tool()
@audited
def integration_status() -> dict:
    """Connection status for GitHub and Linear (connected? masked token hint, no values)."""
    return integrations.status()


@mcp.tool()
@audited
def disconnect_integration(service: str) -> dict:
    """Remove the stored token for a service ("github" or "linear")."""
    return {"service": service, "removed": integrations.clear_token(service)}


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #
def _repo_or_default(repo: str | None) -> str:
    repo = (repo or "").strip() or integrations.get_meta("github", "default_repo")
    if not repo:
        raise ValueError("no repo given and no github default_repo configured")
    return repo


@mcp.tool()
@audited
def github_create_issue(title: str, body: str = "", repo: str | None = None,
                        labels: list[str] | None = None) -> dict:
    """Create a GitHub issue. repo defaults to the configured github default_repo."""
    return integrations.github_create_issue(_repo_or_default(repo), title, body, labels)


@mcp.tool()
@audited
def github_list_issues(repo: str | None = None, state: str = "open",
                       label: str | None = None, limit: int = 20) -> list[dict]:
    """List GitHub issues (open by default). repo defaults to github default_repo."""
    return integrations.github_list_issues(_repo_or_default(repo), state, label, limit)


@mcp.tool()
@audited
def github_get_issue(number: int, repo: str | None = None) -> dict:
    """Fetch one GitHub issue (title, body, state, labels). repo defaults to github default_repo."""
    return integrations.github_get_issue(_repo_or_default(repo), number)


# --------------------------------------------------------------------------- #
# Linear
# --------------------------------------------------------------------------- #
@mcp.tool()
@audited
def linear_list_issues(limit: int = 20) -> list[dict]:
    """List recent Linear issues (id, identifier, title, state, url)."""
    return integrations.linear_list_issues(limit)


@mcp.tool()
@audited
def linear_get_issue(issue_id: str) -> dict:
    """Fetch one Linear issue by id or identifier (e.g. 'ENG-123')."""
    return integrations.linear_get_issue(issue_id)


# --------------------------------------------------------------------------- #
# bridges: Linear -> GitHub -> TaskLane
# --------------------------------------------------------------------------- #
@mcp.tool()
@audited
def linear_to_github_issue(linear_id: str, repo: str | None = None, labels: list[str] | None = None) -> dict:
    """Create a GitHub issue from a Linear ticket (title + description + back-link)."""
    ticket = integrations.linear_get_issue(linear_id)
    repo = _repo_or_default(repo)
    title = f"[{ticket['identifier']}] {ticket['title']}" if ticket.get("identifier") else ticket["title"]
    body_parts = [ticket.get("description") or ""]
    if ticket.get("url"):
        body_parts.append(f"\n\n---\nFrom Linear: {ticket['url']}")
    issue = integrations.github_create_issue(repo, title, "".join(body_parts).strip(), labels)
    return {"linear": {"identifier": ticket.get("identifier"), "url": ticket.get("url")},
            "github_issue": issue, "repo": repo}


@mcp.tool()
@audited
def github_issue_to_task(number: int, repo_path: str, work_branch: str,
                         github_repo: str | None = None, base_branch: str = "main",
                         delivery_mode: str = "pull-request", id: str | None = None) -> dict:
    """Create a TaskLane coding job from a GitHub issue.

    number/github_repo identify the GitHub issue (github_repo "owner/name", or the
    configured default). repo_path is the LOCAL git checkout the job runs in (must be
    allowlisted, like create_task). The issue title+body+link become the brief;
    delivery defaults to a PR on work_branch that closes the issue."""
    cfg = _cfg()
    if not repo_path_allowed(repo_path, cfg):
        raise ValueError(f"repo path not in allowlist: {repo_path}")
    gh_repo = _repo_or_default(github_repo)
    issue = integrations.github_get_issue(gh_repo, number)
    body = (
        f"Implement GitHub issue #{issue['number']} ({gh_repo}): {issue['title']}\n\n"
        f"{issue.get('body') or '(no description)'}\n\n"
        f"---\nSource issue: {issue.get('url')}\n"
        f"Reference the issue in the PR so it auto-closes (e.g. 'Closes #{issue['number']}')."
    )
    spec = {
        "repo": {"path": repo_path},
        "request": {"type": "task-small", "title": f"[#{issue['number']}] {issue['title']}", "body": body},
        "branch": {"mode": "new-branch", "base_branch": base_branch,
                   "work_branch": work_branch, "pr_target": base_branch},
        "delivery_mode": delivery_mode,
        "source": {"github_issue": issue.get("url"), "github_repo": gh_repo, "issue_number": issue["number"]},
    }
    if id:
        spec["id"] = id
    record = _store().put(spec)
    return {"id": record["id"], "state": record["state"], "github_issue": issue.get("url")}
