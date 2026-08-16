"""Chaos proxy behaviour, verified against a local fake upstream.

The key property under test is that `drop_after_commit` still *commits* — the
upstream must observe the request even though the caller sees a failure. If that
ever regressed, the harness would be injecting plain failures instead of the
committed-but-unacknowledged state the whole experiment is about.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from ledgertruth.chaos import REFUND_CREATE, ChaosProxy, FaultRule


class _Upstream:
    """Records every request it receives and always answers 200."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []
        lock = threading.Lock()
        seen = self.seen

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:  # noqa: A003
                pass

            def _record_and_reply(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                with lock:
                    seen.append((method, self.path))
                payload = json.dumps({"id": "rfnd_fake", "status": "processed"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802
                self._record_and_reply("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._record_and_reply("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def upstream():
    server = _Upstream()
    yield server
    server.close()


REFUND_PATH = "/v1/payments/pay_X1/refund"


def test_passes_through_when_no_rules(upstream):
    with ChaosProxy(upstream=upstream.url) as proxy:
        resp = httpx.post(f"{proxy.base_url}{REFUND_PATH}", json={"amount": 100}, timeout=10)
        assert resp.status_code == 200
        assert resp.json()["id"] == "rfnd_fake"
    assert upstream.seen == [("POST", REFUND_PATH)]


def test_drop_after_commit_still_commits_upstream(upstream):
    """The point of the whole module: caller sees failure, upstream saw the write."""
    rules = [FaultRule(fault="drop_after_commit", method="POST", path_regex=REFUND_CREATE)]
    with ChaosProxy(upstream=upstream.url, rules=rules) as proxy, pytest.raises(httpx.HTTPError):
        httpx.post(f"{proxy.base_url}{REFUND_PATH}", json={"amount": 100}, timeout=10)

    # Committed exactly once, despite the caller getting nothing back.
    assert upstream.seen == [("POST", REFUND_PATH)]


def test_drop_applies_only_max_applications_times(upstream):
    """A retry after the dropped response must get through -- that is how a
    duplicate arises naturally rather than being fabricated."""
    rules = [
        FaultRule(
            fault="drop_after_commit",
            method="POST",
            path_regex=REFUND_CREATE,
            max_applications=1,
        )
    ]
    with ChaosProxy(upstream=upstream.url, rules=rules) as proxy:
        url = f"{proxy.base_url}{REFUND_PATH}"
        with pytest.raises(httpx.HTTPError):
            httpx.post(url, json={"amount": 100}, timeout=10)
        second = httpx.post(url, json={"amount": 100}, timeout=10)
        assert second.status_code == 200
        assert proxy.faults_applied == 1

    # Two commits upstream: the induced duplicate.
    assert upstream.seen == [("POST", REFUND_PATH), ("POST", REFUND_PATH)]


def test_error_after_commit_returns_502_but_commits(upstream):
    rules = [FaultRule(fault="error_after_commit", method="POST", path_regex=REFUND_CREATE)]
    with ChaosProxy(upstream=upstream.url, rules=rules) as proxy:
        resp = httpx.post(f"{proxy.base_url}{REFUND_PATH}", json={"amount": 100}, timeout=10)
        assert resp.status_code == 502
    assert upstream.seen == [("POST", REFUND_PATH)]


def test_rule_does_not_match_other_paths(upstream):
    rules = [FaultRule(fault="drop_after_commit", method="POST", path_regex=REFUND_CREATE)]
    with ChaosProxy(upstream=upstream.url, rules=rules) as proxy:
        resp = httpx.get(f"{proxy.base_url}/v1/payments/pay_X1", timeout=10)
        assert resp.status_code == 200
        assert proxy.faults_applied == 0


def test_get_requests_are_unaffected_by_post_rules(upstream):
    """Reads must stay clean, or arm C would be verifying through the fault."""
    rules = [FaultRule(fault="drop_after_commit", method="POST", path_regex=r".*")]
    with ChaosProxy(upstream=upstream.url, rules=rules) as proxy:
        resp = httpx.get(f"{proxy.base_url}{REFUND_PATH}", timeout=10)
        assert resp.status_code == 200


def test_duplicate_delivers_twice_and_returns_success(upstream):
    rules = [FaultRule(fault="duplicate", method="POST", path_regex=REFUND_CREATE)]
    with ChaosProxy(upstream=upstream.url, rules=rules) as proxy:
        resp = httpx.post(f"{proxy.base_url}{REFUND_PATH}", json={"amount": 100}, timeout=10)
        assert resp.status_code == 200
    assert len(upstream.seen) == 2


def test_records_capture_fault_and_status(upstream):
    rules = [FaultRule(fault="error_after_commit", method="POST", path_regex=REFUND_CREATE)]
    with ChaosProxy(upstream=upstream.url, rules=rules) as proxy:
        httpx.post(f"{proxy.base_url}{REFUND_PATH}", json={"amount": 100}, timeout=10)
        httpx.get(f"{proxy.base_url}/v1/payments/pay_X1", timeout=10)
        records = [r.as_dict() for r in proxy.records]

    assert records[0]["fault"] == "error_after_commit"
    assert records[0]["upstream_status"] == 200  # upstream succeeded; caller did not hear it
    assert records[1]["fault"] is None


def test_reject_before_commit_never_reaches_upstream(upstream):
    """The mirror of drop_after_commit: the write genuinely did not happen, so
    a retry is the correct move rather than the dangerous one."""
    rules = [
        FaultRule(
            fault="reject_before_commit",
            method="POST",
            path_regex=REFUND_CREATE,
            status=503,
        )
    ]
    with ChaosProxy(upstream=upstream.url, rules=rules) as proxy:
        resp = httpx.post(f"{proxy.base_url}{REFUND_PATH}", json={"amount": 100}, timeout=10)
        assert resp.status_code == 503

    assert upstream.seen == [], "nothing must have committed upstream"
