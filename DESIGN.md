# codex_gateway Design

## Goal

Provide a lightweight Discord control surface for multiple local sessions
running on either `codex` or `opencode`, without introducing a long-running
app server. The gateway runs locally, receives slash commands from Discord,
maps them onto persisted project/session records, and dispatches to the
backend bound to the targeted session.

## Non-goals

- Replacing the existing Discord MCP server (still optional inside codex)
- Managing arbitrary local sessions that are not registered in the gateway state
- Building a remote TUI
- Implementing a backend service beyond local JSON state, runtime folders,
  and a co-resident `opencode serve` process when the opencode runtime is
  enabled

## Channel model

Two kinds of Discord channels are involved.

- **Control channel** (`CONTROL_CHANNEL_ID`): cross-project commands. Acts
  on the gateway-global selection (`state.selection_state`).
- **Project channels** (`ProjectDefinition.project_channel_id`): per-project
  scope. Channel-aware commands invoked here implicitly target the bound
  project and use that project's `active_session_id`. Project notifications
  (blocked / failed / follow-up) are emitted into the same channel.

The gateway validates guild + user allowlist + channel before accepting any
command. Channel-aware commands additionally accept any registered project
channel; remaining commands stay control-channel only.

## High-level architecture

Logical modules:

- `bot.py` — registers slash commands, validates context, dispatches to
  the right backend, and owns the optional `opencode_runtime` +
  `permission_router` injections.
- `project_registry.py` — loads and saves registered projects.
- `session_store.py` — per-project session records.
  `SessionRecord.backend` selects the backend; `model_profile` holds the
  bound model (codex profile or opencode `provider/model`).
- `state.py` — gateway-global selection, per-project pending model map,
  per-project active runs, last-run summaries.
- `runner.py` — codex backend implementation: `run_codex` /
  `stop_active_run` and the `prepare_runtime_home_dir` helpers.
- `backend/__init__.py` — `Backend` ABC, `RunRequest`, `StopResult`.
- `backend/codex.py` — `CodexBackend` adapter delegating to `runner`.
- `backend/opencode.py` — `OpencodeClient` (REST + SSE) and
  `OpencodeBackend` (driven against an opencode session).
- `backend/opencode_server.py` — `opencode serve` lifecycle.
- `backend/opencode_runtime.py` — `OpencodeRuntime` (server + client +
  backend holder), built from env vars.
- `permission_router.py` — bridges opencode `permission.asked` to
  Discord button + slash UX, plus idle-timeout abort.
- `formatter.py` — Discord-safe summaries.

## Execution model

- Single Python process, one global `/ask` lock at a time.
- Session targeting is explicit: `/ask` requires a project + session
  resolvable from either the channel binding or the gateway-global
  selection.
- Each `SessionRecord.backend` selects the dispatch path:
  - `codex` → `runner.run_codex` (subprocess, per-session codex-home,
    shared auth linked from the gateway's codex home).
  - `opencode` → `OpencodeBackend.run` over HTTP + SSE against
    `opencode serve`. Permission asks fan out via `PermissionRouter`.
- Project notifications are emitted with project and session labels when
  the runner detects blocked or failed conditions.

## Command contracts

Channel-aware commands distinguish two scopes.

- **control scope**: targets `state.selection_state`.
- **project channel scope**: targets the channel's project and that
  project's `active_session_id`; never touches the gateway-global
  selection.

### `/project_select <project_id>`

- Control only.
- Validates that the project exists and is not archived.
- Persists `selected_project_id`. Clears the selected session.
- Does **not** prefill any pending model profile (that is a per-project
  value set by `/model_select`).

### `/session_select <session_id>`

- Channel-aware. In a project channel, target = channel project; in
  control, target = `selected_project_id`.
- Validates the session exists under the target project.
- In control: persists `selected_session_id` and updates the project's
  `active_session_id`.
- In project channel: updates only the project's `active_session_id`.

### `/session_new <label> [backend?]`

- Channel-aware. Target project resolution as above.
- `backend` is `codex` (default) or `opencode`.
- For `codex`: model is bound from `ProjectDefinition.default_model_profile`.
- For `opencode`: model is the per-project pending value from
  `/model_select`, falling back to `OPENCODE_PROVIDER_ID/OPENCODE_MODEL_ID`.
  Fails if the opencode runtime is not configured.
- Records `SessionRecord.backend`, `model_profile`, and the canonical
  per-session paths.

### `/model_select <model_profile>`

- Channel-aware.
- Accepts a project-allowed codex profile or a free-form `provider/model`
  string for opencode.
- Persists into `state.pending_model_profile_per_project[target_project]`.
- Codex sessions ignore this map; opencode sessions consume it on creation.

### `/ask <prompt>`

- Channel-aware.
- Rejects empty or overlong prompts.
- Rejects if another gateway-managed `/ask` is already active.
- Rejects when external codex activity is detected in `CODEX_CWD`.
- Acknowledges the accepted target as `project/session` and a prompt
  excerpt.
- Dispatches to the session's bound backend.

### `/status` / `/current`

- Channel-aware.
- In a project channel, render the project's view (its `active_runs`
  entry, `project_last_runs` entry, the channel project's
  `active_session_id`).
