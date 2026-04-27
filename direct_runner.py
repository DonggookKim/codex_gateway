from __future__ import annotations

import asyncio
import glob as glob_mod
import json
import logging
import re
from pathlib import Path

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
