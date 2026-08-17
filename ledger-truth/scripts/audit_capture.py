"""Write-safety audit, arm 2: capture_payment.

    uv run python scripts/audit_capture.py --runs 8

FIND-1/6/7 measured create_refund, the one MCP write tool that sits on a
Razorpay endpoint documented to support an idempotency key. Grepping the SDK
(razorpay-go@v1.4.0) shows every Create/Update call across all 17 write tools
passes `nil` for extra headers -- but idempotency keys are only meaningful
where Razorpay's API honours one at all, and that's just refunds, payouts
(no create_payout tool exists in this server), and route transfers (also not
exposed). Everything else has to be safe some other way, or not be safe.

capture_payment is the cleanest next case: it is money-moving (an authorized
hold becomes captured, real settlement), and Razorpay's own state machine
already blocks a second capture -- confirmed live in scripts/spike_capture.py,
a second POST to /v1/payments/{id}/capture returns 400 "This payment has
already been captured". So the risk here is not duplicate money movement
(structurally impossible -- capture is a status field on one object, not a
new line item per call, unlike refunds). The risk is the mirror image: does
the agent read that rejection correctly?

Under drop_after_commit, the capture always genuinely lands upstream before
the response is dropped. So arm C should read CAPTURED on every single run --
this fault cannot produce a real failure. The question this measures is
whether the agent's own report matches that, i.e. `false_failure_a`: agent
says the capture failed or needs escalation, when the ledger shows it
plainly worked. That's not a money bug. It's the thing that gets a customer
told "your payment didn't go through, please try again" when it did --
which, if they believe it and pay a second time, becomes a money bug anyway,
just one this contract doesn't reach.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spike_feasibility import load_env  # noqa: E402

from ledgertruth.agent import AgentUnderTest  # noqa: E402
from ledgertruth.chaos import PROFILES, ChaosProxy  # noqa: E402
from ledgertruth.mcp import StdioMCPClient  # noqa: E402
from ledgertruth.missions import BY_ID  # noqa: E402
from ledgertruth.providers import RazorpayLedgerReader  # noqa: E402
from ledgertruth.providers.razorpay_seed import RazorpaySeeder  # noqa: E402
from ledgertruth.runner import score, write_record  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT.parent / ".tools"
MISSION = BY_ID["capture_authorized"]


@dataclass(frozen=True)
class Cell:
    model: str
    effort: str | None

    @property
    def label(self) -> str:
        return f"{self.model}@{self.effort or 'n/a'}"


# The same three anchor points FIND-7 used, so this result sits on the same
# axis as the duplicate-rate table rather than needing its own scale.
LADDER: tuple[Cell, ...] = (
    Cell("claude-opus-5", "high"),
    Cell("claude-sonnet-5", "high"),
    Cell("claude-haiku-4-5", None),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=8, help="runs per cell")
    parser.add_argument("--model", action="append", help="restrict to these models")
    parser.add_argument("--out", default="runs/capture_audit.jsonl")
    args = parser.parse_args()

    env = load_env(ROOT / ".env")
    key, secret = env.get("RAZORPAY_KEY_ID", ""), env.get("RAZORPAY_KEY_SECRET", "")
    anthropic_key = env.get("ANTHROPIC_API_KEY", "")
    if not key.startswith("rzp_test_"):
        print("REFUSING: test-mode Razorpay key required")
        return 1

    binary = TOOLS / "razorpay-mcp-chaos.exe"
    if not binary.exists():
        print(f"FAIL: chaos-capable MCP binary not found at {binary}")
        return 1

    records_path = ROOT / args.out
    summary_path = records_path.with_name(records_path.stem + "_summary.json")
    cells = [c for c in LADDER if not args.model or c.model in args.model]
    client = anthropic.Anthropic(api_key=anthropic_key)
    seeder = RazorpaySeeder(key, secret)
    reader = RazorpayLedgerReader(key, secret)

    started = datetime.now(tz=UTC)
    print(f"capture audit: {len(cells)} cells x {args.runs} runs, mission={MISSION.id}\n")

    tally: dict[str, dict] = {}

    try:
        for cell in cells:
            print(f"=== {cell.label} ===")
            counts = {
                "runs": 0,
                "verified": 0,
                "failed": 0,
                "indeterminate": 0,
                "false_failure_a": 0,
                "false_success_a": 0,
                "multi_capture_calls": 0,
                "aborted": 0,
                "errors": 0,
            }

            proxy = ChaosProxy(rules=list(PROFILES["drop_capture_response"].rules)).start()
            try:
                with StdioMCPClient(
                    [str(binary), "stdio", "--key", key, "--secret", secret],
                    env={"RAZORPAY_API_BASE_URL": proxy.base_url},
                ) as mcp:
                    mcp.initialize()
                    agent = AgentUnderTest(client, mcp, model=cell.model, effort=cell.effort)

                    for i in range(args.runs):
                        stamp = datetime.now(tz=UTC).strftime("%H%M%S")
                        try:
                            seeded = seeder.mint_authorized(
                                MISSION.seed_amount, receipt=f"cap-{stamp}-{i}"
                            )
                            proxy._applied.clear()
                            run = agent.run(MISSION.id, MISSION.prompt(seeded.payment_id))
                            snap = reader.snapshot_for_payment(seeded.payment_id)
                            record = score(
                                MISSION,
                                seeded.payment_id,
                                run,
                                snap,
                                chaos="drop_capture_response",
                            )
                            write_record(record, records_path)
                        except Exception as exc:
                            counts["errors"] += 1
                            print(f"  [{i + 1}/{args.runs}] ERROR {type(exc).__name__}: {exc}")
                            traceback.print_exc(limit=1, file=sys.stdout)
                            continue

                        counts["runs"] += 1
                        counts[record.arm_c.value.lower()] += 1
                        captures = sum(1 for c in run.tool_calls if c.name == "capture_payment")
                        if captures > 1:
                            counts["multi_capture_calls"] += 1
                        if record.false_failure_a:
                            counts["false_failure_a"] += 1
                        if record.false_success_a:
                            counts["false_success_a"] += 1
                        if run.aborted:
                            counts["aborted"] += 1

                        flag = ""
                        if record.false_failure_a:
                            flag = "  *** FALSE FAILURE: ledger says captured, agent said no ***"
                        print(
                            f"  [{i + 1}/{args.runs}] C={record.arm_c.value:<13}"
                            f" A={record.arm_a} captures={captures}"
                            f" tools={len(run.tool_calls)}{flag}"
                        )
            finally:
                proxy.stop()

            tally[cell.label] = counts
            print(f"  -> {counts}\n")
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(
                json.dumps(
                    {
                        "started_at": started.isoformat(),
                        "updated_at": datetime.now(tz=UTC).isoformat(),
                        "mission": MISSION.id,
                        "chaos": "drop_capture_response",
                        "runs_per_cell": args.runs,
                        "cells": tally,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
    finally:
        seeder.close()
        reader.close()

    print("=== audit complete ===")
    print(f"{'cell':<26}{'n':>4}{'verified':>10}{'false_failure':>15}{'multi_capture':>15}")
    for label, c in tally.items():
        print(
            f"{label:<26}{c['runs']:>4}{c['verified']:>10}"
            f"{c['false_failure_a']:>15}{c['multi_capture_calls']:>15}"
        )
    print(f"\nrecords: {records_path}\nsummary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
