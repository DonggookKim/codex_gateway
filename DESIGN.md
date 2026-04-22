# codex_gateway v1 Design

## Goal

Allow a trusted Discord user to send follow-up prompts into the most recent
local Codex session without introducing a heavy app-server architecture.

The gateway runs locally, receives slash commands from Discord, executes Codex
CLI as a subprocess, and reports status back to the Discord control channel.

## Non-goals

- Replacing the existing Discord MCP server
- Full multi-project orchestration
- Full remote TUI or app-server control
- Interrupting arbitrary Codex sessions not started by the gateway
- Reconstructing full Codex state from every local session artifact

## Channel model

Two Discord channels are used in v1:

- `일반`: existing Codex Discord MCP output channel
- `control`: gateway command channel

Role split:

- Codex MCP tools keep using `일반`
- The gateway bot only accepts `/ask`, `/status`, `/stop` in `control`
- The gateway bot only posts its own acknowledgements and summaries in `control`

This keeps human-issued remote control separate from Codex-originated questions
or reports.

## High-level architecture

The gateway is a single local Python process built around `discord.py`.

Logical modules:

- `bot.py`
  Registers slash commands and routes validated requests to the runner/state
  layer.
- `config.py`
  Loads environment variables and validates required configuration.
- `runner.py`
  Builds the Codex subprocess argv, starts it with
  `asyncio.create_subprocess_exec`, captures stdout/stderr, and manages stop
  signals.
- `state.py`
  Owns in-memory active-run state plus a small persisted last-run snapshot.
- `formatter.py`
  Produces short Discord-safe summaries and truncates long output.

## Execution model

The gateway serializes all `/ask` requests with one `asyncio.Lock()`.

Rules:

- At most one gateway-managed Codex subprocess may run at a time.
- If `/ask` arrives while another run is active, the command is rejected with a
  clear `busy` message.
- `/stop` only operates on the current active run tracked by the gateway.
- `/status` never blocks on Codex; it reads gateway-owned state only.

## Security boundaries

The gateway must reject requests unless all of the following match:

- Discord guild ID is allowlisted
- Discord channel ID matches the control channel
- Discord user ID is allowlisted

Implementation constraints:

- Never use `shell=True`
- Use argv array form for subprocess execution
- Do not echo full stdout/stderr blindly into Discord
- Keep prompts and responses truncated in status surfaces

## Codex invocation contract

Base command:

```bash
codex exec resume --last --skip-git-repo-check -o <last_message_file> "<prompt>"
```

Recommended defaults:

- `cwd`: `/home/boor123/work/codex_sandbox`
- `-o <last_message_file>`: captures the final assistant message directly
- capture `stdout` and `stderr` for short summaries only

Optional prompt wrapper:

- Prefix the user prompt with a short fixed instruction telling Codex to return
  its final answer to stdout and avoid Discord MCP usage unless necessary.
- This is a soft guardrail, not a hard guarantee.

## Command contracts

### `/ask <prompt>`

Purpose:

- Send a follow-up prompt into the most recent Codex session.

Validation:

- Caller must pass guild, channel, and user allowlist checks
- Prompt must be non-empty and below a configured maximum length
- Request is rejected if another gateway-managed run is active

Processing flow:

1. Acknowledge receipt in the control channel
2. Acquire the run lock
3. Create per-run temp files for last message and optional logs
4. Spawn the Codex subprocess
5. Mark state as `running`
6. Wait for process completion while capturing stdout/stderr tails
7. Read `last_message_file`
8. Persist a compact last-run snapshot
9. Release the run lock and return to `idle`

Response policy:

- `/ask` only confirms whether the request was accepted
- Completion details are read later through `/status`
- The gateway does not mirror every final answer back into the control channel

Important UX limit:

- `/ask` resumes the same Codex session on disk, but it does so through a
  separate non-interactive process
- An already-open TUI will not live-refresh to show that remote execution as it
  happens

Stored run metadata:

- run ID
- requester Discord user ID
- requester display name
- prompt excerpt
- start time
- finish time
- subprocess PID
- exit code or stop signal
- stdout tail
- stderr tail
- last assistant response excerpt

### `/status`

Purpose:

- Report the gateway-owned view of current and most recent activity.

Important limit:

- `/status` does not promise the absolute live state of every Codex session on
  the machine.
- `/status` reports only what the gateway knows about its own managed runs.

When active run exists, show:

