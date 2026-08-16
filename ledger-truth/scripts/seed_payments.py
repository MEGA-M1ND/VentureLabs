"""Mint captured test payments and record them in a manifest.

    uv run python scripts/seed_payments.py --count 40

Tops the manifest up to --count rather than blindly minting, so re-running is
cheap and does not litter the account. Payments already consumed by a mission
should be marked used in the manifest, not deleted -- knowing what a run
touched matters more than a tidy file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spike_feasibility import load_env  # noqa: E402

from ledgertruth.money import Money, inr  # noqa: E402
from ledgertruth.providers.razorpay_seed import RazorpaySeeder, SeedFailed  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "runs" / "seeded_payments.json"

#: Varied amounts so missions are not all the same shape, and so partial-refund
#: arithmetic has non-round cases that would expose float error.
AMOUNTS = [inr("1000"), inr("249.50"), inr("99.99"), inr("5000"), inr("1"), inr("12345.67")]


def load_manifest() -> list[dict]:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return []


def save_manifest(rows: list[dict]) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=40, help="target number of unused payments")
    parser.add_argument("--amount", type=str, default="", help="fixed amount in rupees")
    args = parser.parse_args()

    env = load_env(ROOT / ".env")
    key, secret = env.get("RAZORPAY_KEY_ID", ""), env.get("RAZORPAY_KEY_SECRET", "")
    if not key:
        print("FAIL: no RAZORPAY_KEY_ID in .env")
        return 1

    rows = load_manifest()
    unused = [r for r in rows if not r.get("used")]
    needed = args.count - len(unused)
    print(f"manifest: {len(rows)} total, {len(unused)} unused -> minting {max(0, needed)}")
    if needed <= 0:
        return 0

    fixed: Money | None = inr(args.amount) if args.amount else None
    seeder = RazorpaySeeder(key, secret)
    minted = 0
    try:
        for i in range(needed):
            amount = fixed or AMOUNTS[i % len(AMOUNTS)]
            receipt = f"lt-seed-{len(rows) + i + 1}"
            try:
                seeded = seeder.mint(amount, receipt=receipt)
            except SeedFailed as exc:
                print(f"  [{i + 1}/{needed}] FAILED: {exc}")
                continue
            row = seeded.as_dict() | {"used": False, "receipt": receipt}
            rows.append(row)
            minted += 1
            print(f"  [{i + 1}/{needed}] {seeded.payment_id} {seeded.amount}")
            # Persist as we go: a rate-limit failure halfway through must not
            # lose the payments already created and paid for.
            save_manifest(rows)
    finally:
        seeder.close()

    print(f"\nminted {minted}, manifest now {len(rows)} rows at {MANIFEST}")
    return 0 if minted == needed else 1


if __name__ == "__main__":
    raise SystemExit(main())
