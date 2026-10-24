"""Causa as an MCP server: the investigator's tools for Claude Desktop, Claude Code or any MCP client.

The client's own Claude plays the investigator (on the user's Claude plan, no API key here); every
figure still comes from Causa's engines. Tools are the agent's tools, unchanged: one definition, two
front ends. The warehouse connection opens on the first tool call, so the client's handshake never
waits on a cluster that is waking up.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from importlib.metadata import version
from typing import Any

import anyio
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

from causa.agent.agent import GUIDANCE
from causa.agent.tools import TOOL_DEFINITIONS, Toolbox

INSTRUCTIONS = (
    """\
Causa runs root-cause engines on the company's warehouse: lineage from the query log, data health \
detection, metric attribution, a verdict that separates data incidents from real business change \
(re-running the pipeline on repaired data where possible), and recall of everyone who consumed wrong \
numbers. Use these tools when someone asks why a business metric moved, whether a number can be \
trusted, or who saw wrong numbers. The data's "today" can differ from the calendar: call \
list_metrics first to learn it. A verdict can take a minute or more on a large warehouse.

"""
    + GUIDANCE
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def mcp_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The agent's strict schemas list every field as required (nullable when optional); MCP clients
    do better when optional fields are simply optional."""

    def nullable(prop: dict[str, Any]) -> bool:
        kind = prop.get("type")
        return isinstance(kind, list) and "null" in kind

    props = schema.get("properties", {})
    return schema | {"required": [k for k in schema.get("required", []) if not nullable(props.get(k, {}))]}


TOOLS = [
    Tool(
        name=t["name"],
        description=t["description"],
        input_schema=mcp_input_schema(t["input_schema"]),
        annotations=READ_ONLY,
    )
    for t in TOOL_DEFINITIONS
]


def create_server(toolbox: Toolbox | Callable[[], Toolbox]) -> Server:
    """An MCP server over a toolbox, or over a factory that builds one on first use."""
    lock = threading.Lock()  # one warehouse connection: serialise engine work
    box: list[Toolbox] = [toolbox] if isinstance(toolbox, Toolbox) else []

    def call(name: str, arguments: dict[str, Any]) -> str:
        with lock:
            if not box:
                box.append(toolbox())  # type: ignore[operator]
            return box[0].call(name, arguments)

    async def list_tools(ctx: Any, params: Any) -> ListToolsResult:
        return ListToolsResult(tools=TOOLS)

    async def call_tool(ctx: Any, params: Any) -> CallToolResult:
        # Omitted and null both mean "use the default".
        arguments = {k: v for k, v in (params.arguments or {}).items() if v is not None}
        try:
            text = await anyio.to_thread.run_sync(call, params.name, arguments)
        except KeyError as exc:
            return error(f"not found: {exc}")
        except Exception as exc:
            return error(f"{type(exc).__name__}: {exc}")
        return CallToolResult(content=[TextContent(type="text", text=text)])

    return Server(
        "causa",
        version=version("causa"),
        title="Causa",
        description="Root-cause engine for business metrics: real business change versus data incidents.",
        instructions=INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def error(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=message)], is_error=True)


def serve_stdio(toolbox: Toolbox | Callable[[], Toolbox]) -> None:
    server = create_server(toolbox)

    async def main() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(main)
