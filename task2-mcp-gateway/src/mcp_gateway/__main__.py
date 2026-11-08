"""Run the gateway: `uv run mcp-gateway`. Settings come from environment variables."""

import logging
import signal
import sys

import anyio
import httpx2 as httpx
import uvicorn

from mcp_gateway.app import build_app
from mcp_gateway.config import ConfigurationError, GatewaySettings


async def serve(settings: GatewaySettings) -> None:
    # One pooled client for the gateway's lifetime: connections to the downstream server
    # are reused across requests.
    timeout = httpx.Timeout(settings.downstream_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as downstream:
        config = uvicorn.Config(
            build_app(settings, downstream), host=settings.host, port=settings.port, log_config=None
        )
        await uvicorn.Server(config).serve()


def _raise_keyboard_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def main() -> None:
    try:
        settings = GatewaySettings.from_env()
    except ConfigurationError as exc:
        sys.exit(f"mcp-gateway: {exc}")
    logging.basicConfig(
        stream=sys.stderr,
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if sys.platform == "win32":
        # Uvicorn stops on Ctrl+Break and then raises the signal again. The default Windows
        # action ends the process at once; treating it like Ctrl+C lets exit hooks run.
        signal.signal(signal.SIGBREAK, _raise_keyboard_interrupt)
    try:
        anyio.run(serve, settings)
    except KeyboardInterrupt:
        pass  # Normal shutdown: uvicorn has already closed every connection.


if __name__ == "__main__":
    main()
