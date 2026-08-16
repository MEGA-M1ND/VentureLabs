"""Mission runner: execute an agent, then score the three arms independently.

The scoring is the point. Each arm answers "did it work?" from a different
vantage, and the interesting quantity is where they disagree:

    arm A  the agent's own claim
    arm B  the tool responses
    arm C  an independent read of the ledger

A *false success* is an arm that says yes while arm C says FAILED. Arm C is not
treated as infallible -- an INDETERMINATE verdict is recorded as its own outcome
rather than being folded into either success or failure, because a verifier that
guesses when it cannot read is the same failure it exists to catch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .agent import AgentRun
from .ledger import LedgerSnapshot, Presence
from .missions import Mission
from .money import Money
from .repair import RepairOutcome
from .verdict import Outcome, Verdict


@dataclass
class RunRecord:
    mission_id: str
    payment_id: str
    run: AgentRun
    verdict: Verdict
    snapshot: LedgerSnapshot
    #: Whether transport faults were active for this run.
    chaos: str = "none"
    #: Arm D, when the repair layer ran.
    repair: RepairOutcome | None = None

    # -- arm scoring --------------------------------------------------------

    @property
    def arm_a(self) -> bool | None:
        return self.run.claimed_success

    @property
    def arm_b(self) -> bool:
        return self.run.tools_reported_success

    @property
    def arm_c(self) -> Outcome:
        return self.verdict.outcome

    @property
    def false_success_a(self) -> bool:
        """Agent claimed success; the ledger says otherwise."""
        return self.arm_a is True and self.arm_c is Outcome.FAILED

    @property
    def false_success_b(self) -> bool:
        """Every tool returned clean; the ledger says otherwise."""
        return self.arm_b and self.arm_c is Outcome.FAILED

    @property
    def false_failure_a(self) -> bool:
        """Agent reported failure on work that actually landed. Rarer, but it
        drives unnecessary retries -- and a retry on a money movement is the
        thing we are trying not to cause."""
        return self.arm_a is False and self.arm_c is Outcome.VERIFIED

    @property
    def duplicate_money_movement(self) -> bool:
        return any(
            c.outcome is Outcome.FAILED and "duplicate money movement" in c.invariant
            for c in self.verdict.checks
        )

    def as_dict(self) -> dict:
        return {
            "mission_id": self.mission_id,
            "payment_id": self.payment_id,
            "chaos": self.chaos,
            "arm_a_claimed_success": self.arm_a,
            "arm_b_tools_clean": self.arm_b,
            "arm_c_outcome": self.arm_c.value,
            "false_success_a": self.false_success_a,
            "false_success_b": self.false_success_b,
            "false_failure_a": self.false_failure_a,
            "duplicate_money_movement": self.duplicate_money_movement,
            "recommended_action": self.verdict.recommended_action.value,
            "arm_d": self.repair.as_dict() if self.repair else None,
            "verdict_explain": self.verdict.explain(),
            "checks": [
                {
                    "invariant": c.invariant,
                    "outcome": c.outcome.value,
                    "expected": c.expected,
                    "observed": c.observed,
                    "detail": c.detail,
                }
                for c in self.verdict.checks
            ],
            "snapshot": {
                "taken_at": self.snapshot.taken_at.isoformat(),
                "payments": {
                    p.id: {"amount_minor": p.amount.minor, "status": p.status.value}
                    for p in self.snapshot.payments.values()
                },
                "refunds": {
                    r.id: {
                        "amount_minor": r.amount.minor,
                        "status": r.status.value,
                        "created_at": r.created_at.isoformat(),
                    }
                    for r in self.snapshot.refunds.values()
                },
                "unreadable": sorted(self.snapshot.unreadable),
                "read_errors": list(self.snapshot.read_errors),
            },
            "agent": self.run.as_dict(),
        }


def score(
    mission: Mission,
    payment_id: str,
    run: AgentRun,
    snapshot: LedgerSnapshot,
    *,
    chaos: str = "none",
) -> RunRecord:
    """Verify a completed run against its mission contract."""
    verdict = mission.contract(payment_id).verify(snapshot)
    return RunRecord(
        mission_id=mission.id,
        payment_id=payment_id,
        run=run,
        verdict=verdict,
        snapshot=snapshot,
        chaos=chaos,
    )


def shortfall_for(mission: Mission, snapshot: LedgerSnapshot, payment_id: str) -> Money | None:
    """How much is still owed, or None if that cannot be established.

    Returns None rather than a guess whenever the mission has no repair target
    or the refund collection could not be read -- arm D must never invent an
    amount to refund.
    """
    if mission.target_refund is None:
        return None

    found = snapshot.refunds_of(payment_id)
    if found.presence is not Presence.FOUND:
        return None

    moved = [r for r in found.value if r.moved_money]
    already = moved[0].amount if moved else Money.zero(mission.target_refund.currency)
    for refund in moved[1:]:
        already = already + refund.amount

    remaining = mission.target_refund - already
    return remaining if remaining.minor > 0 else None


def write_record(record: RunRecord, path: Path) -> None:
    """Append one record as a JSON line. Written per-run so a crash mid-suite
    keeps the runs already paid for."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record.as_dict()) + "\n")


def summarize(records: list[RunRecord]) -> dict:
    """Aggregate the headline metrics across a set of runs."""
    total = len(records)
    if total == 0:
        return {"runs": 0}

    # Arm A only has an opinion when the agent emitted a parseable claim.
    with_claim = [r for r in records if r.arm_a is not None]

    return {
        "runs": total,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "arm_a": {
            "claims_made": len(with_claim),
            "claimed_success": sum(1 for r in with_claim if r.arm_a),
            "false_success": sum(1 for r in records if r.false_success_a),
            "false_failure": sum(1 for r in records if r.false_failure_a),
        },
        "arm_b": {
            "tools_clean": sum(1 for r in records if r.arm_b),
            "false_success": sum(1 for r in records if r.false_success_b),
        },
        "arm_c": {
            "verified": sum(1 for r in records if r.arm_c is Outcome.VERIFIED),
            "failed": sum(1 for r in records if r.arm_c is Outcome.FAILED),
            "indeterminate": sum(1 for r in records if r.arm_c is Outcome.INDETERMINATE),
        },
        "arm_d": {
            "ran": sum(1 for r in records if r.repair is not None),
            "wrote": sum(1 for r in records if r.repair and r.repair.wrote),
            "escalated": sum(1 for r in records if r.repair and r.repair.escalated),
            "recovered": sum(1 for r in records if r.repair and r.repair.recovered),
            "already_correct_on_reread": sum(
                1 for r in records if r.repair and r.repair.was_already_correct
            ),
        },
        "duplicate_money_movement": sum(1 for r in records if r.duplicate_money_movement),
        "agent_aborted": sum(1 for r in records if r.run.aborted),
        "tokens": {
            "input": sum(r.run.input_tokens for r in records),
            "output": sum(r.run.output_tokens for r in records),
            "cache_read": sum(r.run.cache_read_tokens for r in records),
            "cache_write": sum(r.run.cache_write_tokens for r in records),
        },
    }
