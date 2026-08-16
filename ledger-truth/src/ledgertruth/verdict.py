"""Three-valued verdicts and the evidence trail behind them."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Outcome(StrEnum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    INDETERMINATE = "INDETERMINATE"


#: Lower number wins when combining. A proven violation stays proven even if a
#: sibling check could not be evaluated -- unreadable state cannot un-prove a
#: fact we already established. VERIFIED only survives if nothing else did.
_PRECEDENCE = {Outcome.FAILED: 0, Outcome.INDETERMINATE: 1, Outcome.VERIFIED: 2}


def combine(outcomes: list[Outcome]) -> Outcome:
    if not outcomes:
        return Outcome.INDETERMINATE
    return min(outcomes, key=lambda o: _PRECEDENCE[o])


class Action(StrEnum):
    NONE = "none"
    RETRY_IDEMPOTENT = "retry_idempotent"
    COMPENSATE = "compensate"
    ESCALATE = "escalate"
    REREAD = "reread"


@dataclass(frozen=True)
class CheckResult:
    """One invariant's evaluation, carrying enough detail to argue with."""

    invariant: str
    outcome: Outcome
    expected: str
    observed: str
    detail: str = ""

    def __str__(self) -> str:
        line = (
            f"[{self.outcome.value}] {self.invariant}: "
            f"expected {self.expected}, observed {self.observed}"
        )
        return f"{line} ({self.detail})" if self.detail else line


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    intent: str
    checks: tuple[CheckResult, ...] = ()
    recommended_action: Action = Action.NONE
    read_errors: tuple[str, ...] = field(default=())

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.outcome is Outcome.FAILED)

    @property
    def unresolved(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.outcome is Outcome.INDETERMINATE)

    def explain(self) -> str:
        lines = [f"{self.outcome.value}: {self.intent}"]
        lines += [f"  {c}" for c in self.checks]
        if self.read_errors:
            lines.append("  read errors:")
            lines += [f"    - {e}" for e in self.read_errors]
        if self.recommended_action is not Action.NONE:
            lines.append(f"  recommended action: {self.recommended_action.value}")
        return "\n".join(lines)
