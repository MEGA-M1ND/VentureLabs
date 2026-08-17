"""Mint captured Razorpay test payments.

The mission suite needs many independent refundable payments. Server-to-server
payment creation (`/v1/payments/create/{json,upi}`) is disabled on a standard
test account, but the endpoints the browser checkout uses are not -- they simply
authenticate differently, taking the public `key_id` in a form body rather than
HTTP basic auth. That is why probing them with basic auth returns `401` rather
than `404`.

Called the way checkout calls them, with the test VPA `success@razorpay`, a
payment is created and auto-captures immediately. No browser, no 3-D Secure.

    order (basic auth)  ->  create/ajax (key_id in form)  ->  captured
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from ..money import Money
from .razorpay import API_BASE, NotTestMode

#: Razorpay's test VPA that always succeeds.
SUCCESS_VPA = "success@razorpay"


class SeedFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class SeededPayment:
    payment_id: str
    order_id: str
    amount: Money

    def as_dict(self) -> dict:
        return {
            "payment_id": self.payment_id,
            "order_id": self.order_id,
            "amount_minor": self.amount.minor,
            "currency": self.amount.currency,
        }


class RazorpaySeeder:
    def __init__(
        self,
        key_id: str,
        key_secret: str,
        *,
        base_url: str = API_BASE,
        client: httpx.Client | None = None,
        poll_attempts: int = 8,
        poll_interval: float = 1.5,
    ) -> None:
        if not key_id.startswith("rzp_test_"):
            raise NotTestMode(
                f"seeding creates real payments and is test-mode only; got key {key_id[:10]}..."
            )
        self._key_id = key_id
        self._client = client or httpx.Client(base_url=base_url, timeout=30.0)
        self._auth = (key_id, key_secret)
        self._poll_attempts = poll_attempts
        self._poll_interval = poll_interval

    # -- steps --------------------------------------------------------------

    def _create_order(self, amount: Money, receipt: str) -> str:
        resp = self._client.post(
            "/v1/orders",
            auth=self._auth,
            json={"amount": amount.minor, "currency": amount.currency, "receipt": receipt},
        )
        if not resp.is_success:
            raise SeedFailed(f"order creation failed: HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()["id"]

    def _create_payment(self, order_id: str, amount: Money) -> str:
        """Checkout-style call: public key_id in the body, no basic auth."""
        form = {
            "key_id": self._key_id,
            "amount": amount.minor,
            "currency": amount.currency,
            "order_id": order_id,
            "email": "harness@ledgertruth.test",
            "contact": "9999999999",
            "method": "upi",
            "upi[flow]": "collect",
            "upi[vpa]": SUCCESS_VPA,
            "_[source]": "checkoutjs",
        }
        resp = self._client.post(
            "/v1/payments/create/ajax",
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if not resp.is_success:
            raise SeedFailed(f"payment creation failed: HTTP {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        payment_id = body.get("payment_id") or body.get("razorpay_payment_id")
        if not payment_id:
            raise SeedFailed(f"no payment_id in response: {str(body)[:200]}")
        return payment_id

    def _await_capture(self, payment_id: str, amount: Money) -> None:
        """Poll to captured, capturing explicitly if the account does not
        auto-capture."""
        for attempt in range(self._poll_attempts):
            resp = self._client.get(f"/v1/payments/{payment_id}", auth=self._auth)
            if not resp.is_success:
                time.sleep(self._poll_interval)
                continue
            status = resp.json().get("status")
            if status == "captured":
                return
            if status == "authorized":
                cap = self._client.post(
                    f"/v1/payments/{payment_id}/capture",
                    auth=self._auth,
                    json={"amount": amount.minor, "currency": amount.currency},
                )
                if cap.is_success:
                    return
                raise SeedFailed(f"capture failed: HTTP {cap.status_code}: {cap.text[:200]}")
            if status == "failed":
                raise SeedFailed(f"payment {payment_id} failed during seeding")
            if attempt < self._poll_attempts - 1:
                time.sleep(self._poll_interval)
        raise SeedFailed(f"payment {payment_id} never reached captured")

    # -- public -------------------------------------------------------------

    def mint(self, amount: Money, *, receipt: str) -> SeededPayment:
        order_id = self._create_order(amount, receipt)
        payment_id = self._create_payment(order_id, amount)
        self._await_capture(payment_id, amount)
        return SeededPayment(payment_id=payment_id, order_id=order_id, amount=amount)

    def mint_authorized(self, amount: Money, *, receipt: str) -> SeededPayment:
        """Mint a payment held in 'authorized' status, for missions that must
        capture explicitly rather than start from an already-settled payment.

        `payment_capture: 0` on the order is what makes this deterministic --
        the checkout-style success VPA otherwise auto-captures immediately
        (confirmed empirically; see scripts/spike_capture.py), regardless of
        payment method.
        """
        resp = self._client.post(
            "/v1/orders",
            auth=self._auth,
            json={
                "amount": amount.minor,
                "currency": amount.currency,
                "receipt": receipt,
                "payment_capture": 0,
            },
        )
        if not resp.is_success:
            raise SeedFailed(f"order creation failed: HTTP {resp.status_code}: {resp.text[:200]}")
        order_id = resp.json()["id"]

        payment_id = self._create_payment(order_id, amount)

        check = self._client.get(f"/v1/payments/{payment_id}", auth=self._auth)
        status = check.json().get("status") if check.is_success else None
        if status != "authorized":
            raise SeedFailed(f"payment {payment_id} landed in status '{status}', not 'authorized'")

        return SeededPayment(payment_id=payment_id, order_id=order_id, amount=amount)

    def close(self) -> None:
        self._client.close()
