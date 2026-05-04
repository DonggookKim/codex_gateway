#!/usr/bin/env bash
#
# codex_gateway install / environment check.
#
# Usage:
#   bash codex_gateway/install.sh             # default: check only, print missing
#   bash codex_gateway/install.sh --check     # explicit check mode
#   bash codex_gateway/install.sh --install   # attempt to install missing pieces
#   bash codex_gateway/install.sh --help
#
# The script is idempotent. It never modifies an existing `.env`. It will
# install Python deps into the active python3, and offer brew/curl/npm
# install paths for codex / opencode CLIs.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
ENV_EXAMPLE="${SCRIPT_DIR}/.env.example"
REQUIREMENTS="${SCRIPT_DIR}/requirements.txt"

MODE="check"
case "${1:-}" in
  --install|-i) MODE="install" ;;
  --check|-c|"") MODE="check" ;;
  --help|-h)
    cat <<EOF
Usage: bash codex_gateway/install.sh [--check | --install]

  --check    (default) Verify dependencies are present; print missing.
  --install  Attempt to install missing dependencies. Python deps are
             installed via pip into the active python3; codex / opencode
             CLIs are installed via brew (mac), curl (linux), or npm.

Environment:
  PYTHON     python interpreter to use (default: python3)
EOF
    exit 0
    ;;
  *)
    echo "Unknown option: $1" >&2
    echo "Run with --help for usage." >&2
    exit 2
    ;;
esac

PYTHON="${PYTHON:-python3}"
OS_KIND="$(uname -s)"
case "$OS_KIND" in
  Darwin) OS=mac ;;
  Linux)  OS=linux ;;
  *)      OS=other ;;
esac

# ---- output helpers ---------------------------------------------------
if [[ -t 1 ]]; then
  C_GREEN="$(printf '\033[32m')"
  C_RED="$(printf '\033[31m')"
  C_YELLOW="$(printf '\033[33m')"
  C_DIM="$(printf '\033[2m')"
  C_RESET="$(printf '\033[0m')"
else
  C_GREEN=""; C_RED=""; C_YELLOW=""; C_DIM=""; C_RESET=""
fi

MISSING=0

ok()    { printf '  %s✓%s  %s\n'  "$C_GREEN"  "$C_RESET" "$*"; }
miss()  { printf '  %s✗%s  %s\n'  "$C_RED"    "$C_RESET" "$*"; MISSING=$((MISSING+1)); }
warn()  { printf '  %s!%s  %s\n'  "$C_YELLOW" "$C_RESET" "$*"; }
info()  { printf '     %s%s%s\n'  "$C_DIM"    "$*"      "$C_RESET"; }

section() { printf '\n%s%s%s\n' "$C_DIM" "$1" "$C_RESET"; }

# ---- checks -----------------------------------------------------------

