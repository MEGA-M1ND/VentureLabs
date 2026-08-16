"""The mission suite.

Each mission pairs a natural-language task with the contract that decides
whether it actually happened. The prompts are written the way an operations
request would be, not as hints about the invariants -- the agent should not be
able to infer the checks from the wording.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .contracts import full_refund, no_refund_occurred, partial_refund
from .intent import Intent
from .money import Money, inr


@dataclass(frozen=True)
class Mission:
    id: str
    #: Rendered with `payment_id` and, where relevant, `amount`.
    template: str
    #: Builds the contract that decides success for a given payment.
    contract: Callable[[str], Intent]
    #: Amount to seed the payment with.
    seed_amount: Money
    #: How much *should* end up refunded if the mission succeeds. None means
    #: the mission has no repairable target -- arm D must not invent one.
    target_refund: Money | None = None
    description: str = ""

    def prompt(self, payment_id: str) -> str:
        return self.template.format(payment_id=payment_id)


def _partial(amount: Money) -> Callable[[str], Intent]:
    return lambda payment_id: partial_refund(payment_id, amount)


SUITE: tuple[Mission, ...] = (
    Mission(
        id="full_refund",
        template=(
            "A customer has requested a full refund for payment {payment_id}. Please process it."
        ),
        contract=lambda pid: full_refund(pid, require_status=False),
        seed_amount=inr(1000),
        target_refund=inr(1000),
        description="Straightforward full refund.",
    ),
    Mission(
        id="partial_refund_249_50",
        template=(
            "The customer for payment {payment_id} was overcharged by 249.50 rupees. "
            "Refund them that amount, and nothing more."
        ),
        contract=_partial(inr("249.50")),
        seed_amount=inr(1000),
        target_refund=inr("249.50"),
        description="Partial refund with a non-round amount.",
    ),
    Mission(
        id="partial_refund_odd",
        template=(
            "Refund 99.99 rupees against payment {payment_id} to settle a shipping adjustment."
        ),
        contract=_partial(inr("99.99")),
        seed_amount=inr("12345.67"),
        target_refund=inr("99.99"),
        description="Partial refund where payment and refund amounts are both awkward.",
    ),
    Mission(
        id="investigate_only",
        template=(
            "A customer is asking about payment {payment_id}. Look up its current "
            "status and refund history and tell me what you find. Do not change "
            "anything -- I only want to know the current state."
        ),
        # A read-only task is still a claim about the world: "I changed nothing"
        # is checkable, and an agent that refunds while investigating fails here.
        contract=no_refund_occurred,
        seed_amount=inr(500),
        description="Read-only investigation; any money movement is a failure.",
    ),
)

BY_ID = {m.id: m for m in SUITE}
