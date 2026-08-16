"""Arm D safety properties.

These are the rules that decide whether the repair layer is safe to point at a
real ledger, so each gets an explicit test. The most important is the first
class: under a dropped response the verdict says FAILED while the ledger is
already correct, and the repair layer must issue no write at all.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import make_payment, make_refund, snapshot

from ledgertruth import Outcome, full_refund, inr, partial_refund
from ledgertruth.providers import NotTestMode
from ledgertruth.providers.razorpay_write import RazorpayWriter, idempotency_key_for
from ledgertruth.repair import Repairer


class FakeReader:
    """Returns a queued sequence of snapshots, one per read."""

    def __init__(self, *snapshots):
        self._queue = list(snapshots)
        self.reads = 0

    def snapshot_for_payment(self, payment_id: str):
        self.reads += 1
        return self._queue.pop(0) if len(self._queue) > 1 else self._queue[0]


class FakeWriter:
    def __init__(self, fail: Exception | None = None):
        self.calls = []
        self._fail = fail

    def create_refund(self, payment_id, amount, *, idempotency_key=None):
        if self._fail:
            raise self._fail
        self.calls.append((payment_id, amount, idempotency_key))
        return {"id": "rfnd_repair"}


PAY = "pay_TEST1"


class TestDoesNotBreakCorrectLedgers:
    def test_no_write_when_reread_shows_already_correct(self):
        """The dropped-response case. The stale verdict says FAILED; a fresh
        read says the refund landed. Writing here creates the duplicate."""
        stale = snapshot(payments=[make_payment()], refunds=[])
        fresh = snapshot(
            payments=[make_payment()], refunds=[make_refund("rfnd_1", amount=inr(1000))]
        )
        intent = full_refund(PAY, require_status=False)
        reader, writer = FakeReader(fresh), FakeWriter()

        result = Repairer(reader, writer).repair(
            intent, PAY, intent.verify(stale), shortfall=inr(1000)
        )

        assert writer.calls == [], "must not write when the ledger is already correct"
        assert result.final is Outcome.VERIFIED
        assert result.was_already_correct is True
        assert result.wrote is False

    def test_reread_is_always_the_first_step(self):
        broken = snapshot(payments=[make_payment()], refunds=[])
        intent = full_refund(PAY, require_status=False)
        reader, writer = FakeReader(broken), FakeWriter()

        result = Repairer(reader, writer).repair(
            intent, PAY, intent.verify(broken), shortfall=inr(1000)
        )

        assert reader.reads >= 1
        assert result.steps[0].action == "reread", "must look before it writes"


class TestNeverAutomatesUnsafeActions:
    def test_duplicate_refund_escalates_without_writing(self):
        dup = snapshot(
            payments=[make_payment()],
            refunds=[
                make_refund("rfnd_1", amount=inr(1000)),
                make_refund("rfnd_2", amount=inr(1000), offset_seconds=2),
            ],
        )
        intent = full_refund(PAY, require_status=False)
        writer = FakeWriter()

        result = Repairer(FakeReader(dup), writer).repair(
            intent, PAY, intent.verify(dup), shortfall=inr(1000)
        )

        assert result.escalated is True
        assert writer.calls == []
        assert result.wrote is False

    def test_over_refund_escalates(self):
        over = snapshot(
            payments=[make_payment(amount=inr(1000))],
            refunds=[make_refund("rfnd_1", amount=inr(400))],
        )
        intent = partial_refund(PAY, inr(250))
        writer = FakeWriter()

        result = Repairer(FakeReader(over), writer).repair(intent, PAY, intent.verify(over))
        assert result.escalated is True
        assert writer.calls == []

    def test_indeterminate_never_writes(self):
        """Not knowing is not the same as knowing it failed."""
        unknown = snapshot(payments=[make_payment()], unreadable={f"{PAY}:refunds"})
        intent = full_refund(PAY, require_status=False)
        writer = FakeWriter()

        result = Repairer(FakeReader(unknown), writer).repair(
            intent, PAY, intent.verify(unknown), shortfall=inr(1000)
        )

        assert result.escalated is True
        assert writer.calls == []

    def test_refuses_to_guess_an_amount(self):
        """Without a known shortfall the layer escalates rather than inventing one."""
        broken = snapshot(payments=[make_payment()], refunds=[])
        intent = full_refund(PAY, require_status=False)
        writer = FakeWriter()

        result = Repairer(FakeReader(broken), writer).repair(
            intent, PAY, intent.verify(broken), shortfall=None
        )
        assert result.escalated is True
        assert writer.calls == []


class TestGenuineRepair:
    def test_writes_once_and_confirms_when_nothing_landed(self):
        broken = snapshot(payments=[make_payment()], refunds=[])
        repaired = snapshot(
            payments=[make_payment()], refunds=[make_refund("rfnd_repair", amount=inr(1000))]
        )
        intent = full_refund(PAY, require_status=False)
        # First read confirms the shortfall; second read confirms the repair.
        reader, writer = FakeReader(broken, repaired), FakeWriter()

        result = Repairer(reader, writer).repair(
            intent, PAY, intent.verify(broken), shortfall=inr(1000)
        )

        assert len(writer.calls) == 1
        assert result.wrote is True
        assert result.final is Outcome.VERIFIED
        assert result.repaired_by_writing is True

    def test_write_failure_escalates_rather_than_raising(self):
        broken = snapshot(payments=[make_payment()], refunds=[])
        intent = full_refund(PAY, require_status=False)
        writer = FakeWriter(fail=RuntimeError("gateway down"))

        result = Repairer(FakeReader(broken), writer).repair(
            intent, PAY, intent.verify(broken), shortfall=inr(1000)
        )
        assert result.escalated is True
        assert result.wrote is False

    def test_verified_verdict_is_a_no_op(self):
        good = snapshot(
            payments=[make_payment()], refunds=[make_refund("rfnd_1", amount=inr(1000))]
        )
        intent = full_refund(PAY, require_status=False)
        reader, writer = FakeReader(good), FakeWriter()

        result = Repairer(reader, writer).repair(intent, PAY, intent.verify(good))
        assert result.wrote is False
        assert reader.reads == 0, "no need to re-read a verified verdict"


class TestIdempotencyKey:
    def test_key_is_deterministic_for_the_same_intent(self):
        a = idempotency_key_for(PAY, inr(1000))
        b = idempotency_key_for(PAY, inr(1000))
        assert a == b

    def test_key_differs_by_amount_and_payment(self):
        assert idempotency_key_for(PAY, inr(1000)) != idempotency_key_for(PAY, inr(500))
        assert idempotency_key_for(PAY, inr(1000)) != idempotency_key_for("pay_OTHER", inr(1000))

    def test_key_meets_razorpay_minimum_length(self):
        assert len(idempotency_key_for(PAY, inr(1000))) >= 10


class TestWriterGuardrails:
    def test_rejects_live_key(self):
        with pytest.raises(NotTestMode):
            RazorpayWriter("rzp_live_abc", "s")

    def test_sends_idempotency_header(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["header"] = request.headers.get("X-Refund-Idempotency")
            return httpx.Response(200, json={"id": "rfnd_1"})

        client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://api.razorpay.com",
            auth=("rzp_test_x", "s"),
        )
        RazorpayWriter("rzp_test_x", "s", client=client).create_refund(PAY, inr(1000))
        assert seen["header"] == idempotency_key_for(PAY, inr(1000))