check_python() {
  section "python"
  if ! command -v "$PYTHON" >/dev/null 2>&1; then
    miss "$PYTHON not found"
    info "install Python 3.10+ (https://www.python.org/downloads/)"
    return
  fi
  local py_ver py_maj py_min
  py_ver=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  py_maj=${py_ver%.*}
  py_min=${py_ver#*.}
  if (( py_maj < 3 )) || { (( py_maj == 3 )) && (( py_min < 10 )); }; then
    miss "$PYTHON $py_ver is too old; need 3.10+"
    return
  fi
  ok "$PYTHON $py_ver ($(command -v "$PYTHON"))"
}

check_python_deps() {
  section "python packages"
  if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    miss "pip not available for $PYTHON"
    info "$PYTHON -m ensurepip --upgrade"
    return
  fi
  ok "pip $($PYTHON -m pip --version | awk '{print $2}')"

  if "$PYTHON" -c 'import discord, aiohttp' >/dev/null 2>&1; then
    local dpy aio
    dpy=$("$PYTHON" -c 'import discord; print(discord.__version__)')
    aio=$("$PYTHON" -c 'import aiohttp; print(aiohttp.__version__)')
    ok "discord.py $dpy, aiohttp $aio"
    return
  fi

  miss "discord.py / aiohttp missing in $PYTHON"
  if [[ "$MODE" == "install" ]]; then
    info "running: $PYTHON -m pip install -r $REQUIREMENTS"
    if ! "$PYTHON" -m pip install -r "$REQUIREMENTS"; then
      miss "pip install failed"
      info "you may need: $PYTHON -m pip install --user -r $REQUIREMENTS"
    fi
  else
    info "$PYTHON -m pip install -r $REQUIREMENTS"
  fi
}

check_codex() {
  section "codex (codex backend)"
  if command -v codex >/dev/null 2>&1; then
    local codex_ver
    codex_ver=$(codex --version 2>&1 | head -1 || true)
    ok "codex ${codex_ver:-found} ($(command -v codex))"
    if [[ ! -f "$HOME/.codex/auth.json" ]]; then
      warn "~/.codex/auth.json not found — run 'codex login' before driving codex sessions"
    fi
    return
  fi
  miss "codex CLI not found"
  if [[ "$MODE" == "install" ]]; then
    if [[ "$OS" == "mac" ]] && command -v brew >/dev/null 2>&1; then
      info "running: brew install codex"
      brew install codex || miss "brew install codex failed"
    elif command -v npm >/dev/null 2>&1; then
      info "running: npm install -g @openai/codex"
      npm install -g @openai/codex || miss "npm install failed (sudo may be required)"
    else
      info "no package manager (brew / npm) found; install codex manually:"
      info "  https://developers.openai.com/codex/"
    fi
  else
    if [[ "$OS" == "mac" ]]; then
      info "brew install codex"
      info "  or: npm install -g @openai/codex"
    else
      info "npm install -g @openai/codex"
      info "  (https://developers.openai.com/codex/)"
    fi
  fi
}

check_opencode() {
  section "opencode (opencode backend, optional)"
  if command -v opencode >/dev/null 2>&1; then
    local oc_ver
    oc_ver=$(opencode --version 2>&1 | head -1 || true)
    ok "opencode ${oc_ver:-found} ($(command -v opencode))"
    if [[ ! -f "$HOME/.config/opencode/opencode.json" ]] \
       && [[ ! -f "$HOME/.config/opencode/opencode.jsonc" ]]; then
      warn "no opencode provider config found at ~/.config/opencode/opencode.json{,c}"
      warn "  configure at least one provider (e.g. model-connect, openai, anthropic) before enabling OPENCODE_GATEWAY_ENABLED=1"
    fi
    return
  fi
  warn "opencode CLI not found (only required if you plan to use backend=opencode)"
  if [[ "$MODE" == "install" ]]; then
    if [[ "$OS" == "mac" ]] && command -v brew >/dev/null 2>&1; then
      info "running: brew install sst/tap/opencode"
      brew install sst/tap/opencode || warn "brew install failed; try: curl -fsSL https://opencode.ai/install | bash"
    elif command -v curl >/dev/null 2>&1; then
      info "running: curl -fsSL https://opencode.ai/install | bash"
      curl -fsSL https://opencode.ai/install | bash || warn "curl install failed"
    else
      info "no installer (brew / curl) found; install manually: https://opencode.ai"
    fi
  else
    if [[ "$OS" == "mac" ]]; then
      info "brew install sst/tap/opencode"
      info "  or: curl -fsSL https://opencode.ai/install | bash"
    else
      info "curl -fsSL https://opencode.ai/install | bash"
    fi
  fi
}

check_env_file() {
  section ".env"
  if [[ -f "$ENV_FILE" ]]; then
    ok "$ENV_FILE present"
    if grep -q "replace_with_\|/path/to/" "$ENV_FILE"; then
      warn ".env still has placeholder values"
      warn "  fill in: DISCORD_GATEWAY_TOKEN, CONTROL_GUILD_ID, CONTROL_CHANNEL_ID, ALLOWED_USER_IDS, CODEX_CWD, STATE_ROOT, RUNTIME_ROOT, PROJECTS_FILE"
    fi
    return
  fi
  miss ".env not present"
  if [[ "$MODE" == "install" ]]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    info "copied .env.example → .env (edit it before running the gateway)"
  else
    info "cp $ENV_EXAMPLE $ENV_FILE  (then edit the placeholder values)"
  fi
}

check_run_script_perms() {
  section "run scripts"
  for s in run_gateway.sh attach-gateway-session.sh install.sh; do
    if [[ -x "${SCRIPT_DIR}/${s}" ]]; then
      ok "${s} executable"
    else
      warn "${s} is not executable (you can still use 'bash ${s}')"
      if [[ "$MODE" == "install" ]]; then
        chmod +x "${SCRIPT_DIR}/${s}" && info "chmod +x ${s}"
      fi
    fi
  done
}

# ---- run --------------------------------------------------------------

printf 'codex_gateway install check (mode=%s, os=%s)\n' "$MODE" "$OS_KIND"

check_python
check_python_deps
check_codex
check_opencode
check_env_file
check_run_script_perms

echo
if (( MISSING == 0 )); then
  printf '%sAll required dependencies are present.%s\n' "$C_GREEN" "$C_RESET"
  echo "Next:"
  echo "  1. Edit ${ENV_FILE} (placeholder values still present? see warnings above)"
  echo "  2. bash ${SCRIPT_DIR}/run_gateway.sh"
  exit 0
fi

printf '%s%d required item(s) missing.%s\n' "$C_RED" "$MISSING" "$C_RESET"
if [[ "$MODE" == "check" ]]; then
  echo "Re-run with --install to attempt automatic installation, or apply the commands above manually."
fi
exit 1
