"""Minimal MCP stdio client.

Speaks newline-delimited JSON-RPC to an MCP server subprocess. Deliberately
small: the harness needs to drive a server and record exactly what crossed the
wire, not to be a general MCP implementation.

One non-obvious detail: stderr is drained on a background thread. The Razorpay
server logs there, and an undrained pipe fills and blocks the child mid-write —
which looks exactly like a hung agent.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from types import TracebackType


class MCPError(RuntimeError):
    pass


@dataclass
class ToolCallRecord:
    """One tool invocation, as the harness observed it."""

    name: str
    arguments: dict
    #: Raw text content returned by the server.
    result_text: str
    #: MCP-level error flag. This is the signal arm B ("the tool said it worked")
    #: is built from -- it is the tool's own claim, not the ledger's.
    is_error: bool
    #: Parsed result when the server returned JSON, else None.
    parsed: dict | None = None


@dataclass
class StdioMCPClient:
    argv: list[str]
    timeout: float = 60.0
    #: Extra environment for the subprocess, merged over os.environ. Used to
    #: point the server's SDK at the chaos proxy via RAZORPAY_API_BASE_URL.
    env: dict[str, str] | None = None
    _proc: subprocess.Popen | None = field(default=None, repr=False)
    _id: int = 0
    _stderr: list[str] = field(default_factory=list, repr=False)

    # -- lifecycle ----------------------------------------------------------

    def __enter__(self) -> StdioMCPClient:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def start(self) -> None:
        child_env = {**os.environ, **self.env} if self.env else None
        self._proc = subprocess.Popen(
            self.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=child_env,
        )
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            self._stderr.append(line.rstrip())

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        finally:
            self._proc = None

    # -- JSON-RPC -----------------------------------------------------------

    def _write(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise MCPError("server not started")
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        want = self._id
        self._write({"jsonrpc": "2.0", "id": want, "method": method, "params": params or {}})

        proc = self._proc
        assert proc is not None and proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if not line:
                raise MCPError(
                    f"server closed stdout during {method}. stderr tail:\n"
                    + "\n".join(self._stderr[-20:])
                )
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # Not every line on stdout is a response; ignore noise rather
                # than aborting a run over a stray log line.
                continue
            if msg.get("id") == want:
                if "error" in msg:
                    raise MCPError(f"{method} failed: {msg['error']}")
                return msg.get("result", {})

    def notify(self, method: str, params: dict | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # -- MCP ----------------------------------------------------------------

    def initialize(self, client_name: str = "ledgertruth", version: str = "0.1.0") -> dict:
        result = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": client_name, "version": version},
            },
        )
        self.notify("notifications/initialized")
        return result

    def list_tools(self) -> list[dict]:
        return self.request("tools/list").get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> ToolCallRecord:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        blocks = result.get("content", [])
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")

        parsed: dict | None = None
        try:
            candidate = json.loads(text)
            if isinstance(candidate, dict):
                parsed = candidate
        except (json.JSONDecodeError, TypeError):
            parsed = None

        return ToolCallRecord(
            name=name,
            arguments=arguments,
            result_text=text,
            is_error=bool(result.get("isError", False)),
            parsed=parsed,
        )


def to_anthropic_tools(mcp_tools: list[dict]) -> list[dict]:
    """Convert MCP tool descriptors to Anthropic tool definitions.

    The schemas are passed through unmodified on purpose: the experiment is
    about the surface Razorpay actually ships, so rewriting a description here
    would be measuring our own prompt engineering instead.
    """
    converted = []
    for tool in mcp_tools:
        schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
        converted.append(
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": schema,
            }
        )
    return converted
