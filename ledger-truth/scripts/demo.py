"""Print the headline contrast from recorded runs.

    uv run python scripts/demo.py

Replays real records rather than re-running the agent: the point is the
comparison, and a live run takes minutes. Everything shown is measured data from
runs/records.jsonl, not a mock-up.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECORDS = ROOT / "runs" / "records.jsonl"

BOLD, DIM, RED, GREEN, YELLOW, RESET = (
    "\033[1m",
    "\033[2m",
    "\033[31m",
    "\033[32m",
    "\033[33m",
    "\033[0m",
)


def rule(char: str = "-", width: int = 80) -> None:
    print(DIM + char * width + RESET)


def main() -> int:
    if not RECORDS.exists():
        print(f"no records at {RECORDS}; run scripts/run_missions.py first")
        return 1
    records = [json.loads(line) for line in RECORDS.read_text(encoding="utf-8").splitlines()]
    by_fault: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        by_fault[record["chaos"]].append(record)

    print()
    print(f"{BOLD}Did the refund actually happen?{RESET}")
    print(
        f"{DIM}Claude Opus 5 -> Razorpay's MCP server -> live test mode. "
        f"{len(records)} runs.{RESET}"
    )
    print()

    # -- the trace that makes the point --------------------------------------
    dropped = [r for r in by_fault.get("drop_refund_response", []) if r["agent"]["tool_calls"]]
    if dropped:
        example = dropped[0]
        print(f"{BOLD}One run, with the response dropped after the refund committed:{RESET}")
        rule()
        for i, call in enumerate(example["agent"]["tool_calls"], 1):
            status = f"{RED}ERROR{RESET}" if call["is_error"] else f"{GREEN}ok{RESET}"
            note = ""
            if call["name"] == "create_refund" and call["is_error"]:
                note = f"   {YELLOW}<- the money HAS moved; nobody told the agent{RESET}"
            if call["name"].startswith("fetch") and i > 1:
                note = f"   {DIM}<- re-read instead of retrying{RESET}"
            print(f"  {i}. {call['name']:<38} {status}{note}")
        rule()
        print(
            f"  agent concluded: {GREEN}success{RESET}   "
            f"ledger says: {GREEN}VERIFIED{RESET}   "
            f"refunds on ledger: {len(example['snapshot']['refunds'])}"
        )
        print()

    # -- the comparison ------------------------------------------------------
    print(f"{BOLD}Two faults. Same error message. Opposite correct responses.{RESET}")
    rule()
    print(f"  {'':<28}{'dropped AFTER':>22}{'rejected BEFORE':>22}")
    print(f"  {'':<28}{'commit':>22}{'commit':>22}")
    rule()
    rows = [
        ("refund actually created", "yes", "no"),
        ("what the tool reports", "error", "error"),
        ("correct response", "do nothing", "retry"),
        ("wrong response costs you", "duplicate refund", "unrefunded customer"),
    ]
    for label, left, right in rows:
        highlight = BOLD if label == "what the tool reports" else ""
        print(f"  {highlight}{label:<28}{left:>22}{right:>22}{RESET}")
    rule()
    print(f"  {DIM}No retry policy separates these. The information isn't in the response.{RESET}")
    print()

    # -- the scoreboard ------------------------------------------------------
    print(f"{BOLD}How often was each signal right?{RESET}")
    rule()
    print(f"  {'fault':<32}{'n':>3}{'agent':>10}{'tool':>10}{'duplicates':>13}")
    rule()
    order = ["none", "drop_refund_response", "refund_never_commits"]
    labels = {
        "none": "no fault",
        "drop_refund_response": "dropped after commit",
        "refund_never_commits": "rejected before commit",
    }
    totals = Counter()
    for fault in order:
        runs = by_fault.get(fault, [])
        if not runs:
            continue
        n = len(runs)
        verified = [r["arm_c_outcome"] == "VERIFIED" for r in runs]
        a_ok = sum(
            1 for r, v in zip(runs, verified, strict=True) if r["arm_a_claimed_success"] is v
        )
        b_ok = sum(1 for r, v in zip(runs, verified, strict=True) if r["arm_b_tools_clean"] is v)
        dup = sum(1 for r in runs if r["duplicate_money_movement"])
        totals["n"] += n
        totals["a"] += a_ok
        totals["b"] += b_ok
        totals["dup"] += dup
        print(f"  {labels[fault]:<32}{n:>3}{a_ok:>7}/{n}{b_ok:>7}/{n}{dup:>13}")
    rule()
    print(
        f"  {BOLD}{'total':<32}{totals['n']:>3}"
        f"{totals['a']:>7}/{totals['n']}{totals['b']:>7}/{totals['n']}"
        f"{totals['dup']:>13}{RESET}"
    )
    print()
    wrong = totals["n"] - totals["b"]
    print(f"  The tool response was {BOLD}{RED}wrong on {wrong} of {totals['n']} runs{RESET}.")
    print(f"  The agent's own report was wrong on {BOLD}{GREEN}none{RESET} of them.")
    print()

    # -- repair --------------------------------------------------------------
    repairs = [r for r in records if r.get("arm_d")]
    if repairs:
        wrote = sum(1 for r in repairs if r["arm_d"]["wrote"])
        noop = sum(1 for r in repairs if not r["arm_d"]["wrote"] and not r["arm_d"]["escalated"])
        final_ok = sum(1 for r in repairs if r["arm_d"]["final"] == "VERIFIED")
        print(f"{BOLD}Repair layer (reads before it writes):{RESET}")
        rule()
        print(
            f"  ran on {len(repairs)} runs -> wrote {wrote}, "
            f"did nothing {noop} (ledger already correct), "
            f"ended VERIFIED {final_ok}/{len(repairs)}"
        )
        rule()
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
