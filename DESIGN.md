# codex_gateway Design

## Goal

Provide a lightweight Discord control surface for multiple local Codex projects
without introducing a long-running app server or replacing the existing Discord
MCP workflow.

The gateway runs locally, receives slash commands from a control channel, maps
those commands onto persisted project/session records, and resumes the selected
Codex session through `codex exec resume`.

## Non-goals

- Replacing the existing Discord MCP server
- Managing arbitrary local Codex sessions that are not registered in the
  gateway state
- Building a remote TUI
- Implementing a backend service beyond local JSON state and runtime folders

## Channel model

Two Discord paths are expected:

- `control`: slash-command ingress for gateway operators
- project channels: operational notifications for blocked, failed, or
  follow-up-worthy runs

The gateway validates guild, control-channel, and user allowlists before it
accepts commands.

## High-level architecture

Logical modules:

- `bot.py`
  Registers slash commands, validates the selected project/session/model state,
  and acknowledges accepted `/ask` requests.
- `project_registry.py`
  Loads and saves registered projects plus project defaults.
- `session_store.py`
  Stores per-project session records, session metadata, and per-session paths.
- `runner.py`
  Executes `codex exec resume <session_ref>`, captures tails, and writes
  last-run summaries.
- `state.py`
  Persists selected project/session/model state plus the latest run summaries.
- `formatter.py`
  Produces short Discord-safe summaries for status and failure surfaces.

## Execution model

- The gateway keeps a single process and a single global `/ask` lock today.
- Session targeting is explicit: `/ask` requires both a selected project and a
  selected session.
- The runner resumes `selected_session_id` rather than using `resume --last`.
- Each session now binds an `execution_env`:
  - `openai` links shared auth from the normal Codex home
  - `local_ollama` removes `auth.json` so Ollama models are not treated as ChatGPT-account requests
- Codex home ownership is per session, not per gateway process.
- Project notifications are emitted with project and session labels when the
  runner detects blocked or failed conditions.

## Command contracts

### `/project_select <project_id>`

- Validates that the project exists and is not archived.
- Persists `selected_project_id`.
- Clears the selected session.
- Seeds the default model profile for future session creation from the project
  definition.

### `/session_select <session_id>`

- Requires a selected project.
- Validates that the session exists under that project and is not archived.
- Persists `selected_session_id`.
- Updates the project registry's `active_session_id`.

### `/model_select <model_profile>`

- Requires a selected project.
- Validates the requested profile against the project's
  `allowed_model_profiles`.
- Persists `selected_model_profile_for_new_session`.

### `/ask <prompt>`

- Requires a selected project and selected session.
- Rejects empty or overlong prompts.
- Rejects if another gateway-managed `/ask` is already active.
- Rejects when external Codex activity is detected in `CODEX_CWD`.
- Acknowledges the accepted target as `project/session` and includes a prompt
  excerpt.
- Runs `codex exec resume <session_ref> --skip-git-repo-check -o <file>`.

### `/status`

- Reports gateway-owned state, last known run summary, and local Codex
  activity.

### `/last`

- Attaches the latest captured assistant response artifact.

### `/stop`

- Attempts to stop the current gateway-managed subprocess only.

## Persisted state

### Project registry

Each project record stores:

- `project_id`
- `label`
- `cwd`
- `project_channel_id`
- `default_model_profile`
- `allowed_model_profiles`
- `active_session_id`
- `archived`

### Selection state

The gateway state file stores:

- `selected_project_id`
- `selected_session_id`
- `selected_model_profile_for_new_session`

### Session records

Each session record stores:

- `session_id`
- `project_id`
- `label`
- `model_profile`
- `execution_env`
- `status`
- `codex_home_path`
- `runtime_root`
- `last_response_path`
- blocked/expired metadata fields
- last prompt excerpt and last-run summary fields

## Environment variables

Required:

- `DISCORD_GATEWAY_TOKEN`
- `CONTROL_GUILD_ID`
- `CONTROL_CHANNEL_ID`
- `ALLOWED_USER_IDS`

Important storage/runtime settings:

- `STATE_ROOT`
- `RUNTIME_ROOT`
- `PROJECTS_FILE`
- `STATE_FILE`
- `TMP_DIR`
- `LAST_RESPONSE_FILE`

Execution settings:

- `CODEX_BIN`
- `CODEX_CWD`
- optional shared auth source for `openai` sessions
- `PROMPT_MAX_CHARS`
- `STATUS_TEXT_MAX_CHARS`
- `STREAM_TAIL_CHARS`
- `STOP_SIGINT_GRACE_SECONDS`
- `STOP_SIGTERM_GRACE_SECONDS`

## Prompt shaping

The gateway may prepend a short control-gateway instruction block so Codex
returns the final answer in the CLI response and avoids redundant Discord
traffic unless truly needed.

## Operational limits

- Current concurrency is still one gateway-managed `/ask` at a time.
- Existing runner warnings or local filesystem quirks should be treated as
  implementation follow-up, not silently ignored in operator summaries.
- The gateway tracks only sessions and projects registered in its own storage
  root.
