"""Feasibility spike: can a payment be minted and held in 'authorized' status?

`RazorpaySeeder.mint()` auto-captures. This calls the same two private steps
and stops before capture, to check what status test-mode actually lands the
payment in and whether the explicit capture call behaves as documented
("already captured" on a second attempt).

    uv run python scripts/spike_capture.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spike_feasibility import load_env  # noqa: E402

from ledgertruth.money import inr  # noqa: E402
from ledgertruth.providers.razorpay_seed import RazorpaySeeder  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    env = load_env(ROOT / ".env")
    key, secret = env.get("RAZORPAY_KEY_ID", ""), env.get("RAZORPAY_KEY_SECRET", "")
    if not key.startswith("rzp_test_"):
        print("REFUSING: test-mode Razorpay key required")
        return 1

    seeder = RazorpaySeeder(key, secret)
    amount = inr(500)
    try:
        order_id = seeder._create_order(amount, receipt="spike-capture-1")
        payment_id = seeder._create_payment(order_id, amount)
        print(f"order={order_id} payment={payment_id}")

        resp = seeder._client.get(f"/v1/payments/{payment_id}", auth=seeder._auth)
        status = resp.json().get("status")
        print(f"status immediately after checkout call: {status}")

        if status != "authorized":
            print("account auto-captures; cannot naturally land in 'authorized' this way")
            return 0

        cap1 = seeder._client.post(
            f"/v1/payments/{payment_id}/capture",
            auth=seeder._auth,
            json={"amount": amount.minor, "currency": amount.currency},
        )
        print(f"first capture: HTTP {cap1.status_code} {cap1.json().get('status')}")

        cap2 = seeder._client.post(
            f"/v1/payments/{payment_id}/capture",
            auth=seeder._auth,
            json={"amount": amount.minor, "currency": amount.currency},
        )
        print(f"second capture (same amount): HTTP {cap2.status_code} {cap2.text[:200]}")
    finally:
        seeder.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
