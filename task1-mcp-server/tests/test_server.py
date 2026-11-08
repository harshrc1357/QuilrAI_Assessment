"""Seam 2: the MCP protocol surface, through the official SDK client connected in-process."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp_types import INTERNAL_ERROR, INVALID_PARAMS

from customer_mcp.repository import Customer, InMemoryCustomerRepository, Refund
from customer_mcp.server import build_server

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

DB_ERROR = "connection to db://admin:hunter2@10.0.0.5 refused"
KNOWN_CUSTOMER = "CUST-12345"
UNKNOWN_CUSTOMER = "CUST-99999"
VALID_REASON = "Item arrived damaged"


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with Client(build_server(InMemoryCustomerRepository.with_sample_data())) as connected:
        yield connected


async def _expect_protocol_error(client: Client, tool: str, arguments: Any) -> MCPError:
    with pytest.raises(MCPError) as exc_info:
        await client.call_tool(tool, arguments)
    return exc_info.value


def _invalid_fields(error: MCPError) -> set[str]:
    return {item["field"] for item in error.data["errors"]}


class TestToolsList:
    async def test_lists_exactly_the_two_tools(self, client: Client) -> None:
        result = await client.list_tools()

        assert sorted(tool.name for tool in result.tools) == [
            "get_customer_record",
            "trigger_refund",
        ]

    async def test_advertises_the_validation_rules_in_the_input_schema(
        self, client: Client
    ) -> None:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

        refund_schema = tools["trigger_refund"].input_schema
        assert refund_schema["type"] == "object"
        assert refund_schema["additionalProperties"] is False
        assert set(refund_schema["required"]) == {"customer_id", "amount", "reason"}
        assert refund_schema["properties"]["customer_id"]["pattern"] == "^CUST-[0-9]{5}$"
        assert refund_schema["properties"]["amount"]["exclusiveMinimum"] == 0
        assert refund_schema["properties"]["amount"]["maximum"] == 1_000_000
        assert refund_schema["properties"]["reason"]["minLength"] == 10
        assert refund_schema["properties"]["reason"]["maxLength"] == 500

    async def test_marks_lookup_read_only_and_refund_not_idempotent(self, client: Client) -> None:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

        assert tools["get_customer_record"].annotations.read_only_hint is True
        assert tools["trigger_refund"].annotations.read_only_hint is False
        assert tools["trigger_refund"].annotations.idempotent_hint is False


class TestGetCustomerRecord:
    async def test_returns_structured_record_for_known_customer(self, client: Client) -> None:
        result = await client.call_tool("get_customer_record", {"customer_id": KNOWN_CUSTOMER})

        assert result.is_error is False
        assert result.structured_content["customer_id"] == KNOWN_CUSTOMER
        assert result.structured_content["name"] == "Ada Lovelace"

    async def test_unknown_customer_is_a_tool_execution_error(self, client: Client) -> None:
        result = await client.call_tool("get_customer_record", {"customer_id": UNKNOWN_CUSTOMER})

        assert result.is_error is True
        assert UNKNOWN_CUSTOMER in result.content[0].text

    @pytest.mark.parametrize(
        "arguments",
        [
            pytest.param({"customer_id": "CUST-1"}, id="bad-format"),
            pytest.param({"customer_id": 12345}, id="wrong-type"),
            pytest.param({}, id="missing-field"),
            pytest.param({"customer_id": KNOWN_CUSTOMER, "extra": 1}, id="extra-field"),
        ],
    )
    async def test_invalid_input_is_jsonrpc_invalid_params(
        self, client: Client, arguments: dict[str, Any]
    ) -> None:
        error = await _expect_protocol_error(client, "get_customer_record", arguments)

        assert error.code == INVALID_PARAMS, (
            f"expected {INVALID_PARAMS}, got {error.code}: {error.message}"
        )
        assert error.data["tool"] == "get_customer_record"

    async def test_missing_arguments_object_is_invalid_params(self, client: Client) -> None:
        error = await _expect_protocol_error(client, "get_customer_record", None)

        assert error.code == INVALID_PARAMS

    async def test_error_data_does_not_echo_the_rejected_value(self, client: Client) -> None:
        secret_like = "CUST-<script>alert(1)</script>"

        error = await _expect_protocol_error(
            client, "get_customer_record", {"customer_id": secret_like}
        )

        assert secret_like not in str(error.data)
        assert _invalid_fields(error) == {"customer_id"}

    async def test_unexpected_field_is_reported_without_echoing_its_name(
        self, client: Client
    ) -> None:
        error = await _expect_protocol_error(
            client,
            "get_customer_record",
            {"customer_id": KNOWN_CUSTOMER, "<img src=x onerror=alert(1)>": 1},
        )

        assert error.code == INVALID_PARAMS
        assert _invalid_fields(error) == {"<unexpected field>"}
        assert "onerror" not in str(error.data)


class TestTriggerRefund:
    async def test_returns_receipt_for_valid_refund(self, client: Client) -> None:
        result = await client.call_tool(
            "trigger_refund",
            {"customer_id": KNOWN_CUSTOMER, "amount": 25.5, "reason": VALID_REASON},
        )

        assert result.is_error is False
        receipt = result.structured_content
        assert receipt["customer_id"] == KNOWN_CUSTOMER
        assert receipt["amount"] == 25.5
        assert receipt["reason"] == VALID_REASON
        assert receipt["status"] == "accepted"
        assert receipt["refund_id"].startswith("RF-")

    async def test_each_refund_gets_a_unique_id(self, client: Client) -> None:
        arguments = {"customer_id": KNOWN_CUSTOMER, "amount": 10, "reason": VALID_REASON}

        first = await client.call_tool("trigger_refund", arguments)
        second = await client.call_tool("trigger_refund", arguments)

        assert first.structured_content["refund_id"] != second.structured_content["refund_id"]

    async def test_refund_for_unknown_customer_is_a_tool_execution_error(
        self, client: Client
    ) -> None:
        result = await client.call_tool(
            "trigger_refund",
            {"customer_id": UNKNOWN_CUSTOMER, "amount": 10, "reason": VALID_REASON},
        )

        assert result.is_error is True
        assert UNKNOWN_CUSTOMER in result.content[0].text

    async def test_reports_every_invalid_field_in_error_data(self, client: Client) -> None:
        error = await _expect_protocol_error(
            client, "trigger_refund", {"customer_id": "CUST-1", "amount": -1, "reason": "short"}
        )

        assert error.code == INVALID_PARAMS
        assert _invalid_fields(error) == {"customer_id", "amount", "reason"}

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("amount", 0),
            ("amount", "25.50"),
            ("amount", 10.005),
            ("reason", "          "),
            ("reason", "x" * 501),
        ],
    )
    async def test_rejects_boundary_violations_with_invalid_params(
        self, client: Client, field: str, value: Any
    ) -> None:
        arguments = {"customer_id": KNOWN_CUSTOMER, "amount": 10, "reason": VALID_REASON}

        error = await _expect_protocol_error(client, "trigger_refund", {**arguments, field: value})

        assert error.code == INVALID_PARAMS
        assert _invalid_fields(error) == {field}


class TestProtocolErrors:
    async def test_unknown_tool_is_invalid_params(self, client: Client) -> None:
        error = await _expect_protocol_error(client, "delete_customer", {"customer_id": "CUST-1"})

        assert error.code == INVALID_PARAMS
        assert "delete_customer" in error.message


class _UnreachableDatabase:
    def get_customer(self, customer_id: str) -> Customer | None:
        raise RuntimeError(DB_ERROR)

    def create_refund(self, customer_id: str, amount: float, reason: str) -> Refund:
        raise RuntimeError(DB_ERROR)


class _RefundWriteFails(InMemoryCustomerRepository):
    def create_refund(self, customer_id: str, amount: float, reason: str) -> Refund:
        raise RuntimeError(DB_ERROR)


class TestInternalErrors:
    @pytest.mark.parametrize(
        ("repository", "tool", "arguments"),
        [
            pytest.param(
                _UnreachableDatabase(),
                "get_customer_record",
                {"customer_id": KNOWN_CUSTOMER},
                id="lookup-read-fails",
            ),
            pytest.param(
                _RefundWriteFails.with_sample_data(),
                "trigger_refund",
                {"customer_id": KNOWN_CUSTOMER, "amount": 1, "reason": VALID_REASON},
                id="refund-write-fails",
            ),
        ],
    )
    async def test_unexpected_failure_is_internal_error_without_details(
        self, repository: Any, tool: str, arguments: dict[str, Any]
    ) -> None:
        async with Client(build_server(repository)) as broken_client:
            error = await _expect_protocol_error(broken_client, tool, arguments)

        assert error.code == INTERNAL_ERROR
        assert "hunter2" not in error.message
        assert "hunter2" not in str(error.data)
