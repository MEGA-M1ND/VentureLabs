"""The only component in this package permitted to move money.

Kept apart from RazorpayLedgerReader on purpose: the reader is what arm C
trusts, and a reader that could also write would be a component able to
manufacture the state it later attests to.

Every write carries `X-Refund-Idempotency`. FIND-1 established that the agent's
own tooling does not send one, so the repair layer cannot inherit that
protection -- it has to supply it itself, and the key is derived from the intent
rather than generated per call so that a repeated repair is inert rather than
additive.
"""

from __future__ import annotations

import hashlib

import httpx

from ..money import Money
from .razorpay import API_BASE, NotTestMode

#: Razorpay requires at least 10 characters.
_KEY_PREFIX = "lt"


def idempotency_key_for(payment_id: str, amount: Money, purpose: str = "refund") -> str:
    """Derive a stable key from the intent.

    Deterministic on purpose: two repair attempts for the same intent produce
    the same key, so the second is answered with the first refund instead of
    creating another one. A random key per attempt would defeat the entire
    mechanism while appearing to use it.
    """
    material = f"{purpose}:{payment_id}:{amount.minor}:{amount.currency}"
    digest = hashlib.sha256(material.encode()).hexdigest()[:24]
    return f"{_KEY_PREFIX}-{digest}"


class RefundFailed(RuntimeError):
    pass


class RazorpayWriter:
    def __init__(
        self,
        key_id: str,
        key_secret: str,
        *,
        base_url: str = API_BASE,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
        allow_live: bool = False,
    ) -> None:
        if not allow_live and not key_id.startswith("rzp_test_"):
            raise NotTestMode(f"key {key_id[:10]}... is not a test key; this class moves money")
        self._client = client or httpx.Client(
            base_url=base_url, auth=(key_id, key_secret), timeout=timeout
        )

    def create_refund(
        self,
        payment_id: str,
        amount: Money,
        *,
        idempotency_key: str | None = None,
        send_key: bool = True,
    ) -> dict:
        """Create a refund, idempotently.

        Replaying this call with the same derived key returns the original
        refund rather than creating a second one.

        `send_key=False` deliberately omits the header. That is never correct in
        production -- it exists so an experiment can isolate the effect of the
        key from the effect of the race it protects against.
        """
        headers = {}
        if send_key:
            headers["X-Refund-Idempotency"] = idempotency_key or idempotency_key_for(
                payment_id, amount
            )
        response = self._client.post(
            f"/v1/payments/{payment_id}/refund",
            json={"amount": amount.minor},
            headers=headers,
        )
        if not response.is_success:
            detail = ""
            try:
                detail = str(response.json().get("error", {}).get("description", ""))
            except ValueError:
                detail = response.text[:200]
            raise RefundFailed(f"HTTP {response.status_code}: {detail or '<no detail>'}")
        return response.json()

    def close(self) -> None:
        self._client.close()
