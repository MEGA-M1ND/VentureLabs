"""Drive the locally-built razorpay-mcp-server over MCP stdio.

Confirms the agent-facing surface actually works, and specifically that
`create_refund` is exposed by the local build (it is excluded from Razorpay's
hosted remote server).

    LEDGERTRUTH_MCP_BIN=../.tools/razorpay-mcp-server.exe uv run python scripts/spike_mcp.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spike_feasibility import load_env  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BIN = ROOT.parent / ".tools" / "razorpay-mcp-server.exe"


class StdioMCP:
    """Minimal MCP stdio client -- newline-delimited JSON-RPC."""

    def __init__(self, argv: list[str]) -> None:
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._id = 0

    def _send(self, payload: dict) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        assert self.proc.stdout
        while True:
            line = self.proc.stdout.readline()
            if not line:
                err = self.proc.stderr.read() if self.proc.stderr else ""
                raise RuntimeError(f"server closed stdout. stderr:\n{err}")
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("id") == self._id:
                return msg

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def main() -> int:
    env = load_env(ROOT / ".env")
    key, secret = env.get("RAZORPAY_KEY_ID", ""), env.get("RAZORPAY_KEY_SECRET", "")
    if not key.startswith("rzp_test_"):
        print("REFUSING: test-mode key required")
        return 1

    binary = Path(os.environ.get("LEDGERTRUTH_MCP_BIN", str(DEFAULT_BIN)))
    if not binary.exists():
        print(f"FAIL: MCP binary not found at {binary}")
        return 1

    mcp = StdioMCP([str(binary), "stdio", "--key", key, "--secret", secret])
    try:
        init = mcp.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ledgertruth-spike", "version": "0.1.0"},
            },
        )
        info = init.get("result", {}).get("serverInfo", {})
        print(f"initialized: {info.get('name')} {info.get('version')}")
        mcp.notify("notifications/initialized")

        tools = mcp.request("tools/list").get("result", {}).get("tools", [])
        names = sorted(t["name"] for t in tools)
        print(f"\ntools exposed: {len(names)}")

        for wanted in ("create_refund", "fetch_payment", "fetch_multiple_refunds_for_payment"):
            print(f"  {wanted:<36} {'PRESENT' if wanted in names else 'ABSENT'}")

        refund_tool = next((t for t in tools if t["name"] == "create_refund"), None)
        if refund_tool:
            props = refund_tool.get("inputSchema", {}).get("properties", {})
            print(f"\ncreate_refund parameters: {sorted(props)}")
            has_idem = any("idempot" in p.lower() for p in props)
            print(f"  idempotency parameter exposed: {has_idem}")
            if "receipt" in props:
                print(f"  receipt description: {props['receipt'].get('description')!r}")

        # A real read through the agent-facing surface.
        print("\n--- live tool call: fetch_payment ---")
        pid = os.environ.get("LEDGERTRUTH_TEST_PAYMENT", "pay_TQU98yLAipflbr")
        res = mcp.request("tools/call", {"name": "fetch_payment", "arguments": {"payment_id": pid}})
        content = res.get("result", {}).get("content", [])
        text = content[0].get("text", "") if content else str(res)
        try:
            parsed = json.loads(text)
            print(
                f"  {parsed.get('id')} status={parsed.get('status')} "
                f"amount={parsed.get('amount')} refunded={parsed.get('amount_refunded')}"
            )
        except Exception:
            print(f"  {text[:300]}")
        return 0
    finally:
        mcp.close()


if __name__ == "__main__":
    raise SystemExit(main())
