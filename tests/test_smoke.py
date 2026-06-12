"""Smoke tests — the package imports and the CLI parser builds."""

import importlib

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "tasklane.paths",
        "tasklane.specs",
        "tasklane.store",
        "tasklane.config",
        "tasklane.runner",
        "tasklane.worktree",
        "tasklane.worker",
        "tasklane.cli",
        "tasklane.mcp_server",
    ],
)
def test_imports(module: str) -> None:
    importlib.import_module(module)


def test_cli_parser_builds() -> None:
    from tasklane.cli import build_parser

    parser = build_parser()
    assert parser.prog == "tasklane"


def test_spec_validation_roundtrip() -> None:
    from tasklane.specs import validate_job_spec

    spec = validate_job_spec(
        {
            "repo": {"path": "/tmp/x"},
            "request": {"type": "task", "title": "t", "body": "b"},
            "branch": {"mode": "detached-review"},
            "delivery_mode": "report-only",
        }
    )
    assert spec["delivery_mode"] == "report-only"
    assert spec["branch"]["mode"] == "detached-review"
