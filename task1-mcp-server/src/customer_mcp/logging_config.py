"""Logging goes to stderr only. On the stdio transport, stdout carries JSON-RPC frames and
any other byte written there corrupts the protocol stream."""

import logging
import sys

LOG_LEVEL_ENV_VAR = "CUSTOMER_MCP_LOG_LEVEL"
DEFAULT_LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level_name: str | None) -> None:
    levels = logging.getLevelNamesMapping()
    level = levels.get((level_name or DEFAULT_LOG_LEVEL).upper())
    logging.basicConfig(
        stream=sys.stderr,
        level=level if level is not None else levels[DEFAULT_LOG_LEVEL],
        format=LOG_FORMAT,
        force=True,
    )
    if level is None:
        logging.getLogger(__name__).warning(
            "Unknown %s=%r; using %s", LOG_LEVEL_ENV_VAR, level_name, DEFAULT_LOG_LEVEL
        )