- In control, render the gateway-global view.

### `/perms`, `/perm_allow [id?] [scope?]`, `/perm_reject [id?]`

- Channel-aware.
- `/perms` lists pending opencode permission asks scoped to the channel
  project (or selected session in control).
- `/perm_allow` / `/perm_reject` without an explicit `id` resolve the
  latest pending ask for the channel-or-selected session.
- Mirrors the buttons posted by `PermissionRouter` into the project
  channel.

### `/last`

- Control only. Attaches the latest captured assistant response artifact.

### `/tui`

- Control only. Returns the local attach command for the selected
  session (codex backend).

### `/watch <on|off> [interval]`

- Control only. Arms a repeating status snapshot for the selected
  session until the active run goes idle.

### `/stop`

- Control only. Stops the current gateway-managed run.

## Persisted state

### Project registry (`PROJECTS_FILE`)

Each project record stores:

- `project_id`, `label`, `cwd`
- `project_channel_id`
- `default_model_profile`, `allowed_model_profiles`
- `active_session_id`
- `archived`

### Gateway state (`STATE_FILE`)

- `selection_state`: `selected_project_id`, `selected_session_id`,
  watch keys.
- `pending_model_profile_per_project`: `dict[project_id, profile]` set
  by `/model_select`.
- `last_run`: latest gateway-global run summary.
- `project_last_runs`: per-project last run summaries.

### Session records

Each `STATE_ROOT/projects/<id>/sessions/<id>/session.json` stores:

- `session_id`, `project_id`, `label`
- `backend` (`codex` or `opencode`)
- `model_profile`, `execution_env` (legacy informational; always `openai`)
- `status`, `codex_home_path`, `runtime_root`, `last_response_path`
- `codex_thread_ref` — for codex, the codex thread id; for opencode, the
  opencode session id (`ses_...`)
- blocked / expired metadata fields
- last prompt excerpt and last-run summary fields

## Permission UX (opencode)

Permission asks emitted by `opencode serve` over `/event` (`permission.asked`)
are bridged to Discord by `PermissionRouter`:

- Project-channel button message with `✅ Once` / `♾️ Always` /
  `❌ Reject`.
- Authorized clicks call `OpencodeClient.reply_permission`.
- Slash fallbacks: `/perm_allow`, `/perm_reject`, `/perms`.
- On idle timeout (`OPENCODE_IDLE_TIMEOUT_SECONDS`, default 7200s), the
  router calls `OpencodeClient.abort_session` and edits the message to
  reflect the timeout.
- "Always" sends `permission.asked.always[0]` as-is — the operator may
  override via slash command.

## Environment variables

Required (Discord + storage + codex):

- `DISCORD_GATEWAY_TOKEN` (or `DISCORD_TOKEN`)
- `CONTROL_GUILD_ID` (or `DISCORD_GUILD_ID`)
- `CONTROL_CHANNEL_ID`
- `ALLOWED_USER_IDS`
- `STATE_ROOT`, `RUNTIME_ROOT`, `PROJECTS_FILE`
- `CODEX_BIN`, `CODEX_CWD`

Optional (sizing, codex home):

- `STATE_FILE`, `TMP_DIR`, `LAST_RESPONSE_FILE`
- `CODEX_HOME_PARENT`, `CODEX_HOME_SEED_FROM`, `CODEX_STATUS_HOME`
- `OPENAI_SHARED_AUTH_SOURCE` / `CODEX_SHARED_AUTH_SOURCE`
- `PROMPT_MAX_CHARS`, `STATUS_TEXT_MAX_CHARS`, `STREAM_TAIL_CHARS`
- `STOP_SIGINT_GRACE_SECONDS`, `STOP_SIGTERM_GRACE_SECONDS`

OpenCode runtime (opt-in):

- `OPENCODE_GATEWAY_ENABLED` — `1`/`true`/`yes` enables the runtime
- `OPENCODE_PROVIDER_ID`, `OPENCODE_MODEL_ID` — required when enabled
- `OPENCODE_BIN`, `OPENCODE_SERVER_PORT`, `OPENCODE_SERVER_HOSTNAME`,
  `OPENCODE_SERVER_PASSWORD`
- `OPENCODE_DEFAULT_AGENT`, `OPENCODE_IDLE_TIMEOUT_SECONDS`

## Prompt shaping

The gateway prepends a short control-gateway preamble (`PROMPT_PREAMBLE`)
so the model returns the final answer through the CLI response and avoids
redundant Discord MCP traffic.

## Operational limits

- Concurrency: one gateway-managed `/ask` at a time.
- Sessions and projects must be registered in the gateway's own storage
  root; arbitrary external sessions are not tracked.
- Gateway restart preserves selection, per-project pending model values,
  and last-run summaries through the persisted state file. In-flight
  opencode permission asks are not preserved across restart in memory,
  but the opencode server side keeps them; `/perm_allow per_xxx` will
  still resolve them after restart.
