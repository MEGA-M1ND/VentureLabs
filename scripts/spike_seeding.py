"""Probe: how do we mint CAPTURED payments at scale in test mode?

The experiment needs ~40 independent refundable payments. The account currently
has one. This script probes every server-side payment-creation path to find one
that is enabled, and verifies that refund create + read-back works.

Run with --refund to perform a real (test-mode) 1 rupee refund.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spike_feasibility import load_env  # noqa: E402

BASE = "https://api.razorpay.com"


def main() -> int:
    env = load_env(Path(__file__).resolve().parent.parent / ".env")
    key, secret = env["RAZORPAY_KEY_ID"], env["RAZORPAY_KEY_SECRET"]
    assert key.startswith("rzp_test_"), "test mode only"
    client = httpx.Client(base_url=BASE, auth=(key, secret), timeout=30.0)

    order = client.post(
        "/v1/orders",
        json={"amount": 100000, "currency": "INR", "receipt": "seed-probe"},
    ).json()
    order_id = order.get("id", "")
    print(f"seed order: {order_id}\n")

    common = {
        "amount": 100000,
        "currency": "INR",
        "order_id": order_id,
        "email": "spike@example.com",
        "contact": "9999999999",
    }

    print("--- payment-creation endpoint probe ---")
    probes: list[tuple[str, dict]] = [
        ("/v1/payments/create/json", {**common, "method": "card",
                                      "card": {"number": "4111111111111111", "cvv": "123",
                                               "expiry_month": "12", "expiry_year": "30",
                                               "name": "Test"}}),
        ("/v1/payments/create/ajax", {**common, "method": "card"}),
        ("/v1/payments/create/upi", {**common, "method": "upi",
                                     "upi": {"flow": "collect", "vpa": "success@razorpay"}}),
        ("/v1/payments", {**common, "method": "card"}),
        ("/v1/payments/create/checkout", {**common, "method": "card"}),
    ]
    for path, payload in probes:
        try:
            r = client.post(path, json=payload)
        except Exception as exc:  # pragma: no cover - network
            print(f"  {path:<34} EXC {exc}")
            continue
        note = ""
        try:
            j = r.json()
            if not r.is_success:
                err = j.get("error", {})
                note = f"{err.get('code','?')}: {err.get('description','')[:80]}"
            else:
                note = str(j)[:120]
        except Exception:
            note = r.text[:80]
        print(f"  {path:<34} {r.status_code}  {note}")

    # ---- refund path ------------------------------------------------------
    print("\n--- refund capability ---")
    payments = client.get("/v1/payments", params={"count": 20}).json().get("items", [])
    refundable = [
        p for p in payments
        if p.get("status") == "captured" and p.get("amount", 0) > p.get("amount_refunded", 0)
    ]
    if not refundable:
        print("  no refundable payment")
        return 0

    target = refundable[0]
    pid = target["id"]
    print(f"  target {pid} amount={target['amount']} refunded={target['amount_refunded']}")

    if "--refund" not in sys.argv:
        print("  (pass --refund to actually create one)")
        return 0

    # Idempotency: Razorpay honours a per-request idempotency key header.
    r = client.post(
        f"/v1/payments/{pid}/refund",
        json={"amount": 100, "speed": "normal", "notes": {"src": "ledgertruth-spike"}},
        headers={"X-Payment-Idempotency-Key": "ledgertruth-spike-001"},
    )
    print(f"  POST refund -> {r.status_code}: {str(r.json())[:260]}")
    if not r.is_success:
        return 1
    refund = r.json()

    rb = client.get(f"/v1/refunds/{refund['id']}")
    print(f"  read-back   -> {rb.status_code}: {str(rb.json())[:260]}")

    lst = client.get(f"/v1/payments/{pid}/refunds")
    print(f"  refunds for payment -> {lst.status_code}: count={lst.json().get('count')}")

    after = client.get(f"/v1/payments/{pid}").json()
    print(f"  payment now: status={after['status']} amount_refunded={after['amount_refunded']}")

    # Does the same idempotency key suppress a duplicate?
    r2 = client.post(
        f"/v1/payments/{pid}/refund",
        json={"amount": 100, "speed": "normal"},
        headers={"X-Payment-Idempotency-Key": "ledgertruth-spike-001"},
    )
    same = r2.is_success and r2.json().get("id") == refund["id"]
    print(f"  replay same idempotency key -> {r2.status_code}, same refund id: {same}")
    if r2.is_success and not same:
        print(f"    !! created a SECOND refund {r2.json().get('id')} -- key not honoured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
