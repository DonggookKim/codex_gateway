#!/bin/bash

set -euo pipefail

# Resolve symlinks (see run_gateway.sh).
SCRIPT_PATH="${BASH_SOURCE[0]}"
while [ -L "$SCRIPT_PATH" ]; do
  SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
  SCRIPT_PATH="$(readlink "$SCRIPT_PATH")"
  [[ $SCRIPT_PATH != /* ]] && SCRIPT_PATH="${SCRIPT_DIR}/${SCRIPT_PATH}"
done
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
PACKAGE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${PACKAGE_DIR}/.." && pwd)"
export CODEX_SHARED_AUTH_SOURCE="${CODEX_SHARED_AUTH_SOURCE:-${HOME}/.codex/auth.json}"

if [[ $# -lt 2 ]]; then
  echo "usage: bash codex_gateway/scripts/attach-gateway-session.sh <project_id> <session_id>" >&2
  exit 1
fi

PROJECT_ID="$1"
SESSION_ID="$2"

cd "${REPO_ROOT}"
python3 -m codex_gateway.tui_attach \
  "${PROJECT_ID}" \
  "${SESSION_ID}" \
  --repo-root "${REPO_ROOT}" \
  --state-root "${PACKAGE_DIR}/state" \
  --projects-file "${PACKAGE_DIR}/state/projects.json"
