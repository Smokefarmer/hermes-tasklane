"""Minimal TaskLane CLI for local testing / ops.

The MCP server is the primary control plane; this CLI mirrors a subset so the
worker can be exercised without a remote client.
"""

from __future__ import annotations

import argparse
import json
import sys

from tasklane.config import load_config, repo_path_allowed
from tasklane.paths import logs_root
from tasklane.store import JobStore


def _build_spec(args: argparse.Namespace) -> dict:
    spec: dict = {
        "repo": {"path": args.repo},
        "request": {"type": args.type, "title": args.title, "body": args.body},
        "delivery_mode": args.delivery,
    }
    if args.id:
        spec["id"] = args.id
    branch: dict = {"mode": args.branch_mode}
    if args.base:
        branch["base_branch"] = args.base
    if args.work:
        branch["work_branch"] = args.work
    if args.pr_target:
        branch["pr_target"] = args.pr_target
    spec["branch"] = branch
    return spec


def cmd_submit(args: argparse.Namespace) -> int:
    cfg = load_config()
    if not repo_path_allowed(args.repo, cfg):
        print(f"error: repo path not in allowlist: {args.repo}", file=sys.stderr)
        return 2
    store = JobStore()
    record = store.put(_build_spec(args))
    print(json.dumps({"id": record["id"], "state": record["state"]}, indent=2))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store = JobStore()
    states = [args.state] if args.state else None
    rows = [{"id": r["id"], "state": r["state"], "attempt": r.get("attempt"), "title": (r.get("spec") or {}).get("request", {}).get("title")}
            for r in store.list(states=states)]
    print(json.dumps(rows, indent=2))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = JobStore()
    record = store.get(args.id)
    if record is None:
        print(f"not found: {args.id}", file=sys.stderr)
        return 1
    print(json.dumps(record, indent=2))
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    store = JobStore()
    print(json.dumps(store.events(args.id, limit=args.limit), indent=2))
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    path = logs_root() / f"{args.id}.log"
    if not path.exists():
        print(f"no log for {args.id}", file=sys.stderr)
        return 1
    print(path.read_text(encoding="utf-8"))
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    JobStore().retry(args.id)
    print(f"retried {args.id}")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    JobStore().cancel(args.id)
    print(f"cancelled {args.id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tasklane", description="TaskLane job control")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("submit", help="create a job")
    s.add_argument("--repo", required=True)
    s.add_argument("--title", required=True)
    s.add_argument("--body", required=True)
    s.add_argument("--type", default="task-small")
    s.add_argument("--branch-mode", default="new-branch", dest="branch_mode")
    s.add_argument("--base", default=None)
    s.add_argument("--work", default=None)
    s.add_argument("--pr-target", default=None, dest="pr_target")
    s.add_argument("--delivery", default="report-only")
    s.add_argument("--id", default=None)
    s.set_defaults(func=cmd_submit)

    s = sub.add_parser("list", help="list jobs"); s.add_argument("--state", default=None); s.set_defaults(func=cmd_list)
    s = sub.add_parser("show", help="show a job"); s.add_argument("id"); s.set_defaults(func=cmd_show)
    s = sub.add_parser("events", help="job events"); s.add_argument("id"); s.add_argument("--limit", type=int, default=None); s.set_defaults(func=cmd_events)
    s = sub.add_parser("logs", help="job log"); s.add_argument("id"); s.set_defaults(func=cmd_logs)
    s = sub.add_parser("retry", help="retry a job"); s.add_argument("id"); s.set_defaults(func=cmd_retry)
    s = sub.add_parser("cancel", help="cancel a job"); s.add_argument("id"); s.set_defaults(func=cmd_cancel)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
