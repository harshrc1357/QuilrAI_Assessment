"""Seam 1: the Pydantic input models. Each failure names the field and the bad value."""

import math

import pytest
from pydantic import ValidationError

from customer_mcp.schemas import GetCustomerRecordInput, TriggerRefundInput

pytestmark = pytest.mark.unit

VALID_REASON = "Item arrived damaged"


def _error_fields(exc: ValidationError) -> set[str]:
    return {".".join(str(part) for part in err["loc"]) for err in exc.errors()}


def _refund(**overrides: object) -> dict[str, object]:
    return {"customer_id": "CUST-12345", "amount": 25.5, "reason": VALID_REASON, **overrides}


class TestCustomerId:
    @pytest.mark.parametrize("customer_id", ["CUST-00000", "CUST-12345", "CUST-99999"])
    def test_accepts_cust_prefix_and_five_ascii_digits(self, customer_id: str) -> None:
        parsed = GetCustomerRecordInput.model_validate({"customer_id": customer_id})

        assert parsed.customer_id == customer_id

    @pytest.mark.parametrize(
        "customer_id",
        [
            pytest.param("", id="empty"),
            pytest.param("CUST-1234", id="four-digits"),
            pytest.param("CUST-123456", id="six-digits"),
            pytest.param("cust-12345", id="lowercase-prefix"),
            pytest.param("CUST12345", id="missing-dash"),
            pytest.param("CUST-1234A", id="letter-in-digits"),
            pytest.param(" CUST-12345", id="leading-space"),
            pytest.param("CUST-12345 ", id="trailing-space"),
            pytest.param("CUST-12345\n", id="trailing-newline"),
            pytest.param("CUST-١٢٣٤٥", id="arabic-indic-digits"),
            pytest.param("CUST-１２３４５", id="fullwidth-digits"),
            pytest.param("XCUST-12345", id="prefix-not-anchored"),
        ],
    )
    def test_rejects_malformed_string(self, customer_id: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            GetCustomerRecordInput.model_validate({"customer_id": customer_id})

        assert _error_fields(exc_info.value) == {"customer_id"}, (
            f"{customer_id!r} must fail on customer_id only"
        )

    @pytest.mark.parametrize("customer_id", [12345, None, ["CUST-12345"], {"id": "CUST-12345"}])
    def test_rejects_non_string_type(self, customer_id: object) -> None:
        with pytest.raises(ValidationError) as exc_info:
            GetCustomerRecordInput.model_validate({"customer_id": customer_id})

        assert _error_fields(exc_info.value) == {"customer_id"}

    def test_rejects_missing_field(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            GetCustomerRecordInput.model_validate({})

        assert _error_fields(exc_info.value) == {"customer_id"}

    def test_rejects_unknown_extra_field(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            GetCustomerRecordInput.model_validate({"customer_id": "CUST-12345", "admin": True})

        assert _error_fields(exc_info.value) == {"admin"}


class TestRefundAmount:
    @pytest.mark.parametrize("amount", [0.01, 1, 10.5, 99.99, 1_000_000])
    def test_accepts_positive_amount_with_at_most_two_decimals(self, amount: float) -> None:
        parsed = TriggerRefundInput.model_validate(_refund(amount=amount))

        assert parsed.amount == amount

    @pytest.mark.parametrize(
        "amount",
        [
            pytest.param(0, id="zero"),
            pytest.param(0.0, id="zero-float"),
            pytest.param(-0.0, id="negative-zero"),
            pytest.param(-1, id="negative-int"),
            pytest.param(-0.01, id="negative-cent"),
            pytest.param(math.nan, id="nan"),
            pytest.param(math.inf, id="inf"),
            pytest.param(-math.inf, id="negative-inf"),
            pytest.param(10.005, id="three-decimals"),
            pytest.param(0.001, id="sub-cent"),
            pytest.param(1_000_000.01, id="above-ceiling"),
            pytest.param(1e20, id="absurdly-large"),
            pytest.param("10", id="numeric-string"),
            pytest.param("ten", id="word-string"),
            pytest.param(True, id="boolean-true"),
            pytest.param(None, id="null"),
        ],
    )
    def test_rejects_invalid_amount(self, amount: object) -> None:
        with pytest.raises(ValidationError) as exc_info:
            TriggerRefundInput.model_validate(_refund(amount=amount))

        assert _error_fields(exc_info.value) == {"amount"}, f"{amount!r} must fail on amount only"


class TestRefundReason:
    @pytest.mark.parametrize(
        "reason",
        [
            pytest.param("a" * 10, id="exactly-10"),
            pytest.param("a" * 500, id="exactly-500"),
            pytest.param(VALID_REASON, id="sentence"),
        ],
    )
    def test_accepts_reason_within_length_bounds(self, reason: str) -> None:
        parsed = TriggerRefundInput.model_validate(_refund(reason=reason))

        assert parsed.reason == reason

    def test_accepts_multi_line_reason(self) -> None:
        reason = "Damaged item.\nCustomer sent photos."

        parsed = TriggerRefundInput.model_validate(_refund(reason=reason))

        assert parsed.reason == reason

    def test_trims_surrounding_whitespace(self) -> None:
        parsed = TriggerRefundInput.model_validate(_refund(reason=f"   {VALID_REASON}\n"))

        assert parsed.reason == VALID_REASON

    @pytest.mark.parametrize(
        "reason",
        [
            pytest.param("a" * 9, id="nine-chars"),
            pytest.param(" " * 10, id="ten-spaces"),
            pytest.param("   short   ", id="short-after-trim"),
            pytest.param("\t\n" * 10, id="only-whitespace"),
            pytest.param(" " * 12, id="only-non-breaking-spaces"),
            pytest.param("​" * 10, id="only-zero-width-spaces"),
            pytest.param("Refund ‮evil reason", id="bidi-override"),
            pytest.param("Item damaged\x00 badly", id="nul-byte"),
            pytest.param("Item damaged\x1b[31m badly", id="ansi-escape"),
            pytest.param("a" * 501, id="501-chars"),
            pytest.param("", id="empty"),
            pytest.param(1234567890, id="integer"),
            pytest.param(None, id="null"),
        ],
    )
    def test_rejects_invalid_reason(self, reason: object) -> None:
        with pytest.raises(ValidationError) as exc_info:
            TriggerRefundInput.model_validate(_refund(reason=reason))

        assert _error_fields(exc_info.value) == {"reason"}, f"{reason!r} must fail on reason only"


class TestRefundInputShape:
    def test_reports_every_invalid_field_at_once(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            TriggerRefundInput.model_validate({"customer_id": "bad", "amount": -5, "reason": "no"})

        assert _error_fields(exc_info.value) == {"customer_id", "amount", "reason"}

    def test_rejects_unknown_extra_field(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            TriggerRefundInput.model_validate(_refund(currency="USD"))

        assert _error_fields(exc_info.value) == {"currency"}

    def test_is_immutable_after_validation(self) -> None:
        parsed = TriggerRefundInput.model_validate(_refund())

        with pytest.raises(ValidationError):
            parsed.amount = 999  # type: ignore[misc]
