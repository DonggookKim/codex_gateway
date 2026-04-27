from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_gateway.mcp_bridge import McpBridge, parse_mcp_config


class ParseMcpConfigTest(unittest.TestCase):
    def test_returns_empty_dict_when_no_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = parse_mcp_config(Path(tmp) / "nonexistent" / ".codex")
            self.assertEqual(result, {})

    def test_parses_stdio_server_from_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = Path(tmp) / ".codex"
            codex_dir.mkdir()
            config_path = codex_dir / "config.toml"
            config_path.write_text(
                '[mcp_servers.myserver]\n'
                'command = "node"\n'
                'args = ["server.js"]\n',
                encoding="utf-8",
            )
            result = parse_mcp_config(codex_dir)
            self.assertIn("myserver", result)
            self.assertEqual(result["myserver"]["command"], "node")
            self.assertEqual(result["myserver"]["args"], ["server.js"])

    def test_parses_sse_server_from_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = Path(tmp) / ".codex"
            codex_dir.mkdir()
            config_path = codex_dir / "config.toml"
            config_path.write_text(
                '[mcp_servers.remote]\n'
                'url = "http://localhost:3000/sse"\n',
                encoding="utf-8",
            )
            result = parse_mcp_config(codex_dir)
            self.assertIn("remote", result)
            self.assertEqual(result["remote"]["url"], "http://localhost:3000/sse")


class McpBridgeToolNamingTest(unittest.TestCase):
    def test_builtin_collision_adds_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = Path(tmp) / ".codex"
            codex_dir.mkdir()
            bridge = McpBridge(codex_dir)
        raw_tools = [
            {"type": "function", "function": {"name": "shell", "description": "remote shell", "parameters": {}}},
            {"type": "function", "function": {"name": "custom_tool", "description": "custom", "parameters": {}}},
        ]
        renamed = bridge._apply_collision_prefix("myserver", raw_tools)
        names = [t["function"]["name"] for t in renamed]
        self.assertIn("myserver__shell", names)
        self.assertIn("custom_tool", names)
        self.assertNotIn("shell", names)


class McpBridgeRuntimeTest(unittest.TestCase):
    """Cover async lifecycle behavior using fake clients (no real MCP)."""

    def _make_bridge_with_servers(self, tmp: Path):
        codex_dir = tmp / ".codex"
        codex_dir.mkdir()
        bridge = McpBridge(codex_dir)
        return bridge

    def test_resolve_prefixed_tool_returns_server_and_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._make_bridge_with_servers(Path(tmp))
            bridge._clients["myserver"] = {"session": object(), "transport": object()}
            bridge._server_tools["myserver"] = [
                {"type": "function", "function": {"name": "shell", "description": "", "parameters": {}}},
            ]
            resolved = bridge.resolve_server_and_tool("myserver__shell")
            self.assertEqual(resolved, ("myserver", "shell"))

    def test_resolve_unprefixed_tool_falls_back_to_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._make_bridge_with_servers(Path(tmp))
            bridge._clients["s1"] = {"session": object(), "transport": object()}
            bridge._server_tools["s1"] = [
                {"type": "function", "function": {"name": "custom", "description": "", "parameters": {}}},
            ]
            resolved = bridge.resolve_server_and_tool("custom")
            self.assertEqual(resolved, ("s1", "custom"))

    def test_resolve_unknown_tool_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._make_bridge_with_servers(Path(tmp))
            self.assertIsNone(bridge.resolve_server_and_tool("nope"))

    def test_call_tool_returns_error_for_unknown_server(self) -> None:
        import asyncio
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._make_bridge_with_servers(Path(tmp))
            result = asyncio.get_event_loop().run_until_complete(
                bridge.call_tool("absent", "x", {})
            )
            self.assertIn("not connected", result)

    def test_close_clears_state_even_when_session_close_fails(self) -> None:
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._make_bridge_with_servers(Path(tmp))
            bad_session = MagicMock()
            bad_session.__aexit__ = AsyncMock(side_effect=RuntimeError("nope"))
            bad_transport = MagicMock()
            bad_transport.__aexit__ = AsyncMock(side_effect=RuntimeError("nope"))
            bridge._clients["s1"] = {
                "session": bad_session,
                "transport": bad_transport,
            }
            bridge._server_tools["s1"] = []
            asyncio.get_event_loop().run_until_complete(bridge.close())
            self.assertEqual(bridge._clients, {})
            self.assertEqual(bridge._server_tools, {})

    def test_get_tools_combines_servers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._make_bridge_with_servers(Path(tmp))
            bridge._server_tools["s1"] = [
                {"type": "function", "function": {"name": "alpha", "description": "", "parameters": {}}},
            ]
            bridge._server_tools["s2"] = [
                {"type": "function", "function": {"name": "beta", "description": "", "parameters": {}}},
            ]
            tools = bridge.get_tools()
            names = {t["function"]["name"] for t in tools}
            self.assertEqual(names, {"alpha", "beta"})


class ParseMcpConfigErrorTest(unittest.TestCase):
    def test_returns_empty_dict_for_invalid_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = Path(tmp) / ".codex"
            codex_dir.mkdir()
            (codex_dir / "config.toml").write_text(
                "[mcp_servers.bad\nbroken =", encoding="utf-8",
            )
            self.assertEqual(parse_mcp_config(codex_dir), {})

    def test_ignores_non_dict_server_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = Path(tmp) / ".codex"
            codex_dir.mkdir()
            (codex_dir / "config.toml").write_text(
                "[mcp_servers]\nflat = \"value\"\n[mcp_servers.real]\ncommand = \"x\"\n",
                encoding="utf-8",
            )
            result = parse_mcp_config(codex_dir)
            self.assertNotIn("flat", result)
            self.assertIn("real", result)
