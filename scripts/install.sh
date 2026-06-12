#!/usr/bin/env bash
# Minimal TaskLane installer. Adjust PREFIX / user to your environment.
set -euo pipefail

PREFIX="${PREFIX:-/opt/tasklane}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "Installing TaskLane from $REPO_DIR into $PREFIX"
mkdir -p "$PREFIX"
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install "$REPO_DIR"   # installs the tasklane package + console scripts

echo
echo "Done. Next steps:"
echo "  1. Edit scripts/tasklane-*.service for your paths/user, then:"
echo "       sudo cp scripts/tasklane-worker.service scripts/tasklane-mcp.service /etc/systemd/system/"
echo "       sudo systemctl daemon-reload && sudo systemctl enable --now tasklane-worker tasklane-mcp"
echo "  2. (optional) sudo cp scripts/tasklane.sudoers /etc/sudoers.d/tasklane && sudo chmod 440 /etc/sudoers.d/tasklane"
echo "  3. The app_token is auto-generated in \$TASKLANE_HOME/config.yaml on first run."
