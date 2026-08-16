"""Intent contracts: what the agent said it would do, stated as checkable claims."""

from __future__ import annotations

from dataclasses import dataclass, field

from .invariants import Bindings, Comparison
from .ledger import LedgerSnapshot
from .verdict import Action, Outcome, Verdict, combine

#: Which remediation wins when several invariants fail at once. Escalation
#: dominates: if any part of the failure involves money we cannot safely touch,
#: the whole verdict goes to a human.
_ACTION_SEVERITY = {
    Action.ESCALATE: 0,
    Action.COMPENSATE: 1,
    Action.RETRY_IDEMPOTENT: 2,
    Action.REREAD: 3,
    Action.NONE: 4,
}


@dataclass(frozen=True)
class Intent:
    """A declared outcome plus the invariants that must hold if it happened.

    `bindings` maps `$name` references used inside invariants to concrete ids,
    so one contract template can be reused across missions.
    """

    action: str
    invariants: tuple[Comparison, ...]
    bindings: Bindings = field(default_factory=dict)
    description: str = ""

    @property
    def label(self) -> str:
        if self.description:
            return self.description
        args = ", ".join(f"{k}={v}" for k, v in self.bindings.items())
        return f"{self.action}({args})"

    def verify(self, snap: LedgerSnapshot) -> Verdict:
        checks = tuple(inv.check(snap, self.bindings) for inv in self.invariants)
        outcome = combine([c.outcome for c in checks])

        action = Action.NONE
        if outcome is Outcome.FAILED:
            failed_actions = [
                inv.on_fail
                for inv, chk in zip(self.invariants, checks, strict=True)
                if chk.outcome is Outcome.FAILED
            ]
            action = min(failed_actions, key=lambda a: _ACTION_SEVERITY[a])
        elif outcome is Outcome.INDETERMINATE:
            # We do not know what happened. Read again before doing anything
            # that moves money.
            action = Action.REREAD

        return Verdict(
            outcome=outcome,
            intent=self.label,
            checks=checks,
            recommended_action=action,
            read_errors=snap.read_errors,
        )
