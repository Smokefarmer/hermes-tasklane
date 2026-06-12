from __future__ import annotations

from copy import deepcopy
from enum import Enum
import re
from typing import Any, Mapping


class RequestType(str, Enum):
    BUG_SMALL = "bug-small"
    TASK_SMALL = "task-small"
    FEATURE_LARGE = "feature-large"
    REFACTOR_LARGE = "refactor-large"


class BranchMode(str, Enum):
    NEW_BRANCH = "new-branch"
    EXISTING_BRANCH = "existing-branch"
    DETACHED_REVIEW = "detached-review"


class DeliveryMode(str, Enum):
    DIRECT_PUSH = "direct-push"
    PULL_REQUEST = "pull-request"
    REPORT_ONLY = "report-only"


REQUEST_TYPES = {item.value for item in RequestType}
BRANCH_MODES = {item.value for item in BranchMode}
DELIVERY_MODES = {item.value for item in DeliveryMode}

_REQUEST_TYPE_ALIASES = {
    "bug": RequestType.BUG_SMALL.value,
    "bugfix": RequestType.BUG_SMALL.value,
    "bug-small": RequestType.BUG_SMALL.value,
    "task": RequestType.TASK_SMALL.value,
    "task-small": RequestType.TASK_SMALL.value,
    "feature": RequestType.FEATURE_LARGE.value,
    "feature-large": RequestType.FEATURE_LARGE.value,
    "refactor": RequestType.REFACTOR_LARGE.value,
    "refactor-large": RequestType.REFACTOR_LARGE.value,
}

_BRANCH_MODE_ALIASES = {
    "new": BranchMode.NEW_BRANCH.value,
    "new-branch": BranchMode.NEW_BRANCH.value,
    "existing": BranchMode.EXISTING_BRANCH.value,
    "existing-branch": BranchMode.EXISTING_BRANCH.value,
    "detached": BranchMode.DETACHED_REVIEW.value,
    "detached-review": BranchMode.DETACHED_REVIEW.value,
    "review": BranchMode.DETACHED_REVIEW.value,
}

_DELIVERY_MODE_ALIASES = {
    "direct": DeliveryMode.DIRECT_PUSH.value,
    "direct-push": DeliveryMode.DIRECT_PUSH.value,
    "push": DeliveryMode.DIRECT_PUSH.value,
    "pr": DeliveryMode.PULL_REQUEST.value,
    "pr-required": DeliveryMode.PULL_REQUEST.value,
    "pull-request": DeliveryMode.PULL_REQUEST.value,
    "pullrequest": DeliveryMode.PULL_REQUEST.value,
    "report": DeliveryMode.REPORT_ONLY.value,
    "report-only": DeliveryMode.REPORT_ONLY.value,
    "report-only-allowed": DeliveryMode.REPORT_ONLY.value,
    "readonly": DeliveryMode.REPORT_ONLY.value,
    "read-only": DeliveryMode.REPORT_ONLY.value,
}


def normalize_job_spec(raw: dict) -> dict:
    """Return the canonical JSON-serializable job spec contract."""
    if not isinstance(raw, dict):
        raise ValueError("Job spec must be a dictionary")

    payload = deepcopy(raw)
    repo = _normalize_repo(_mapping_value(payload, "repo"))
    request = _normalize_request(_mapping_value(payload, "request"), payload)
    raw_branch = _mapping_value(payload, "branch")
    branch = _normalize_branch(raw_branch, repo)

    delivery_mode = _normalize_delivery_mode(
        payload.get("delivery_mode") or payload.get("delivery") or raw_branch.get("delivery_mode")
    )
    if delivery_mode is None:
        delivery_mode = _default_delivery_mode(branch.get("mode"))

    spec = {
        "schema_version": _normalize_schema_version(payload.get("schema_version")),
        "id": _normalize_job_id(payload.get("id"), request),
        "source": _normalize_optional_mapping(payload.get("source")),
        "project": _optional_string(payload.get("project")),
        "repo": repo,
        "request": request,
        "branch": branch,
        "delivery_mode": delivery_mode,
        "dependencies": _normalize_dependencies(payload.get("dependencies")),
        "pipeline": _normalize_pipeline(_mapping_value(payload, "pipeline")),
        "scope": _normalize_scope(_mapping_value(payload, "scope")),
    }
    _validate_normalized_job_spec(spec)
    return spec


