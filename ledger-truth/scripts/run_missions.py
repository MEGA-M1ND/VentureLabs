"""Run the mission suite end to end and score all three arms.

    uv run python scripts/run_missions.py --repeats 1
    uv run python scripts/run_missions.py --chaos drop_refund_response --repeats 3

Each mission gets a freshly minted payment so runs never contend for ledger
state. Records are appended to runs/records.jsonl as they complete.

With --chaos, a local proxy sits between the MCP server and the Razorpay API and
injects transport faults. The ledger reader and the seeder deliberately do NOT
go through it: arm C has to observe the real ledger by a path the agent did not
use, and seeding through a fault would corrupt the setup rather than the test.
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
from ledgertruth.chaos import PROFILES, ChaosProxy  # noqa: E402
from ledgertruth.mcp import StdioMCPClient  # noqa: E402
from ledgertruth.missions import BY_ID, SUITE  # noqa: E402
from ledgertruth.providers import RazorpayLedgerReader  # noqa: E402
from ledgertruth.providers.razorpay_seed import RazorpaySeeder  # noqa: E402
from ledgertruth.runner import score, summarize, write_record  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT.parent / ".tools"
RECORDS = ROOT / "runs" / "records.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mission", action="append", help="mission id (repeatable)")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default="high")
    parser.add_argument("--chaos", default="none", choices=sorted(PROFILES))
    parser.add_argument(
        "--mcp-bin",
        default=None,
        help="defaults to the chaos-capable build when --chaos is set",
    )
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

    chaos_on = args.chaos != "none"
    default_bin = "razorpay-mcp-chaos.exe" if chaos_on else "razorpay-mcp-server.exe"
    binary = Path(args.mcp_bin) if args.mcp_bin else TOOLS / default_bin
    if not binary.exists():
        print(f"FAIL: MCP binary not found at {binary}")
        return 1

    missions = [BY_ID[m] for m in args.mission] if args.mission else list(SUITE)
    client = anthropic.Anthropic(api_key=anthropic_key)
    # Seeder and reader talk to Razorpay directly, never via the proxy.
    seeder = RazorpaySeeder(key, secret)
    reader = RazorpayLedgerReader(key, secret)
    records = []

    proxy: ChaosProxy | None = None
    mcp_env: dict[str, str] | None = None
    if chaos_on:
        profile = PROFILES[args.chaos]
        proxy = ChaosProxy(rules=list(profile.rules)).start()
        mcp_env = {"RAZORPAY_API_BASE_URL": proxy.base_url}
        print(f"chaos profile '{profile.name}' -> proxy at {proxy.base_url}")

    try:
        with StdioMCPClient(
            [str(binary), "stdio", "--key", key, "--secret", secret], env=mcp_env
        ) as mcp:
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

                    if proxy is not None:
                        # Faults are per-mission, not per-suite: each mission
                        # should meet a fresh dropped response.
                        proxy._applied.clear()

                    run = agent.run(mission.id, mission.prompt(pid))
                    snapshot = reader.snapshot_for_payment(pid)
                    record = score(mission, pid, run, snapshot, chaos=args.chaos)
                    records.append(record)
                    write_record(record, RECORDS)

                    print(
                        f"    A={record.arm_a}  B={record.arm_b}  C={record.arm_c.value}"
                        f"  tools={len(run.tool_calls)}"
                        + (f"  faults={proxy.faults_applied}" if proxy else "")
                        + (f"  ABORTED={run.aborted}" if run.aborted else "")
                    )
                    if record.false_success_a or record.false_success_b:
                        print("    *** FALSE SUCCESS ***")
                    if record.duplicate_money_movement:
                        print("    *** DUPLICATE MONEY MOVEMENT ***")
    finally:
        seeder.close()
        reader.close()
        if proxy is not None:
            proxy.stop()

    print("\n--- summary ---")
    result = summarize(records)
    result["chaos"] = args.chaos
    print(json.dumps(result, indent=2))
    print(f"\nrecords appended to {RECORDS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
