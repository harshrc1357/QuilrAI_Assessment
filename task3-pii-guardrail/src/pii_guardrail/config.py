"""Gateway settings, read and checked once at startup."""

import os
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

DEFAULT_PROVIDER_URL = "http://127.0.0.1:8002/v1/chat/completions"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_PORT = 65535


class ConfigurationError(ValueError):
    """A setting is missing or has a bad value. The message names the setting."""


@dataclass(frozen=True)
class GatewaySettings:
    provider_url: str = DEFAULT_PROVIDER_URL
    provider_api_key: str | None = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    provider_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "GatewaySettings":
        return cls(
            provider_url=_provider_url(env.get("GUARDRAIL_PROVIDER_URL", DEFAULT_PROVIDER_URL)),
            provider_api_key=env.get("GUARDRAIL_PROVIDER_API_KEY") or None,
            host=env.get("GUARDRAIL_HOST", DEFAULT_HOST),
            port=_port(env.get("GUARDRAIL_PORT", str(DEFAULT_PORT))),
            provider_timeout_seconds=_timeout(
                env.get("GUARDRAIL_PROVIDER_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
            ),
        )


def _provider_url(value: str) -> str:
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as exc:
        raise ConfigurationError("GUARDRAIL_PROVIDER_URL is not a valid URL") from exc
    if url.scheme not in ("http", "https") or not url.host:
        raise ConfigurationError("GUARDRAIL_PROVIDER_URL must be an http(s) URL with a host")
    return value


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise ConfigurationError("GUARDRAIL_PORT must be an integer") from exc
    if not 1 <= port <= MAX_PORT:
        raise ConfigurationError(f"GUARDRAIL_PORT must be between 1 and {MAX_PORT}")
    return port


def _timeout(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise ConfigurationError("GUARDRAIL_PROVIDER_TIMEOUT_SECONDS must be a number") from exc
    if not seconds > 0:  # also refuses NaN
        raise ConfigurationError("GUARDRAIL_PROVIDER_TIMEOUT_SECONDS must be greater than 0")
    return seconds
