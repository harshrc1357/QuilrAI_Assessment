"""E2e seam: the real gateway process in front of a real SDK MCP server process.

The official MCP client talks Streamable HTTP to the gateway, exactly as an AI agent would.
"""

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx2 as httpx
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

pytestmark = [pytest.mark.anyio, pytest.mark.e2e]

SECRET = "e2e-secret-that-is-long-enough-for-hs256!!"
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


@pytest.fixture(scope="module")
def gateway(tmp_path_factory) -> Iterator[Service]:
    logs = tmp_path_factory.mktemp("logs")
    downstream_port, gateway_port = _free_port(), _free_port()
    downstream = _start(
        "mcp_gateway.mock_server",
        {"MOCK_MCP_PORT": str(downstream_port)},
        downstream_port,
        logs / "downstream.log",
    )
    try:  # The mock server must stop even when the gateway fails to start.
        gateway = _start(
            "mcp_gateway",
            {
                "GATEWAY_PORT": str(gateway_port),
                "GATEWAY_DOWNSTREAM_URL": f"http://127.0.0.1:{downstream_port}/mcp",
                "GATEWAY_JWT_SECRET": SECRET,
            },
            gateway_port,
            logs / "gateway.log",
        )
        try:
            yield gateway
        finally:
            _stop(gateway)
    finally:
        _stop(downstream)


def token_from_cli(role: str) -> str:
    """Mints the token with the project's own `issue-token` command."""
    completed = subprocess.run(
        [sys.executable, "-m", "mcp_gateway.token_cli", "--role", role],
        env={**os.environ, "GATEWAY_JWT_SECRET": SECRET},
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


@asynccontextmanager
async def mcp_client(gateway: Service, role: str, mode: str = "auto"):
    token = token_from_cli(role)
    http_client = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"})
    async with http_client:
        url = f"http://127.0.0.1:{gateway.port}/mcp"
        transport = streamable_http_client(url, http_client=http_client)
        async with Client(transport, mode=mode) as client:
            yield client


async def test_tools_list_passes_through_the_gateway(gateway):
    async with mcp_client(gateway, "viewer") as client:
        tools = await client.list_tools()

    assert sorted(tool.name for tool in tools.tools) == ["admin_reset_key", "get_weather"]


async def test_a_viewer_can_call_a_normal_tool(gateway):
    async with mcp_client(gateway, "viewer") as client:
        result = await client.call_tool("get_weather", {"city": "Pune"})

    assert result.is_error is False
    assert result.content[0].text == "Sunny in Pune"


async def test_a_viewer_calling_an_admin_tool_gets_unauthorized_tool_call(gateway):
    async with mcp_client(gateway, "viewer") as client:
        with pytest.raises(MCPError) as caught:
            await client.call_tool("admin_reset_key", {"key_id": "k-1"})

    assert caught.value.error.code == -32001
    assert caught.value.error.message == "Unauthorized Tool Call"
    assert "denied role=viewer method='tools/call' tool='admin_reset_key'" in gateway.log()


async def test_an_admin_can_call_an_admin_tool(gateway):
    async with mcp_client(gateway, "admin") as client:
        result = await client.call_tool("admin_reset_key", {"key_id": "k-1"})

    assert result.is_error is False
    assert result.content[0].text == "Key k-1 was reset"


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        pytest.param({"GATEWAY_JWT_SECRET": ""}, "GATEWAY_JWT_SECRET must be", id="no-secret"),
        pytest.param({"GATEWAY_JWT_SECRET": "short"}, "GATEWAY_JWT_SECRET must be", id="short"),
        pytest.param({"GATEWAY_DOWNSTREAM_URL": "ftp://x/mcp"}, "GATEWAY_DOWNSTREAM_URL", id="ftp"),
        pytest.param({"GATEWAY_DOWNSTREAM_URL": "not a url"}, "GATEWAY_DOWNSTREAM_URL", id="url"),
        pytest.param(
            {"GATEWAY_DOWNSTREAM_URL": "http://[::1/mcp"}, "GATEWAY_DOWNSTREAM_URL", id="bad-host"
        ),
        pytest.param({"GATEWAY_PORT": "http"}, "GATEWAY_PORT", id="port-not-int"),
        pytest.param({"GATEWAY_PORT": "70000"}, "GATEWAY_PORT", id="port-out-of-range"),
        pytest.param({"GATEWAY_LOG_LEVEL": "LOUD"}, "GATEWAY_LOG_LEVEL", id="log-level"),
        pytest.param({"GATEWAY_MAX_BODY_BYTES": "0"}, "GATEWAY_MAX_BODY_BYTES", id="body-limit"),
        pytest.param(
            {"GATEWAY_DOWNSTREAM_TIMEOUT_SECONDS": "-1"},
            "GATEWAY_DOWNSTREAM_TIMEOUT_SECONDS",
            id="timeout",
        ),
    ],
)
def test_the_gateway_refuses_to_start_with_a_bad_setting(env, reason):
    completed = subprocess.run(
        [sys.executable, "-m", "mcp_gateway"],
        env={**os.environ, "GATEWAY_JWT_SECRET": SECRET, **env},
        capture_output=True,
        text=True,
        timeout=STARTUP_TIMEOUT_SECONDS,
    )

    assert completed.returncode == 1
    # One clean line that names the setting, not a traceback.
    assert completed.stderr.startswith(f"mcp-gateway: {reason}")
    assert "Traceback" not in completed.stderr
    assert completed.stdout == ""


async def test_a_session_based_client_works_through_the_gateway(gateway):
    # "legacy" mode uses the initialize handshake and a session: the client opens the GET
    # event stream and sends DELETE to end the session. Both must pass through the gateway.
    async with mcp_client(gateway, "viewer", mode="legacy") as client:
        result = await client.call_tool("get_weather", {"city": "Oslo"})
        with pytest.raises(MCPError) as caught:
            await client.call_tool("admin_reset_key", {"key_id": "k-2"})

    assert result.content[0].text == "Sunny in Oslo"
    assert caught.value.error.code == -32001
    log = gateway.log()
    assert '"GET /mcp HTTP/1.1" 200' in log
    assert '"DELETE /mcp HTTP/1.1" 200' in log
