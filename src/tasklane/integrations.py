"""Outbound integrations: GitHub and Linear, exposed to a connected Claude.

TaskLane is the connector your Claude app reaches over OAuth. These integrations
let that one connection also reach GitHub and Linear — there is no GitHub
connector in the Claude apps, so TaskLane carries it. Unlike pairing/oauth (where
TaskLane is the auth *server* and stores only token hashes), here TaskLane is the
*client* to GitHub/Linear, so it must hold the real API tokens. They live in
``$TASKLANE_HOME/integrations.json`` (mode 600, atomic) and are never returned by
any read API or logged — status reports a masked hint only.

HTTP goes through a single ``_http_json`` seam (stdlib ``urllib`` — no new runtime
dependency) so tests can stub it without real network calls.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from tasklane.atomicio import atomic_write_json
from tasklane.paths import tasklane_home
from tasklane.store import utc_now

SERVICES = ("github", "linear")
GITHUB_API = "https://api.github.com"
LINEAR_API = "https://api.linear.app/graphql"
_HTTP_TIMEOUT = 30


def integrations_path():
    return tasklane_home() / "integrations.json"


def _load() -> dict[str, Any]:
    path = integrations_path()
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _save(data: dict[str, Any]) -> None:
    atomic_write_json(integrations_path(), data, mode=0o600)


# --------------------------------------------------------------------------- #
# credential management
# --------------------------------------------------------------------------- #
def set_token(service: str, token: str, **meta: Any) -> dict[str, Any]:
    """Store (or replace) the API token for ``service``. Extra kwargs (e.g.
    default_repo) are kept as metadata. Returns the masked status, never the token."""
    svc = _service(service)
    tok = str(token or "").strip()
    if not tok:
        raise ValueError("token is required")
    data = _load()
    entry = dict(data.get(svc) or {})
    entry["token"] = tok
    entry["configured_at"] = utc_now()
    for key, value in meta.items():
        if value is not None:
            entry[key] = str(value).strip() or None
    data[svc] = entry
    _save(data)
    return _public_entry(svc, entry)


def clear_token(service: str) -> bool:
    svc = _service(service)
    data = _load()
    if svc in data:
        del data[svc]
        _save(data)
        return True
    return False


def get_token(service: str) -> str | None:
    entry = _load().get(_service(service)) or {}
    tok = entry.get("token")
    return str(tok) if tok else None


def get_meta(service: str, key: str) -> Any:
    return (_load().get(_service(service)) or {}).get(key)


def status() -> dict[str, Any]:
    """Connection status for every service — masked token hint only, never the value."""
    data = _load()
    out: dict[str, Any] = {}
    for svc in SERVICES:
        entry = data.get(svc)
        out[svc] = _public_entry(svc, entry) if entry else {"connected": False}
    return out


def _public_entry(svc: str, entry: dict[str, Any]) -> dict[str, Any]:
    tok = str(entry.get("token") or "")
    masked = (tok[:4] + "…" + tok[-2:]) if len(tok) >= 8 else "set"
    public = {k: v for k, v in entry.items() if k != "token"}
    return {"connected": True, "token_hint": masked, **public}


def _service(service: str) -> str:
    svc = str(service or "").strip().lower()
    if svc not in SERVICES:
        raise ValueError(f"unknown service {service!r}; expected one of: {', '.join(SERVICES)}")
    return svc


# --------------------------------------------------------------------------- #
# HTTP seam (stubbed in tests)
# --------------------------------------------------------------------------- #
def _http_json(method: str, url: str, *, headers: dict[str, str], body: dict | None = None) -> tuple[int, Any]:
    """Make a JSON HTTP request; return (status_code, parsed_body). Raises on
    network failure. The single place real I/O happens — tests monkeypatch this."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = {"message": raw[:500]}
        return exc.code, parsed


# --------------------------------------------------------------------------- #
# GitHub (REST)
# --------------------------------------------------------------------------- #
def _github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "tasklane",
        "Content-Type": "application/json",
    }


def _require_github() -> str:
    token = get_token("github")
    if not token:
        raise ValueError("GitHub is not connected; set a token with set_integration_token('github', ...)")
    return token


def _validate_repo(repo: str) -> str:
    repo = str(repo or "").strip()
    if repo.count("/") != 1 or not all(repo.split("/")):
        raise ValueError(f"repo must be 'owner/name': {repo!r}")
    return repo


