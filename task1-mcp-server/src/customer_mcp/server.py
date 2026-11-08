"""Wires the tool registry into a low-level MCP `Server`.

The low-level server is used on purpose: the high-level `MCPServer` turns argument validation
failures into `isError` results, but the task requires JSON-RPC error codes for them.
"""

import json
import logging
from collections.abc import Mapping
from importlib.metadata import version
from typing import Any

import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from pydantic import ValidationError

from customer_mcp.errors import (
    ToolExecutionError,
    field_errors,
    field_names,
    internal_error,
    invalid_params,
    unknown_tool,
)
from customer_mcp.repository import CustomerRepository
from customer_mcp.tools import TOOL_SPECS, ToolSpec

DISTRIBUTION_NAME = "customer-mcp"
SERVER_NAME = DISTRIBUTION_NAME
SERVER_INSTRUCTIONS = "Customer support tools: look up customer records and issue refunds."

logger = logging.getLogger(__name__)


def build_server(repository: CustomerRepository) -> Server:
    specs = {spec.name: spec for spec in TOOL_SPECS}
    mcp_tools = [spec.to_mcp_tool() for spec in TOOL_SPECS]

    async def list_tools(
        _ctx: ServerRequestContext, _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=mcp_tools)

    async def call_tool(
        _ctx: ServerRequestContext, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        spec = specs.get(params.name)
        if spec is None:
            logger.warning("Rejected call to an unknown tool")
            raise unknown_tool(params.name)
        return _execute(spec, params.arguments or {}, repository)

    return Server(
        SERVER_NAME,
        version=version(DISTRIBUTION_NAME),
        instructions=SERVER_INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def _execute(
    spec: ToolSpec, arguments: Mapping[str, Any], repository: CustomerRepository
) -> types.CallToolResult:
    try:
        validated = spec.parse_arguments(arguments)
    except ValidationError as exc:
        errors = field_errors(exc)
        logger.info("Tool %r rejected invalid arguments %s", spec.name, field_names(errors))
        raise invalid_params(spec.name, errors) from None

    try:
        output = spec.handler(validated, repository)
    except ToolExecutionError as exc:
        logger.info("Tool %r failed: %s", spec.name, exc.log_label)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=str(exc))], is_error=True
        )
    except Exception:
        logger.exception("Tool %r raised an unexpected exception", spec.name)
        raise internal_error() from None

    payload = output.model_dump(mode="json")
    logger.info("Tool %r succeeded", spec.name)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload))],
        structured_content=payload,
    )
