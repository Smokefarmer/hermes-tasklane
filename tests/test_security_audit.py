"""Unit tests for the security_audit MCP tool: it creates ONE report-only,
detached-review job with the 'audit' role, and rejects repos outside the allowlist.
"""

import pytest
import yaml

from tasklane.specs import validate_job_spec


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point TASKLANE_HOME at a temp dir so the store + audit log are isolated."""
    monkeypatch.setenv("TASKLANE_HOME", str(tmp_path))
    monkeypatch.delenv("TASKLANE_APP_TOKEN", raising=False)
    return tmp_path


def _write_config(home, *, allowlist):
    (home / "config.yaml").write_text(
        yaml.safe_dump({"app_token": "x", "repos_allowlist": allowlist}),
        encoding="utf-8",
    )


def test_security_audit_creates_report_only_detached_audit_job(home):
    _write_config(home, allowlist=[])  # empty allowlist = allow any
    from tasklane.mcp_server import _store, security_audit

    result = security_audit(repo="/tmp/some-repo")
    assert "error" not in result
    assert result["role"] == "audit"
    assert result["state"] == "ready"

    record = _store().get(result["id"])
    spec = record["spec"]
    assert spec["role"] == "audit"
    assert spec["delivery_mode"] == "report-only"
    assert spec["branch"]["mode"] == "detached-review"
    assert spec["branch"]["base_branch"] == "main"
    # the spec is a valid, normalized job spec
    assert validate_job_spec(spec)["role"] == "audit"


def test_security_audit_scope_narrows_title_and_body(home):
    _write_config(home, allowlist=[])
    from tasklane.mcp_server import _store, security_audit

    result = security_audit(repo="/tmp/some-repo", scope="backend auth only",
                            base_branch="develop", id="audit-1")
    assert result["id"] == "audit-1"
    spec = _store().get("audit-1")["spec"]
    assert "backend auth only" in spec["request"]["title"]
    assert "backend auth only" in spec["request"]["body"]
    assert spec["branch"]["base_branch"] == "develop"
    # the audit mission still references OWASP + decomposition in the body
    assert "OWASP" in spec["request"]["body"]
    assert "proposed_tasks" in spec["request"]["body"]


def test_security_audit_rejects_repo_outside_allowlist(home):
    _write_config(home, allowlist=["/srv/allowed"])
    from tasklane.mcp_server import _store, security_audit

    result = security_audit(repo="/tmp/not-allowed")
    # the @audited wrapper turns the ValueError into an error envelope
    assert "error" in result
    assert "allowlist" in result["error"]
    # nothing was created
    assert _store().list() == []


def test_security_audit_creates_exactly_one_job(home):
    _write_config(home, allowlist=[])
    from tasklane.mcp_server import _store, security_audit

    security_audit(repo="/tmp/some-repo", id="solo-audit")
    assert len(_store().list()) == 1
