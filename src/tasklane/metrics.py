"""Job cost/usage telemetry: per-job accumulation and store-wide aggregation.

The Claude CLI's JSON output reports ``total_cost_usd``, token usage, turns,
and duration per invocation. The worker sums these across a job's main +
repair runs (and across retry attempts) into ``record["metrics"]``; this
module aggregates them for the status page, the ``metrics`` MCP tool, the
``tasklane stats`` CLI, and the daily budget gate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from tasklane.store import JobStore, parse_timestamp, utc_now

_SUMMED_FIELDS = (
    "cost_usd",
    "duration_ms",
    "num_turns",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "runs",
    "wall_seconds",
)


def _num(value: Any) -> float | int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


def merge_metrics(*parts: Mapping[str, Any] | None) -> dict[str, Any]:
    """Sum numeric telemetry across runs/attempts; stamps ``recorded_at``."""
    merged: dict[str, Any] = {field: 0 for field in _SUMMED_FIELDS}
    for part in parts:
        if not part:
            continue
        for field in _SUMMED_FIELDS:
            merged[field] = merged[field] + _num(part.get(field))
        if part.get("session_id"):
            merged["session_id"] = part["session_id"]  # latest run wins
    merged["cost_usd"] = round(float(merged["cost_usd"]), 6)
    merged["recorded_at"] = utc_now()
    return merged


def run_metrics(result: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize one runner result's metrics into a single-run telemetry dict."""
    metrics = dict((result or {}).get("metrics") or {})
    metrics["runs"] = 1
    return metrics


def spend_since(store: JobStore, since: datetime) -> float:
    """Total cost (USD) recorded on job records at or after ``since``."""
    total = 0.0
    for record in store.list():
        metrics = record.get("metrics")
        if not isinstance(metrics, dict):
            continue
        recorded_at = parse_timestamp(metrics.get("recorded_at"))
        if recorded_at is None or recorded_at < since:
            continue
        total += float(_num(metrics.get("cost_usd")))
    return round(total, 6)


def spend_last_24h(store: JobStore, *, now: datetime | None = None) -> float:
    base = now or datetime.now(timezone.utc)
    return spend_since(store, base - timedelta(hours=24))


def aggregate(store: JobStore, *, now: datetime | None = None) -> dict[str, Any]:
    """Store-wide telemetry report for the status page / metrics tool / CLI."""
    base = now or datetime.now(timezone.utc)
    counts: dict[str, int] = {}
    totals: dict[str, Any] = {field: 0 for field in _SUMMED_FIELDS}
    wall_seconds: list[float] = []
    jobs_with_metrics = 0
    for record in store.list():
        state = str(record.get("state") or "?")
        counts[state] = counts.get(state, 0) + 1
        metrics = record.get("metrics")
        if isinstance(metrics, dict) and metrics:
            jobs_with_metrics += 1
            for field in _SUMMED_FIELDS:
                totals[field] = totals[field] + _num(metrics.get(field))
            if _num(metrics.get("wall_seconds")):
                wall_seconds.append(float(_num(metrics.get("wall_seconds"))))
    totals["cost_usd"] = round(float(totals["cost_usd"]), 6)
    return {
        "at": utc_now(),
        "counts": counts,
        "jobs_with_metrics": jobs_with_metrics,
        "totals": totals,
        "spend_24h_usd": spend_last_24h(store, now=base),
        "avg_wall_seconds": round(sum(wall_seconds) / len(wall_seconds), 1) if wall_seconds else None,
    }
