# codex_gateway

Discord control-plane gateway for project-scoped local sessions on
**codex** (default) and **opencode** backends. One gateway process drives
both; sessions are bound to a project + backend at creation time, and
remote permission approval works over Discord buttons for opencode.

> 한국어: [README_kor.md](./README_kor.md)

- [Quick start](#quick-start)
- [Everyday usage](#everyday-usage)
- [Channel model](#channel-model) · [Slash command reference](#slash-command-reference) · [Backends](#backends)
- [Configuration](#configuration) · [Local Ollama for opencode](#local-ollama-as-an-opencode-provider)
- [Storage layout](#storage-layout) · [Operational notes](#operational-notes) · [Repository layout](#repository-layout)

For the dev narrative and migration notes see
[docs/HISTORY.md](./docs/HISTORY.md); for architecture + persisted state
see [docs/DESIGN.md](./docs/DESIGN.md); for the local-Ollama tool-calling
matrix behind the `mistral-nemo:latest` default see
[docs/local-ollama-toolcall-evaluation.md](./docs/local-ollama-toolcall-evaluation.md).

## Quick start

```bash
# 1. Check what's missing (read-only) or auto-install + register bin shim
bash codex_gateway/scripts/install.sh             # check only
bash codex_gateway/scripts/install.sh --install   # install + create ~/.local/bin/codex-gateway

# 2. Fill in .env (--install copies .env.example for you)
$EDITOR .env

# 3. Run
codex-gateway                                     # if the bin shim is on $PATH
# or:
bash codex_gateway/scripts/run_gateway.sh
```

Minimum `.env` to start a codex-only gateway:

```dotenv
DISCORD_GATEWAY_TOKEN=...           # or DISCORD_TOKEN
CONTROL_GUILD_ID=...                # or DISCORD_GUILD_ID
CONTROL_CHANNEL_ID=...
ALLOWED_USER_IDS=123,456            # comma-separated Discord user IDs
CODEX_BIN=codex
CODEX_CWD=/path/to/repo
STATE_ROOT=~/.codex_gateway/state
RUNTIME_ROOT=~/.codex_gateway/runtime
PROJECTS_FILE=~/.codex_gateway/projects.json
```

To also enable the opencode backend, add `OPENCODE_GATEWAY_ENABLED=1`
plus `OPENCODE_PROVIDER_ID` / `OPENCODE_MODEL_ID` — see
[Configuration → OpenCode runtime](#opencode-runtime-opt-in).
`BIN_DIR=` (empty) on the install script skips the bin shim; `BIN_DIR=...`
puts it elsewhere. If `~/.local/bin` isn't on `$PATH`, the script prints
the rc line to add.

## Everyday usage

Once the gateway is up and a project is registered, the typical Discord flow:

| step | command | where |
|---|---|---|
| pick a project (global) | `/project_select <project_id>` | control channel |
| start a session | `/session_new <label> [codex\|opencode]` | control or project channel |
| (opencode only) pick a model first | `/model_select <provider/model>` | control or project channel |
| run a prompt | `/ask <prompt>` | control or project channel |
| see what's happening | `/status`, `/current`, `/last` | same channel |
| watch a long run | `/watch on [interval]` | control channel |
| approve a permission ask (opencode) | tap ✅ / ♾️ / ❌ button, or `/perm_allow` / `/perm_reject` | project channel |
| attach a local TUI | `/tui` → run the printed command | same channel |
| stop | `/stop` | same channel |

The full command list and channel-binding rules are in
[Slash command reference](#slash-command-reference).

## Channel model

Two kinds of Discord channels are involved.

- **Control channel** (`CONTROL_CHANNEL_ID`): cross-project commands.
  Acts on the gateway-global selection.
- **Project channels** (`ProjectDefinition.project_channel_id`): per-project
  scope. Commands invoked here implicitly target that channel's project and
  use `ProjectDefinition.active_session_id` as the session, without
  touching the gateway-global selection.

Channel-aware commands: `/ask`, `/session_select`, `/session_list`,
`/session_new`, `/model_select`, `/current`, `/status`, `/last`,
`/tui`, `/stop`, `/perms`, `/perm_allow`, `/perm_reject`. The remaining
commands (`/project_select`, `/project_list`, `/watch`) stay
control-only — `/watch` keeps a single global target by design.

## Slash command reference

Project / session selection:

- `/project_select <project_id>`, `/project_list`
- `/session_select <session_id>`, `/session_list`
- `/session_new <label> [backend?]` — `backend` is `codex` (default) or
  `opencode`
- `/model_select <model_profile>` — per-project, applied to the next
  **opencode**-backed session creation. Codex sessions ignore this and
  always use `ProjectDefinition.default_model_profile`.

Run / observe:

- `/ask <prompt>` — runs the channel/control-bound session
- `/status`, `/current`, `/last`, `/tui`
- `/watch <on|off> [interval]` — arms a repeating status snapshot until
  the active run goes idle. Default interval `10m`. Forms: `30s`, `1m`, `10m`.
- `/stop`

OpenCode permission approval (only meaningful for opencode-backed sessions):

- `/perms` — list pending permission asks
- `/perm_allow [permission_id?] [scope=once|always]`
- `/perm_reject [permission_id?]`

When opencode emits `permission.asked`, the gateway also posts a 3-button
message (`✅ Once / ♾️ Always / ❌ Reject`) into the project channel for
operator approval; the slash commands above are the typed fallback.

## Backends

`codex` (default):
- Runs `codex exec resume <thread_ref>` as a subprocess.
- Each session owns its own `codex-home/.codex` under `STATE_ROOT`, with
  `auth.json` linked from `~/.codex/auth.json`.
- `model_profile` is bound at creation time from
  `ProjectDefinition.default_model_profile`.

`opencode`:
- The gateway manages a single long-lived `opencode serve` process and
  drives sessions over its HTTP + SSE API. Permission asks fan out to
  Discord buttons, with a configurable timeout that aborts the session if
  no operator answers.
- `model_profile` follows opencode's `provider/model` convention (e.g.
  `model-connect/Qwen3.5-...`). Defaults come from `OPENCODE_PROVIDER_ID` /
  `OPENCODE_MODEL_ID`; `/model_select` overrides per project.

The same gateway process handles both; the split is per-session through
the `backend` field, not through separate gateway processes.

## Configuration

### Required

| variable | purpose |
|---|---|
| `DISCORD_GATEWAY_TOKEN` (or `DISCORD_TOKEN`) | Gateway bot token |
| `CONTROL_GUILD_ID` (or `DISCORD_GUILD_ID`) | Discord guild for control |
| `CONTROL_CHANNEL_ID` | Control channel ID |
| `ALLOWED_USER_IDS` | comma-separated allowed Discord user IDs |
| `CODEX_BIN` | path to `codex` binary (default `codex`) |
| `CODEX_CWD` | working directory codex sessions run in |
| `STATE_ROOT` | durable project/session metadata |
| `RUNTIME_ROOT` | per-session runtime artifacts |
| `PROJECTS_FILE` | registered project list |

Optional storage knobs: `STATE_FILE`, `TMP_DIR`, `LAST_RESPONSE_FILE`,
`CODEX_HOME_PARENT`, `CODEX_HOME_SEED_FROM`, `CODEX_STATUS_HOME`.

### OpenCode runtime (opt-in)

Set these to enable the `opencode` backend. Without them, sessions
flagged `backend=opencode` fail at dispatch with a clear error.

| variable | default | purpose |
|---|---|---|
| `OPENCODE_GATEWAY_ENABLED` | `0` | `1`/`true`/`yes` enables the runtime |
| `OPENCODE_PROVIDER_ID` | (required) | e.g. `ollama`, `model-connect`, `openai`, `anthropic` |
| `OPENCODE_MODEL_ID` | (required) | e.g. `qwen3:14b`, `Qwen3.5-397B-A17B-FP8` |
| `OPENCODE_BIN` | `opencode` | binary path |
| `OPENCODE_SERVER_PORT` | `14096` | listen port |
| `OPENCODE_SERVER_HOSTNAME` | `127.0.0.1` | listen host |
| `OPENCODE_SERVER_PASSWORD` | (none) | enables Basic auth on the server |
| `OPENCODE_DEFAULT_AGENT` | `build` | opencode agent name |
| `OPENCODE_IDLE_TIMEOUT_SECONDS` | `7200` | abort if no operator answers a permission ask |

### Local Ollama as an opencode provider

opencode does not auto-detect a running local Ollama. Its built-in
catalog (models.dev) only lists `ollama-cloud` (the hosted Turbo
service) and `lmstudio`; the local `:11434` daemon must be registered
explicitly as a provider.

A ready-to-use template lives at
[`scripts/opencode.json.example`](./scripts/opencode.json.example) — it
defines an `ollama` provider over `@ai-sdk/openai-compatible` pointing
at `http://localhost:11434/v1`.

- If `~/.config/opencode/opencode.json` does not exist:
  ```bash
  mkdir -p ~/.config/opencode
  cp codex_gateway/scripts/opencode.json.example ~/.config/opencode/opencode.json
  # then edit the model list to match `ollama list`
  ```
- If it already exists, **do not overwrite it**. Open the template and
  hand-merge its `provider.ollama` block under your existing
  `"provider"` key.

> ### ⚠ Two non-obvious gotchas
>
> **1. Minimum model metadata is silently insufficient.** A model entry
> with only `"tool_call": true` is **not enough** to make opencode
> propagate the `tools[]` array on outgoing chat-completion requests.
> opencode keys this off the *full catalog-style* shape — the
> template carries `id`, `name`, `family`, `attachment`, `reasoning`,
> `tool_call`, `temperature`, `release_date`, `last_updated`,
> `modalities`, `open_weights`, `cost`, `limit` for each model, and
> all of those fields together flip the switch. Without them the
> agent receives no tools and the model will refuse with *"I don't
> have a bash function"* even though Ollama's
> `/v1/chat/completions` happily forwards `tool_calls` when called
> directly. **Copy the template's exact field set** when adding a
> new local model.
>
> **2. Gemma family is unsupported on Ollama for tool calling.**
> Ollama itself rejects tool-calling chat completions for Gemma
> models (`Error: registry.ollama.ai/library/gemma3:4b does not
> support tools`). This applies to `gemma2:*`, `gemma3:*`, and
> `gemma4:*`. If you want a tool-capable local backend, prefer
> `mistral-nemo:latest` (recommended), `qwen2.5:14b`, or other
> tool-trained instruct models. See
> [docs/local-ollama-toolcall-evaluation.md](./docs/local-ollama-toolcall-evaluation.md)
> for the full matrix and reasoning behind the recommended default.

`bash codex_gateway/scripts/install.sh --check` inspects the config and
prints the exact next step for whichever state you are in (no config /
no `provider.ollama` / models with insufficient metadata / config
fully registered).

After registration, with the gateway environment set as
`OPENCODE_PROVIDER_ID="ollama"` and
`OPENCODE_MODEL_ID="mistral-nemo:latest"` (or any model from
`ollama list` that the matrix above showed as tool-capable),
`/session_new <label> opencode` sessions route through the local
daemon.

### Optional: ChatGPT subscription auth for opencode

To run opencode against the same ChatGPT Plus/Pro account that codex
uses (instead of a separate OpenAI API key), install the community
plugin once on the gateway host:

```bash
npx -y opencode-openai-codex-auth@latest
opencode auth login
```

Plugin: <https://github.com/numman-ali/opencode-openai-codex-auth>.
Personal-use only per OpenAI ToS.

## Storage layout

- `PROJECTS_FILE` — registered project list, `active_session_id` per project.
- `STATE_ROOT` — `gateway_state.json` (selection, per-project pending
  model, last runs) plus `projects/<id>/sessions/<id>/session.json`.
- `RUNTIME_ROOT` — per-session runtime tmp + last-response artifacts.

Each codex session also materializes `codex-home/.codex` under its
session root.

## Operational notes

- `/ask` refuses to run unless both a project and session are resolvable
  (channel binding or explicit selection).
- `/last` attachments are written `utf-8-sig` so downloaded text opens
  cleanly on Android mobile Discord.
- `/watch on` arms a repeating status snapshot until the active run goes
  idle; turns off automatically if the targeted project or session changes.
- `/tui` returns a local terminal command for continuing the selected
  session at-desk:
  - codex sessions → `bash codex_gateway/scripts/attach-gateway-session.sh ...`,
    reusing the gateway runtime `HOME` so the TUI sees the same per-session
    `.codex` config.
  - opencode sessions →
    `opencode attach http://<host>:<port> --session ses_...`, which
    connects a local `opencode` TUI to the same `opencode serve` instance
    the gateway is driving. `--password` is appended when
    `OPENCODE_SERVER_PASSWORD` is set; remote-host invocations get an
    SSH-tunneling reminder.
- Selection state and per-project pending model values survive gateway
  restart through the persisted state file.
- When `OPENCODE_GATEWAY_ENABLED` is set, `opencode serve` is started in
  the background and stopped on gateway shutdown. If a server is already
  listening on the configured port, it is reused without spawning.

## Repository layout

```
codex_gateway/
├── README.md                # this file
├── __init__.py / __main__.py
├── config.py                # env → GatewayConfig
├── state.py                 # gateway-global selection, per-project pending model, runs
├── execution_env.py
├── runner.py                # codex backend implementation (subprocess)
├── bot.py                   # discord client + slash command surface
├── formatter.py             # discord-safe summaries
├── notification_router.py   # project channel notifications
├── permission_router.py     # opencode permission UX (buttons + slash fallback)
├── tui_attach.py            # `/tui` CLI for codex sessions
├── backend/                 # backend ABC + codex / opencode adapters
│   ├── codex.py
│   ├── opencode.py          # OpencodeClient + OpencodeBackend
│   ├── opencode_runtime.py  # holder for server + client + backend
│   └── opencode_server.py   # `opencode serve` lifecycle
├── storage/                 # persistent JSON storage
│   ├── session_store.py
│   ├── project_registry.py
│   └── last_response_store.py
├── inspectors/              # read-only inspectors for live local state
│   ├── process_inspector.py
│   └── session_inspector.py
├── ollama_pull.py           # on-demand `ollama pull` driver for `/model_select`
├── scripts/                 # shell entry points
│   ├── install.sh           # env check / auto-install / bin shim
│   ├── run_gateway.sh       # main gateway launcher
│   └── attach-gateway-session.sh   # `/tui` codex helper
├── docs/
│   ├── DESIGN.md
│   ├── HISTORY.md
│   └── opencode_discord_remote_dev_reference.md
└── tests/                   # 162 unit tests
```
