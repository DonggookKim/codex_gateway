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
    run_direct_ollama,
)
from codex_gateway.state import GatewayState, LastRunSummary


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


class AgentLoopTest(unittest.TestCase):
    def _make_config(self, tmp_dir: str) -> MagicMock:
        config = MagicMock()
        config.codex_cwd = Path(tmp_dir)
        config.ollama_host = "http://localhost:11434"
        config.direct_max_iterations = 3
        config.direct_context_chars = 90000
        config.direct_shell_timeout = 10
        config.direct_tool_result_max_chars = 8000
        config.direct_system_prompt = "You are a coding assistant."
        config.status_text_max_chars = 700
        config.state_root = Path(tmp_dir) / "state"
        config.state_root.mkdir(parents=True, exist_ok=True)
        return config

    @patch("codex_gateway.direct_runner.ollama.AsyncClient")
    def test_simple_text_response(self, mock_client_cls) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self._make_config(tmp)
            state_file = Path(tmp) / "state" / "gateway_state.json"
            state = GatewayState(state_file)

            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_response = MagicMock()
            mock_response.message = MagicMock()
            mock_response.message.content = "Hello! I'm here to help."
            mock_response.message.tool_calls = None
            mock_client.chat = AsyncMock(return_value=mock_response)

            summary = asyncio.get_event_loop().run_until_complete(
                run_direct_ollama(
                    state=state,
                    config=config,
                    requester_user_id=123,
                    requester_name="testuser",
                    prompt="hello",
                    model_profile="qwen3:8b",
                )
            )

            self.assertIsInstance(summary, LastRunSummary)
            self.assertEqual(summary.exit_code, 0)
            self.assertIn("Hello", summary.assistant_response_excerpt)

    @patch("codex_gateway.direct_runner.ollama.AsyncClient")
    def test_tool_call_then_text_response(self, mock_client_cls) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self._make_config(tmp)
            state_file = Path(tmp) / "state" / "gateway_state.json"
            state = GatewayState(state_file)

            # Create a file for the shell tool to find
            (Path(tmp) / "test.py").write_text("print('hi')", encoding="utf-8")

            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client

            # First call: model requests a tool call
            tool_response = MagicMock()
            tool_response.message = MagicMock()
            tool_response.message.content = ""
            tool_call = MagicMock()
            tool_call.function = MagicMock()
            tool_call.function.name = "shell"
            tool_call.function.arguments = {"command": "ls"}
            tool_response.message.tool_calls = [tool_call]

            # Second call: model returns text
            text_response = MagicMock()
            text_response.message = MagicMock()
            text_response.message.content = "Found test.py in the directory."
            text_response.message.tool_calls = None

            mock_client.chat = AsyncMock(side_effect=[tool_response, text_response])

            summary = asyncio.get_event_loop().run_until_complete(
                run_direct_ollama(
                    state=state,
                    config=config,
                    requester_user_id=123,
                    requester_name="testuser",
                    prompt="list files",
                    model_profile="qwen3:8b",
                )
            )

            self.assertEqual(summary.exit_code, 0)
            self.assertEqual(mock_client.chat.await_count, 2)

    @patch("codex_gateway.direct_runner.ollama.AsyncClient")
    def test_max_iterations_stops_loop(self, mock_client_cls) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self._make_config(tmp)
            config.direct_max_iterations = 2
            state_file = Path(tmp) / "state" / "gateway_state.json"
            state = GatewayState(state_file)

            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client

            # Model always requests tool calls
            tool_response = MagicMock()
            tool_response.message = MagicMock()
            tool_response.message.content = "Let me check..."
            tool_call = MagicMock()
            tool_call.function = MagicMock()
            tool_call.function.name = "shell"
            tool_call.function.arguments = {"command": "echo hi"}
            tool_response.message.tool_calls = [tool_call]

            mock_client.chat = AsyncMock(return_value=tool_response)

            summary = asyncio.get_event_loop().run_until_complete(
                run_direct_ollama(
                    state=state,
                    config=config,
                    requester_user_id=123,
                    requester_name="testuser",
                    prompt="loop test",
                    model_profile="qwen3:8b",
                )
            )

            self.assertEqual(summary.exit_code, 0)
            # Should have called chat exactly max_iterations times
            self.assertEqual(mock_client.chat.await_count, 2)
