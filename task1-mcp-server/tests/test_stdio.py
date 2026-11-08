"""Seam 3: the real stdio transport. The server runs as a subprocess and the test speaks raw
newline-delimited JSON-RPC, exactly like an MCP host (Claude Desktop, an IDE, an agent)."""

import ast
import json
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, INVALID_REQUEST, METHOD_NOT_FOUND, PARSE_ERROR

pytestmark = pytest.mark.e2e

RESPONSE_TIMEOUT_SECONDS = 15
POLL_INTERVAL_SECONDS = 0.2
PROTOCOL_VERSION = "2025-11-25"
SOURCE_DIR = Path(__file__).resolve().parents[1] / "src" / "customer_mcp"
# The entry point owns the transport and redirects sys.stdout; nothing else may touch it.
STDOUT_OWNER_MODULES = {"__main__.py"}

NOISY_SERVER_SCRIPT = """
from customer_mcp.__main__ import main
from customer_mcp.repository import InMemoryCustomerRepository

class NoisyRepository(InMemoryCustomerRepository):
    def get_customer(self, customer_id):
        print("STRAY-PRINT-FROM-HANDLER")
        return super().get_customer(customer_id)

main(NoisyRepository.with_sample_data())
"""


def _jsonrpc_message(
    method: str, params: dict[str, Any] | None = None, request_id: int | None = None
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        **({"id": request_id} if request_id is not None else {}),
        "method": method,
        **({"params": params} if params is not None else {}),
    }


class StdioSession:
    """Drives one server subprocess and records every byte it writes to stdout."""

    def __init__(self, command: list[str]) -> None:
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
        )
        self.stdout_lines: list[str] = []
        self._stderr_lines: list[str] = []
        self._responses: queue.Queue[str] = queue.Queue()
        self._next_id = 0
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        # Drain stderr continuously so a chatty server never blocks on a full pipe.
        self._stderr_thread = threading.Thread(target=self._pump_stderr, daemon=True)
        self._stderr_thread.start()

    def _pump_stdout(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self.stdout_lines.append(line)
            self._responses.put(line)

    def _pump_stderr(self) -> None:
        assert self._process.stderr is not None
        for line in self._process.stderr:
            self._stderr_lines.append(line)

    def send_raw(self, line: str) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(line + "\n")
        self._process.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.send_raw(json.dumps(_jsonrpc_message(method, params)))

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        self.send_raw(json.dumps(_jsonrpc_message(method, params, request_id=self._next_id)))
        return self.wait_for(self._next_id)

    def error_replies_with_null_id(self) -> list[dict[str, Any]]:
        messages = [json.loads(line) for line in self.stdout_lines]
        return [m for m in messages if "error" in m and m.get("id") is None]

    def wait_for(self, request_id: int | None) -> dict[str, Any]:
        deadline = time.monotonic() + RESPONSE_TIMEOUT_SECONDS
        while True:
            try:
                line = self._responses.get(timeout=POLL_INTERVAL_SECONDS)
            except queue.Empty:
                if self._process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail(f"no response for id={request_id!r}; stderr:\n{self.stderr()}")
                continue
            message = json.loads(line)
            if message.get("id") == request_id:
                return message

    def initialize(self) -> dict[str, Any]:
        response = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "pytest-stdio-client", "version": "1.0"},
            },
        )
        self.notify("notifications/initialized")
        return response

    def close(self) -> int:
        assert self._process.stdin is not None
        if not self._process.stdin.closed:
            self._process.stdin.close()
        return self._process.wait(timeout=RESPONSE_TIMEOUT_SECONDS)

    def stderr(self) -> str:
        if self._process.poll() is None:
            self.close()
        self._stderr_thread.join(timeout=RESPONSE_TIMEOUT_SECONDS)
        return "".join(self._stderr_lines)

    def shutdown(self) -> None:
        """Close stdin so the server exits on its own (and flushes coverage); kill if stuck."""
        try:
            self.close()
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()


def _start(command: list[str]) -> Iterator[StdioSession]:
    session = StdioSession(command)
    try:
        yield session
    finally:
        session.shutdown()


@pytest.fixture
def server() -> Iterator[StdioSession]:
    yield from _start([sys.executable, "-m", "customer_mcp"])


@pytest.fixture
def noisy_server() -> Iterator[StdioSession]:
    yield from _start([sys.executable, "-c", NOISY_SERVER_SCRIPT])


def _assert_pure_jsonrpc(lines: list[str]) -> None:
    for number, line in enumerate(lines, start=1):
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            pytest.fail(f"stdout line {number} is not JSON (stray print/log?): {line!r}")
        assert message.get("jsonrpc") == "2.0", (
            f"stdout line {number} is not JSON-RPC 2.0: {line!r}"
        )
        assert ("result" in message) ^ ("error" in message) or "method" in message, (
            f"stdout line {number} is neither a response nor a notification: {line!r}"
        )


