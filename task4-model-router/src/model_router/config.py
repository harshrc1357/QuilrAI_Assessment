"""Gateway settings, read and checked once at startup."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

DEFAULT_TOKENS_PER_MINUTE = 50_000
DEFAULT_PRIMARY_TIMEOUT_MS = 3_000
DEFAULT_SECONDARY_TIMEOUT_MS = 30_000
DEFAULT_DB_PATH = "router.db"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_PRIMARY_PORT = 8101
DEFAULT_SECONDARY_PORT = 8102
MAX_PORT = 65535
MIN_TENANT_KEY_LENGTH = 16
MS_PER_SECOND = 1000


class ConfigurationError(ValueError):
    """A setting is missing or has a bad value. The message names the setting."""


@dataclass(frozen=True)
class ProviderSettings:
    name: str
    url: str
    model: str
    api_key: str | None
    timeout_seconds: float


@dataclass(frozen=True)
class GatewaySettings:
    tenant_keys: frozenset[str]
    primary: ProviderSettings
    secondary: ProviderSettings
    db_path: Path
    tokens_per_minute: int = DEFAULT_TOKENS_PER_MINUTE
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "GatewaySettings":
        return cls(
            tenant_keys=_tenant_keys(env.get("ROUTER_TENANT_KEYS", "")),
            primary=_provider(env, "primary", DEFAULT_PRIMARY_PORT, DEFAULT_PRIMARY_TIMEOUT_MS),
            secondary=_provider(
                env, "secondary", DEFAULT_SECONDARY_PORT, DEFAULT_SECONDARY_TIMEOUT_MS
            ),
            db_path=Path(env.get("ROUTER_DB_PATH", DEFAULT_DB_PATH)),
            tokens_per_minute=_positive_int(
                env, "ROUTER_TOKENS_PER_MINUTE", DEFAULT_TOKENS_PER_MINUTE
            ),
            host=env.get("ROUTER_HOST", DEFAULT_HOST),
            port=_port(env),
        )


def _tenant_keys(value: str) -> frozenset[str]:
    keys = frozenset(key.strip() for key in value.split(",") if key.strip())
    if not keys:
        raise ConfigurationError("ROUTER_TENANT_KEYS must list at least one tenant API key")
    if any(len(key) < MIN_TENANT_KEY_LENGTH for key in keys):
        raise ConfigurationError(
            f"ROUTER_TENANT_KEYS: each key must be at least {MIN_TENANT_KEY_LENGTH} characters"
        )
    return keys


def _provider(
    env: Mapping[str, str], name: str, default_port: int, default_timeout_ms: int
) -> ProviderSettings:
    prefix = f"ROUTER_{name.upper()}"
    url = env.get(f"{prefix}_URL", f"http://127.0.0.1:{default_port}/v1/chat/completions")
    return ProviderSettings(
        name=name,
        url=_url(f"{prefix}_URL", url),
        model=env.get(f"{prefix}_MODEL", f"{name}-model"),
        api_key=env.get(f"{prefix}_API_KEY") or None,
        timeout_seconds=_positive_int(env, f"{prefix}_TIMEOUT_MS", default_timeout_ms)
        / MS_PER_SECOND,
    )


def _url(name: str, value: str) -> str:
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as exc:
        raise ConfigurationError(f"{name} is not a valid URL") from exc
    if url.scheme not in ("http", "https") or not url.host:
        raise ConfigurationError(f"{name} must be an http(s) URL with a host")
    return value


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    number = _int(env, name, default)
    if number <= 0:
        raise ConfigurationError(f"{name} must be greater than 0")
    return number


def _port(env: Mapping[str, str]) -> int:
    port = _int(env, "ROUTER_PORT", DEFAULT_PORT)
    if not 1 <= port <= MAX_PORT:
        raise ConfigurationError(f"ROUTER_PORT must be between 1 and {MAX_PORT}")
    return port
