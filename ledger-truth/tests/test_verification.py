"""Verdict behaviour on synthetic ledger states.

These test the verifier, not any agent. The scenarios are hand-built ledger
snapshots chosen because they are the states real payment systems actually end
up in after a partial failure -- not because an agent produced them.
"""

from conftest import make_payment, make_refund, snapshot

from ledgertruth import (
    Action,
    Outcome,
    PaymentStatus,
    RefundStatus,
    full_refund,
    inr,
    no_refund_occurred,
    partial_refund,
)


class TestFullRefund:
    def test_clean_full_refund_verifies(self):
        snap = snapshot(
            payments=[make_payment(status=PaymentStatus.REFUNDED)],
            refunds=[make_refund("rfnd_1", amount=inr(1000))],
        )
        verdict = full_refund("pay_TEST1").verify(snap)
        assert verdict.outcome is Outcome.VERIFIED
        assert verdict.recommended_action is Action.NONE

    def test_duplicate_refund_fails_and_escalates(self):
        """The headline failure: the agent retried, money left twice."""
        snap = snapshot(
            payments=[make_payment(status=PaymentStatus.REFUNDED)],
            refunds=[
                make_refund("rfnd_1", amount=inr(1000)),
                make_refund("rfnd_2", amount=inr(1000), offset_seconds=3),
            ],
        )
        verdict = full_refund("pay_TEST1").verify(snap)

        assert verdict.outcome is Outcome.FAILED
        # Never auto-remediate excess money movement.
        assert verdict.recommended_action is Action.ESCALATE
        failed = {c.invariant for c in verdict.failures}
        assert "no duplicate money movement" in failed
        assert "refunds never exceed the payment" in failed

    def test_missing_refund_fails_and_is_retryable(self):
        snap = snapshot(payments=[make_payment()], refunds=[])
        verdict = full_refund("pay_TEST1").verify(snap)

        assert verdict.outcome is Outcome.FAILED
        # Nothing moved, so an idempotency-keyed retry is the safe remedy.
        assert verdict.recommended_action is Action.RETRY_IDEMPOTENT

    def test_partial_amount_when_full_expected_is_retryable(self):
        snap = snapshot(
            payments=[make_payment()],
            refunds=[make_refund("rfnd_1", amount=inr(400))],
        )
        verdict = full_refund("pay_TEST1").verify(snap)
        assert verdict.outcome is Outcome.FAILED
        assert verdict.recommended_action is Action.RETRY_IDEMPOTENT

    def test_failed_refund_does_not_count_as_money_moved(self):
        snap = snapshot(
            payments=[make_payment()],
            refunds=[make_refund("rfnd_1", amount=inr(1000), status=RefundStatus.FAILED)],
        )
        verdict = full_refund("pay_TEST1").verify(snap)
        assert verdict.outcome is Outcome.FAILED
        assert verdict.recommended_action is Action.RETRY_IDEMPOTENT

    def test_pending_refund_counts_as_money_moved(self):
        """An in-flight refund must block a retry, or we create a duplicate."""
        snap = snapshot(
            payments=[make_payment()],
            refunds=[make_refund("rfnd_1", amount=inr(1000), status=RefundStatus.PENDING)],
        )
        verdict = full_refund("pay_TEST1", require_status=False).verify(snap)
        assert verdict.outcome is Outcome.VERIFIED

    def test_status_lag_is_a_reread_not_a_money_failure(self):
        snap = snapshot(
            payments=[make_payment(status=PaymentStatus.CAPTURED)],
            refunds=[make_refund("rfnd_1", amount=inr(1000))],
        )
        verdict = full_refund("pay_TEST1").verify(snap)
        assert verdict.outcome is Outcome.FAILED
        assert verdict.recommended_action is Action.REREAD


