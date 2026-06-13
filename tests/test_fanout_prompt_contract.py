"""Drift guard: the ``proposed_tasks`` example shipped in the analyze/audit prompt
templates MUST parse through the real fan-out parser and build valid draft specs.

This is the regression test the big-review asked for: previously the analyze
prompt emitted markdown bullets while the parser required JSON, so a faithful
analyze run produced zero drafts — and no test caught it because the fan-out
tests fed hand-written JSON the prompt never produces. Here we render the actual
shipped template and drive its own documented output through the parser, so the
prompt and parser can never silently diverge again.
"""

import pytest

from tasklane.fanout import _draft_spec, create_proposed_drafts, parse_proposed_tasks
from tasklane.specs import validate_job_spec
from tasklane.store import JobStore
from tasklane.worktree import job_prompt

ROLES_WITH_FANOUT = ["analyze", "audit"]


def _role_record(role: str) -> dict:
    spec = validate_job_spec({
        "id": f"{role}-parent",
        "repo": {"path": "/tmp/demo-repo"},
        "request": {"type": "task", "title": "Audit me", "body": "audit the repo"},
        "branch": {"mode": "detached-review", "base_branch": "main"},
        "delivery_mode": "report-only",
        "role": role,
    })
    return {"id": f"{role}-parent", "spec": spec}


@pytest.fixture()
def store(tmp_path):
    s = JobStore(root=tmp_path / "jobs")
    s.ensure_dirs()
    return s


@pytest.mark.parametrize("role", ROLES_WITH_FANOUT)
def test_prompt_example_parses_as_json(role):
    """The fenced proposed_tasks block in the rendered prompt is valid JSON the
    parser accepts — not markdown bullets."""
    prompt = job_prompt(_role_record(role))
    entries = parse_proposed_tasks(prompt)
    assert entries, f"{role}.md proposed_tasks example produced zero parsed entries"
    for entry in entries:
        assert entry.get("title"), f"{role} example entry missing title"


@pytest.mark.parametrize("role", ROLES_WITH_FANOUT)
def test_prompt_example_builds_valid_drafts(role, store):
    """Each example entry builds a spec that survives full validation and carries
    a non-empty body (the audit field-name bug created empty-body drafts)."""
    parent = _role_record(role)
    entries = parse_proposed_tasks(job_prompt(parent))
    for index, entry in enumerate(entries, start=1):
        spec = _draft_spec(parent, entry, f"{parent['id']}-p{index:02d}")
        validated = validate_job_spec(spec)  # raises on contract violation
        assert validated["request"]["body"].strip(), f"{role} draft #{index} has empty body"
        assert validated["delivery_mode"] == "report-only"
        assert validated["branch"]["mode"] == "detached-review"


def test_inline_mention_does_not_shadow_real_block():
    """Prose mentioning a fenced ```proposed_tasks``` block inline must NOT be
    captured instead of the real trailing block (the bug this suite guards)."""
    text = (
        "Please end with a fenced ```proposed_tasks``` block of remediations.\n\n"
        "Here it is:\n\n"
        '```proposed_tasks\n'
        '[{"title": "real task", "body": "do the real thing", "type": "task-small", "severity": "high"}]\n'
        "```\n"
    )
    entries = parse_proposed_tasks(text)
    assert len(entries) == 1
    assert entries[0]["title"] == "real task"


def test_markdown_bullet_block_is_rejected_gracefully(store):
    """The OLD (broken) markdown-bullet format must fail safe: no crash, zero
    drafts, a recorded parse error — never a half-created queue."""
    parent = _role_record("analyze")
    store.put(parent["spec"], state="completed")
    bullet_response = (
        "Findings... \n\n```proposed_tasks\n"
        "- [high] Break god-module src/foo.py into units.\n"
        "- [low] Delete dead code.\n```\n"
    )
    result = create_proposed_drafts(store, store.get(parent["id"]), bullet_response)
    assert result["created"] == []
    assert result["error"] is not None  # recorded, not raised
    events = [e["event_type"] for e in store.events(parent["id"])]
    assert "job_fanout_parse_failed" in events


@pytest.mark.parametrize("role", ROLES_WITH_FANOUT)
def test_prompt_example_creates_drafts_end_to_end(role, store):
    """Driving the rendered prompt straight into create_proposed_drafts yields
    draft jobs (not a parse failure)."""
    parent = _role_record(role)
    store.put(parent["spec"], state="completed")
    result = create_proposed_drafts(store, store.get(parent["id"]), job_prompt(parent))
    assert result["error"] is None
    assert result["created"], f"{role} prompt example created zero drafts"
    first = store.get(result["created"][0])
    assert first["state"] == "draft"
    assert first["spec"]["request"]["body"].strip()
    assert first["spec"]["source"]["proposed_by"] == parent["id"]
