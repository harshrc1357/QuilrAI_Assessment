"""Entry point: `python -m customer_mcp` or the `customer-mcp` console script.

Two layers keep stdout pure JSON-RPC:
1. `stdio_server()` claims file descriptor 1 and points it at stderr while serving, which
   catches fd-level writes (C extensions, child processes).
2. Inside that block, Python-level `sys.stdout` is redirected to stderr. Without this, a stray
   print() sits in the stdout buffer and is flushed onto the wire at exit, after the SDK
   restores fd 1. It must happen after the claim, which checks that sys.stdout is fd 1.
"""

import logging
import os
import sys
from contextlib import redirect_stdout

import anyio
from mcp.server.stdio import stdio_server

from customer_mcp.logging_config import LOG_LEVEL_ENV_VAR, configure_logging
from customer_mcp.repository import CustomerRepository, InMemoryCustomerRepository
from customer_mcp.server import build_server
from customer_mcp.transport import ProtocolErrorReplyingStream

logger = logging.getLogger("customer_mcp")


async def serve_stdio(repository: CustomerRepository) -> None:
    server = build_server(repository)
    sys.stdout.flush()
    async with stdio_server() as (read_stream, write_stream):
        with redirect_stdout(sys.stderr):
            logger.info("customer-mcp serving on stdio")
            await server.run(
                ProtocolErrorReplyingStream(read_stream, write_stream),
                write_stream,
                server.create_initialization_options(),
            )
    logger.info("stdin closed; customer-mcp shutting down")


def main(repository: CustomerRepository | None = None) -> None:
    configure_logging(os.environ.get(LOG_LEVEL_ENV_VAR))
    try:
        anyio.run(serve_stdio, repository or InMemoryCustomerRepository.with_sample_data())
    except KeyboardInterrupt:
        logger.info("Interrupted; customer-mcp shutting down")


if __name__ == "__main__":
    main()
