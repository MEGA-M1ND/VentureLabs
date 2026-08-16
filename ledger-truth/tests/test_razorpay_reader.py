"""Razorpay reader tests.

Hermetic: every response is served by an httpx MockTransport, so the suite never
touches the network. The scenarios are the real shapes Razorpay returns --
notably that a missing payment arrives as HTTP 400 with a description, not 404.
"""

from __future__ import annotations

import httpx
import pytest

from ledgertruth import Outcome, PaymentStatus, RefundStatus, full_refund, inr
from ledgertruth.providers import NotTestMode, RazorpayLedgerReader

PAYMENT = {
    "id": "pay_X1",
    "amount": 100000,
    "currency": "INR",
    "status": "captured",
    "created_at": 1_755_000_000,
    "order_id": "order_X1",
    "amount_refunded": 0,
}


def reader_for(handler) -> RazorpayLedgerReader:
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.razorpay.com",
        auth=("rzp_test_x", "s"),
    )
    return RazorpayLedgerReader("rzp_test_x", "s", client=client)


def refund(rid: str, amount: int, status: str = "processed") -> dict:
    return {
        "id": rid,
        "payment_id": "pay_X1",
        "amount": amount,
        "currency": "INR",
        "status": status,
        "created_at": 1_755_000_100,
    }


def err(status: int, description: str) -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"code": "BAD_REQUEST_ERROR", "description": description}}
    )


class TestGuardrail:
    def test_rejects_live_key(self):
        with pytest.raises(NotTestMode, match="not a test key"):
            RazorpayLedgerReader("rzp_live_abc", "secret")

    def test_live_key_allowed_only_explicitly(self):
        RazorpayLedgerReader("rzp_live_abc", "secret", allow_live=True).close()


class TestReads:
    def test_reads_payment_and_refunds(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/payments/pay_X1":
                return httpx.Response(200, json=PAYMENT)
            if request.url.path == "/v1/payments/pay_X1/refunds":
                return httpx.Response(200, json={"count": 1, "items": [refund("rfnd_1", 100000)]})
            raise AssertionError(f"unexpected {request.url}")

        snap = reader_for(handler).snapshot_for_payment("pay_X1")
        assert snap.payments["pay_X1"].amount == inr(1000)
        assert snap.payments["pay_X1"].status is PaymentStatus.CAPTURED
        assert snap.refunds["rfnd_1"].status is RefundStatus.PROCESSED
        assert not snap.unreadable

        # End to end through the verifier.
        assert full_refund("pay_X1", require_status=False).verify(snap).outcome is Outcome.VERIFIED

    def test_missing_payment_is_absent_not_unreadable(self):
        """Razorpay signals a missing id with 400 + description, not 404."""

        def handler(request: httpx.Request) -> httpx.Response:
            return err(400, "The id provided does not exist")

        snap = reader_for(handler).snapshot_for_payment("pay_GONE")
        assert snap.payments == {}
        assert not snap.unreadable, "absent must not be reported as unreadable"
        assert snap.read_errors == ()
        # Absent payment is a definitive failure, not an unknown.
        assert full_refund("pay_GONE", require_status=False).verify(snap).outcome is Outcome.FAILED

    def test_server_error_is_unreadable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return err(500, "we are having trouble")

        snap = reader_for(handler).snapshot_for_payment("pay_X1")
        assert "pay_X1" in snap.unreadable
        assert "pay_X1:refunds" in snap.unreadable
        verdict = full_refund("pay_X1", require_status=False).verify(snap)
        assert verdict.outcome is Outcome.INDETERMINATE

    def test_transport_failure_is_unreadable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out")

        snap = reader_for(handler).snapshot_for_payment("pay_X1")
        assert "pay_X1" in snap.unreadable
        assert any("ConnectTimeout" in e for e in snap.read_errors)

    def test_refund_read_failure_does_not_poison_payment_read(self):
        """The two reads are independent; one failing must not hide the other."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/refunds"):
                return err(503, "unavailable")
            return httpx.Response(200, json=PAYMENT)

        snap = reader_for(handler).snapshot_for_payment("pay_X1")
        assert snap.payments["pay_X1"].amount == inr(1000)  # payment still readable
        assert "pay_X1:refunds" in snap.unreadable
        assert "pay_X1" not in snap.unreadable

    def test_duplicate_refunds_surface_to_the_verifier(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/refunds"):
                return httpx.Response(
                    200,
                    json={
                        "count": 2,
                        "items": [refund("rfnd_1", 100000), refund("rfnd_2", 100000)],
                    },
                )
            return httpx.Response(200, json=PAYMENT)

        snap = reader_for(handler).snapshot_for_payment("pay_X1")
        verdict = full_refund("pay_X1", require_status=False).verify(snap)
        assert verdict.outcome is Outcome.FAILED
        assert "no duplicate money movement" in {c.invariant for c in verdict.failures}


class TestMapping:
    def test_unknown_refund_status_counts_as_money_moved(self):
        """An unrecognised status must not be read as 'failed' -- that would
        erase money movement and license an unsafe retry."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/refunds"):
                return httpx.Response(
                    200, json={"count": 1, "items": [refund("rfnd_1", 100000, status="wat")]}
                )
            return httpx.Response(200, json=PAYMENT)

        snap = reader_for(handler).snapshot_for_payment("pay_X1")
        assert snap.refunds["rfnd_1"].status is RefundStatus.PENDING
        assert snap.refunds["rfnd_1"].moved_money is True

    def test_amounts_stay_integer_paise(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/refunds"):
                return httpx.Response(200, json={"count": 1, "items": [refund("rfnd_1", 24950)]})
            return httpx.Response(200, json={**PAYMENT, "amount": 24950})

        snap = reader_for(handler).snapshot_for_payment("pay_X1")
        assert snap.payments["pay_X1"].amount.minor == 24950
        assert str(snap.refunds["rfnd_1"].amount) == "249.50 INR"

    def test_receipt_is_captured_as_idempotency_key(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/refunds"):
                item = {**refund("rfnd_1", 100000), "receipt": "order-42-refund"}
                return httpx.Response(200, json={"count": 1, "items": [item]})
            return httpx.Response(200, json=PAYMENT)

        snap = reader_for(handler).snapshot_for_payment("pay_X1")
        assert snap.refunds["rfnd_1"].idempotency_key == "order-42-refund"
