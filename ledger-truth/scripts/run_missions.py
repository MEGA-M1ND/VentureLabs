"""Run the mission suite end to end and score all three arms.

    uv run python scripts/run_missions.py --repeats 1
    uv run python scripts/run_missions.py --mission full_refund --repeats 3

Each mission gets a freshly minted payment so runs never contend for ledger
state. Records are appended to runs/records.jsonl as they complete.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spike_feasibility import load_env  # noqa: E402

from ledgertruth.agent import DEFAULT_MODEL, AgentUnderTest  # noqa: E402
from ledgertruth.mcp import StdioMCPClient  # noqa: E402
from ledgertruth.missions import BY_ID, SUITE  # noqa: E402
from ledgertruth.providers import RazorpayLedgerReader  # noqa: E402
from ledgertruth.providers.razorpay_seed import RazorpaySeeder  # noqa: E402
from ledgertruth.runner import score, summarize, write_record  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BIN = ROOT.parent / ".tools" / "razorpay-mcp-server.exe"
RECORDS = ROOT / "runs" / "records.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mission", action="append", help="mission id (repeatable)")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default="high")
    parser.add_argument("--mcp-bin", default=str(DEFAULT_BIN))
    args = parser.parse_args()

    env = load_env(ROOT / ".env")
    key, secret = env.get("RAZORPAY_KEY_ID", ""), env.get("RAZORPAY_KEY_SECRET", "")
    anthropic_key = env.get("ANTHROPIC_API_KEY", "")
    if not key.startswith("rzp_test_"):
        print("REFUSING: test-mode Razorpay key required")
        return 1
    if not anthropic_key:
        print("FAIL: ANTHROPIC_API_KEY missing from .env")
        return 1

    binary = Path(args.mcp_bin)
    if not binary.exists():
        print(f"FAIL: MCP binary not found at {binary}")
        return 1

    missions = [BY_ID[m] for m in args.mission] if args.mission else list(SUITE)
    client = anthropic.Anthropic(api_key=anthropic_key)
    seeder = RazorpaySeeder(key, secret)
    reader = RazorpayLedgerReader(key, secret)
    records = []

    try:
        with StdioMCPClient([str(binary), "stdio", "--key", key, "--secret", secret]) as mcp:
            info = mcp.initialize()
            print(f"MCP: {info.get('serverInfo', {}).get('name')} | model: {args.model}\n")
            agent = AgentUnderTest(client, mcp, model=args.model, effort=args.effort)

            for repeat in range(args.repeats):
                for mission in missions:
                    stamp = datetime.now(tz=UTC).strftime("%H%M%S")
                    seeded = seeder.mint(
                        mission.seed_amount, receipt=f"lt-{mission.id[:12]}-{stamp}"
                    )
                    pid = seeded.payment_id
                    print(f"[{repeat + 1}/{args.repeats}] {mission.id} -> {pid}")

                    run = agent.run(mission.id, mission.prompt(pid))
                    snapshot = reader.snapshot_for_payment(pid)
                    record = score(mission, pid, run, snapshot)
                    records.append(record)
                    write_record(record, RECORDS)

                    print(
                        f"    A={record.arm_a}  B={record.arm_b}  C={record.arm_c.value}"
                        f"  tools={len(run.tool_calls)}"
                        + (f"  ABORTED={run.aborted}" if run.aborted else "")
                    )
                    if record.false_success_a or record.false_success_b:
                        print("    *** FALSE SUCCESS ***")
                    if record.duplicate_money_movement:
                        print("    *** DUPLICATE MONEY MOVEMENT ***")
    finally:
        seeder.close()
        reader.close()

    print("\n--- summary ---")
    print(json.dumps(summarize(records), indent=2))
    print(f"\nrecords appended to {RECORDS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
