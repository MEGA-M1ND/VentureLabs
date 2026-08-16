"""Prebuilt intent contracts for common payment operations.

The decomposition here is the point. A naive contract says
`refund_total == payment_amount` and calls any deviation a failure. But the two
directions of that deviation are completely different incidents:

* refunded **less** than intended -> the operation is incomplete. An
  idempotency-keyed retry is safe and appropriate -- but only if the repair
  layer supplies the key *itself*. It must not assume the agent's tooling did:
  Razorpay's own MCP server passes `nil` for extra headers and so sends no
  `X-Refund-Idempotency` at all (see docs/findings.md, FIND-1). `RETRY_IDEMPOTENT`
  names an obligation on the repairer, not an observed property of the call.
* refunded **more** than intended -> money left the merchant that should not
  have. There is no safe automatic remedy; clawing it back means charging a
  customer again. This escalates to a human, always.

Collapsing both into one invariant throws away exactly the information the
remediation layer needs.
"""

from __future__ import annotations

from .intent import Intent
from .invariants import Comparison, PaymentAmount, PaymentStatusOf, RefundCount, RefundTotal
from .ledger import PaymentStatus
from .money import Money
from .verdict import Action

_P = "$payment"


def _no_duplicate_movement() -> Comparison:
    return (
        RefundCount(_P).at_most(1).named("no duplicate money movement").remediate(Action.ESCALATE)
    )


def _never_over_payment() -> Comparison:
    return (
        RefundTotal(_P)
        .at_most(PaymentAmount(_P))
        .named("refunds never exceed the payment")
        .remediate(Action.ESCALATE)
    )


def full_refund(payment_id: str, *, require_status: bool = True) -> Intent:
    """The payment should be fully refunded, exactly once."""
    invariants = [
        _never_over_payment(),
        RefundTotal(_P)
        .at_least(PaymentAmount(_P))
        .named("refund is complete")
        .remediate(Action.RETRY_IDEMPOTENT),
        _no_duplicate_movement(),
    ]
    if require_status:
        invariants.append(
            # Status commonly lags the refund record by seconds. A mismatch here
            # is a reason to look again, not to declare the money wrong.
            PaymentStatusOf(_P)
            .equals(PaymentStatus.REFUNDED)
            .named("payment marked refunded")
            .remediate(Action.REREAD)
        )
    return Intent(
        action="full_refund",
        invariants=tuple(invariants),
        bindings={"payment": payment_id},
        description=f"fully refund payment {payment_id}",
    )


def partial_refund(payment_id: str, amount: Money) -> Intent:
    """Exactly `amount` should be refunded against the payment, exactly once."""
    return Intent(
        action="partial_refund",
        invariants=(
            _never_over_payment(),
            RefundTotal(_P)
            .at_most(amount)
            .named(f"refunded no more than {amount}")
            .remediate(Action.ESCALATE),
            RefundTotal(_P)
            .at_least(amount)
            .named(f"refunded at least {amount}")
            .remediate(Action.RETRY_IDEMPOTENT),
            _no_duplicate_movement(),
        ),
        bindings={"payment": payment_id},
        description=f"refund {amount} of payment {payment_id}",
    )


def no_refund_occurred(payment_id: str) -> Intent:
    """Negative contract: used to verify that a *rejected* or aborted mission
    left no money movement behind. Agents that decide not to act still need
    checking -- 'I decided not to refund' is also a claim about the world."""
    return Intent(
        action="no_refund",
        invariants=(
            RefundCount(_P).at_most(0).named("no refund was created").remediate(Action.ESCALATE),
        ),
        bindings={"payment": payment_id},
        description=f"no refund exists against payment {payment_id}",
    )