class TestStdioIsolation:
    def test_full_session_writes_only_jsonrpc_to_stdout(self, server: StdioSession) -> None:
        server.initialize()
        server.request("tools/list")
        server.request(
            "tools/call",
            {"name": "get_customer_record", "arguments": {"customer_id": "CUST-12345"}},
        )
        server.request(
            "tools/call",
            {
                "name": "trigger_refund",
                "arguments": {"customer_id": "CUST-1", "amount": -1, "reason": "no"},
            },
        )
        server.request(
            "tools/call",
            {"name": "get_customer_record", "arguments": {"customer_id": "CUST-99999"}},
        )

        exit_code = server.close()

        assert exit_code == 0
        assert server.stdout_lines, "server wrote nothing to stdout"
        _assert_pure_jsonrpc(server.stdout_lines)

    def test_logs_are_written_to_stderr(self, server: StdioSession) -> None:
        server.initialize()
        server.request(
            "tools/call",
            {
                "name": "trigger_refund",
                "arguments": {
                    "customer_id": "CUST-12345",
                    "amount": 5,
                    "reason": "Duplicate charge on card",
                },
            },
        )

        stderr = server.stderr()

        assert "Tool 'trigger_refund' succeeded" in stderr

    def test_logs_do_not_contain_caller_argument_values(self, server: StdioSession) -> None:
        server.initialize()
        server.request(
            "tools/call",
            {
                "name": "trigger_refund",
                "arguments": {
                    "customer_id": "CUST-12345",
                    "amount": 5,
                    "reason": "SECRET-REASON-TEXT-123",
                },
            },
        )

        assert "SECRET-REASON-TEXT-123" not in server.stderr()

    def test_logs_do_not_contain_the_unknown_customer_id(self, server: StdioSession) -> None:
        server.initialize()
        server.request(
            "tools/call",
            {"name": "get_customer_record", "arguments": {"customer_id": "CUST-77777"}},
        )

        assert "CUST-77777" not in server.stderr()

    def test_caller_chosen_field_names_are_not_echoed_or_logged(self, server: StdioSession) -> None:
        injected_key = "injected\nFAKE-LOG-LINE"
        server.initialize()

        response = server.request(
            "tools/call",
            {
                "name": "get_customer_record",
                "arguments": {"customer_id": "CUST-12345", injected_key: 1},
            },
        )

        assert response["error"]["code"] == INVALID_PARAMS
        assert "FAKE-LOG-LINE" not in json.dumps(response)
        assert "FAKE-LOG-LINE" not in server.stderr()

    def test_stray_print_in_a_handler_goes_to_stderr_not_the_wire(
        self, noisy_server: StdioSession
    ) -> None:
        noisy_server.initialize()
        response = noisy_server.request(
            "tools/call",
            {"name": "get_customer_record", "arguments": {"customer_id": "CUST-12345"}},
        )

        stderr = noisy_server.stderr()

        assert response["result"]["isError"] is False
        _assert_pure_jsonrpc(noisy_server.stdout_lines)
        assert "STRAY-PRINT-FROM-HANDLER" in stderr

    def test_source_has_no_direct_stdout_writes(self) -> None:
        offenders = []
        for path in sorted(SOURCE_DIR.rglob("*.py")):
            may_touch_stdout = path.name in STDOUT_OWNER_MODULES
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                is_print = (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "print"
                )
                is_stdout = (
                    not may_touch_stdout
                    and isinstance(node, ast.Attribute)
                    and node.attr == "stdout"
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "sys"
                )
                if is_print or is_stdout:
                    offenders.append(f"{path.name}:{node.lineno}")

        assert offenders == [], f"stdout is reserved for JSON-RPC; remove: {offenders}"


