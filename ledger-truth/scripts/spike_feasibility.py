"""Day-2 feasibility spike against Razorpay test mode.

Answers the load-bearing questions before any harness code depends on them:

  1. do the test keys authenticate?
  2. can we create an order?
  3. can we obtain a CAPTURED payment? (the hard one -- refunds need one)
  4. can we create a refund, and read it back?
  5. do settlement / reconciliation endpoints return anything usable?

Read-only except for order creation, which is free and inert in test mode.
Refund creation is gated behind --write so a stray run cannot mutate state.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

BASE = "https://api.razorpay.com"


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def main() -> int:
    env = load_env(Path(__file__).resolve().parent.parent / ".env")
    key = env.get("RAZORPAY_KEY_ID") or os.environ.get("RAZORPAY_KEY_ID", "")
    secret = env.get("RAZORPAY_KEY_SECRET") or os.environ.get("RAZORPAY_KEY_SECRET", "")

    if not key or not secret:
        print("FAIL: no credentials found in .env or environment")
        return 1
    if not key.startswith("rzp_test_"):
        print(f"REFUSING: key {key[:12]}... is not a test key. This harness is test-mode only.")
        return 1

    print(f"key: {key[:16]}... (test mode confirmed)\n")
    client = httpx.Client(base_url=BASE, auth=(key, secret), timeout=30.0)
    write = "--write" in sys.argv

    def show(label: str, resp: httpx.Response, *, body: bool = False) -> dict | None:
        ok = "OK " if resp.is_success else "ERR"
        print(f"[{ok}] {label}: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except Exception:
            print(f"       <non-JSON body: {resp.text[:160]}>")
            return None
        if not resp.is_success:
            err = data.get("error", {})
            print(f"       {err.get('code', '?')}: {err.get('description', data)}")
            return None
        if body:
            print(f"       {data}")
        return data

    # 1. auth ---------------------------------------------------------------
    print("--- 1. authentication ---")
    payments = show("GET /v1/payments?count=10", client.get("/v1/payments", params={"count": 10}))
    if payments is None:
        print("\nGATE FAILED: credentials rejected.")
        return 1

    items = payments.get("items", [])
    print(f"       existing payments in account: {payments.get('count', 0)}")
    captured = [p for p in items if p.get("status") == "captured"]
    refundable = [p for p in captured if p.get("amount", 0) > p.get("amount_refunded", 0)]
    for p in items[:5]:
        print(
            f"       - {p['id']} {p['status']:<10} "
            f"amount={p.get('amount')} refunded={p.get('amount_refunded')} "
            f"method={p.get('method')}"
        )

    # 2. orders -------------------------------------------------------------
    print("\n--- 2. order creation ---")
    order = show(
        "POST /v1/orders",
        client.post(
            "/v1/orders",
            json={"amount": 100000, "currency": "INR", "receipt": "ledgertruth-spike-1"},
        ),
    )
    if order:
        print(f"       created {order['id']} amount={order['amount']} status={order['status']}")

    # 3. can we manufacture a captured payment server-side? -----------------
    print("\n--- 3. server-to-server payment creation (needed to seed refunds) ---")
    s2s = client.post(
        "/v1/payments/create/upi",
        json={
            "amount": 100000,
            "currency": "INR",
            "order_id": (order or {}).get("id", ""),
            "email": "spike@example.com",
            "contact": "9999999999",
            "method": "upi",
            "upi": {"flow": "collect", "vpa": "success@razorpay"},
        },
    )
    show("POST /v1/payments/create/upi", s2s, body=True)

    # 4. refunds ------------------------------------------------------------
    print("\n--- 4. refunds ---")
    show("GET /v1/refunds?count=5", client.get("/v1/refunds", params={"count": 5}))

    if refundable:
        target = refundable[0]
        print(f"       refundable payment available: {target['id']}")
        if write:
            created = show(
                f"POST /v1/payments/{target['id']}/refund",
                client.post(
                    f"/v1/payments/{target['id']}/refund",
                    json={"amount": 100},
                    headers={"X-Razorpay-Account": ""} if False else {},
                ),
                body=True,
            )
            if created:
                show(
                    f"GET /v1/refunds/{created['id']} (read-back)",
                    client.get(f"/v1/refunds/{created['id']}"),
                    body=True,
                )
        else:
            print("       (re-run with --write to attempt an actual refund)")
    else:
        print("       NO refundable captured payment exists -- cannot test refunds yet.")

    # 5. settlements --------------------------------------------------------
    print("\n--- 5. settlements / recon ---")
    show("GET /v1/settlements?count=3", client.get("/v1/settlements", params={"count": 3}))
    show(
        "GET /v1/settlements/recon/combined",
        client.get("/v1/settlements/recon/combined", params={"year": 2026, "month": 8}),
    )

    print("\n--- summary ---")
    print(f"auth:                {'yes' if payments is not None else 'no'}")
    print(f"order creation:      {'yes' if order else 'no'}")
    print(f"captured payments:   {len(captured)}")
    print(f"refundable payments: {len(refundable)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
