# Direct Ollama Tool Orchestration

When a session's `execution_env` is `local_ollama`, the gateway bypasses
Codex CLI and calls the Ollama API directly. This avoids Codex CLI's 64K
context floor and 8-20K token system prompt overhead, which prevented 8B
models on a 12GB GPU from using tools or MCP reliably.

OpenAI sessions are unaffected — they continue through `runner.run_codex`.

## Routing

`bot._run_codex_attempt` branches on `execution_env`:

```
_run_codex_attempt
  ├─ execution_env == "local_ollama" → direct_runner.run_direct_ollama
  └─ execution_env == "openai"       → runner.run_codex (unchanged)
```

Both branches return the same `LastRunSummary`, so Discord messaging,
session updates, and the watch loop work without changes.

## Modules

| File | Responsibility |
|------|----------------|
| `direct_runner.py` | Built-in tools, JSON repair, agent loop |
| `mcp_bridge.py` | MCP server connection (stdio/SSE), tool discovery |
| `conversation_store.py` | JSONL conversation persistence + budget truncation |

## Built-in tools

| Tool | Parameters | Notes |
|------|-----------|-------|
| `shell` | `command` | 120s timeout, dangerous-pattern blocklist, child process killed on timeout |
| `read_file` | `path`, `offset?`, `limit?` | Output capped at `direct_tool_result_max_chars` |
| `write_file` | `path`, `content` | Creates parent directories |
| `edit_file` | `path`, `old_text`, `new_text` | Single literal replacement |
| `list_files` | `path?`, `pattern?` | Glob or directory listing |

All four file tools refuse paths that resolve outside the working
directory (`config.codex_cwd`).

### Dangerous command blocklist

Applied to `shell` only. Returns `"Blocked: potentially dangerous command"`
without execution:

- `sudo …`
- `rm -rf /` (root deletion)
- `mkfs …`
- `dd …`
- `chmod 777 /`
- Fork bomb patterns (`:(){ :|:& };:`)

## Agent loop

```
run_direct_ollama(prompt, ...)
  1. Load conversation history from ConversationStore
  2. Insert system prompt if missing, append user message
  3. Connect MCP bridge (if codex_home_path provided)
  4. all_tools = BUILTIN_TOOLS + mcp_bridge.get_tools()
  5. Loop up to direct_max_iterations:
       a. truncate_to_budget → ollama.AsyncClient.chat
       b. No tool_calls and no text-embedded calls → final text, break
       c. Tool calls → dispatch to built-in or MCP, append tool result
  6. Append new turns to conversation.jsonl
  7. Write last_response file
  8. Close MCP bridge
  9. Return LastRunSummary
```

### JSON repair

When an 8B model emits a tool call as text content (instead of a
structured `tool_calls` field), the loop:

1. Regex-extracts `{"name": ..., "arguments": ...}` patterns,
2. Falls back to the `json-repair` library for single-quoted or
   trailing-comma JSON,
3. Treats the response as text otherwise.

### Context budget

`ConversationStore.truncate_to_budget` keeps the system prompt and
removes oldest non-system turns until the serialized message list fits
within `direct_context_chars` (default 90,000 chars ≈ 32K tokens).
Truncation is in-memory only — the JSONL file on disk is append-only.

## MCP bridge

The bridge reads MCP server config from
`STATE_ROOT/projects/<project>/sessions/<session>/codex-home/.codex/config.toml`
and connects to each `[mcp_servers.<name>]` entry:

- `command` + `args` → stdio transport
- `url` → SSE transport

MCP tool names that collide with built-in names are prefixed as
`<server_name>__<tool_name>`. Server connection failures, missing
config, and tool call timeouts (30s) all degrade gracefully — the loop
continues with whatever tools remain.

## Configuration

New `GatewayConfig` fields, all read from environment variables on
startup:

| Field | Env var | Default |
|-------|---------|---------|
| `ollama_host` | `OLLAMA_HOST` | `http://localhost:11434` |
| `direct_max_iterations` | `DIRECT_MAX_ITERATIONS` | `10` |
| `direct_context_chars` | `DIRECT_CONTEXT_CHARS` | `90000` |
| `direct_shell_timeout` | `DIRECT_SHELL_TIMEOUT` | `120` |
| `direct_tool_result_max_chars` | `DIRECT_TOOL_RESULT_MAX_CHARS` | `8000` |
| `direct_system_prompt` | `DIRECT_SYSTEM_PROMPT` | (built-in coding-assistant prompt) |

These are only read by the `local_ollama` path; OpenAI sessions ignore
them.

## Storage layout

Adds one file per session alongside the existing `session.json`,
`codex-home/`, and `artifacts/`:

```
STATE_ROOT/projects/<project>/sessions/<session>/conversation.jsonl
```

One JSON message per line, in Ollama chat format:

```jsonl
{"role":"system","content":"You are a coding assistant..."}
{"role":"user","content":"list all python files"}
{"role":"assistant","content":"","tool_calls":[...]}
{"role":"tool","content":"./main.py\n./config.py"}
{"role":"assistant","content":"Found 2 Python files."}
```

## Failure modes

| Scenario | Behavior |
|----------|----------|
| Ollama server unreachable | `LastRunSummary` with `exit_signal="SPAWN_FAILED"` |
| Max iterations exceeded | Loop stops, last assistant text used as final response |
| Malformed tool call JSON | Regex + json-repair fallback, then treat as text |
| Shell command timeout | Process killed; tool result `"Command timed out after Ns"` |
| Dangerous command | Tool result `"Blocked: potentially dangerous command"` |
| Path escapes cwd | Tool result `"Error: path escapes working directory"` |
| MCP server connect fails | Skip server, continue with remaining tools |
| MCP tool call timeout | Tool result `"MCP tool timed out after 30s"` |

## Dependencies

```
ollama>=0.4
mcp>=1.0
json-repair>=0.30
```

## Testing

Run the new module test suites:

```
PYTHONPATH=".venv/lib/pythonpath" .venv/bin/python -m pytest \
  tests/test_direct_runner.py \
  tests/test_conversation_store.py \
  tests/test_mcp_bridge.py \
  tests/test_config.py -v
```

Test surface:

- `test_direct_runner.py` (32) — built-in tool definitions, dangerous
  command detection, result truncation, JSON repair, tool execution,
  path traversal protection, shell timeout cleanup, agent loop happy
  paths, text-embedded tool call extraction, end-to-end smoke,
  Ollama API failure path.
- `test_mcp_bridge.py` (12) — config parsing (TOML, stdio, SSE,
  malformed), tool collision prefix, async lifecycle (resolve,
  call_tool, close), get_tools combination.
- `test_conversation_store.py` (10) — load/append, JSONL corruption
  tolerance, unicode round-trip, budget truncation edge cases.
- `test_config.py` (5) — direct-mode defaults, env var override
  integration.

Spec: `docs/superpowers/specs/2026-04-27-direct-ollama-tool-orchestration-design.md`
Plan: `docs/superpowers/plans/2026-04-27-direct-ollama-tool-orchestration.md`
