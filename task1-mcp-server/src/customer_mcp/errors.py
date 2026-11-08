"""Error taxonomy, following the MCP spec's two error channels:

- Protocol errors (JSON-RPC `error` object): unknown tool, invalid arguments, server faults.
- Tool execution errors (`CallToolResult.isError = true`): business failures the model can
  read and act on, such as an unknown customer.

Nothing the caller chose (argument values, unexpected key names) is copied into error data or
logs, so a hostile client cannot reflect payloads or forge log lines.
"""

from collections.abc import Sequence
from typing import TypedDict

from mcp.shared.exceptions import MCPError
from mcp_types import INTERNAL_ERROR, INVALID_PARAMS
from pydantic import ValidationError

UNEXPECTED_FIELD = "<unexpected field>"


class FieldError(TypedDict):
    field: str
    message: str
    type: str


class ToolExecutionError(Exception):
    """A business failure. `str(exc)` is shown to the client; `log_label` is safe to log."""

    log_label = "tool_execution_error"


class CustomerNotFoundError(ToolExecutionError):
    log_label = "customer_not_found"

    def __init__(self, customer_id: str) -> None:
        super().__init__(f"Customer {customer_id} not found")


def field_errors(exc: ValidationError) -> list[FieldError]:
    return [
        FieldError(
            field=UNEXPECTED_FIELD if err["type"] == "extra_forbidden" else _field_path(err["loc"]),
            message=err["msg"],
            type=err["type"],
        )
        for err in exc.errors(include_url=False, include_input=False, include_context=False)
    ]


def field_names(errors: Sequence[FieldError]) -> list[str]:
    return sorted({error["field"] for error in errors})


def invalid_params(tool_name: str, errors: Sequence[FieldError]) -> MCPError:
    return MCPError(
        code=INVALID_PARAMS,
        message=f"Invalid arguments for tool '{tool_name}'",
        data={"tool": tool_name, "errors": list(errors)},
    )


def unknown_tool(tool_name: str) -> MCPError:
    # Message format taken from the MCP specification's own example.
    return MCPError(code=INVALID_PARAMS, message=f"Unknown tool: {tool_name}")


def internal_error() -> MCPError:
    return MCPError(code=INTERNAL_ERROR, message="Internal server error")


def _field_path(loc: Sequence[int | str]) -> str:
    return ".".join(str(part) for part in loc) or "<root>"
