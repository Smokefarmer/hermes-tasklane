# Installation Guide

## Requirements

- Python 3.10+
- Hermes already installed
- Hermes queue watcher enabled
- Git configured for the repos you want to automate

## Minimal install

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
  "github_owner_hint": "your-github-user"
}
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
