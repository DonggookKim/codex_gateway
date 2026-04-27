from __future__ import annotations

import asyncio
import glob as glob_mod
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Callable

import ollama

from .config import GatewayConfig
from .conversation_store import ConversationStore
from .execution_env import LOCAL_OLLAMA_ENV
from .formatter import excerpt
from .last_response_store import write_last_response_text
from .mcp_bridge import McpBridge
from .notification_router import build_project_notification
from .state import ActiveRun, GatewayState, LastRunSummary, utc_now

try:
    from json_repair import repair_json
except ImportError:
    def repair_json(text: str, **kwargs) -> str:
        return text

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Built-in tool definitions (Ollama tools format)
# ---------------------------------------------------------------------------

BUILTIN_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Execute a shell command and return stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file and return its contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to CWD."},
                    "offset": {"type": "integer", "description": "Start line (0-based)."},
                    "limit": {"type": "integer", "description": "Max lines to read."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file, creating or overwriting it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to CWD."},
                    "content": {"type": "string", "description": "Content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace a text fragment in a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to CWD."},
                    "old_text": {"type": "string", "description": "Text to find."},
                    "new_text": {"type": "string", "description": "Replacement text."},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in a directory or matching a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path relative to CWD. Defaults to '.'."},
                    "pattern": {"type": "string", "description": "Glob pattern (e.g. '*.py')."},
                },
            },
        },
    },
]

BUILTIN_TOOL_NAMES = frozenset(
    t["function"]["name"] for t in BUILTIN_TOOLS
)


# ---------------------------------------------------------------------------
# Dangerous command detection
# ---------------------------------------------------------------------------

