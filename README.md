# codex_gateway

Discord control-plane gateway for project-scoped Codex session management.

The gateway listens in a dedicated Discord control channel, lets an allowlisted
user choose a project, choose a session inside that project, optionally choose
the model profile for new sessions, and then runs `codex exec resume` against
the selected session.

Each session also binds a fixed `execution_env` at creation time:

- `openai` for GPT-family sessions that should reuse the normal Codex auth flow
- `local_ollama` for Ollama-backed sessions that must not inherit ChatGPT/OpenAI auth

## Current command surface

- `/project_select <project_id>`
- `/project_list`
- `/session_select <session_id>`
- `/session_new <label>`
- `/session_list`
- `/model_select <model_profile>`
- `/ask <prompt>`
- `/status`
- `/current`
- `/tui`
- `/watch <on|off> [interval]`
- `/last`
- `/stop`

## Control flow

1. Select a project in the control channel.
2. Either select an existing session or create a new session under that project.
3. Optionally select the model profile to use for new sessions.
4. Use `/ask` against the selected project/session.
5. Optionally use `/watch on [interval]` for the selected session.
6. Watch the project channel for blocked or operational follow-up when the
   runner emits project notifications.
7. Use `/tui` to get the local terminal command that attaches to the selected
   gateway session with the matching gateway `HOME`.

`/ask` no longer depends on `resume --last`. It requires an explicit selected
project and selected session, and the acceptance message echoes the target plus
the prompt excerpt that was accepted. New sessions start with `codex exec --json`
on the first `/ask`, then persist the discovered Codex thread ID for later
resumes.

Execution isolation is session-scoped:

- each session owns `STATE_ROOT/projects/<project>/sessions/<session>/codex-home/.codex`
- OpenAI sessions link shared auth from the normal Codex home
- local Ollama sessions remove `auth.json`, so local models are not rejected as ChatGPT-account requests
- `/status`, `/last`, `/current`, and `/tui` all use the selected session's own home and artifacts

## Documents

- [../docs/superpowers/specs/2026-04-22-codex-gateway-multi-session-design.md](../docs/superpowers/specs/2026-04-22-codex-gateway-multi-session-design.md):
  approved multi-project/session design spec
- [../docs/superpowers/plans/2026-04-22-codex-gateway-multi-session-implementation.md](../docs/superpowers/plans/2026-04-22-codex-gateway-multi-session-implementation.md):
  implementation plan used for the current rollout
- [DESIGN.md](./DESIGN.md): multi-project architecture, command contracts, and
  persisted state model.

## Configuration

Minimum required `.env` fields:

```bash
CONTROL_GUILD_ID="1494463231647813736"
CONTROL_CHANNEL_ID="replace_with_control_channel_id"
ALLOWED_USER_IDS="replace_with_your_discord_user_id"
CODEX_BIN="codex"
CODEX_CWD="/mnt/d/study_things/codex_sandbox"
STATE_ROOT="/path/to/codex_gateway/state"
RUNTIME_ROOT="/home/boor123/codex_gateway_runtime"
PROJECTS_FILE="/path/to/codex_gateway/state/projects.json"
STATE_FILE="/path/to/codex_gateway/state/gateway_state.json"
TMP_DIR="/home/boor123/codex_gateway_runtime/tmp"
LAST_RESPONSE_FILE="/home/boor123/codex_gateway_runtime/tmp/last_response.txt"
```

Notes:

- `STATE_ROOT` stores durable project/session metadata.
- `RUNTIME_ROOT` stores per-session runtime temp files.
- The default runtime root is `~/codex_gateway_runtime`, not `/tmp`, so
  attached TUI sessions can keep their sandbox helper binaries available.
- `PROJECTS_FILE` stores the registered project list.
- Each session also materializes its own `codex-home/.codex` under `STATE_ROOT`.
- OpenAI sessions reuse the auth source from `~/.codex/auth.json`.
- Local Ollama sessions do not link shared auth.
- `/last` attachments are written as `utf-8-sig` so downloaded text opens
  cleanly on Android mobile Discord and desktop viewers.

