from __future__ import annotations

import asyncio
import logging
import tomllib
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

# Built-in tool names that may collide with MCP server tool names.
# NOTE: When Task 4 creates direct_runner.py with its own BUILTIN_TOOL_NAMES,
# Task 5 will consolidate them. For now this is the authoritative definition.
BUILTIN_TOOL_NAMES = {"shell", "read_file", "write_file", "edit_file", "list_files"}

MCP_TOOL_CALL_TIMEOUT = 30


def parse_mcp_config(codex_dir: Path) -> dict[str, dict[str, Any]]:
    config_path = codex_dir / "config.toml"
    if not config_path.exists():
        return {}
    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except Exception:
        LOGGER.warning("Failed to parse MCP config at %s", config_path)
        return {}
    servers = data.get("mcp_servers", {})
    if not isinstance(servers, dict):
        return {}
    return {
        name: config
        for name, config in servers.items()
        if isinstance(config, dict)
    }


def _mcp_tool_to_ollama_tool(tool: Any) -> dict:
    input_schema = tool.inputSchema if hasattr(tool, "inputSchema") else {}
    if isinstance(input_schema, dict):
        parameters = input_schema
    else:
        parameters = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": parameters,
        },
    }


class McpBridge:
    def __init__(self, codex_home_path: Path) -> None:
        codex_dir = codex_home_path
        if codex_dir.name != ".codex":
            codex_dir = codex_home_path / ".codex"
        self._config = parse_mcp_config(codex_dir)
        self._clients: dict[str, Any] = {}
        self._server_tools: dict[str, list[dict]] = {}
        self._builtin_names = set(BUILTIN_TOOL_NAMES)

    async def connect(self) -> None:
        if not self._config:
            return
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            from mcp.client.sse import sse_client
        except ImportError:
            LOGGER.warning("mcp package not installed; skipping MCP servers")
            return

        for server_name, server_config in self._config.items():
            try:
                if "url" in server_config:
                    await self._connect_sse_server(
                        server_name, server_config,
                        ClientSession, sse_client,
                    )
                elif "command" in server_config:
                    await self._connect_stdio_server(
                        server_name, server_config,
                        ClientSession, StdioServerParameters, stdio_client,
                    )
                else:
                    LOGGER.warning(
                        "MCP server %s has no 'command' or 'url' key", server_name,
                    )
            except Exception:
                LOGGER.warning(
                    "Failed to connect to MCP server %s", server_name,
                    exc_info=True,
                )

    async def _connect_stdio_server(
        self,
        server_name: str,
        server_config: dict,
        ClientSession: Any,
        StdioServerParameters: Any,
        stdio_client: Any,
    ) -> None:
        command = server_config.get("command")
        if not command:
            return
        args = server_config.get("args", [])
        env = server_config.get("env")

        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=env,
        )
        transport = stdio_client(server_params)
        read_stream, write_stream = await transport.__aenter__()
        session = ClientSession(read_stream, write_stream)
        await session.__aenter__()
        await session.initialize()

        tools_result = await session.list_tools()
        ollama_tools = [_mcp_tool_to_ollama_tool(t) for t in tools_result.tools]

        self._clients[server_name] = {
            "session": session,
            "transport": transport,
        }
        self._server_tools[server_name] = ollama_tools
        LOGGER.info(
            "MCP server %s connected (stdio), %d tools discovered",
            server_name,
            len(ollama_tools),
        )

    async def _connect_sse_server(
        self,
        server_name: str,
        server_config: dict,
        ClientSession: Any,
        sse_client: Any,
    ) -> None:
        url = server_config.get("url")
        if not url:
            return
        headers = server_config.get("headers", {})

        transport = sse_client(url=url, headers=headers)
        read_stream, write_stream = await transport.__aenter__()
        session = ClientSession(read_stream, write_stream)
        await session.__aenter__()
        await session.initialize()

        tools_result = await session.list_tools()
        ollama_tools = [_mcp_tool_to_ollama_tool(t) for t in tools_result.tools]

        self._clients[server_name] = {
            "session": session,
            "transport": transport,
        }
        self._server_tools[server_name] = ollama_tools
        LOGGER.info(
            "MCP server %s connected (sse), %d tools discovered",
            server_name,
            len(ollama_tools),
        )

    def get_tools(self) -> list[dict]:
        all_tools: list[dict] = []
        for server_name, tools in self._server_tools.items():
            renamed = self._apply_collision_prefix(server_name, tools)
            all_tools.extend(renamed)
        return all_tools

    def _apply_collision_prefix(
        self,
        server_name: str,
        tools: list[dict],
    ) -> list[dict]:
        result: list[dict] = []
        for tool in tools:
            func = tool.get("function", {})
            name = func.get("name", "")
            if name in self._builtin_names:
                prefixed_name = f"{server_name}__{name}"
                func_copy = {**func, "name": prefixed_name}
                result.append({**tool, "function": func_copy})
            else:
                result.append(tool)
        return result

    def resolve_server_and_tool(
        self,
        tool_name: str,
    ) -> tuple[str, str] | None:
        if "__" in tool_name:
            server_name, _, original_name = tool_name.partition("__")
            if server_name in self._clients:
                return server_name, original_name
        for server_name, tools in self._server_tools.items():
            for tool in tools:
                func = tool.get("function", {})
                if func.get("name") == tool_name:
                    return server_name, tool_name
        return None

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict,
    ) -> str:
        client_info = self._clients.get(server_name)
        if client_info is None:
            return f"MCP server '{server_name}' not connected"
        session = client_info["session"]
        try:
            result = await asyncio.wait_for(
                session.call_tool(tool_name, arguments),
                timeout=MCP_TOOL_CALL_TIMEOUT,
            )
            if hasattr(result, "content") and result.content:
                parts = []
                for item in result.content:
                    if hasattr(item, "text"):
                        parts.append(item.text)
                return "\n".join(parts) if parts else str(result)
            return str(result)
        except asyncio.TimeoutError:
            return f"MCP tool timed out after {MCP_TOOL_CALL_TIMEOUT}s"
        except Exception as exc:
            return f"MCP tool error: {exc}"

    async def close(self) -> None:
        for server_name, client_info in self._clients.items():
            try:
                session = client_info.get("session")
                if session is not None:
                    await session.__aexit__(None, None, None)
                transport = client_info.get("transport")
                if transport is not None:
                    await transport.__aexit__(None, None, None)
            except Exception:
                LOGGER.warning(
                    "Error closing MCP server %s", server_name,
                    exc_info=True,
                )
        self._clients.clear()
        self._server_tools.clear()
