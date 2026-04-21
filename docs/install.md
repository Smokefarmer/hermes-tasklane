# Installation Guide

## Requirements

- Python 3.10+
- Hermes already installed
- Hermes v10 JobStore watcher enabled in the gateway
- Git configured for the repos you want to automate

## Minimal install

```bash
git clone https://github.com/Smokefarmer/hermes-tasklane.git
cd hermes-tasklane
./scripts/install.sh --systemd
```

The installer also copies bundled Hermes skills to:

```text
~/.hermes/skills/software-development/
```

Pass `--no-skills` to skip that step.

## Manual install

```bash
git clone https://github.com/Smokefarmer/hermes-tasklane.git
cd hermes-tasklane
python3 -m pip install .
hermes-tasklane init
hermes-tasklane doctor
```

## Config file

Default path:

```text
~/.config/hermes-tasklane/config.json
```

Example:

```json
{
  "hermes_home": "/home/your-user/.hermes",
  "task_root": "/home/your-user/.local/share/hermes-tasklane",
  "poll_repo_idle": true,
  "max_pending_per_repo": 1,
  "github_owner_hint": "your-github-user",
  "default_platform": null,
  "default_chat_id": null,
  "default_thread_id": null,
  "watch": {
    "mode": "observe",
    "stale_running_minutes": 180,
    "max_retry_attempts": 3,
    "expected_base_branches": {
      "Project Name": "develop"
    },
    "ignored_blocked_jobs": []
  }
}
```

Set `default_platform` to `telegram` and `default_chat_id` to a group/chat ID only when all tasklane jobs from this machine should report to that chat. Otherwise put `platform`, `chat_id`, and `thread_id` in individual task files.

## Systemd user units

The repo ships ready-made user units in `systemd/` and the installer can place them automatically. During install, the script resolves the absolute `hermes-tasklane` executable path and writes it into the unit files so the timers still work even when `~/.local/bin` is not in the systemd user PATH.

```bash
./scripts/install.sh --systemd
```

Manual setup is also possible by copying the files from `systemd/` into:

```text
~/.config/systemd/user/
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now hermes-tasklane-sync.timer
systemctl --user enable --now hermes-tasklane-reconcile.timer
systemctl --user enable --now hermes-tasklane-watch.timer
```

The watch timer runs `hermes-tasklane watch --mode observe --quiet-ok` every 15 minutes. It reports failed, blocked, stale, dead-gateway, and branch-policy problems in the systemd journal without mutating the queue.

The installer also copies a disabled dashboard service:

```bash
systemctl --user enable --now hermes-tasklane-dashboard.service
```

The packaged service binds to `127.0.0.1:8765`. For a trusted internal network, edit the service and change the dashboard command to:

```text
hermes-tasklane --config /path/to/config.json dashboard --host 0.0.0.0 --port 8765
```

Do not expose the dashboard directly to the public internet without an authenticating reverse proxy.

## Cron example

```cron
*/5 * * * * /usr/bin/env hermes-tasklane sync
*/5 * * * * /usr/bin/env hermes-tasklane reconcile
*/15 * * * * /usr/bin/env hermes-tasklane watch --mode observe --quiet-ok
```

## Watchdog command

Manual health check:

```bash
hermes-tasklane watch
```

Project/repo branch policy can be configured in `watch.expected_base_branches` or passed per run:

```bash
hermes-tasklane watch --expected-base Alvin=develop --expected-base "Treasure Hunter=development"
```

Known obsolete blocked jobs can be ignored:

```bash
hermes-tasklane watch --ignore-blocked tasklane_d08a145fbccc
```

Guarded mode retries only narrowly classified transient failures, including provider HTTP 500/502/503/504, APIError, timeouts, and rate-limit transport failures. It never retries blocked jobs, schema/planning failures, dirty-worktree failures, or no-code-change failures:

```bash
hermes-tasklane watch --mode guarded
```

## Dashboard command

Start a read-only local dashboard:

```bash
hermes-tasklane dashboard --host 127.0.0.1 --port 8765
```

Start it for a trusted internal network:

```bash
hermes-tasklane dashboard --host 0.0.0.0 --port 8765
```

Open:

```text
http://<server-lan-ip>:8765
```

## GitHub auth

For delivery reconciliation to work cleanly on private repos, set one of:
- `GITHUB_TOKEN`
- `GH_TOKEN`
- or a valid GitHub credential in `~/.git-credentials`