def github_create_issue(repo: str, title: str, body: str = "", labels: list[str] | None = None) -> dict[str, Any]:
    token = _require_github()
    repo = _validate_repo(repo)
    payload: dict[str, Any] = {"title": str(title or "").strip()}
    if not payload["title"]:
        raise ValueError("issue title is required")
    if body:
        payload["body"] = str(body)
    if labels:
        payload["labels"] = [str(l) for l in labels]
    code, data = _http_json("POST", f"{GITHUB_API}/repos/{repo}/issues",
                            headers=_github_headers(token), body=payload)
    if code not in (200, 201):
        raise ValueError(f"GitHub issue creation failed ({code}): {_msg(data)}")
    return {"number": data.get("number"), "url": data.get("html_url"),
            "title": data.get("title"), "state": data.get("state")}


def github_list_issues(repo: str, state: str = "open", label: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    token = _require_github()
    repo = _validate_repo(repo)
    from urllib.parse import urlencode

    q = {"state": state, "per_page": max(1, min(100, int(limit)))}
    if label:
        q["labels"] = label
    code, data = _http_json("GET", f"{GITHUB_API}/repos/{repo}/issues?{urlencode(q)}",
                            headers=_github_headers(token))
    if code != 200 or not isinstance(data, list):
        raise ValueError(f"GitHub list issues failed ({code}): {_msg(data)}")
    # GitHub returns PRs in the issues list too; filter them out.
    return [{"number": i.get("number"), "title": i.get("title"), "state": i.get("state"),
             "url": i.get("html_url"), "labels": [l.get("name") for l in i.get("labels") or []]}
            for i in data if "pull_request" not in i]


def github_get_issue(repo: str, number: int) -> dict[str, Any]:
    token = _require_github()
    repo = _validate_repo(repo)
    code, data = _http_json("GET", f"{GITHUB_API}/repos/{repo}/issues/{int(number)}",
                            headers=_github_headers(token))
    if code != 200:
        raise ValueError(f"GitHub get issue failed ({code}): {_msg(data)}")
    return {"number": data.get("number"), "title": data.get("title"), "body": data.get("body"),
            "state": data.get("state"), "url": data.get("html_url"),
            "labels": [l.get("name") for l in data.get("labels") or []]}


# --------------------------------------------------------------------------- #
# Linear (GraphQL)
# --------------------------------------------------------------------------- #
def _require_linear() -> str:
    token = get_token("linear")
    if not token:
        raise ValueError("Linear is not connected; set a token with set_integration_token('linear', ...)")
    return token


def _linear_graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    token = _require_linear()
    headers = {"Authorization": token, "Content-Type": "application/json", "User-Agent": "tasklane"}
    code, data = _http_json("POST", LINEAR_API, headers=headers,
                            body={"query": query, "variables": variables or {}})
    if code != 200 or not isinstance(data, dict):
        raise ValueError(f"Linear request failed ({code}): {_msg(data)}")
    if data.get("errors"):
        raise ValueError(f"Linear GraphQL error: {data['errors']}")
    return data.get("data") or {}


_LINEAR_ISSUE_FIELDS = "id identifier title description url state { name } "


def linear_list_issues(limit: int = 20) -> list[dict[str, Any]]:
    data = _linear_graphql(
        "query($n:Int!){ issues(first:$n, orderBy:updatedAt) { nodes { " + _LINEAR_ISSUE_FIELDS + "} } }",
        {"n": max(1, min(100, int(limit)))},
    )
    nodes = (((data.get("issues") or {}).get("nodes")) or [])
    return [_linear_node(n) for n in nodes]


def linear_get_issue(issue_id: str) -> dict[str, Any]:
    data = _linear_graphql(
        "query($id:String!){ issue(id:$id) { " + _LINEAR_ISSUE_FIELDS + "} }",
        {"id": str(issue_id or "").strip()},
    )
    node = data.get("issue")
    if not node:
        raise ValueError(f"Linear issue not found: {issue_id!r}")
    return _linear_node(node)


def _linear_node(n: dict[str, Any]) -> dict[str, Any]:
    return {"id": n.get("id"), "identifier": n.get("identifier"), "title": n.get("title"),
            "description": n.get("description"), "url": n.get("url"),
            "state": (n.get("state") or {}).get("name")}


def _msg(data: Any) -> str:
    if isinstance(data, dict):
        return str(data.get("message") or data)[:300]
    return str(data)[:300]