class TestWireProtocol:
    def test_initialize_reports_server_identity_and_tools_capability(
        self, server: StdioSession
    ) -> None:
        response = server.initialize()

        assert response["result"]["serverInfo"]["name"] == "customer-mcp"
        assert "tools" in response["result"]["capabilities"]

    def test_successful_call_returns_structured_content(self, server: StdioSession) -> None:
        server.initialize()

        response = server.request(
            "tools/call",
            {
                "name": "trigger_refund",
                "arguments": {
                    "customer_id": "CUST-12345",
                    "amount": 19.99,
                    "reason": "Late delivery refund",
                },
            },
        )

        result = response["result"]
        assert result["isError"] is False
        assert result["structuredContent"]["amount"] == 19.99
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]

    def test_invalid_arguments_return_jsonrpc_invalid_params_error(
        self, server: StdioSession
    ) -> None:
        server.initialize()

        response = server.request(
            "tools/call",
            {
                "name": "trigger_refund",
                "arguments": {
                    "customer_id": "CUST-12345",
                    "amount": "100",
                    "reason": "Late delivery refund",
                },
            },
        )

        assert "result" not in response
        assert response["error"]["code"] == INVALID_PARAMS
        assert response["error"]["data"]["errors"][0]["field"] == "amount"

    @pytest.mark.parametrize(
        "params",
        [
            pytest.param(
                {"name": "get_customer_record", "arguments": "CUST-12345"},
                id="arguments-is-a-string",
            ),
            pytest.param(
                {"name": "get_customer_record", "arguments": ["CUST-12345"]},
                id="arguments-is-a-list",
            ),
            pytest.param({"arguments": {}}, id="name-missing"),
            pytest.param({"name": 7, "arguments": {}}, id="name-not-a-string"),
            pytest.param(None, id="params-missing"),
        ],
    )
    def test_malformed_call_params_return_invalid_params(
        self, server: StdioSession, params: dict[str, Any] | None
    ) -> None:
        server.initialize()

        response = server.request("tools/call", params)

        assert response["error"]["code"] == INVALID_PARAMS

    def test_unknown_customer_returns_result_with_is_error(self, server: StdioSession) -> None:
        server.initialize()

        response = server.request(
            "tools/call",
            {"name": "get_customer_record", "arguments": {"customer_id": "CUST-99999"}},
        )

        assert response["result"]["isError"] is True

    def test_unknown_method_returns_method_not_found(self, server: StdioSession) -> None:
        server.initialize()

        response = server.request("resources/list")

        assert response["error"]["code"] == METHOD_NOT_FOUND

    def test_unknown_tool_returns_invalid_params(self, server: StdioSession) -> None:
        server.initialize()

        response = server.request("tools/call", {"name": "drop_tables", "arguments": {}})

        assert response["error"]["code"] == INVALID_PARAMS

    def test_malformed_json_returns_parse_error_and_server_keeps_serving(
        self, server: StdioSession
    ) -> None:
        server.initialize()

        server.send_raw("{this is not json")
        parse_error = server.wait_for(None)
        follow_up = server.request("tools/list")

        assert parse_error["error"]["code"] == PARSE_ERROR
        assert parse_error["id"] is None
        assert len(follow_up["result"]["tools"]) == 2
        _assert_pure_jsonrpc(server.stdout_lines)

    def test_blank_line_returns_parse_error(self, server: StdioSession) -> None:
        server.initialize()

        server.send_raw("")
        response = server.wait_for(None)

        assert response["error"]["code"] == PARSE_ERROR

    @pytest.mark.parametrize(
        "raw_message",
        [
            pytest.param('{"jsonrpc":"2.0","method":"tools/list"}', id="id-absent"),
            pytest.param('{"jsonrpc":"2.0","id":null,"method":"tools/list"}', id="id-null"),
            pytest.param('{"jsonrpc":"2.0","id":32.5,"method":"tools/list"}', id="id-float"),
            pytest.param('{"jsonrpc":"2.0","id":true,"method":"tools/list"}', id="id-boolean"),
            pytest.param('{"jsonrpc":"2.0","id":{"a":1},"method":"ping"}', id="id-object"),
        ],
    )
    def test_request_without_a_valid_id_returns_invalid_request(
        self, server: StdioSession, raw_message: str
    ) -> None:
        server.initialize()

        server.send_raw(raw_message)
        response = server.wait_for(None)
        follow_up = server.request("ping")

        assert response["error"]["code"] == INVALID_REQUEST
        assert response["id"] is None
        assert "result" in follow_up

    def test_genuine_notification_gets_no_reply(self, server: StdioSession) -> None:
        server.initialize()

        server.notify("notifications/cancelled", {"requestId": 999})
        server.request("ping")

        assert server.error_replies_with_null_id() == []

    @pytest.mark.parametrize(
        ("raw_message", "expected_id"),
        [
            pytest.param('{"jsonrpc":"2.0","id":41}', 41, id="no-method-or-result"),
            pytest.param('{"jsonrpc":"1.0","id":42,"method":"tools/list"}', 42, id="jsonrpc-1.0"),
            pytest.param('{"jsonrpc":"2.0","id":43,"method":7}', 43, id="method-not-a-string"),
            pytest.param('[{"jsonrpc":"2.0","id":44,"method":"tools/list"}]', None, id="batch"),
            pytest.param('"just a string"', None, id="json-scalar"),
        ],
    )
    def test_invalid_request_object_returns_invalid_request(
        self, server: StdioSession, raw_message: str, expected_id: int | None
    ) -> None:
        server.initialize()

        server.send_raw(raw_message)
        response = server.wait_for(expected_id)

        assert response["error"]["code"] == INVALID_REQUEST
        assert response["id"] == expected_id

    @pytest.mark.anyio
    async def test_official_sdk_client_works_over_stdio(self) -> None:
        params = StdioServerParameters(command=sys.executable, args=["-m", "customer_mcp"])

        async with Client(params) as client:
            tools = await client.list_tools()
            result = await client.call_tool("get_customer_record", {"customer_id": "CUST-00001"})
            with pytest.raises(MCPError) as invalid:
                await client.call_tool("get_customer_record", {"customer_id": "CUST-0001"})

        assert len(tools.tools) == 2
        assert result.structured_content["name"] == "Alan Turing"
        assert invalid.value.code == INVALID_PARAMS

    def test_server_exits_cleanly_when_stdin_closes(self, server: StdioSession) -> None:
        server.initialize()

        assert server.close() == 0
