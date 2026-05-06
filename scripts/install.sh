#!/usr/bin/env bash
#
# codex_gateway install / environment check.
#
# Usage:
#   bash codex_gateway/scripts/install.sh             # default: check only, print missing
#   bash codex_gateway/scripts/install.sh --check     # explicit check mode
#   bash codex_gateway/scripts/install.sh --install   # attempt to install missing pieces
#   bash codex_gateway/scripts/install.sh --help
#
# The script is idempotent. It never modifies an existing `.env`. It will
# install Python deps into the active python3, and offer brew/curl/npm
# install paths for codex / opencode CLIs.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${PACKAGE_DIR}/.env"
ENV_EXAMPLE="${PACKAGE_DIR}/.env.example"
REQUIREMENTS="${PACKAGE_DIR}/requirements.txt"
LAUNCHER="${SCRIPT_DIR}/run_gateway.sh"
BIN_DIR="${BIN_DIR-${HOME}/.local/bin}"
BIN_NAME="${BIN_NAME:-codex-gateway}"

MODE="check"
case "${1:-}" in
  --install|-i) MODE="install" ;;
  --check|-c|"") MODE="check" ;;
  --help|-h)
    cat <<EOF
Usage: bash codex_gateway/scripts/install.sh [--check | --install]

  --check    (default) Verify dependencies are present; print missing.
  --install  Attempt to install missing dependencies. Python deps are
             installed via pip into the active python3; codex / opencode
             CLIs are installed via brew (mac), curl (linux), or npm.
             A bin shim is also linked into BIN_DIR so the gateway can be
             launched as `codex-gateway` from anywhere.

Environment:
  PYTHON     python interpreter to use (default: python3)
  BIN_DIR    where to install the bin shim (default: \$HOME/.local/bin)
             set BIN_DIR= (empty) to skip the bin link
  BIN_NAME   bin command name (default: codex-gateway)
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

check_opencode_ollama_provider() {
  # Read-only check: never modify the user's opencode config. Detect whether
  # the local-Ollama provider is registered, and if not, point at the
  # template they can copy or merge. This matters because opencode does NOT
  # auto-detect a running Ollama; it only knows providers defined in its
  # config (or in the models.dev catalog, which excludes local Ollama).
  local config_dir="$HOME/.config/opencode"
  local config_json="${config_dir}/opencode.json"
  local config_jsonc="${config_dir}/opencode.jsonc"
  local example="${SCRIPT_DIR}/opencode.json.example"

  local config=""
  if [[ -f "$config_json" ]]; then
    config="$config_json"
  elif [[ -f "$config_jsonc" ]]; then
    config="$config_jsonc"
  fi

  if [[ -z "$config" ]]; then
    warn "no opencode config at ${config_json}"
    info "to drive a local Ollama through opencode, create that file."
    info "starter template (covers ollama provider only):"
    info "  ${example}"
    info ""
    info "  mkdir -p '${config_dir}'"
    info "  cp '${example}' '${config_json}'"
    info "  # then edit the model list to match \`ollama list\`"
    return
  fi

  local rc=0
  local probe_out
  probe_out=$("$PYTHON" - "$config" <<'PY'
import json, re, sys
path = sys.argv[1]
src = open(path).read()
try:
    data = json.loads(src)
except json.JSONDecodeError:
    # Probably JSONC: strip line + block comments and retry. The strip is
    # naive (URLs inside string literals can be mangled), so it only runs
    # when strict JSON parsing has already failed.
    stripped = re.sub(r'//[^\n]*', '', src)
    stripped = re.sub(r'/\*.*?\*/', '', stripped, flags=re.DOTALL)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        sys.exit(2)
provider = data.get('provider', {}) if isinstance(data, dict) else {}
ollama = provider.get('ollama') if isinstance(provider, dict) else None
if not isinstance(ollama, dict):
    sys.exit(1)
# provider.ollama exists. Now validate each model has the catalog-style
# fields that opencode uses to decide whether to forward `tools[]` on
# outgoing chat-completion requests. Empirically, declaring only
# `tool_call: true` is NOT enough; the full shape is required.
required_per_model = ('tool_call', 'family', 'modalities', 'limit')
models = ollama.get('models', {}) if isinstance(ollama.get('models'), dict) else {}
if not models:
    print('NO_MODELS')
    sys.exit(3)
under = []
for name, meta in models.items():
    if not isinstance(meta, dict):
        under.append(name + ' (not an object)')
        continue
    missing = [k for k in required_per_model if k not in meta]
    if missing:
        under.append(f"{name} (missing: {', '.join(missing)})")
if under:
    for line in under:
        print(line)
    sys.exit(3)
sys.exit(0)
PY
) || rc=$?

  case "$rc" in
    0)
      ok "opencode config registers 'ollama' provider with catalog-style model metadata"
      ;;
    2)
      warn "could not parse ${config} as JSON/JSONC"
      info "fix the syntax, or compare against the reference template at:"
      info "  ${example}"
      ;;
    3)
      warn "${config} has 'provider.ollama' but model entries lack catalog-style metadata"
      while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        info "  - ${line}"
      done <<<"$probe_out"
      info ""
      info "Minimum metadata (only \`tool_call: true\`) is silently insufficient — opencode"
      info "will fail to forward the tools[] array and the model will refuse with"
      info "\"I don't have a bash function\". Each model entry needs the catalog shape:"
      info "  id, name, family, attachment, reasoning, tool_call, temperature,"
      info "  release_date, last_updated, modalities, open_weights, cost, limit"
      info ""
      info "Reference shape:"
      info "  ${example}"
      info "Background: docs/local-ollama-toolcall-evaluation.md"
      ;;
    *)
      warn "${config} has no 'provider.ollama' entry"
      info "to add local Ollama without overwriting your existing providers,"
      info "merge the 'provider.ollama' block from this template into your config:"
      info "  ${example}"
      info ""
      info "  cat ${example}        # show the snippet"
      info "  # then hand-merge it into ${config} under the \"provider\" key"
      ;;
  esac
}