## Quick start

1. Copy `.env.example` to `.env`.
2. Fill in the Discord control guild/channel/user IDs.
3. Set the storage paths for gateway state:

   ```bash
   STATE_ROOT="/path/to/codex_gateway/state"
   RUNTIME_ROOT="/home/boor123/codex_gateway_runtime"
   PROJECTS_FILE="/path/to/codex_gateway/state/projects.json"
   ```

4. Install Python dependencies:

   ```bash
   pip install -r codex_gateway/requirements.txt
   ```

5. Run the gateway:

   ```bash
   bash codex_gateway/run_gateway.sh
   ```

## Run Script

Single gateway entrypoint:

```bash
bash codex_gateway/run_gateway.sh
```

The same process now handles both GPT and local Ollama sessions. The split
happens per session through `execution_env`, not through separate gateway
processes.

## Restart For Manual Testing

If an older gateway is already running, stop it first and then start the new
one from a clean terminal.

Recommended sequence:

1. Stop the old gateway process.
2. Start the updated gateway:

   ```bash
   bash codex_gateway/run_gateway.sh
   ```

3. In Discord control, validate in this order:
   - `/project_select <project_id>`
   - `/session_list` or `/session_select <session_id>`
   - `/model_select <model_profile>` if needed
   - `/session_new <label>` if you want a fresh session
   - `/ask <prompt>`
   - `/status`
   - `/last`
   - `/stop`

## Current Validation Focus

When manually testing the current rollout, confirm these points:

- `/ask` refuses to run until both project and session are selected
- accepted `/ask` messages show the selected `project/session` and the prompt
  excerpt
- `/last` downloads open without garbled text on Android mobile Discord and on
  desktop
- blocked or missing-session conditions produce readable operational summaries
- selection state survives gateway restart through the persisted state file

## Storage layout

- `PROJECTS_FILE`
  Stores registered projects, their control metadata, and the last active
  session per project.
- `STATE_ROOT`
  Stores persisted gateway state plus per-project session records.
- `RUNTIME_ROOT`
  Stores per-session runtime directories and temporary output artifacts.

## Operational notes

- The gateway still runs as a single local Python process.
- Session selection is explicit; the gateway does not infer the target session
  from `--last`.
- `/status` and `/last` remain operator-facing summaries rather than a remote
  TUI.
- `/current` reports the currently selected project/session, the selected
  session model, its `execution_env`, any bound Codex thread ref, and whether
  `/watch` is active.
- `/tui` reports the `bash codex_gateway/attach-gateway-session.sh ...` command
  for the currently selected session and includes the resolved gateway `HOME`
  plus Codex thread reference.
- `/status` shows the bound `model_profile` and `execution_env` for the
  currently selected session.
- `/watch on [interval]` arms a post-`/ask` repeating check for the selected
  session. If no interval is supplied, the default is `10m`. Examples:
  `30s`, `1m`, `10m`.
- `/watch` remains scoped to one selected session and turns off automatically if
  the selected project or session changes.
- Each `/ask` schedules a watch loop at the configured interval until the
  session reaches `idle`. While it is still `running`, the gateway posts a
  status snapshot on each interval. Once the session becomes `idle`, successful
  runs attach only the latest response, failed runs include exit details plus
  the latest stderr artifact, and the watch loop stops until the next `/ask`.
- `/model_select` accepts the union of the project's configured model list and
  the Ollama models discovered when the gateway starts via `ollama list`.
- `codex_gateway/attach-gateway-session.sh` reuses the selected session's
  gateway runtime `HOME`, so the attached local TUI sees the same config,
  MCP setup, trust settings, and writable roots as the gateway run.
- If the worktree is already dirty, prefer reviewing changes before creating a
  commit from gateway-driven work.
