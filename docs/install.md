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
  "default_thread_id": null
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
```

## Cron example

```cron
*/5 * * * * /usr/bin/env hermes-tasklane sync
*/5 * * * * /usr/bin/env hermes-tasklane reconcile
```

## Systemd user units

If you prefer systemd user timers, create one timer for `sync` and one for `reconcile`.

## GitHub auth

For delivery reconciliation to work cleanly on private repos, set one of:
- `GITHUB_TOKEN`
- `GH_TOKEN`
- or a valid GitHub credential in `~/.git-credentials`