check_opencode() {
  section "opencode (opencode backend, optional)"
  if command -v opencode >/dev/null 2>&1; then
    local oc_ver
    oc_ver=$(opencode --version 2>&1 | head -1 || true)
    ok "opencode ${oc_ver:-found} ($(command -v opencode))"
    check_opencode_ollama_provider
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

_resolve_link_target() {
  # Print the absolute path the symlink at $1 points to.
  local link="$1"
  local target
  target="$(readlink "$link")"
  if [[ "$target" != /* ]]; then
    target="$(cd "$(dirname "$link")" && cd "$(dirname "$target")" && pwd)/$(basename "$target")"
  fi
  printf '%s' "$target"
}

_warn_path_unless_present() {
  case ":$PATH:" in
    *":${BIN_DIR}:"*) return ;;
  esac
  warn "${BIN_DIR} is not on \$PATH"
  info "add to your shell rc: export PATH=\"${BIN_DIR}:\$PATH\""
}

check_bin_link() {
  section "bin shim"
  if [[ -z "${BIN_DIR}" ]]; then
    info "BIN_DIR is empty; skipping bin link"
    return
  fi
  local link="${BIN_DIR}/${BIN_NAME}"
  if [[ -L "$link" ]]; then
    local target
    target="$(_resolve_link_target "$link")"
    if [[ "$target" == "$LAUNCHER" ]]; then
      ok "${link} → ${LAUNCHER}"
      _warn_path_unless_present
      return
    fi
    warn "${link} points elsewhere: ${target}"
    info "remove it before re-installing: rm '${link}'"
    return
  fi
  if [[ -e "$link" ]]; then
    warn "${link} exists and is not a symlink; not touching"
    return
  fi
  miss "${BIN_NAME} not registered in ${BIN_DIR}"
  if [[ "$MODE" == "install" ]]; then
    if mkdir -p "${BIN_DIR}" && ln -s "$LAUNCHER" "$link"; then
      info "linked ${link} → ${LAUNCHER}"
      _warn_path_unless_present
    else
      miss "failed to create symlink at ${link}"
    fi
  else
    info "mkdir -p '${BIN_DIR}' && ln -s '${LAUNCHER}' '${link}'"
    info "(set BIN_DIR='' before --install to skip the bin link)"
  fi
}

# ---- run --------------------------------------------------------------

printf 'codex_gateway install check (mode=%s, os=%s)\n' "$MODE" "$OS_KIND"

check_python
check_python_deps
check_codex
check_opencode
check_env_file
check_run_script_perms
check_bin_link

echo
if (( MISSING == 0 )); then
  printf '%sAll required dependencies are present.%s\n' "$C_GREEN" "$C_RESET"
  echo "Next:"
  echo "  1. Edit ${ENV_FILE} (placeholder values still present? see warnings above)"
  if [[ -n "${BIN_DIR}" && -L "${BIN_DIR}/${BIN_NAME}" ]]; then
    echo "  2. ${BIN_NAME}"
  else
    echo "  2. bash ${LAUNCHER}"
  fi
  exit 0
fi

printf '%s%d required item(s) missing.%s\n' "$C_RED" "$MISSING" "$C_RESET"
if [[ "$MODE" == "check" ]]; then
  echo "Re-run with --install to attempt automatic installation, or apply the commands above manually."
fi
exit 1