class TestIndeterminate:
    def test_unreadable_refunds_yield_indeterminate(self):
        snap = snapshot(
            payments=[make_payment()],
            unreadable={"pay_TEST1:refunds"},
            read_errors=("refund list timed out",),
        )
        verdict = full_refund("pay_TEST1", require_status=False).verify(snap)

        assert verdict.outcome is Outcome.INDETERMINATE
        assert verdict.recommended_action is Action.REREAD
        assert verdict.read_errors == ("refund list timed out",)

    def test_absent_payment_is_readable_and_fails(self):
        """Absent != unreadable. A payment that does not exist is a real answer."""
        snap = snapshot(payments=[], refunds=[])
        verdict = full_refund("pay_MISSING", require_status=False).verify(snap)
        assert verdict.outcome is Outcome.FAILED

    def test_proven_failure_dominates_unreadable_sibling(self):
        """A duplicate we can see stays proven even if another check can't run."""
        snap = snapshot(
            payments=[make_payment()],
            refunds=[
                make_refund("rfnd_1", amount=inr(1000)),
                make_refund("rfnd_2", amount=inr(1000), offset_seconds=2),
            ],
            unreadable={"pay_TEST1"},
        )
        verdict = full_refund("pay_TEST1", require_status=False).verify(snap)
        assert verdict.outcome is Outcome.FAILED
        assert verdict.recommended_action is Action.ESCALATE


class TestPartialRefund:
    def test_exact_partial_verifies(self):
        snap = snapshot(
            payments=[make_payment(amount=inr(1000))],
            refunds=[make_refund("rfnd_1", amount=inr(250))],
        )
        verdict = partial_refund("pay_TEST1", inr(250)).verify(snap)
        assert verdict.outcome is Outcome.VERIFIED

    def test_over_refund_within_payment_still_escalates(self):
        """Refunded 400 when 250 was intended. Under the payment total, so the
        'never exceed payment' check passes -- the intent-specific cap is what
        catches this."""
        snap = snapshot(
            payments=[make_payment(amount=inr(1000))],
            refunds=[make_refund("rfnd_1", amount=inr(400))],
        )
        verdict = partial_refund("pay_TEST1", inr(250)).verify(snap)
        assert verdict.outcome is Outcome.FAILED
        assert verdict.recommended_action is Action.ESCALATE

    def test_split_refunds_summing_correctly_still_flag_duplication(self):
        """Two refunds of 125 sum to the intended 250, but two money movements
        happened where one was intended. The amount check passes; the movement
        count check is what catches it."""
        snap = snapshot(
            payments=[make_payment(amount=inr(1000))],
            refunds=[
                make_refund("rfnd_1", amount=inr(125)),
                make_refund("rfnd_2", amount=inr(125), offset_seconds=1),
            ],
        )
        verdict = partial_refund("pay_TEST1", inr(250)).verify(snap)
        assert verdict.outcome is Outcome.FAILED
        assert verdict.recommended_action is Action.ESCALATE


class TestNegativeContract:
    def test_no_refund_contract_verifies_when_nothing_moved(self):
        snap = snapshot(payments=[make_payment()], refunds=[])
        assert no_refund_occurred("pay_TEST1").verify(snap).outcome is Outcome.VERIFIED

    def test_no_refund_contract_catches_unintended_movement(self):
        snap = snapshot(
            payments=[make_payment()],
            refunds=[make_refund("rfnd_1", amount=inr(1000))],
        )
        verdict = no_refund_occurred("pay_TEST1").verify(snap)
        assert verdict.outcome is Outcome.FAILED
        assert verdict.recommended_action is Action.ESCALATE


def test_verdict_explains_itself():
    snap = snapshot(
        payments=[make_payment()],
        refunds=[
            make_refund("rfnd_1", amount=inr(1000)),
            make_refund("rfnd_2", amount=inr(1000), offset_seconds=3),
        ],
    )
    text = full_refund("pay_TEST1").verify(snap).explain()
    assert "FAILED" in text
    assert "no duplicate money movement" in text
    assert "rfnd_2" in text  # evidence names the offending object
