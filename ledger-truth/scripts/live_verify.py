"""Verify a real Razorpay payment against a real contract.

    uv run python scripts/live_verify.py pay_XXXXXXXX [--partial 250]

Reads live test-mode state through RazorpayLedgerReader and prints the verdict.
This is the end-to-end path the harness will use, minus the agent.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spike_feasibility import load_env  # noqa: E402

from ledgertruth import full_refund, inr, partial_refund  # noqa: E402
from ledgertruth.providers import RazorpayLedgerReader  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    payment_id = args[0]

    env = load_env(ROOT / ".env")
    reader = RazorpayLedgerReader(env["RAZORPAY_KEY_ID"], env["RAZORPAY_KEY_SECRET"])
    try:
        snap = reader.snapshot_for_payment(payment_id)
    finally:
        reader.close()

    print(f"snapshot taken {snap.taken_at.isoformat()}")
    for p in snap.payments.values():
        print(f"  payment {p.id}: {p.amount} status={p.status.value}")
    for r in sorted(snap.refunds.values(), key=lambda r: r.created_at):
        print(f"  refund  {r.id}: {r.amount} status={r.status.value}")
    if snap.read_errors:
        print(f"  read errors: {snap.read_errors}")
    print()

    if "--partial" in sys.argv:
        amount = inr(sys.argv[sys.argv.index("--partial") + 1])
        intent = partial_refund(payment_id, amount)
    else:
        intent = full_refund(payment_id)

    print(intent.verify(snap).explain())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