- state: `running` or `stopping`
- current PID
- elapsed time
- requester
- current prompt excerpt
- recent stderr tail if present

When no active run exists, show:

- state: `idle`
- last run start and end time
- last requester
- last prompt excerpt
- last assistant response excerpt
- last exit code
- last stderr summary if relevant

Source of truth:

- In-memory state while running
- Persisted last-run snapshot when idle

Recommended implementation:

- Do not parse `~/.codex/sessions/*.jsonl` during `/status`
- Instead, rely on gateway-captured data from `-o <last_message_file>` plus
  buffered stdout/stderr tails

### `/stop`

Purpose:

- Attempt to stop the active gateway-managed Codex subprocess.

Important limit:

- `/stop` does not cancel Codex globally
- `/stop` does not target arbitrary local Codex processes
- `/stop` only affects the current gateway-managed active run, if any

Behavior:

- If no active run exists, return a no-op status message
- If an active run exists, move state to `stopping`
- Send `SIGINT`
- Wait a short grace period
- If still running, send `SIGTERM`
- Record final signal/exit details and report the outcome

Recommended v1 policy:

- Do not use forced kill by default
- If the process ignores `SIGINT` and `SIGTERM`, return that it did not exit
  cleanly and leave stronger termination for a future revision

## State model

### Runtime state

Minimal in-memory fields:

```text
mode: idle | running | stopping
active_run: null | ActiveRun
last_run: null | LastRunSummary
lock: asyncio.Lock
```

`ActiveRun`:

```text
run_id
requester_user_id
requester_name
prompt_excerpt
started_at
pid
status_message
stdout_tail
stderr_tail
last_message_path
stop_requested_at
```

`LastRunSummary`:

```text
run_id
requester_user_id
requester_name
prompt_excerpt
started_at
finished_at
exit_code
exit_signal
stdout_excerpt
stderr_excerpt
assistant_response_excerpt
```

### Persistence

Persist a compact JSON state file so the gateway can recover the last known
completed run after restart.

Persist only `last_run`, not a resumable live process object.

If the bot restarts while a subprocess was active, the gateway should come up in
`idle` and mark the previous active run as `unknown after restart` if desired.

## Error handling

Expected failure cases:

- unauthorized user
- wrong guild or channel
- empty prompt
- command received while busy
- `codex` binary missing
- Codex exits non-zero
- last message file missing or empty
- stop requested when no active run exists

Response principle:

- Return short operationally useful messages
- Include only trimmed stderr, not full raw logs
- Preserve enough detail to distinguish config failure from prompt failure

## Suggested environment variables

Required:

- `DISCORD_GATEWAY_TOKEN`
- `CONTROL_GUILD_ID`
- `CONTROL_CHANNEL_ID`
- `ALLOWED_USER_IDS`

Runtime:

- `CODEX_BIN`
- `CODEX_CWD`
- `PROMPT_MAX_CHARS`
- `STATUS_TEXT_MAX_CHARS`
- `STOP_SIGINT_GRACE_SECONDS`
- `STOP_SIGTERM_GRACE_SECONDS`
- `STATE_FILE`
- `TMP_DIR`

## Prompt shaping

Recommended wrapper template:

```text
You are being resumed via a Discord control gateway.
Prefer returning your final answer in the CLI final response.
Avoid Discord MCP messages unless the task genuinely requires them.

User request:
<prompt>
```

This reduces duplicate reporting between the gateway and the existing Discord
MCP channel, while still allowing Codex to use Discord MCP when truly needed.

## Failure-mode notes

### Gateway restarts during a run

The subprocess is no longer tracked by the bot.

v1 handling:

- clear in-memory active state on startup
- keep only the last persisted completed summary
- do not claim the old run is still controllable

### Codex writes to Discord MCP anyway

This remains possible because prompt wrapping is only advisory.

v1 mitigation:

- keep MCP traffic in `일반`
- keep gateway control traffic in `control`

### Another local Codex process changes the most recent session

This can affect `--last`.

v1 acceptance:

- document that the gateway assumes one active project flow
- accept this limitation for the single-project first version

## Recommended next implementation steps

1. Create `config.py` and validate all environment variables on startup
2. Create `state.py` with the runtime and persisted summary structures
3. Create `runner.py` around `asyncio.create_subprocess_exec`
4. Add `discord.py` slash commands in `bot.py`
5. Add output truncation helpers in `formatter.py`
6. Add a small `README` section for WSL launch and `.env` usage
