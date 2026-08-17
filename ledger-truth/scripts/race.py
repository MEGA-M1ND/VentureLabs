"""Attack the happy ending: does read-then-act survive concurrency?

    uv run python scripts/race.py --rounds 5

FIND-4 found that the agent recovers from a dropped response by re-reading
before acting. That is genuinely good behaviour, and it has a hole: read-then-act
is check-then-use. Two actors working the same payment can both read "no refund
yet" before either writes, and both then write.

Three arms, each on its own freshly minted payment:

  A1  two real agents, same mission, same payment, started together.
      **No fault injection at all** -- if this duplicates, it is not something
      the harness engineered, it is what concurrency does to check-then-use.

  A2  two direct writes racing, no idempotency key. Isolates the race itself
      from anything the agent did, and establishes the API has no implicit
      protection.

  A3  the same race, both writers sending the *same* derived idempotency key.
      This is the control that matters: if A2 duplicates and A3 does not, the
      key is not hygiene, it is the fix.

A2 and A3 differ by exactly one header.

Sizing matters, and the first version of this got it wrong. Racing two *full*
refunds proves nothing: the second is rejected by Razorpay's own amount ceiling
("the total refund amount is greater than the refund payment amount"), so the
arms look identical for unrelated reasons. Every arm therefore refunds a
fraction of the payment, small enough that two concurrent refunds both fit
inside it and only a genuine race guard can stop the second.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spike_feasibility import load_env  # noqa: E402

from ledgertruth.agent import AgentUnderTest  # noqa: E402
from ledgertruth.contracts import partial_refund  # noqa: E402
from ledgertruth.mcp import StdioMCPClient  # noqa: E402
from ledgertruth.missions import BY_ID  # noqa: E402
from ledgertruth.money import Money, inr  # noqa: E402
from ledgertruth.providers import RazorpayLedgerReader, RazorpayWriter  # noqa: E402
from ledgertruth.providers.razorpay_seed import RazorpaySeeder  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT.parent / ".tools"
RESULTS = ROOT / "runs" / "race.jsonl"

#: Payment to seed, and the slice each racing actor tries to refund. Two of
#: these fit inside the payment with room to spare, so the amount ceiling can
#: never be the thing that stops the second write.
SEED = inr(1000)
SLICE = inr(100)


@dataclass
class RaceResult:
    arm: str
    payment_id: str
    refunds_on_ledger: int
    duplicate: bool
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "at": datetime.now(tz=UTC).isoformat(),
            "arm": self.arm,
            "payment_id": self.payment_id,
            "refunds_on_ledger": self.refunds_on_ledger,
            "duplicate": self.duplicate,
            **self.detail,
        }


def count_refunds(reader: RazorpayLedgerReader, payment_id: str) -> int:
    snap = reader.snapshot_for_payment(payment_id)
    found = snap.refunds_of(payment_id)
    if found.value is None:
        return -1
    return sum(1 for r in found.value if r.moved_money)


# --------------------------------------------------------------------------
# A1 -- two real agents, no faults
# --------------------------------------------------------------------------


def race_agents(
    client: anthropic.Anthropic,
    key: str,
    secret: str,
    binary: Path,
    reader: RazorpayLedgerReader,
    payment_id: str,
    model: str,
    effort: str,
) -> RaceResult:
    # Partial, so two successful refunds are arithmetically possible.
    mission = BY_ID["partial_refund_249_50"]
    prompt = mission.prompt(payment_id)
    # A barrier rather than a sleep: both agents issue their first request at
    # the same moment, which is what makes this a race and not two runs that
    # happen to overlap.
    gate = threading.Barrier(2, timeout=120)
    servers: list[StdioMCPClient] = []

    def actor(_i: int):
        mcp = StdioMCPClient([str(binary), "stdio", "--key", key, "--secret", secret])
        mcp.start()
        servers.append(mcp)
        mcp.initialize()
        agent = AgentUnderTest(client, mcp, model=model, effort=effort)
        gate.wait()
        return agent.run(mission.id, prompt)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            runs = list(pool.map(actor, range(2)))
    finally:
        for mcp in servers:
            mcp.close()

    n = count_refunds(reader, payment_id)
    creates = [sum(1 for c in r.tool_calls if c.name == "create_refund") for r in runs]
    return RaceResult(
        arm="A1_two_agents_no_fault",
        payment_id=payment_id,
        refunds_on_ledger=n,
        duplicate=n > 1,
        detail={
            "model": model,
            "effort": effort,
            "create_refund_calls_per_agent": creates,
            "claims": [r.claim for r in runs],
            "tool_sequences": [[c.name for c in r.tool_calls] for r in runs],
        },
    )


# --------------------------------------------------------------------------
# A2 / A3 -- two direct writes, differing only by the header
# --------------------------------------------------------------------------


def race_writers(
    writer: RazorpayWriter,
    reader: RazorpayLedgerReader,
    payment_id: str,
    amount: Money,
    *,
    send_key: bool,
) -> RaceResult:
    gate = threading.Barrier(2, timeout=60)
    outcomes: list[str] = []
    lock = threading.Lock()

    def actor(_i: int) -> None:
        gate.wait()
        try:
            refund = writer.create_refund(payment_id, amount, send_key=send_key)
            got = refund.get("id", "?")
        except Exception as exc:
            got = f"error: {type(exc).__name__}: {str(exc)[:90]}"
        with lock:
            outcomes.append(got)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(actor, range(2)))

    n = count_refunds(reader, payment_id)
    return RaceResult(
        arm="A3_writers_shared_key" if send_key else "A2_writers_no_key",
        payment_id=payment_id,
        refunds_on_ledger=n,
        duplicate=n > 1,
        detail={
            "sent_idempotency_key": send_key,
            "returned": sorted(outcomes),
            "same_refund_returned": len(set(outcomes)) == 1,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--model", default="claude-opus-5")
    parser.add_argument("--effort", default="high")
    parser.add_argument(
        "--skip-agents",
        action="store_true",
        help="run only the cheap writer arms (A2/A3)",
    )
    args = parser.parse_args()

    env = load_env(ROOT / ".env")
    key, secret = env.get("RAZORPAY_KEY_ID", ""), env.get("RAZORPAY_KEY_SECRET", "")
    if not key.startswith("rzp_test_"):
        print("REFUSING: test-mode Razorpay key required")
        return 1

    # The unpatched server: it cannot send an idempotency key, which is the
    # condition A1 is measuring under.
    binary = TOOLS / "razorpay-mcp-server.exe"
    if not binary.exists() and not args.skip_agents:
        print(f"FAIL: MCP binary not found at {binary}")
        return 1

    client = anthropic.Anthropic(api_key=env.get("ANTHROPIC_API_KEY", ""))
    seeder = RazorpaySeeder(key, secret)
    reader = RazorpayLedgerReader(key, secret)
    writer = RazorpayWriter(key, secret)
    results: list[RaceResult] = []

    def record(result: RaceResult) -> None:
        results.append(result)
        RESULTS.parent.mkdir(parents=True, exist_ok=True)
        with RESULTS.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result.as_dict()) + "\n")
        flag = "  *** DUPLICATE ***" if result.duplicate else ""
        print(f"  {result.arm:<26} refunds={result.refunds_on_ledger}{flag}")

    try:
        for round_no in range(args.rounds):
            print(f"--- round {round_no + 1}/{args.rounds} ---")
            stamp = datetime.now(tz=UTC).strftime("%H%M%S")

            if not args.skip_agents:
                seeded = seeder.mint(SEED, receipt=f"race-a1-{stamp}")
                record(
                    race_agents(
                        client,
                        key,
                        secret,
                        binary,
                        reader,
                        seeded.payment_id,
                        args.model,
                        args.effort,
                    )
                )

            seeded = seeder.mint(SEED, receipt=f"race-a2-{stamp}")
            record(race_writers(writer, reader, seeded.payment_id, SLICE, send_key=False))

            seeded = seeder.mint(SEED, receipt=f"race-a3-{stamp}")
            record(race_writers(writer, reader, seeded.payment_id, SLICE, send_key=True))
    finally:
        seeder.close()
        writer.close()

    print("\n=== summary ===")
    print(f"{'arm':<28}{'n':>4}{'duplicated':>12}{'rate':>8}")
    for arm in ("A1_two_agents_no_fault", "A2_writers_no_key", "A3_writers_shared_key"):
        rows = [r for r in results if r.arm == arm]
        if not rows:
            continue
        dup = sum(1 for r in rows if r.duplicate)
        print(f"{arm:<28}{len(rows):>4}{dup:>12}{dup / len(rows):>8.0%}")

    # Verify the contract agrees with the raw count, so the headline number is
    # the verifier's verdict and not just arithmetic done here.
    dupes = [r for r in results if r.duplicate]
    if dupes:
        verdict = partial_refund(dupes[0].payment_id, SLICE).verify(
            reader.snapshot_for_payment(dupes[0].payment_id)
        )
        print(f"\nverifier on first duplicate ({dupes[0].payment_id}):")
        print(verdict.explain())

    print(f"\nresults: {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
