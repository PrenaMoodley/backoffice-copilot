"""Ways for the agent to reach the MCP tools.

StdioBackend is the real thing: it starts the MCP server as a separate process
and talks MCP over stdio, exactly as Claude Code or Claude Desktop would.
InProcessBackend calls the same server object directly, for fast tests.
"""

from __future__ import annotations

import os
import sys
from contextlib import AsyncExitStack
from types import SimpleNamespace
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class StdioBackend:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._stack = AsyncExitStack()
        self.session: ClientSession | None = None

    async def start(self) -> StdioBackend:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "copilot.mcp_server"],
            env={**os.environ, "BACKOFFICE_DB": self.db_path},
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        return self

    async def close(self) -> None:
        await self._stack.aclose()

    async def list_tools(self) -> list[Any]:
        return (await self.session.list_tools()).tools

    async def call_tool(self, name: str, arguments: dict) -> Any:
        return await self.session.call_tool(name, arguments)


class InProcessBackend:
    def __init__(self):
        from . import mcp_server

        self.server = mcp_server.mcp

    async def list_tools(self) -> list[Any]:
        return await self.server.list_tools()

    async def call_tool(self, name: str, arguments: dict) -> Any:
        # Over a real MCP connection, tool errors arrive as results with is_error=True.
        # Called directly, the server raises instead, so convert to the same shape.
        from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

        try:
            return await self.server.call_tool(name, arguments)
        except ToolError as e:
            message = str(e) if not isinstance(e, UnexpectedToolError) else f"Error executing tool {name}"
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=message)], is_error=True)
