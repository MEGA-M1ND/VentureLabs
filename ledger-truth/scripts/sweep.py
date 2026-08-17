"""Degradation sweep: does safe recovery survive down the model ladder?

    uv run python scripts/sweep.py --runs 10

FIND-4 claims that zero duplicate refunds is *model behaviour*, not a platform
guarantee -- the agent chose to re-read after a dropped response rather than
retry. That claim predicts its own falsification: weaken the model or the effort
and duplicates should appear.

This runs the identical `drop_after_commit` fault across a ladder of model and
effort settings and reports the duplicate rate per cell. Cells are independent:
each gets a fresh proxy and a fresh MCP process, so a fault budget or a wedged
server cannot leak across the comparison.

Note the distinction the analysis has to preserve: a weaker model may fail the
mission outright (never refunds at all), which is a *different* outcome from
refunding twice. Only the second falsifies FIND-4. Both are reported.
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
RECORDS = ROOT / "runs" / "sweep.jsonl"
SUMMARY = ROOT / "runs" / "sweep_summary.json"


@dataclass(frozen=True)
class Cell:
    model: str
    effort: str | None

    @property
    def label(self) -> str:
        return f"{self.model}@{self.effort or 'n/a'}"


# The ladder. Opus at high is the FIND-4 baseline; everything below it is a
# weakening in exactly one dimension at a time.
LADDER: tuple[Cell, ...] = (
    Cell("claude-opus-5", "high"),
    Cell("claude-opus-5", "low"),
    Cell("claude-sonnet-5", "high"),
    Cell("claude-sonnet-5", "low"),
    # Haiku 4.5 rejects `output_config.effort` outright, so the cell is simply
    # "no effort control" rather than a comparable rung.
    Cell("claude-haiku-4-5", None),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=10, help="runs per cell")
    parser.add_argument("--mission", default="full_refund")
    parser.add_argument("--chaos", default="drop_refund_response", choices=sorted(PROFILES))
    parser.add_argument("--model", action="append", help="restrict to these models")
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

    cells = [c for c in LADDER if not args.model or c.model in args.model]
    mission = BY_ID[args.mission]
    client = anthropic.Anthropic(api_key=anthropic_key)
    seeder = RazorpaySeeder(key, secret)
    reader = RazorpayLedgerReader(key, secret)

    started = datetime.now(tz=UTC)
    print(
        f"sweep: {len(cells)} cells x {args.runs} runs, fault={args.chaos}, mission={mission.id}\n"
    )

    tally: dict[str, dict] = {}

    try:
        for cell in cells:
            print(f"=== {cell.label} ===")
            counts = {
                "runs": 0,
                "verified": 0,
                "failed": 0,
                "indeterminate": 0,
                "duplicates": 0,
                "multi_create_calls": 0,
                "false_success_a": 0,
                "aborted": 0,
                "errors": 0,
            }

            # Fresh proxy + server per cell: a leftover fault budget or a wedged
            # process would silently bias the next cell's numbers.
            proxy = ChaosProxy(rules=list(PROFILES[args.chaos].rules)).start()
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
                            seeded = seeder.mint(mission.seed_amount, receipt=f"sw-{stamp}-{i}")
                            proxy._applied.clear()
                            run = agent.run(mission.id, mission.prompt(seeded.payment_id))
                            snap = reader.snapshot_for_payment(seeded.payment_id)
                            record = score(mission, seeded.payment_id, run, snap, chaos=args.chaos)
                            write_record(record, RECORDS)
                        except Exception as exc:
                            # One bad run must not abort a multi-hour sweep.
                            counts["errors"] += 1
                            print(f"  [{i + 1}/{args.runs}] ERROR {type(exc).__name__}: {exc}")
                            traceback.print_exc(limit=1, file=sys.stdout)
                            continue

                        counts["runs"] += 1
                        counts[record.arm_c.value.lower()] += 1
                        creates = sum(1 for c in run.tool_calls if c.name == "create_refund")
                        if creates > 1:
                            counts["multi_create_calls"] += 1
                        if record.duplicate_money_movement:
                            counts["duplicates"] += 1
                        if record.false_success_a:
                            counts["false_success_a"] += 1
                        if run.aborted:
                            counts["aborted"] += 1

                        flag = ""
                        if record.duplicate_money_movement:
                            flag = "  *** DUPLICATE ***"
                        elif record.false_success_a:
                            flag = "  *** FALSE SUCCESS ***"
                        print(
                            f"  [{i + 1}/{args.runs}] C={record.arm_c.value:<13}"
                            f" creates={creates} tools={len(run.tool_calls)}{flag}"
                        )
            finally:
                proxy.stop()

            tally[cell.label] = counts
            print(f"  -> {counts}\n")
            SUMMARY.parent.mkdir(parents=True, exist_ok=True)
            SUMMARY.write_text(
                json.dumps(
                    {
                        "started_at": started.isoformat(),
                        "updated_at": datetime.now(tz=UTC).isoformat(),
                        "fault": args.chaos,
                        "mission": mission.id,
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

    print("=== sweep complete ===")
    print(f"{'cell':<34}{'n':>4}{'verified':>10}{'failed':>8}{'dupes':>7}{'>1 create':>11}")
    for label, c in tally.items():
        print(
            f"{label:<34}{c['runs']:>4}{c['verified']:>10}{c['failed']:>8}"
            f"{c['duplicates']:>7}{c['multi_create_calls']:>11}"
        )
    print(f"\nrecords: {RECORDS}\nsummary: {SUMMARY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
