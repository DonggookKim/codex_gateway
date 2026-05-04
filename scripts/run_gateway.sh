#!/bin/bash

set -euo pipefail

# Resolve symlinks so the script works whether invoked via its real path
# (bash codex_gateway/scripts/run_gateway.sh) or via a bin shim such as
# ~/.local/bin/codex-gateway → scripts/run_gateway.sh.
SCRIPT_PATH="${BASH_SOURCE[0]}"
while [ -L "$SCRIPT_PATH" ]; do
  SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
  SCRIPT_PATH="$(readlink "$SCRIPT_PATH")"
  [[ $SCRIPT_PATH != /* ]] && SCRIPT_PATH="${SCRIPT_DIR}/${SCRIPT_PATH}"
done
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
PACKAGE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${PACKAGE_DIR}/.." && pwd)"
DEFAULT_GATEWAY_SEED_HOME="${PACKAGE_DIR}/gateway-home/.codex"
DEFAULT_RUNTIME_ROOT="${RUNTIME_ROOT:-${HOME}/codex_gateway_runtime}"

if [[ "${CODEX_GATEWAY_SKIP_ENV:-0}" != "1" && -f "${PACKAGE_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${PACKAGE_DIR}/.env"
  set +a
fi

export CODEX_SHARED_AUTH_SOURCE="${CODEX_SHARED_AUTH_SOURCE:-${HOME}/.codex/auth.json}"

if [[ -z "${CODEX_HOME_PARENT:-}" && -d "${DEFAULT_GATEWAY_SEED_HOME}" ]]; then
  export CODEX_HOME_PARENT="${DEFAULT_RUNTIME_ROOT}/codex-home"
  export CODEX_HOME_SEED_FROM="${DEFAULT_GATEWAY_SEED_HOME}"
  export CODEX_STATUS_HOME="${CODEX_HOME_PARENT}/.codex"
fi

cd "${REPO_ROOT}"
python3 -m codex_gateway
