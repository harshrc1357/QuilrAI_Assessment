"""The downstream mock MCP server: a real MCP server (official SDK, Streamable HTTP).

It has one normal tool and one `admin_` tool. It does no authorization of its own, so the
gateway is the only thing that stops a viewer from running `admin_reset_key`.
"""

import os

from mcp.server.mcpserver import MCPServer

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8001

server = MCPServer("mock-downstream-mcp")


@server.tool()
def get_weather(city: str) -> str:
    """Return a canned weather report for a city."""
    return f"Sunny in {city}"


@server.tool()
def admin_reset_key(key_id: str) -> str:
    """Rotate an API key. Admin only: the gateway enforces this, not this server."""
    return f"Key {key_id} was reset"


def main() -> None:
    server.run(
        "streamable-http",
        host=os.environ.get("MOCK_MCP_HOST", DEFAULT_HOST),
        port=int(os.environ.get("MOCK_MCP_PORT", DEFAULT_PORT)),
    )


if __name__ == "__main__":
    main()
