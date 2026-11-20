"""One client for several MCP servers.

The hub starts every server over stdio, lists its tools and exposes them under
a namespaced name (`game__get_incident`, `desk__search_tickets`), so both the
model and the trace always show which system a call went to.

A hub lives for one question. Opening a new hub re-reads the tool lists, which
is why a tool added by `synapse build --discover` is usable on the very next
question without restarting anything by hand.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .servers import ServerSpec, scrub

SEP = "__"


@dataclass
class Tool:
    namespace: str
    name: str
    description: str
    input_schema: dict

    @property
    def qualified(self) -> str:
        return f"{self.namespace}{SEP}{self.name}"


class McpHub:
    def __init__(self, specs: list[ServerSpec]):
        self.specs = specs
        self.tools: list[Tool] = []
        self._sessions: dict[str, ClientSession] = {}
        self._logs: dict[str, object] = {}
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> "McpHub":
        # One retry: a hosted runner started through npx occasionally closes its first connection.
        for attempt in (1, 2):
            await self._stack.__aenter__()
            try:
                for spec in self.specs:
                    await self._connect(spec)
                return self
            except BaseException as exc:
                # A server failed to start: stop the ones already running, in this same task.
                output = self._server_output()
                await self._stack.aclose()
                self._stack = AsyncExitStack()
                self._sessions.clear()
                self._logs.clear()
                self.tools.clear()
                if attempt == 2 or not isinstance(exc, Exception):
                    if output and isinstance(exc, Exception):
                        exc.add_note("MCP server output:\n" + output)
                    raise
        return self

    def _server_output(self, limit: int = 1500) -> str:
        """The last lines the servers wrote to stderr, for diagnosing a failed start."""
        parts = []
        for namespace, log in self._logs.items():
            try:
                log.seek(0)
                text = log.read().strip()
            except (OSError, ValueError):
                continue
            if text:
                parts.append(f"[{namespace}] {scrub(text)[-limit:]}")
        return "\n".join(parts)

    async def _connect(self, spec: ServerSpec) -> None:
        if spec.command and spec.args and spec.args[0].endswith(".py") and not os.path.exists(spec.args[0]):
            raise SystemExit(f"MCP server file missing: {spec.args[0]}\nRun: python scripts/build_mcp.py")
        # Only the variables the server needs. The MCP SDK adds a minimal system
        # environment (PATH and friends) itself, so model and account keys that
        # live in this process are not handed to the child.
        params = StdioServerParameters(command=spec.command, args=spec.args, env=dict(spec.env))
        # The generated servers log every request to stderr. Keep that out of the console
        # (set MCP_SERVER_LOGS=1 to see it) but hold on to it in case the start fails.
        if os.environ.get("MCP_SERVER_LOGS"):
            errlog = sys.stderr
        else:
            errlog = self._stack.enter_context(tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace"))
            self._logs[spec.namespace] = errlog
        read, write = await self._stack.enter_async_context(stdio_client(params, errlog=errlog))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._sessions[spec.namespace] = session
        listed = await session.list_tools()
        for tool in listed.tools:
            self.tools.append(
                Tool(
                    namespace=spec.namespace,
                    name=tool.name,
                    description=(tool.description or "").strip(),
                    input_schema=tool.inputSchema or {"type": "object", "properties": {}},
                )
            )

    async def __aexit__(self, *exc) -> None:
        await self._stack.__aexit__(*exc)

    def names(self) -> list[str]:
        return [tool.qualified for tool in self.tools]

    def find(self, qualified: str) -> Tool | None:
        for tool in self.tools:
            if tool.qualified == qualified:
                return tool
        return None

    async def call(self, qualified: str, arguments: dict | None = None) -> tuple[str, bool]:
        """Call a tool. Returns (text, is_error). Never raises for tool-level errors."""
        tool = self.find(qualified)
        if tool is None:
            return f"Unknown tool {qualified!r}. Available: {', '.join(self.names())}", True
        try:
            result = await self._sessions[tool.namespace].call_tool(tool.name, arguments or {})
        except Exception as exc:  # transport-level failure
            return f"{type(exc).__name__}: {exc}", True
        text = "\n".join(getattr(part, "text", "") for part in result.content if getattr(part, "text", ""))
        return text, bool(result.isError)

    async def call_json(self, qualified: str, arguments: dict | None = None) -> Any:
        """Call a tool and parse the JSON answer; raises RuntimeError on a tool error."""
        text, is_error = await self.call(qualified, arguments)
        if is_error:
            raise RuntimeError(f"{qualified} failed: {text}")
        return json.loads(text)