DANGEROUS_PATTERNS: list[re.Pattern] = [
    re.compile(r"sudo\s+"),
    re.compile(r"rm\s+-[rf]*\s+/(?!\w)"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+"),
    re.compile(r"chmod\s+777\s+/"),
    re.compile(r":\(\)\{.*\}"),
]


def _is_dangerous_command(command: str) -> bool:
    for pattern in DANGEROUS_PATTERNS:
        if pattern.search(command):
            return True
    return False


# ---------------------------------------------------------------------------
# Result truncation
# ---------------------------------------------------------------------------

def _truncate_result(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... (truncated)"


# ---------------------------------------------------------------------------
# JSON repair for malformed tool calls
# ---------------------------------------------------------------------------

_TOOL_CALL_PATTERN = re.compile(
    r'\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{[^}]*\}\s*\}'
)


def _extract_tool_calls_from_text(text: str) -> list[dict]:
    calls: list[dict] = []

    # Try direct regex extraction
    matches = _TOOL_CALL_PATTERN.findall(text)
    for match in matches:
        try:
            parsed = json.loads(match)
            if "name" in parsed and "arguments" in parsed:
                calls.append(parsed)
        except json.JSONDecodeError:
            pass

    if calls:
        return calls

    # Try json-repair on the whole text for single-quote or malformed JSON
    try:
        repaired = repair_json(text, return_objects=False)
        if isinstance(repaired, str):
            parsed = json.loads(repaired)
            if isinstance(parsed, dict) and "name" in parsed and "arguments" in parsed:
                return [parsed]
            if isinstance(parsed, list):
                return [
                    item for item in parsed
                    if isinstance(item, dict) and "name" in item and "arguments" in item
                ]
    except Exception:
        pass

    return []


# ---------------------------------------------------------------------------
# Built-in tool execution
# ---------------------------------------------------------------------------

async def _execute_builtin_tool(
    tool_name: str,
    arguments: dict,
    cwd: Path,
    shell_timeout: int,
    max_result_chars: int,
) -> str:
    try:
        if tool_name == "shell":
            return await _exec_shell(arguments, cwd, shell_timeout, max_result_chars)
        if tool_name == "read_file":
            return _exec_read_file(arguments, cwd, max_result_chars)
        if tool_name == "write_file":
            return _exec_write_file(arguments, cwd)
        if tool_name == "edit_file":
            return _exec_edit_file(arguments, cwd)
        if tool_name == "list_files":
            return _exec_list_files(arguments, cwd, max_result_chars)
        return f"Unknown built-in tool: {tool_name}"
    except Exception as exc:
        return f"Tool error: {exc}"


async def _exec_shell(
    arguments: dict,
    cwd: Path,
    timeout: int,
    max_chars: int,
) -> str:
    command = arguments.get("command", "")
    if not command:
        return "Error: empty command"
    if _is_dangerous_command(command):
        return "Blocked: potentially dangerous command"

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return f"Command timed out after {timeout}s"
    except OSError as exc:
        return f"Command failed: {exc}"

    output = stdout.decode("utf-8", errors="replace")
    err_output = stderr.decode("utf-8", errors="replace")
    combined = output
    if err_output:
        combined += f"\nSTDERR:\n{err_output}"
    return _truncate_result(combined, max_chars)


def _check_path_containment(target: Path, cwd: Path) -> str | None:
    resolved_cwd = cwd.resolve()
    if not target.is_relative_to(resolved_cwd):
        return f"Error: path escapes working directory"
    return None


def _exec_read_file(arguments: dict, cwd: Path, max_chars: int) -> str:
    rel_path = arguments.get("path", "")
    if not rel_path:
        return "Error: path is required"
    target = (cwd / rel_path).resolve()
    err = _check_path_containment(target, cwd)
    if err:
        return err
    if not target.exists():
        return f"Error: file not found: {rel_path}"
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Error reading file: {exc}"

    offset = arguments.get("offset")
    limit = arguments.get("limit")
    if offset is not None or limit is not None:
        lines = text.splitlines(keepends=True)
        start = int(offset) if offset is not None else 0
        end = start + int(limit) if limit is not None else len(lines)
        text = "".join(lines[start:end])

    return _truncate_result(text, max_chars)


def _exec_write_file(arguments: dict, cwd: Path) -> str:
    rel_path = arguments.get("path", "")
    content = arguments.get("content", "")
    if not rel_path:
        return "Error: path is required"
    target = (cwd / rel_path).resolve()
    err = _check_path_containment(target, cwd)
    if err:
        return err
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Written {len(content)} chars to {rel_path}"


def _exec_edit_file(arguments: dict, cwd: Path) -> str:
    rel_path = arguments.get("path", "")
    old_text = arguments.get("old_text", "")
    new_text = arguments.get("new_text", "")
    if not rel_path:
        return "Error: path is required"
    target = (cwd / rel_path).resolve()
    err = _check_path_containment(target, cwd)
    if err:
        return err
    if not target.exists():
        return f"Error: file not found: {rel_path}"
    try:
        content = target.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Error reading file: {exc}"
    if old_text not in content:
        return f"Error: old_text not found in {rel_path}"
    updated = content.replace(old_text, new_text, 1)
    target.write_text(updated, encoding="utf-8")
    return f"Edited {rel_path}"


def _exec_list_files(arguments: dict, cwd: Path, max_chars: int) -> str:
    rel_path = arguments.get("path", ".")
    pattern = arguments.get("pattern")
    target = (cwd / rel_path).resolve()
    err = _check_path_containment(target, cwd)
    if err:
        return err
    if not target.exists():
        return f"Error: path not found: {rel_path}"

    if pattern:
        matches = sorted(glob_mod.glob(str(target / pattern), recursive=True))
        entries = [str(Path(m).relative_to(cwd.resolve())) for m in matches]
    else:
        if not target.is_dir():
            return f"Error: not a directory: {rel_path}"
        entries = sorted(entry.name for entry in target.iterdir())

    return _truncate_result("\n".join(entries), max_chars)


# ---------------------------------------------------------------------------
# Agent loop with Ollama integration
# ---------------------------------------------------------------------------

async def run_direct_ollama(
    state: GatewayState,
    config: GatewayConfig,
    requester_user_id: int,
    requester_name: str,
    prompt: str,
    project_id: str | None = None,
    session_id: str | None = None,
    model_profile: str | None = None,
    execution_env: str = LOCAL_OLLAMA_ENV,
    codex_home_path: Path | None = None,
    runtime_root: Path | None = None,
    last_response_file: Path | None = None,
    project_label: str | None = None,
    session_label: str | None = None,
    project_notification_sink: Callable[[str], None] | None = None,
    **kwargs: Any,
) -> LastRunSummary:
    run_id = uuid.uuid4().hex[:8]
    prompt_excerpt = excerpt(prompt, config.status_text_max_chars)
    started_at = utc_now()

    model = model_profile or "qwen3:8b"
    cwd = config.codex_cwd

    # Resolve last response file
    resolved_last_response = last_response_file or (
        (runtime_root / "tmp" / "last_response.txt")
        if runtime_root is not None
        else config.last_response_file
    )

    # Load conversation history
    conversation_store = ConversationStore(config.state_root)
    messages: list[dict] = []
    if project_id and session_id:
        messages = conversation_store.load(project_id, session_id)

    # Ensure system prompt
    if not messages or messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": config.direct_system_prompt})

    # Add user message
    messages.append({"role": "user", "content": prompt})

    # Connect MCP bridge
    mcp_bridge: McpBridge | None = None
    mcp_tools: list[dict] = []
    if codex_home_path is not None:
        mcp_bridge = McpBridge(codex_home_path)
        try:
            await mcp_bridge.connect()
            mcp_tools = mcp_bridge.get_tools()
        except Exception:
            LOGGER.warning("MCP bridge connection failed", exc_info=True)

    all_tools = BUILTIN_TOOLS + mcp_tools

    # Set up active run tracking
    active_run = ActiveRun(
        run_id=run_id,
        requester_user_id=requester_user_id,
        requester_name=requester_name,
        prompt_excerpt=prompt_excerpt,
        started_at=started_at,
        pid=None,
        last_message_path=resolved_last_response,
        project_id=project_id,
        session_id=session_id,
        model_profile=model_profile,
    )
    state.set_active_run(active_run)

    # Create Ollama client
    client = ollama.AsyncClient(host=config.ollama_host)

    final_text = ""
    new_messages: list[dict] = [{"role": "user", "content": prompt}]
    exit_code = 0
    assistant_content = ""

    try:
        for iteration in range(config.direct_max_iterations):
            # Truncate messages to budget
            send_messages = conversation_store.truncate_to_budget(
                messages, config.direct_context_chars,
            )

            # Call Ollama
            try:
                response = await client.chat(
                    model=model,
                    messages=send_messages,
                    tools=all_tools if all_tools else None,
                )
            except Exception as exc:
                LOGGER.error("Ollama API call failed: %s", exc)
                summary = LastRunSummary(
                    run_id=run_id,
                    requester_user_id=requester_user_id,
                    requester_name=requester_name,
                    prompt_excerpt=prompt_excerpt,
                    started_at=started_at,
                    finished_at=utc_now(),
                    exit_code=None,
                    exit_signal="SPAWN_FAILED",
                    stdout_excerpt="",
                    stderr_excerpt=str(exc),
                    assistant_response_excerpt="",
                )
                state.finish_run(summary)
                if mcp_bridge is not None:
                    await mcp_bridge.close()
                return summary

            assistant_content = response.message.content or ""
            tool_calls = response.message.tool_calls

            # Check for tool calls in text content if none structured
            extracted_calls: list[dict] = []
            if not tool_calls and assistant_content:
                extracted_calls = _extract_tool_calls_from_text(assistant_content)

            if not tool_calls and not extracted_calls:
                # Final text response
                final_text = assistant_content
                assistant_msg = {"role": "assistant", "content": final_text}
                messages.append(assistant_msg)
                new_messages.append(assistant_msg)
                break

            # Process structured tool calls
            assistant_msg: dict = {"role": "assistant", "content": assistant_content}
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                            if isinstance(tc.function.arguments, dict)
                            else json.loads(tc.function.arguments)
                            if isinstance(tc.function.arguments, str)
                            else {},
                        },
                    }
                    for tc in tool_calls
                ]
            messages.append(assistant_msg)
            new_messages.append(assistant_msg)

            # Determine which calls to process
            calls_to_process: list[dict] = []
            if tool_calls:
                for tc in tool_calls:
                    name = tc.function.name
                    args = tc.function.arguments
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    if not isinstance(args, dict):
                        args = {}
                    calls_to_process.append({"name": name, "arguments": args})
            else:
                calls_to_process = extracted_calls

            # Execute each tool call
            for call in calls_to_process:
                tool_name = call.get("name", "")
                tool_args = call.get("arguments", {})

                if tool_name in BUILTIN_TOOL_NAMES:
                    result = await _execute_builtin_tool(
                        tool_name, tool_args, cwd,
                        config.direct_shell_timeout,
                        config.direct_tool_result_max_chars,
                    )
                elif mcp_bridge is not None:
                    resolved = mcp_bridge.resolve_server_and_tool(tool_name)
                    if resolved is not None:
                        server_name, original_name = resolved
                        result = await mcp_bridge.call_tool(
                            server_name, original_name, tool_args,
                        )
                    else:
                        result = f"Unknown tool: {tool_name}"
                else:
                    result = f"Unknown tool: {tool_name}"

                tool_msg = {"role": "tool", "content": result}
                messages.append(tool_msg)
                new_messages.append(tool_msg)

            # Update active run status
            active_run.stdout_tail = f"Iteration {iteration + 1}/{config.direct_max_iterations}"

        else:
            # Max iterations reached
            final_text = assistant_content or "(max iterations reached)"

    except Exception as exc:
        LOGGER.exception("Agent loop error")
        final_text = f"Agent loop error: {exc}"
        exit_code = 1

    # Save conversation history
    if project_id and session_id:
        conversation_store.append(project_id, session_id, new_messages)

    # Write last response
    if resolved_last_response:
        resolved_last_response.parent.mkdir(parents=True, exist_ok=True)
        write_last_response_text(resolved_last_response, final_text)

    # Close MCP
    if mcp_bridge is not None:
        await mcp_bridge.close()

    summary = LastRunSummary(
        run_id=run_id,
        requester_user_id=requester_user_id,
        requester_name=requester_name,
        prompt_excerpt=prompt_excerpt,
        started_at=started_at,
        finished_at=utc_now(),
        exit_code=exit_code,
        exit_signal=None,
        stdout_excerpt="",
        stderr_excerpt="",
        assistant_response_excerpt=excerpt(final_text, config.status_text_max_chars),
    )
    state.finish_run(summary)
    return summary
