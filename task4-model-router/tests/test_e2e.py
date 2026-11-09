"""Seam 3: the real gateway process with two real mock provider processes, over real HTTP."""

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.e2e

TENANT_KEY = "e2e-tenant-key-0123456789"
PRIMARY_DEADLINE_SECONDS = 3.0  # the gateway's default primary timeout: 3000 ms
MOCK_SLOW_SECONDS = 6.0
STARTUP_TIMEOUT_SECONDS = 20.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.05
IS_WINDOWS = sys.platform == "win32"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class Service:
    process: subprocess.Popen[bytes]
    port: int
    log_path: Path

    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")


def _start(module: str, env: dict[str, str], port: int, log_path: Path) -> Service:
    log_file = log_path.open("wb")
    process = subprocess.Popen(
        [sys.executable, "-m", module],
        env={**os.environ, **env},
        stdout=log_file,
        stderr=subprocess.STDOUT,
        # Lets the test send CTRL_BREAK so the process exits cleanly (and writes coverage).
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0,
    )
    log_file.close()
    service = Service(process, port, log_path)
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"{module} exited early:\n{service.log()}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=POLL_INTERVAL_SECONDS).close()
            return service
        except OSError:
            time.sleep(POLL_INTERVAL_SECONDS)
    _stop(service)
    pytest.fail(f"{module} did not listen on port {port}:\n{service.log()}")


def _stop(service: Service) -> None:
    service.process.send_signal(signal.CTRL_BREAK_EVENT if IS_WINDOWS else signal.SIGINT)
    try:
        service.process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        service.process.kill()
        service.process.wait()


@dataclass
class Providers:
    primary: Service
    secondary: Service

    def env(self) -> dict[str, str]:
        return {
            "ROUTER_PRIMARY_URL": f"http://127.0.0.1:{self.primary.port}/v1/chat/completions",
            "ROUTER_SECONDARY_URL": f"http://127.0.0.1:{self.secondary.port}/v1/chat/completions",
        }


@pytest.fixture(scope="module")
def providers(tmp_path_factory) -> Iterator[Providers]:
    logs = tmp_path_factory.mktemp("provider-logs")
    started: list[Service] = []
    try:  # Every started provider stops, even when a later one fails to start.
        for name in ("primary", "secondary"):
            port = _free_port()
            env = {"MOCK_NAME": name, "MOCK_PORT": str(port),
                   "MOCK_SLOW_SECONDS": str(MOCK_SLOW_SECONDS)}  # fmt: skip
            started.append(_start("model_router.mock_provider", env, port, logs / f"{name}.log"))
        yield Providers(*started)
    finally:
        for service in started:
            _stop(service)


def start_gateway(providers: Providers, db_path: Path, **env: str) -> Service:
    port = _free_port()
    settings = {
        **providers.env(),
        "ROUTER_TENANT_KEYS": TENANT_KEY,
        "ROUTER_PORT": str(port),
        "ROUTER_DB_PATH": str(db_path),
        **env,
    }
    return _start("model_router", settings, port, db_path.with_suffix(".log"))


@pytest.fixture(scope="module")
def gateway(providers: Providers, tmp_path_factory) -> Iterator[Service]:
    service = start_gateway(providers, tmp_path_factory.mktemp("gateway") / "router.db")
    try:
        yield service
    finally:
        _stop(service)


def complete(gateway: Service, text: str, max_tokens: int = 100) -> httpx.Response:
    body = {"model": "any", "messages": [{"role": "user", "content": text}],
            "max_tokens": max_tokens}  # fmt: skip
    return httpx.post(
        f"http://127.0.0.1:{gateway.port}/v1/chat/completions",
        json=body,
        headers={"authorization": f"Bearer {TENANT_KEY}"},
        timeout=30,
    )


def test_the_primary_model_answers(gateway: Service) -> None:
    response = complete(gateway, "Hello")
    assert response.status_code == 200
    assert response.headers["x-gateway-provider"] == "primary"
    assert response.json()["choices"][0]["message"]["content"] == (
        "Hello from the primary model (primary-model)"
    )


def test_a_429_from_the_primary_fails_over_to_the_secondary(gateway: Service) -> None:
    response = complete(gateway, "Hello [primary:429]")
    assert response.status_code == 200
    assert response.headers["x-gateway-provider"] == "secondary"
    assert "secondary-model" in response.json()["choices"][0]["message"]["content"]


def test_a_primary_slower_than_3000_ms_fails_over_at_the_deadline(gateway: Service) -> None:
    started = time.monotonic()
    response = complete(gateway, "Hello [primary:slow]")
    elapsed = time.monotonic() - started
    assert response.headers["x-gateway-provider"] == "secondary"
    assert PRIMARY_DEADLINE_SECONDS <= elapsed < MOCK_SLOW_SECONDS - 1


def test_when_both_providers_fail_the_client_gets_a_clean_error(gateway: Service) -> None:
    response = complete(gateway, "Hello [primary:500] [secondary:429]")
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "upstream_unavailable"
    assert set(error) == {"code", "message", "request_id"}
    assert "127.0.0.1" not in response.text


def test_the_token_window_survives_a_gateway_restart(providers: Providers, tmp_path: Path) -> None:
    db_path = tmp_path / "router.db"
    first = start_gateway(providers, db_path, ROUTER_TOKENS_PER_MINUTE="1000")
    try:
        assert complete(first, "Hello", max_tokens=900).status_code == 200
    finally:
        _stop(first)
    restarted = start_gateway(providers, db_path, ROUTER_TOKENS_PER_MINUTE="1000")
    try:
        response = complete(restarted, "Hello", max_tokens=900)
    finally:
        _stop(restarted)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limit_exceeded"
    assert TENANT_KEY.encode() not in db_path.read_bytes()


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({"ROUTER_TENANT_KEYS": ""}, "ROUTER_TENANT_KEYS must list at least one"),
        ({"ROUTER_TENANT_KEYS": "short"}, "ROUTER_TENANT_KEYS: each key must be at least 16"),
        ({"ROUTER_PRIMARY_URL": "ftp://x/v1"}, "ROUTER_PRIMARY_URL must be an http(s) URL"),
        ({"ROUTER_SECONDARY_URL": "http://[::1/v1"}, "ROUTER_SECONDARY_URL is not a valid URL"),
        ({"ROUTER_PRIMARY_TIMEOUT_MS": "3s"}, "ROUTER_PRIMARY_TIMEOUT_MS must be an integer"),
        ({"ROUTER_TOKENS_PER_MINUTE": "0"}, "ROUTER_TOKENS_PER_MINUTE must be greater than 0"),
        ({"ROUTER_PORT": "eighty"}, "ROUTER_PORT must be an integer"),
        ({"ROUTER_PORT": "70000"}, "ROUTER_PORT must be between 1 and 65535"),
        ({"ROUTER_DB_PATH": "."}, "ROUTER_DB_PATH cannot be opened as a SQLite database"),
    ],
)
def test_the_gateway_refuses_to_start_with_a_bad_setting(
    env: dict[str, str], reason: str, tmp_path: Path
) -> None:
    settings = {"ROUTER_TENANT_KEYS": TENANT_KEY, "ROUTER_DB_PATH": str(tmp_path / "r.db"), **env}
    completed = subprocess.run(
        [sys.executable, "-m", "model_router"],
        env={**os.environ, **settings},
        capture_output=True,
        text=True,
        timeout=STARTUP_TIMEOUT_SECONDS,
        cwd=tmp_path,
    )
    assert completed.returncode == 1
    assert completed.stderr.startswith(f"model-router: {reason}")
    assert "Traceback" not in completed.stderr
