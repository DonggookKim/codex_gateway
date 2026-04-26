#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
export CODEX_SHARED_AUTH_SOURCE="${CODEX_SHARED_AUTH_SOURCE:-${HOME}/.codex/auth.json}"

if [[ $# -lt 2 ]]; then
  echo "usage: bash codex_gateway/attach-gateway-session.sh <project_id> <session_id>" >&2
  exit 1
fi

PROJECT_ID="$1"
SESSION_ID="$2"

cd "${REPO_ROOT}"
python3 -m codex_gateway.tui_attach \
  "${PROJECT_ID}" \
  "${SESSION_ID}" \
  --repo-root "${REPO_ROOT}" \
  --state-root "${REPO_ROOT}/codex_gateway/state" \
  --projects-file "${REPO_ROOT}/codex_gateway/state/projects.json"
