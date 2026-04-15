from __future__ import annotations

import json
from pathlib import Path

from hermes_tasklane.cli import command_init, command_sync, load_config, load_state


def write_config(path: Path, *, hermes_home: Path, task_root: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "hermes_home": str(hermes_home),
                "task_root": str(task_root),
                "poll_repo_idle": True,
                "max_pending_per_repo": 1,
            }
        )
    )


def test_init_writes_example_outside_inbox(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))

    command_init(cfg, str(config_path))

    assert (task_root / "examples" / "example-task.md").exists()
    assert not (task_root / "inbox" / "example-task.md").exists()


def test_sync_moves_inbox_task_to_submitted_and_writes_queue_payload(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    task_root = tmp_path / "tasklane"
    config_path = tmp_path / "config.json"
    write_config(config_path, hermes_home=hermes_home, task_root=task_root)
    cfg = load_config(str(config_path))
    command_init(cfg, str(config_path))

    repo = tmp_path / "repo"
    repo.mkdir()
    task_file = task_root / "inbox" / "demo.md"
    task_file.write_text(
        f"---\nrepo_path: {repo}\nbranch_base: main\nproject: Demo\n---\nImplement the demo task.\n",
        encoding="utf-8",
    )

    command_sync(cfg)

    submitted_task = task_root / "submitted" / "demo.md"
    assert submitted_task.exists()
    incoming = list((hermes_home / "autocode_queue" / "incoming").glob("*.json"))
    assert len(incoming) == 1
    payload = json.loads(incoming[0].read_text(encoding="utf-8"))
    assert payload["task"] == "Implement the demo task."
    assert payload["metadata"]["source"] == "tasklane-file-bridge"
    state = load_state(cfg)
    assert len(state["submitted"]) == 1
