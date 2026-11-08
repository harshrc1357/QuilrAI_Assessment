"""Gateway settings, read and checked once, from environment variables, at startup."""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar

import httpx2 as httpx

DEFAULT_DOWNSTREAM_URL = "http://127.0.0.1:8001/mcp"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_MAX_BODY_BYTES = 1024 * 1024
DEFAULT_DOWNSTREAM_TIMEOUT_SECONDS = 30.0
# RFC 7518 section 3.2: an HS256 key must be at least as long as the hash output (32 bytes).
MIN_JWT_SECRET_BYTES = 32
MAX_PORT = 65535
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
DOWNSTREAM_URL_SCHEMES = ("http", "https")

Number = TypeVar("Number", int, float)


class ConfigurationError(Exception):
    """A setting is missing or invalid. The gateway refuses to start."""


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    downstream_url: str
    jwt_secret: str
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    log_level: str = DEFAULT_LOG_LEVEL
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    downstream_timeout_seconds: float = DEFAULT_DOWNSTREAM_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "GatewaySettings":
        """Raises `ConfigurationError` that names the first bad setting."""
        return cls(
            downstream_url=_downstream_url(env.get("GATEWAY_DOWNSTREAM_URL")),
            jwt_secret=_jwt_secret(env.get("GATEWAY_JWT_SECRET", "")),
            host=env.get("GATEWAY_HOST", DEFAULT_HOST),
            port=_number(env, "GATEWAY_PORT", DEFAULT_PORT, int, lambda p: 0 < p <= MAX_PORT),
            log_level=_log_level(env.get("GATEWAY_LOG_LEVEL", DEFAULT_LOG_LEVEL)),
            max_body_bytes=_number(
                env, "GATEWAY_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES, int, lambda n: n > 0
            ),
            downstream_timeout_seconds=_number(
                env,
                "GATEWAY_DOWNSTREAM_TIMEOUT_SECONDS",
                DEFAULT_DOWNSTREAM_TIMEOUT_SECONDS,
                float,
                lambda s: s > 0,
            ),
        )


def _jwt_secret(value: str) -> str:
    if len(value.encode()) < MIN_JWT_SECRET_BYTES:
        raise ConfigurationError(
            f"GATEWAY_JWT_SECRET must be set to at least {MIN_JWT_SECRET_BYTES} bytes"
        )
    return value


def _downstream_url(value: str | None) -> str:
    url = DEFAULT_DOWNSTREAM_URL if value is None else value
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        parsed = None
    if parsed is None or parsed.scheme not in DOWNSTREAM_URL_SCHEMES or not parsed.host:
        raise ConfigurationError("GATEWAY_DOWNSTREAM_URL must be an http:// or https:// URL")
    return url


def _log_level(value: str) -> str:
    level = value.upper()
    if level not in LOG_LEVELS:
        raise ConfigurationError(f"GATEWAY_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")
    return level


def _number(
    env: Mapping[str, str],
    name: str,
    default: Number,
    kind: Callable[[str], Number],
    is_valid: Callable[[Number], bool],
) -> Number:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = kind(raw)
    except ValueError:
        value = None
    if value is None or not is_valid(value):
        raise ConfigurationError(f"{name} has an invalid value")
    return value
