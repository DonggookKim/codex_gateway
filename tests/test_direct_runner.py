from __future__ import annotations

import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from codex_gateway.direct_runner import (
    BUILTIN_TOOLS,
    DANGEROUS_PATTERNS,
    _is_dangerous_command,
    _truncate_result,
    _extract_tool_calls_from_text,
    _execute_builtin_tool,
)


class BuiltinToolDefinitionsTest(unittest.TestCase):
    def test_has_five_builtin_tools(self) -> None:
        self.assertEqual(len(BUILTIN_TOOLS), 5)

    def test_all_tools_have_required_fields(self) -> None:
        for tool in BUILTIN_TOOLS:
            self.assertEqual(tool["type"], "function")
            func = tool["function"]
            self.assertIn("name", func)
            self.assertIn("description", func)
            self.assertIn("parameters", func)

    def test_tool_names(self) -> None:
        names = {t["function"]["name"] for t in BUILTIN_TOOLS}
        self.assertEqual(names, {"shell", "read_file", "write_file", "edit_file", "list_files"})


class DangerousCommandTest(unittest.TestCase):
    def test_blocks_sudo(self) -> None:
        self.assertTrue(_is_dangerous_command("sudo rm -rf /tmp"))

    def test_blocks_rm_rf_root(self) -> None:
        self.assertTrue(_is_dangerous_command("rm -rf /"))

    def test_blocks_mkfs(self) -> None:
        self.assertTrue(_is_dangerous_command("mkfs.ext4 /dev/sda1"))

    def test_blocks_dd(self) -> None:
        self.assertTrue(_is_dangerous_command("dd if=/dev/zero of=/dev/sda"))

    def test_allows_normal_commands(self) -> None:
        self.assertFalse(_is_dangerous_command("ls -la"))
        self.assertFalse(_is_dangerous_command("find . -name '*.py'"))
        self.assertFalse(_is_dangerous_command("cat README.md"))

    def test_allows_rm_in_subdirectory(self) -> None:
        self.assertFalse(_is_dangerous_command("rm -rf ./build"))


class TruncateResultTest(unittest.TestCase):
    def test_truncates_long_output(self) -> None:
        result = _truncate_result("a" * 10000, 100)
        self.assertEqual(len(result), 100 + len("\n... (truncated)"))
        self.assertTrue(result.endswith("... (truncated)"))

    def test_preserves_short_output(self) -> None:
        result = _truncate_result("hello", 8000)
        self.assertEqual(result, "hello")


class ExtractToolCallsFromTextTest(unittest.TestCase):
    def test_extracts_valid_tool_call_json(self) -> None:
        text = 'I will use the shell tool: {"name": "shell", "arguments": {"command": "ls"}}'
        calls = _extract_tool_calls_from_text(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "shell")
        self.assertEqual(calls[0]["arguments"]["command"], "ls")

    def test_returns_empty_for_no_tool_calls(self) -> None:
        calls = _extract_tool_calls_from_text("Just a normal response.")
        self.assertEqual(calls, [])

    def test_handles_malformed_json_with_repair(self) -> None:
        # Single quotes instead of double quotes
        text = "{'name': 'shell', 'arguments': {'command': 'ls'}}"
        calls = _extract_tool_calls_from_text(text)
        # json-repair should fix this
        self.assertEqual(len(calls), 1)


class ExecuteBuiltinToolTest(unittest.TestCase):
    def test_shell_executes_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = asyncio.get_event_loop().run_until_complete(
                _execute_builtin_tool("shell", {"command": "echo hello"}, cwd, 120, 8000)
            )
            self.assertIn("hello", result)

    def test_shell_blocks_dangerous_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = asyncio.get_event_loop().run_until_complete(
                _execute_builtin_tool("shell", {"command": "sudo rm -rf /"}, cwd, 120, 8000)
            )
            self.assertIn("Blocked", result)

    def test_read_file_reads_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            test_file = cwd / "test.txt"
            test_file.write_text("file content here", encoding="utf-8")
            result = asyncio.get_event_loop().run_until_complete(
                _execute_builtin_tool("read_file", {"path": "test.txt"}, cwd, 120, 8000)
            )
            self.assertIn("file content here", result)

    def test_write_file_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = asyncio.get_event_loop().run_until_complete(
                _execute_builtin_tool(
                    "write_file",
                    {"path": "new.txt", "content": "new content"},
                    cwd, 120, 8000,
                )
            )
            self.assertIn("Written", result)
            self.assertEqual(
                (cwd / "new.txt").read_text(encoding="utf-8"),
                "new content",
            )

    def test_edit_file_replaces_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            test_file = cwd / "test.txt"
            test_file.write_text("hello world", encoding="utf-8")
            result = asyncio.get_event_loop().run_until_complete(
                _execute_builtin_tool(
                    "edit_file",
                    {"path": "test.txt", "old_text": "hello", "new_text": "goodbye"},
                    cwd, 120, 8000,
                )
            )
            self.assertIn("Edited", result)
            self.assertEqual(
                test_file.read_text(encoding="utf-8"),
                "goodbye world",
            )

    def test_list_files_lists_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / "a.py").touch()
            (cwd / "b.txt").touch()
            result = asyncio.get_event_loop().run_until_complete(
                _execute_builtin_tool("list_files", {}, cwd, 120, 8000)
            )
            self.assertIn("a.py", result)
            self.assertIn("b.txt", result)
