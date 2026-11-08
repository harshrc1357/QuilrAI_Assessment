"""Seam 3: the real gateway process in front of a real mock provider process, over real HTTP."""

import json
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

PROVIDER_KEY = "sk-e2e-provider-key"
CHUNK_CHARS = 4
CHUNK_DELAY_SECONDS = 0.1
STARTUP_TIMEOUT_SECONDS = 20.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.05
IS_WINDOWS = sys.platform == "win32"

PII_MESSAGE = "Mail jane.doe@example.com, SSN 123-45-6789, card 4111 1111 1111 1111. Thanks!"
REDACTED_MESSAGE = "Mail [REDACTED], SSN [REDACTED], card [REDACTED]. Thanks!"


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


@pytest.fixture(scope="module")
def gateway(tmp_path_factory) -> Iterator[Service]:
    logs = tmp_path_factory.mktemp("logs")
    provider_port, gateway_port = _free_port(), _free_port()
    provider = _start(
        "pii_guardrail.mock_provider",
        {
            "MOCK_PROVIDER_PORT": str(provider_port),
            "MOCK_PROVIDER_API_KEY": PROVIDER_KEY,
            "MOCK_CHUNK_CHARS": str(CHUNK_CHARS),
            "MOCK_CHUNK_DELAY_SECONDS": str(CHUNK_DELAY_SECONDS),
        },
        provider_port,
        logs / "provider.log",
    )
    try:  # The provider must stop even when the gateway fails to start.
        gateway = _start(
            "pii_guardrail",
            {
                "GUARDRAIL_PORT": str(gateway_port),
                "GUARDRAIL_PROVIDER_URL": f"http://127.0.0.1:{provider_port}/v1/chat/completions",
                "GUARDRAIL_PROVIDER_API_KEY": PROVIDER_KEY,
            },
            gateway_port,
            logs / "gateway.log",
        )
        try:
            yield gateway
        finally:
            _stop(gateway)
    finally:
        _stop(provider)


def stream_completion(gateway: Service, message: str) -> list[tuple[float, str]]:
    """Return (seconds since the request, delta text) for each content delta received."""
    body = {"model": "mock", "stream": True, "messages": [{"role": "user", "content": message}]}
    received: list[tuple[float, str]] = []
    started = time.monotonic()
    url = f"http://127.0.0.1:{gateway.port}/v1/chat/completions"
    with httpx.stream("POST", url, json=body, timeout=30) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("data: {"):
                for choice in json.loads(line.removeprefix("data: "))["choices"]:
                    text = choice["delta"].get("content", "")
                    if text:
                        received.append((time.monotonic() - started, text))
    return received


def test_the_stream_reaches_the_client_with_pii_redacted(gateway: Service) -> None:
    received = stream_completion(gateway, PII_MESSAGE)
    assert "".join(text for _, text in received) == REDACTED_MESSAGE


def test_the_first_token_arrives_long_before_the_provider_finishes(gateway: Service) -> None:
    message = "Hello there, this reply is long and has no secrets in it at all. " * 2
    provider_seconds = (len(message) // CHUNK_CHARS) * CHUNK_DELAY_SECONDS  # about 3.2 s
    received = stream_completion(gateway, message)
    first_token_at, last_token_at = received[0][0], received[-1][0]
    assert "".join(text for _, text in received) == message
    assert first_token_at < provider_seconds / 4
    assert last_token_at > provider_seconds * 3 / 4
    assert len(received) > len(message) // CHUNK_CHARS // 2  # sent on as it arrives


def test_the_gateway_log_holds_no_message_text(gateway: Service) -> None:
    stream_completion(gateway, PII_MESSAGE)
    log = gateway.log()
    assert "stream started" in log
    assert "jane.doe" not in log and "6789" not in log and "Mail" not in log


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({"GUARDRAIL_PROVIDER_URL": "ftp://x/v1"}, "GUARDRAIL_PROVIDER_URL must be an http(s)"),
        ({"GUARDRAIL_PROVIDER_URL": "http://[::1/v1"}, "GUARDRAIL_PROVIDER_URL is not a valid"),
        ({"GUARDRAIL_PORT": "eighty"}, "GUARDRAIL_PORT must be an integer"),
        ({"GUARDRAIL_PORT": "70000"}, "GUARDRAIL_PORT must be between 1 and 65535"),
        (
            {"GUARDRAIL_PROVIDER_TIMEOUT_SECONDS": "x"},
            "GUARDRAIL_PROVIDER_TIMEOUT_SECONDS must be a",
        ),
        (
            {"GUARDRAIL_PROVIDER_TIMEOUT_SECONDS": "0"},
            "GUARDRAIL_PROVIDER_TIMEOUT_SECONDS must be gr",
        ),
    ],
)
def test_the_gateway_refuses_to_start_with_a_bad_setting(env: dict[str, str], reason: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "pii_guardrail"],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=STARTUP_TIMEOUT_SECONDS,
    )
    assert completed.returncode == 1
    assert completed.stderr.startswith(f"pii-guardrail: {reason}")
    assert "Traceback" not in completed.stderr