def validate_job_spec(raw: dict) -> dict:
    """Normalize and validate a raw job spec, raising ValueError on contract errors."""
    return normalize_job_spec(raw)


def _mapping_value(payload: Mapping[str, Any], key: str) -> dict:
    value = payload.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"Job spec {key} must be an object")
    return dict(value)


def _normalize_schema_version(value: Any) -> int:
    if value is None:
        return 1
    try:
        version = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("schema_version must be an integer") from exc
    if version < 1:
        raise ValueError("schema_version must be >= 1")
    return version


def _normalize_job_id(value: Any, request: Mapping[str, Any]) -> str:
    explicit = _optional_string(value)
    if explicit:
        job_id = explicit
    else:
        title = _optional_string(request.get("title")) or "job"
        job_id = re.sub(r"[^a-z0-9_.-]+", "-", title.lower()).strip("-._") or "job"
    if "/" in job_id or "\\" in job_id or job_id in {".", ".."}:
        raise ValueError("id must not contain path separators")
    return job_id


def _normalize_optional_mapping(value: Any) -> dict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("Job spec source must be an object")
    return {
        str(key): item
        for key, item in value.items()
        if item not in (None, "")
    }


def _normalize_repo(repo: dict) -> dict:
    path = _optional_string(repo.get("path"))
    key = _optional_string(repo.get("key"))
    if key is None and path:
        key = f"repo://{path}"
    return {
        "key": key,
        "path": path,
    }


def _normalize_request(request: dict, payload: Mapping[str, Any]) -> dict:
    request_type = _normalize_request_type(
        request.get("type")
        or request.get("request_type")
        or payload.get("request_type")
        or payload.get("type")
        or RequestType.TASK_SMALL.value
    )
    title = _optional_string(request.get("title") or payload.get("title"))
    body = _first_string(
        request.get("body"),
        request.get("description"),
        request.get("prompt"),
        request.get("goal"),
        payload.get("body"),
        payload.get("description"),
        payload.get("prompt"),
        payload.get("goal"),
    )
    return {
        "type": request_type,
        "title": title,
        "body": body,
    }


def _normalize_branch(branch: dict, repo: Mapping[str, Any]) -> dict:
    work_branch = _first_string(
        branch.get("work_branch"),
        branch.get("working_branch"),
        branch.get("branch"),
        repo.get("working_branch"),
    )
    base_branch = _first_string(branch.get("base_branch"), branch.get("base"), repo.get("base_branch"))
    pr_target = _first_string(branch.get("pr_target"), branch.get("target_branch"), branch.get("merge_target"))

    mode = _normalize_branch_mode(branch.get("mode") or branch.get("branch_mode"))
    if mode is None:
        mode = _infer_branch_mode(work_branch=work_branch, base_branch=base_branch)

    return {
        "mode": mode,
        "base_branch": base_branch,
        "work_branch": work_branch,
        "pr_target": pr_target,
    }


def _normalize_pipeline(pipeline: dict) -> dict:
    budgets = _mapping_value(pipeline, "budgets")
    return {
        "budgets": {
            "review_loops": _normalize_non_negative_int(
                _first_present(
                    budgets,
                    pipeline,
                    keys=("review_loops", "max_review_loops"),
                    default=3,
                ),
                "pipeline.budgets.review_loops",
            ),
        },
        "security_review": _normalize_bool(pipeline.get("security_review"), default=True),
    }


def _normalize_scope(scope: dict) -> dict:
    return {
        "allowed_paths": _normalize_string_list(scope.get("allowed_paths")),
        "denied_paths": _normalize_string_list(scope.get("denied_paths")),
        "allow_unlisted_paths": _normalize_bool(scope.get("allow_unlisted_paths"), default=True),
    }


def _normalize_dependencies(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        candidates = list(value)
    else:
        raise ValueError("dependencies must be a list or comma-separated string")

    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        dependency = _optional_string(candidate)
        if not dependency or dependency in seen:
            continue
        normalized.append(dependency)
        seen.add(dependency)
    return normalized


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, (list, tuple, set)):
        candidates = list(value)
    else:
        raise ValueError("scope path lists must be strings or lists")
    results: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        text = _optional_string(candidate)
        if text and text not in seen:
            results.append(text)
            seen.add(text)
    return results


