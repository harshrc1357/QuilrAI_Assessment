"""Strict input contracts and output contracts for the MCP tools. These models are the single
source of truth for both runtime validation and the JSON Schemas advertised in `tools/list`."""

import unicodedata
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

# [0-9], not \d: Pydantic's Rust regex engine treats \d as any Unicode digit.
CUSTOMER_ID_PATTERN = r"^CUST-[0-9]{5}$"
REASON_MIN_LENGTH = 10
REASON_MAX_LENGTH = 500
AMOUNT_MAX_DECIMAL_PLACES = 2
MAX_REFUND_AMOUNT = 1_000_000
# Unicode "control" (Cc) and "format" (Cf) characters: NUL, ANSI escapes, zero-width spaces,
# bidi overrides. They are invisible or alter rendering, so they never belong in a reason.
_FORBIDDEN_CHAR_CATEGORIES = frozenset({"Cc", "Cf"})
_ALLOWED_CONTROL_CHARS = frozenset("\t\n\r")

CustomerId = Annotated[
    str,
    StringConstraints(pattern=CUSTOMER_ID_PATTERN),
    Field(
        description="Customer identifier in the form CUST-XXXXX (five ASCII digits).",
        examples=["CUST-12345"],
    ),
]

RefundAmount = Annotated[
    float,
    Field(
        gt=0,
        le=MAX_REFUND_AMOUNT,
        allow_inf_nan=False,
        description=(
            f"Refund amount. Positive, finite, at most {MAX_REFUND_AMOUNT}, "
            f"with at most {AMOUNT_MAX_DECIMAL_PLACES} decimal places."
        ),
        examples=[25.5],
    ),
]

RefundReason = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=REASON_MIN_LENGTH, max_length=REASON_MAX_LENGTH
    ),
    Field(
        description=(
            f"Why the refund is issued. {REASON_MIN_LENGTH}-{REASON_MAX_LENGTH} characters "
            "after trimming whitespace; no invisible control or format characters."
        ),
    ),
]


class _StrictInput(BaseModel):
    """Strict mode: no type coercion ("10" is not a number, true is not a number)."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class _Output(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)


class CustomerRecord(_Output):
    customer_id: str
    name: str
    email: str
    tier: str
    account_status: str


class RefundReceipt(_Output):
    refund_id: str
    customer_id: str
    amount: float
    reason: str
    status: str


class GetCustomerRecordInput(_StrictInput):
    customer_id: CustomerId


class TriggerRefundInput(_StrictInput):
    customer_id: CustomerId
    amount: RefundAmount
    reason: RefundReason

    @field_validator("amount")
    @classmethod
    def _at_most_two_decimal_places(cls, amount: float) -> float:
        exponent = Decimal(str(amount)).as_tuple().exponent
        if isinstance(exponent, int) and exponent < -AMOUNT_MAX_DECIMAL_PLACES:
            raise ValueError(f"must have at most {AMOUNT_MAX_DECIMAL_PLACES} decimal places")
        return amount

    @field_validator("reason")
    @classmethod
    def _no_invisible_characters(cls, reason: str) -> str:
        if any(_is_forbidden_char(char) for char in reason):
            raise ValueError("must not contain control or invisible formatting characters")
        return reason


def _is_forbidden_char(char: str) -> bool:
    return (
        char not in _ALLOWED_CONTROL_CHARS
        and unicodedata.category(char) in _FORBIDDEN_CHAR_CATEGORIES
    )
