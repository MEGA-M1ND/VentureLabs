"""Seeder tests. Hermetic -- MockTransport only, never the network."""

from __future__ import annotations

import httpx
import pytest

from ledgertruth import inr
from ledgertruth.providers import NotTestMode
from ledgertruth.providers.razorpay_seed import RazorpaySeeder, SeedFailed


def seeder_for(handler, **kw) -> RazorpaySeeder:
    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.razorpay.com"
    )
    return RazorpaySeeder("rzp_test_x", "s", client=client, poll_interval=0.0, **kw)


def test_rejects_live_key():
    with pytest.raises(NotTestMode, match="test-mode only"):
        RazorpaySeeder("rzp_live_abc", "s")


def test_happy_path_mints_captured_payment():
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen[path] = request
        if path == "/v1/orders":
            return httpx.Response(200, json={"id": "order_1"})
        if path == "/v1/payments/create/ajax":
            return httpx.Response(200, json={"type": "async", "payment_id": "pay_1"})
        if path == "/v1/payments/pay_1":
            return httpx.Response(200, json={"id": "pay_1", "status": "captured"})
        raise AssertionError(path)

    result = seeder_for(handler).mint(inr(1000), receipt="r1")
    assert result.payment_id == "pay_1"
    assert result.order_id == "order_1"
    assert result.amount == inr(1000)

    # The checkout call must NOT carry basic auth -- that is what makes it 401.
    ajax = seen["/v1/payments/create/ajax"]
    assert "authorization" not in {k.lower() for k in ajax.headers}
    body = ajax.content.decode()
    assert "key_id=rzp_test_x" in body
    assert "success%40razorpay" in body or "success@razorpay" in body

    # The order call must carry basic auth.
    assert "authorization" in {k.lower() for k in seen["/v1/orders"].headers}


def test_captures_explicitly_when_only_authorized():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(f"{request.method} {path}")
        if path == "/v1/orders":
            return httpx.Response(200, json={"id": "order_1"})
        if path == "/v1/payments/create/ajax":
            return httpx.Response(200, json={"payment_id": "pay_1"})
        if path == "/v1/payments/pay_1/capture":
            return httpx.Response(200, json={"id": "pay_1", "status": "captured"})
        if path == "/v1/payments/pay_1":
            return httpx.Response(200, json={"id": "pay_1", "status": "authorized"})
        raise AssertionError(path)

    seeder_for(handler).mint(inr(1000), receipt="r1")
    assert "POST /v1/payments/pay_1/capture" in calls


def test_failed_payment_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/orders":
            return httpx.Response(200, json={"id": "order_1"})
        if request.url.path == "/v1/payments/create/ajax":
            return httpx.Response(200, json={"payment_id": "pay_1"})
        return httpx.Response(200, json={"id": "pay_1", "status": "failed"})

    with pytest.raises(SeedFailed, match="failed during seeding"):
        seeder_for(handler).mint(inr(1000), receipt="r1")


def test_never_capturing_raises_rather_than_hanging():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/orders":
            return httpx.Response(200, json={"id": "order_1"})
        if request.url.path == "/v1/payments/create/ajax":
            return httpx.Response(200, json={"payment_id": "pay_1"})
        return httpx.Response(200, json={"id": "pay_1", "status": "created"})

    with pytest.raises(SeedFailed, match="never reached captured"):
        seeder_for(handler, poll_attempts=3).mint(inr(1000), receipt="r1")


def test_missing_payment_id_in_response_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/orders":
            return httpx.Response(200, json={"id": "order_1"})
        return httpx.Response(200, json={"type": "async"})

    with pytest.raises(SeedFailed, match="no payment_id"):
        seeder_for(handler).mint(inr(1000), receipt="r1")