def _normalize_request_type(value: Any) -> str:
    text = _alias_key(value)
    if text in _REQUEST_TYPE_ALIASES:
        return _REQUEST_TYPE_ALIASES[text]
    raise ValueError(
        f"Invalid request.type {value!r}; expected one of: {', '.join(sorted(REQUEST_TYPES))}"
    )


def _normalize_branch_mode(value: Any) -> str | None:
    if value is None or _optional_string(value) is None:
        return None
    text = _alias_key(value)
    if text in _BRANCH_MODE_ALIASES:
        return _BRANCH_MODE_ALIASES[text]
    raise ValueError(f"Invalid branch.mode {value!r}; expected one of: {', '.join(sorted(BRANCH_MODES))}")


def _normalize_delivery_mode(value: Any) -> str | None:
    if value is None or _optional_string(value) is None:
        return None
    text = _alias_key(value)
    if text in _DELIVERY_MODE_ALIASES:
        return _DELIVERY_MODE_ALIASES[text]
    raise ValueError(
        f"Invalid delivery_mode {value!r}; expected one of: {', '.join(sorted(DELIVERY_MODES))}"
    )


def _infer_branch_mode(*, work_branch: str | None, base_branch: str | None) -> str | None:
    if work_branch and base_branch:
        return BranchMode.NEW_BRANCH.value
    if work_branch:
        return BranchMode.EXISTING_BRANCH.value
    return None


def _default_delivery_mode(branch_mode: str | None) -> str | None:
    if branch_mode == BranchMode.NEW_BRANCH.value:
        return DeliveryMode.PULL_REQUEST.value
    if branch_mode == BranchMode.EXISTING_BRANCH.value:
        return DeliveryMode.DIRECT_PUSH.value
    if branch_mode == BranchMode.DETACHED_REVIEW.value:
        return DeliveryMode.REPORT_ONLY.value
    return None


def _validate_normalized_job_spec(spec: Mapping[str, Any]) -> None:
    repo = spec["repo"]
    request = spec["request"]
    branch = spec["branch"]
    delivery_mode = spec["delivery_mode"]
    branch_mode = branch.get("mode")

    if not repo.get("path"):
        raise ValueError("repo.path is required")
    if not spec.get("id"):
        raise ValueError("id is required")
    if not request.get("title"):
        raise ValueError("request.title is required")
    if not request.get("body"):
        raise ValueError("request.body is required; provide body, description, prompt, or goal")
    if branch_mode not in BRANCH_MODES:
        raise ValueError(f"branch.mode is required; expected one of: {', '.join(sorted(BRANCH_MODES))}")
    if delivery_mode not in DELIVERY_MODES:
        raise ValueError(f"delivery_mode is required; expected one of: {', '.join(sorted(DELIVERY_MODES))}")

    if branch_mode == BranchMode.EXISTING_BRANCH.value and not branch.get("work_branch"):
        raise ValueError("branch.work_branch is required when branch.mode is existing-branch")
    if branch_mode == BranchMode.NEW_BRANCH.value:
        if not branch.get("base_branch"):
            raise ValueError("branch.base_branch is required when branch.mode is new-branch")
        if not branch.get("work_branch"):
            raise ValueError("branch.work_branch is required when branch.mode is new-branch")
    if delivery_mode == DeliveryMode.PULL_REQUEST.value and not branch.get("pr_target"):
        raise ValueError("branch.pr_target is required when delivery_mode is pull-request")
    if (
        delivery_mode == DeliveryMode.DIRECT_PUSH.value
        and branch_mode != BranchMode.DETACHED_REVIEW.value
        and not branch.get("work_branch")
    ):
        raise ValueError("branch.work_branch is required when delivery_mode is direct-push")


def _normalize_non_negative_int(value: Any, field_name: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if normalized < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return normalized


def _normalize_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    raise ValueError("pipeline.security_review must be a boolean")


def _first_string(*values: Any) -> str | None:
    for value in values:
        text = _optional_string(value)
        if text:
            return text
    return None


def _first_present(*mappings: Mapping[str, Any], keys: tuple[str, ...], default: Any) -> Any:
    for mapping in mappings:
        for key in keys:
            if key in mapping:
                return mapping[key]
    return default


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _alias_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    return " ".join(text.replace("_", " ").replace("-", " ").split()).replace(" ", "-")
