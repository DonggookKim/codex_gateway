#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEFAULT_GATEWAY_SEED_HOME="${SCRIPT_DIR}/gateway-home/.codex"
DEFAULT_RUNTIME_ROOT="${RUNTIME_ROOT:-${HOME}/codex_gateway_runtime}"

if [[ "${CODEX_GATEWAY_SKIP_ENV:-0}" != "1" && -f "${SCRIPT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/.env"
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
