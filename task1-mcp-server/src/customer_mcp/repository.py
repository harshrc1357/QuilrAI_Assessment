"""Domain entities, the data access port, and an in-memory adapter. A real deployment swaps the
adapter for a database or CRM client that satisfies the same `CustomerRepository` protocol."""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

REFUND_ID_PREFIX = "RF-"
REFUND_ID_RANDOM_HEX_CHARS = 12


@dataclass(frozen=True, slots=True)
class Customer:
    customer_id: str
    name: str
    email: str
    tier: str
    account_status: str


@dataclass(frozen=True, slots=True)
class Refund:
    refund_id: str
    customer_id: str
    amount: float
    reason: str
    status: str


class CustomerRepository(Protocol):
    def get_customer(self, customer_id: str) -> Customer | None: ...

    def create_refund(self, customer_id: str, amount: float, reason: str) -> Refund: ...


_SAMPLE_CUSTOMERS = (
    Customer("CUST-12345", "Ada Lovelace", "ada@example.com", "gold", "active"),
    Customer("CUST-00001", "Alan Turing", "alan@example.com", "standard", "active"),
    Customer("CUST-54321", "Grace Hopper", "grace@example.com", "platinum", "suspended"),
)


class InMemoryCustomerRepository:
    def __init__(self, customers: Iterable[Customer]) -> None:
        self._customers = {customer.customer_id: customer for customer in customers}

    @classmethod
    def with_sample_data(cls) -> "InMemoryCustomerRepository":
        return cls(_SAMPLE_CUSTOMERS)

    def get_customer(self, customer_id: str) -> Customer | None:
        return self._customers.get(customer_id)

    def create_refund(self, customer_id: str, amount: float, reason: str) -> Refund:
        random_part = uuid.uuid4().hex[:REFUND_ID_RANDOM_HEX_CHARS].upper()
        return Refund(
            refund_id=f"{REFUND_ID_PREFIX}{random_part}",
            customer_id=customer_id,
            amount=amount,
            reason=reason,
            status="accepted",
        )
