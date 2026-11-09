"""Run the gateway: `uv run model-router`. Settings come from environment variables."""

import logging
import signal
import sqlite3
import sys

import anyio
import httpx
import uvicorn

from model_router.app import WINDOW_SECONDS, build_app
from model_router.config import ConfigurationError, GatewaySettings
from model_router.limiter import TokenRateLimiter


async def serve(settings: GatewaySettings, limiter: TokenRateLimiter) -> None:
    # One pooled client for the gateway's lifetime. Each provider call also has its own
    # total deadline (see router.py); this timeout is only a backstop.
    backstop = max(settings.primary.timeout_seconds, settings.secondary.timeout_seconds)
    async with httpx.AsyncClient(timeout=httpx.Timeout(backstop)) as client:
        config = uvicorn.Config(
            build_app(settings, client, limiter),
            host=settings.host,
            port=settings.port,
            log_config=None,
        )
        await uvicorn.Server(config).serve()


def _raise_keyboard_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def main() -> None:
    try:
        settings = GatewaySettings.from_env()
    except ConfigurationError as exc:
        sys.exit(f"model-router: {exc}")
    try:
        limiter = TokenRateLimiter(
            settings.db_path, limit=settings.tokens_per_minute, window_seconds=WINDOW_SECONDS
        )
    except sqlite3.Error:
        sys.exit("model-router: ROUTER_DB_PATH cannot be opened as a SQLite database")
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if sys.platform == "win32":
        # Uvicorn stops on Ctrl+Break and then raises the signal again. The default Windows
        # action ends the process at once; treating it like Ctrl+C lets exit hooks run.
        signal.signal(signal.SIGBREAK, _raise_keyboard_interrupt)
    try:
        anyio.run(serve, settings, limiter)
    except KeyboardInterrupt:
        pass  # Normal shutdown: uvicorn has already closed every connection.


if __name__ == "__main__":
    main()
