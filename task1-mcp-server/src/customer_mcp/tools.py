"""Tool registry: each tool is its input contract, output contract, MCP hints, and handler."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from mcp_types import Tool, ToolAnnotations
from pydantic import BaseModel

from customer_mcp.errors import CustomerNotFoundError
from customer_mcp.repository import CustomerRepository
from customer_mcp.schemas import (
    AMOUNT_MAX_DECIMAL_PLACES,
    MAX_REFUND_AMOUNT,
    REASON_MAX_LENGTH,
    REASON_MIN_LENGTH,
    CustomerRecord,
    GetCustomerRecordInput,
    RefundReceipt,
    TriggerRefundInput,
)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    annotations: ToolAnnotations
    # Receives an instance of `input_model`; returns an instance of `output_model`.
    handler: Callable[[Any, CustomerRepository], BaseModel]

    def parse_arguments(self, arguments: Mapping[str, Any]) -> BaseModel:
        """Raises `pydantic.ValidationError` when the arguments break the input contract."""
        return self.input_model.model_validate(arguments)

    def to_mcp_tool(self) -> Tool:
        return Tool(
            name=self.name,
            description=self.description,
            input_schema=self.input_model.model_json_schema(),
            output_schema=self.output_model.model_json_schema(),
            annotations=self.annotations,
        )


def _get_customer_record(
    args: GetCustomerRecordInput, repository: CustomerRepository
) -> CustomerRecord:
    customer = repository.get_customer(args.customer_id)
    if customer is None:
        raise CustomerNotFoundError(args.customer_id)
    return CustomerRecord.model_validate(customer)


def _trigger_refund(args: TriggerRefundInput, repository: CustomerRepository) -> RefundReceipt:
    if repository.get_customer(args.customer_id) is None:
        raise CustomerNotFoundError(args.customer_id)
    refund = repository.create_refund(args.customer_id, args.amount, args.reason)
    return RefundReceipt.model_validate(refund)


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="get_customer_record",
        description="Look up a customer record by its customer ID (format CUST-XXXXX).",
        input_model=GetCustomerRecordInput,
        output_model=CustomerRecord,
        annotations=ToolAnnotations(
            title="Get customer record", read_only_hint=True, open_world_hint=False
        ),
        handler=_get_customer_record,
    ),
    ToolSpec(
        name="trigger_refund",
        description=(
            f"Issue a refund to a customer. Amount must be positive, at most {MAX_REFUND_AMOUNT}, "
            f"with at most {AMOUNT_MAX_DECIMAL_PLACES} decimal places; reason must be "
            f"{REASON_MIN_LENGTH}-{REASON_MAX_LENGTH} characters."
        ),
        input_model=TriggerRefundInput,
        output_model=RefundReceipt,
        annotations=ToolAnnotations(
            title="Trigger refund",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
        handler=_trigger_refund,
    ),
)
