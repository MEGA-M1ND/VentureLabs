"""Razorpay ledger reader.

Reads live state and normalizes it. The interesting work is not the field
mapping, it is the error classification: deciding, for each failed read, whether
we learned "this does not exist" or "we could not look". Razorpay makes that
harder than it sounds -- a missing payment comes back as `400 BAD_REQUEST_ERROR`
with a description, not a `404`, so status code alone is not enough.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from ..ledger import (
    LedgerSnapshot,
    Order,
    Payment,
    PaymentStatus,
    Refund,
    RefundStatus,
)
from ..money import Money

API_BASE = "https://api.razorpay.com"

#: Substrings Razorpay uses in 400 responses to mean "no such object". Matched
#: case-insensitively against the error description.
_ABSENT_MARKERS = (
    "does not exist",
    "is not a valid id",
    "not found",
)

_PAYMENT_STATUS = {
    "created": PaymentStatus.CREATED,
    "authorized": PaymentStatus.AUTHORIZED,
    "captured": PaymentStatus.CAPTURED,
    "refunded": PaymentStatus.REFUNDED,
    "failed": PaymentStatus.FAILED,
}

_REFUND_STATUS = {
    "pending": RefundStatus.PENDING,
    "created": RefundStatus.PENDING,
    "processed": RefundStatus.PROCESSED,
    "failed": RefundStatus.FAILED,
}


class NotTestMode(RuntimeError):
    """Raised at construction if a non-test key is supplied."""


class _Read:
    """Outcome of one HTTP read: found / absent / unreadable."""

    __slots__ = ("data", "absent", "error")

    def __init__(self, data: dict | None = None, *, absent: bool = False, error: str = "") -> None:
        self.data = data
        self.absent = absent
        self.error = error

    @property
    def unreadable(self) -> bool:
        return bool(self.error)


class RazorpayLedgerReader:
    """Independent reader over Razorpay's REST API.

    Deliberately does **not** go through the MCP server. The whole point is to
    observe the ledger by a path the agent did not use -- reusing the agent's
    tool surface would inherit whatever made it wrong.
    """

    def __init__(
        self,
        key_id: str,
        key_secret: str,
        *,
        base_url: str = API_BASE,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
        allow_live: bool = False,
    ) -> None:
        if not allow_live and not key_id.startswith("rzp_test_"):
            raise NotTestMode(
                f"key {key_id[:10]}... is not a test key. This harness moves money; "
                "pass allow_live=True only with a very good reason."
            )
        self._client = client or httpx.Client(
            base_url=base_url, auth=(key_id, key_secret), timeout=timeout
        )

    # -- HTTP ---------------------------------------------------------------

    def _get(self, path: str) -> _Read:
        try:
            resp = self._client.get(path)
        except httpx.HTTPError as exc:
            # Transport failure: we learned nothing about the object.
            return _Read(error=f"{path}: {type(exc).__name__}: {exc}")

        if resp.is_success:
            try:
                return _Read(resp.json())
            except ValueError:
                return _Read(error=f"{path}: non-JSON response (HTTP {resp.status_code})")

        description = ""
        try:
            description = str(resp.json().get("error", {}).get("description", ""))
        except ValueError:
            description = resp.text[:200]

        lowered = description.lower()
        if resp.status_code == 404 or any(m in lowered for m in _ABSENT_MARKERS):
            return _Read(absent=True)

        # 5xx, 429, auth failures: readable state unknown.
        return _Read(error=f"{path}: HTTP {resp.status_code}: {description or '<no detail>'}")

    # -- mapping ------------------------------------------------------------

    @staticmethod
    def _when(epoch: int | None) -> datetime:
        return datetime.fromtimestamp(epoch or 0, tz=UTC)

    @classmethod
    def _payment(cls, d: dict) -> Payment:
        return Payment(
            id=d["id"],
            amount=Money(int(d["amount"]), d.get("currency", "INR")),
            status=_PAYMENT_STATUS.get(d.get("status", ""), PaymentStatus.CREATED),
            created_at=cls._when(d.get("created_at")),
            order_id=d.get("order_id"),
            raw=d,
        )

    @classmethod
    def _refund(cls, d: dict, payment_id: str) -> Refund:
        return Refund(
            id=d["id"],
            payment_id=d.get("payment_id") or payment_id,
            amount=Money(int(d["amount"]), d.get("currency", "INR")),
            # An unrecognised status must not silently become "failed" -- that
            # would erase money movement. Treat the unknown as still in flight.
            status=_REFUND_STATUS.get(d.get("status", ""), RefundStatus.PENDING),
            created_at=cls._when(d.get("created_at")),
            idempotency_key=d.get("receipt"),
            raw=d,
        )

    @classmethod
    def _order(cls, d: dict) -> Order:
        return Order(
            id=d["id"],
            amount=Money(int(d["amount"]), d.get("currency", "INR")),
            status=d.get("status", ""),
            created_at=cls._when(d.get("created_at")),
            raw=d,
        )

    # -- public -------------------------------------------------------------

    def snapshot_for_payment(self, payment_id: str, *, with_order: bool = False) -> LedgerSnapshot:
        taken_at = datetime.now(tz=UTC)
        payments: dict[str, Payment] = {}
        refunds: dict[str, Refund] = {}
        orders: dict[str, Order] = {}
        unreadable: set[str] = set()
        errors: list[str] = []

        pay = self._get(f"/v1/payments/{payment_id}")
        if pay.unreadable:
            unreadable.add(payment_id)
            errors.append(pay.error)
        elif pay.data is not None:
            payments[payment_id] = self._payment(pay.data)

        # Independent read. A failure here must not be inferred from the
        # payment read, nor suppress it -- see LedgerSnapshot.refunds_of.
        rfd = self._get(f"/v1/payments/{payment_id}/refunds?count=100")
        if rfd.unreadable:
            unreadable.add(f"{payment_id}:refunds")
            errors.append(rfd.error)
        elif rfd.absent:
            # The payment itself is gone, so it has no refunds. That is a
            # readable answer of "none", not a failed read.
            pass
        elif rfd.data is not None:
            for item in rfd.data.get("items", []):
                refunds[item["id"]] = self._refund(item, payment_id)

        if with_order:
            order_id = payments.get(payment_id).order_id if payment_id in payments else None
            if order_id:
                ordr = self._get(f"/v1/orders/{order_id}")
                if ordr.unreadable:
                    unreadable.add(order_id)
                    errors.append(ordr.error)
                elif ordr.data is not None:
                    orders[order_id] = self._order(ordr.data)

        return LedgerSnapshot(
            taken_at=taken_at,
            payments=payments,
            orders=orders,
            refunds=refunds,
            unreadable=frozenset(unreadable),
            read_errors=tuple(errors),
        )

    def close(self) -> None:
        self._client.close()
