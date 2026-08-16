from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ledgertruth import (
    LedgerSnapshot,
    Money,
    Payment,
    PaymentStatus,
    Refund,
    RefundStatus,
    inr,
)

T0 = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)


def make_payment(
    payment_id: str = "pay_TEST1",
    amount: Money | None = None,
    status: PaymentStatus = PaymentStatus.CAPTURED,
) -> Payment:
    return Payment(
        id=payment_id,
        amount=amount or inr(1000),
        status=status,
        created_at=T0,
        order_id="order_TEST1",
    )


def make_refund(
    refund_id: str,
    payment_id: str = "pay_TEST1",
    amount: Money | None = None,
    status: RefundStatus = RefundStatus.PROCESSED,
    offset_seconds: int = 0,
) -> Refund:
    return Refund(
        id=refund_id,
        payment_id=payment_id,
        amount=amount or inr(1000),
        status=status,
        created_at=T0 + timedelta(seconds=offset_seconds),
    )


def snapshot(
    *,
    payments: list[Payment] | None = None,
    refunds: list[Refund] | None = None,
    unreadable: set[str] | None = None,
    read_errors: tuple[str, ...] = (),
) -> LedgerSnapshot:
    return LedgerSnapshot(
        taken_at=T0 + timedelta(minutes=1),
        payments={p.id: p for p in (payments or [])},
        refunds={r.id: r for r in (refunds or [])},
        unreadable=frozenset(unreadable or set()),
        read_errors=read_errors,
    )
