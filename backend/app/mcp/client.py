"""Typed MCP client for the LedgerFlow tool gateway.

One client abstraction, two transports:

* **production** — streamable HTTP to ``settings.mcp_server_url``
  (``http://127.0.0.1:8000/mcp``, the MCP server mounted on our own
  FastAPI app);
* **tests** — an injected in-memory transport built from
  ``mcp.shared.memory.create_client_server_memory_streams`` (no sockets,
  no ports).

Sessions are deliberately **per operation**: every ``list_tools`` /
``call_tool`` opens a transport, initializes a :class:`ClientSession`,
does the work and tears everything down. There is no long-lived session
state to invalidate when the server restarts, nothing to close from the
FastAPI lifespan, and the gateway can safely cache the tool catalogue
separately. The local handshake costs a couple of milliseconds.

Error contract (typed end to end):

* transport/protocol failures → :class:`MCPError`;
* tool-level failures (``is_error``) → re-raised as the original
  LedgerFlow error class when the server message carries our
  ``<code>: <message>`` convention, else :class:`ServiceError`.
"""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.core.config import Settings, settings as app_settings
from app.core.errors import (
    MCPError,
    LedgerFlowError,
    ServiceError,
    error_for_code,
)

# Async context manager yielding a connected (read, write) stream pair.
TransportFactory = Callable[[], AsyncIterator[Any]]

# The MCP SDK prefixes tool errors: "Error executing tool <name>: <message>".
_TOOL_PREFIX_RE = re.compile(r"^Error executing tool \S+: ")


@dataclass(frozen=True)
class MCPToolInfo:
    """Everything the gateway needs to enforce policy over one MCP tool."""

    name: str
    description: str
    input_schema: dict[str, Any]
    # Fail closed: only an explicit read_only_hint=True counts as read-only.
    read_only_hint: bool = False


def _error_text(result: Any) -> str:
    for block in getattr(result, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return block.text
    return "tool failed without an error message"


def _raise_typed(tool: str, raw: str) -> None:
    """Map a server-side tool error back onto our typed error classes."""
    body = _TOOL_PREFIX_RE.sub("", (raw or "").strip())
    code, sep, message = body.partition(": ")
    error_cls = error_for_code(code) if sep else None
    if error_cls is not None:
        raise error_cls(message, details={"tool": tool, "server_code": code})
    raise ServiceError(raw, details={"tool": tool})


def _payload(result: Any) -> Any:
    """Extract the structured payload from a successful CallToolResult."""
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    if structured is not None:
        return structured
    for block in getattr(result, "content", None) or []:
        if getattr(block, "type", None) == "text":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return block.text
    return None


class MCPClient:
    """Typed ``list_tools`` / ``call_tool`` over a pluggable MCP transport."""

    def __init__(
        self,
        url: str | None = None,
        *,
        transport_factory: TransportFactory | None = None,
        http_client: Any = None,
        timeout: float | None = None,
        settings: Settings = app_settings,
    ) -> None:
        self.url = url if url is not None else settings.mcp_server_url
        self.timeout = timeout if timeout is not None else settings.mcp_timeout_seconds
        self._http_client = http_client
        self._transport_factory = (
            transport_factory if transport_factory is not None else self._http_transport
        )

    @asynccontextmanager
    async def _http_transport(self) -> AsyncIterator[Any]:
        kwargs: dict[str, Any] = {}
        if self._http_client is not None:
            kwargs["http_client"] = self._http_client
        async with streamable_http_client(self.url, **kwargs) as streams:
            yield streams

    @asynccontextmanager
    async def session(self) -> AsyncIterator[ClientSession]:
        """Open a fresh, initialized MCP session (connect + initialize)."""
        try:
            async with self._transport_factory() as (read_stream, write_stream):
                session = ClientSession(
                    read_stream, write_stream, read_timeout_seconds=self.timeout
                )
                async with session:
                    await session.initialize()
                    yield session
        except LedgerFlowError:
            raise
        except Exception as exc:
            raise MCPError(
                f"could not establish MCP session: {exc}",
                details={"url": self.url},
            ) from exc

    async def list_tools(self) -> list[MCPToolInfo]:
        """Fetch the server's tool catalogue (name, schema, annotations)."""
        try:
            async with self.session() as session:
                result = await session.list_tools()
        except LedgerFlowError:
            raise
        except Exception as exc:
            raise MCPError(
                f"failed to list MCP tools: {exc}", details={"url": self.url}
            ) from exc
        return [
            MCPToolInfo(
                name=tool.name,
                description=tool.description or "",
                input_schema=dict(tool.input_schema or {}),
                read_only_hint=bool(
                    tool.annotations and tool.annotations.read_only_hint
                ),
            )
            for tool in result.tools
        ]

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> Any:
        """Invoke one tool and return its structured payload.

        Raises a typed LedgerFlow error on failure: the original class
        when the server used our ``<code>: <message>`` convention, else
        :class:`ServiceError` (tool level) or :class:`MCPError`
        (transport level).
        """
        try:
            async with self.session() as session:
                result = await session.call_tool(name, dict(arguments or {}))
        except LedgerFlowError:
            raise
        except Exception as exc:
            raise MCPError(
                f"MCP tool call failed: {name}: {exc}",
                details={"tool": name, "url": self.url},
            ) from exc
        if getattr(result, "is_error", False):
            _raise_typed(name, _error_text(result))
        return _payload(result)
