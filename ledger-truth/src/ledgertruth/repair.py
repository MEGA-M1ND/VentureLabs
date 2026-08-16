"""Arm D: bounded repair.

FIND-4 reframed what this layer is for. The failure it most needs to avoid is
not an unrepaired ledger -- it is *breaking a correct one*. Under a dropped
response the tool surface reports failure while the ledger is already right, and
the obvious reaction (retry the write) is exactly what creates a duplicate
refund. So the first move here is always a re-read, never a write.

Three rules, in order of how much damage breaking them does:

1. **Re-read before writing.** A verdict is a snapshot of the past; the ledger
   may have settled since. If the fresh read verifies, the repair is a no-op.
2. **Never auto-act on excess money movement.** ESCALATE means a human, always.
   There is no safe automatic way to un-refund a customer.
3. **Never write on INDETERMINATE.** Not knowing is not the same as knowing it
   failed, and writing on a guess is the behaviour this project exists to catch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .intent import Intent
from .ledger import LedgerSnapshot
from .money import Money
from .verdict import Action, Outcome, Verdict


class LedgerReader(Protocol):
    def snapshot_for_payment(self, payment_id: str) -> LedgerSnapshot: ...


class RefundWriter(Protocol):
    def create_refund(
        self, payment_id: str, amount: Money, *, idempotency_key: str | None = ...
    ) -> dict: ...


@dataclass
class RepairStep:
    action: str
    detail: str


@dataclass
class RepairOutcome:
    """What the repair layer did, and where the ledger ended up."""

    initial: Outcome
    final: Outcome
    steps: list[RepairStep] = field(default_factory=list)
    wrote: bool = False
    escalated: bool = False
    final_verdict: Verdict | None = None

    @property
    def recovered(self) -> bool:
        """Ended verified after starting from something else."""
        return self.initial is not Outcome.VERIFIED and self.final is Outcome.VERIFIED

    @property
    def repaired_by_writing(self) -> bool:
        return self.recovered and self.wrote

    @property
    def was_already_correct(self) -> bool:
        """The verdict said FAILED, a fresh read said otherwise, and no write
        was needed. This is the dropped-response case."""
        return (
            self.initial is not Outcome.VERIFIED
            and self.final is Outcome.VERIFIED
            and not self.wrote
        )

    def as_dict(self) -> dict:
        return {
            "initial": self.initial.value,
            "final": self.final.value,
            "wrote": self.wrote,
            "escalated": self.escalated,
            "recovered": self.recovered,
            "was_already_correct": self.was_already_correct,
            "steps": [{"action": s.action, "detail": s.detail} for s in self.steps],
        }


class Repairer:
    def __init__(
        self,
        reader: LedgerReader,
        writer: RefundWriter | None = None,
        *,
        max_writes: int = 1,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._max_writes = max_writes

    def repair(
        self,
        intent: Intent,
        payment_id: str,
        verdict: Verdict,
        *,
        shortfall: Money | None = None,
    ) -> RepairOutcome:
        """Attempt to bring the ledger to the intended state.

        `shortfall` is the amount still owed, when the caller knows it. Without
        it, no write is attempted -- guessing an amount to refund is not a thing
        this layer will do.
        """
        outcome = RepairOutcome(initial=verdict.outcome, final=verdict.outcome)

        if verdict.outcome is Outcome.VERIFIED:
            outcome.steps.append(RepairStep("none", "already verified"))
            return outcome

        if verdict.recommended_action is Action.ESCALATE:
            # Excess or duplicate money movement. Nothing here is safe to
            # automate; clawing it back means charging a customer again.
            outcome.escalated = True
            outcome.steps.append(
                RepairStep("escalate", f"unsafe to automate: {verdict.outcome.value}")
            )
            return outcome

        # Rule 1: always look again before acting. Under a dropped response the
        # write already landed and there is nothing to repair.
        fresh = self._reader.snapshot_for_payment(payment_id)
        reverdict = intent.verify(fresh)
        outcome.final = reverdict.outcome
        outcome.final_verdict = reverdict
        outcome.steps.append(RepairStep("reread", f"fresh verdict: {reverdict.outcome.value}"))

        if reverdict.outcome is Outcome.VERIFIED:
            outcome.steps.append(
                RepairStep("none", "ledger already correct on re-read; no write issued")
            )
            return outcome

        if reverdict.outcome is Outcome.INDETERMINATE:
            # Rule 3. Not knowing is not knowing.
            outcome.escalated = True
            outcome.steps.append(
                RepairStep("escalate", "state unreadable; refusing to write on a guess")
            )
            return outcome

        # The re-read confirms a genuine shortfall.
        if reverdict.recommended_action is not Action.RETRY_IDEMPOTENT:
            outcome.escalated = True
            outcome.steps.append(
                RepairStep(
                    "escalate", f"action {reverdict.recommended_action.value} is not automatable"
                )
            )
            return outcome

        if self._writer is None or shortfall is None or self._max_writes < 1:
            outcome.escalated = True
            outcome.steps.append(
                RepairStep("escalate", "no writer or no known shortfall; refusing to guess")
            )
            return outcome

        try:
            refund = self._writer.create_refund(payment_id, shortfall)
        except Exception as exc:  # provider errors are an outcome, not a crash
            outcome.escalated = True
            outcome.steps.append(RepairStep("escalate", f"repair write failed: {exc}"))
            return outcome

        outcome.wrote = True
        outcome.steps.append(
            RepairStep(
                "retry_idempotent", f"created refund {refund.get('id', '?')} with idempotency key"
            )
        )

        confirmed = self._reader.snapshot_for_payment(payment_id)
        final_verdict = intent.verify(confirmed)
        outcome.final = final_verdict.outcome
        outcome.final_verdict = final_verdict
        outcome.steps.append(
            RepairStep("confirm", f"post-write verdict: {final_verdict.outcome.value}")
        )
        return outcome
