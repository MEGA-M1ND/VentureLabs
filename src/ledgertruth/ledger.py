"""Normalized, provider-agnostic ledger state.

A LedgerSnapshot is what an *independent reader* saw after the agent claimed it
was done. It is deliberately not the agent's transcript and not the tool's HTTP
response -- those are the things we are trying to check.

The critical distinction this module encodes: an object that is *absent* is not
the same as an object we *could not read*. "No refund exists" is evidence of
failure. "We could not reach the refunds endpoint" is evidence of nothing, and
must produce INDETERMINATE rather than a confident verdict. Conflating the two
is how verification layers quietly start lying in the same way the agents do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, StrEnum
from typing import TypeVar

from .money import Money

T = TypeVar("T")


class Presence(Enum):
    FOUND = "found"
    ABSENT = "absent"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Lookup[T]:
    """Three-valued result of resolving an object in a snapshot."""

    presence: Presence
    value: T | None = None
    detail: str = ""

    @property
    def is_readable(self) -> bool:
        return self.presence is not Presence.UNREADABLE


class PaymentStatus(StrEnum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    REFUNDED = "refunded"
    FAILED = "failed"


class RefundStatus(StrEnum):
    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"


@dataclass(frozen=True)
class Refund:
    id: str
    payment_id: str
    amount: Money
    status: RefundStatus
    created_at: datetime
    idempotency_key: str | None = None
    #: Provider payload, kept verbatim for the evidence trail.
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def moved_money(self) -> bool:
        """A failed refund did not move money; a pending one may still.

        Pending counts as money-moving for safety: if we are deciding whether a
        retry is safe, an in-flight refund must be treated as real.
        """
        return self.status is not RefundStatus.FAILED


@dataclass(frozen=True)
class Payment:
    id: str
    amount: Money
    status: PaymentStatus
    created_at: datetime
    order_id: str | None = None
    raw: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Order:
    id: str
    amount: Money
    status: str
    created_at: datetime
    raw: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class LedgerSnapshot:
    """An independent read of ledger state at a point in time."""

    taken_at: datetime
    payments: dict[str, Payment] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)
    #: All refunds seen, keyed by refund id.
    refunds: dict[str, Refund] = field(default_factory=dict)
    #: Object ids we attempted to read and failed to. Any invariant touching
    #: one of these degrades to INDETERMINATE.
    unreadable: frozenset[str] = frozenset()
    #: Human-readable read failures, carried into the evidence trail.
    read_errors: tuple[str, ...] = ()

    def payment(self, payment_id: str) -> Lookup[Payment]:
        if payment_id in self.unreadable:
            return Lookup(Presence.UNREADABLE, detail=f"payment {payment_id} could not be read")
        if payment_id in self.payments:
            return Lookup(Presence.FOUND, self.payments[payment_id])
        return Lookup(Presence.ABSENT, detail=f"payment {payment_id} not present in ledger")

    def order(self, order_id: str) -> Lookup[Order]:
        if order_id in self.unreadable:
            return Lookup(Presence.UNREADABLE, detail=f"order {order_id} could not be read")
        if order_id in self.orders:
            return Lookup(Presence.FOUND, self.orders[order_id])
        return Lookup(Presence.ABSENT, detail=f"order {order_id} not present in ledger")

    def refunds_of(self, payment_id: str) -> Lookup[tuple[Refund, ...]]:
        """Refunds attached to a payment.

        An empty tuple is a real, readable answer ("no refunds exist"), which is
        why this returns FOUND rather than ABSENT. Only an explicit read failure
        on the payment's refund collection yields UNREADABLE.
        """
        # Deliberately independent of whether the *payment* was readable: the
        # refund collection is a separate read against a separate endpoint, and
        # one failing tells us nothing about the other. Conflating them would
        # let a single unreadable object silently suppress a duplicate-refund
        # finding we can plainly see.
        refund_collection_key = f"{payment_id}:refunds"
        if refund_collection_key in self.unreadable:
            return Lookup(
                Presence.UNREADABLE, detail=f"refunds for {payment_id} could not be read"
            )
        matched = tuple(
            r
            for r in sorted(self.refunds.values(), key=lambda r: (r.created_at, r.id))
            if r.payment_id == payment_id
        )
        return Lookup(Presence.FOUND, matched)
