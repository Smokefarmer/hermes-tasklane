#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="${HOME}/.config/hermes-tasklane/config.json"
HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
INSTALL_SYSTEMD="false"
PIP_EDITABLE="false"
CLI_PATH=""
INSTALL_SKILLS="true"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --editable)
      PIP_EDITABLE="true"
      shift
      ;;
    --systemd)
      INSTALL_SYSTEMD="true"
      shift
      ;;
    --no-skills)
      INSTALL_SKILLS="false"
      shift
      ;;
    --config)
      CONFIG_PATH="$2"
      shift 2
      ;;
    --help|-h)
      cat <<'EOF'
Usage: ./scripts/install.sh [--editable] [--systemd] [--no-skills] [--config /path/to/config.json]

Installs hermes-tasklane, initializes local folders, and optionally installs
user-level systemd units for sync/reconcile/watch timers. Bundled Hermes
skills are installed by default unless --no-skills is passed.
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

cd "$REPO_DIR"

if [[ "$PIP_EDITABLE" == "true" ]]; then
  python3 -m pip install -e .
else
  python3 -m pip install .
fi

CLI_PATH="$(command -v hermes-tasklane || true)"
if [[ -z "$CLI_PATH" ]]; then
  echo "hermes-tasklane was installed but is not on PATH for this shell." >&2
  echo "Try: python3 -m pip install --user .  or add your pip bin directory to PATH." >&2
  exit 1
fi

"$CLI_PATH" --config "$CONFIG_PATH" init
"$CLI_PATH" --config "$CONFIG_PATH" doctor

if [[ "$INSTALL_SKILLS" == "true" && -d "$REPO_DIR/skills" ]]; then
  SKILL_TARGET_DIR="$HERMES_HOME/skills/software-development"
  mkdir -p "$SKILL_TARGET_DIR"
  for skill_dir in "$REPO_DIR"/skills/*; do
    [[ -d "$skill_dir" ]] || continue
    [[ -f "$skill_dir/SKILL.md" ]] || continue
    rm -rf "$SKILL_TARGET_DIR/$(basename "$skill_dir")"
    cp -R "$skill_dir" "$SKILL_TARGET_DIR/"
  done
fi

if [[ "$INSTALL_SYSTEMD" == "true" ]]; then
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not found; skipping systemd timer installation." >&2
  elif ! systemctl --user show-environment >/dev/null 2>&1; then
    echo "systemd user session is not available; skipping systemd timer installation." >&2
    echo "Use cron instead, or run the installer again from a login session with systemd user services available." >&2
  else
    SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
    mkdir -p "$SYSTEMD_USER_DIR"

    sed -e "s|__CONFIG_PATH__|$CONFIG_PATH|g" -e "s|__EXECUTABLE__|$CLI_PATH|g" "$REPO_DIR/systemd/hermes-tasklane-sync.service" > "$SYSTEMD_USER_DIR/hermes-tasklane-sync.service"
    cp "$REPO_DIR/systemd/hermes-tasklane-sync.timer" "$SYSTEMD_USER_DIR/hermes-tasklane-sync.timer"
    sed -e "s|__CONFIG_PATH__|$CONFIG_PATH|g" -e "s|__EXECUTABLE__|$CLI_PATH|g" "$REPO_DIR/systemd/hermes-tasklane-reconcile.service" > "$SYSTEMD_USER_DIR/hermes-tasklane-reconcile.service"
    cp "$REPO_DIR/systemd/hermes-tasklane-reconcile.timer" "$SYSTEMD_USER_DIR/hermes-tasklane-reconcile.timer"
    sed -e "s|__CONFIG_PATH__|$CONFIG_PATH|g" -e "s|__EXECUTABLE__|$CLI_PATH|g" "$REPO_DIR/systemd/hermes-tasklane-watch.service" > "$SYSTEMD_USER_DIR/hermes-tasklane-watch.service"
    cp "$REPO_DIR/systemd/hermes-tasklane-watch.timer" "$SYSTEMD_USER_DIR/hermes-tasklane-watch.timer"

    systemctl --user daemon-reload
    systemctl --user enable --now hermes-tasklane-sync.timer
    systemctl --user enable --now hermes-tasklane-reconcile.timer
    systemctl --user enable --now hermes-tasklane-watch.timer
  fi
fi

echo
cat <<EOF
hermes-tasklane installation complete.

Next steps:
- Review config: $CONFIG_PATH
- Bundled skills installed to: $HERMES_HOME/skills/software-development
- Put task files into your inbox directory
- Run: hermes-tasklane --config "$CONFIG_PATH" status
EOF
