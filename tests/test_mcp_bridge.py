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
