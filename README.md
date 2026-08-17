# VentureLabs

Experiments in agent reliability infrastructure.

## Active

### [`ledger-truth/`](ledger-truth/) — does the agent's refund actually happen?

Claude agents driving **Razorpay's own MCP server** against **live test mode**,
with transport faults injected between the server and the API. An independent
verifier reads the ledger by a path the agent never touches.

**Duplicate refund rate when the network drops a response after the refund has
already committed** — 166 runs:

| Model | Refunded twice | Falsely claimed success |
|---|--:|--:|
| `claude-opus-5` (high and low effort) | **0%** | 0/20 |
| `claude-sonnet-5` (high / low) | **50% / 40%** | 9/20 |
| `claude-haiku-4-5` | **100%** | 10/10 |

Every retry became a duplicate, in every cell, without exception. And two
concurrent agents duplicate **100% of the time even on Opus at high effort, with
no fault injected at all** — re-reading before acting is check-then-use, and
check-then-use has a window. A shared idempotency key was the only mitigation
that held.

On Opus the *tool response* is the liar; below Opus the *agent* lies too. An
independent ledger read caught both, plus the concurrency case neither signal
sees.

Also surfaced a real gap in Razorpay's integration: their MCP server never sends
the idempotency header their own refund API supports, and the tool schema offers
no way to request one. Filed upstream as
[razorpay/razorpay-mcp-server#128](https://github.com/razorpay/razorpay-mcp-server/pull/128).

- [Write-up](ledger-truth/docs/writeup.md) — the full story
- [Findings](ledger-truth/docs/findings.md) — FIND-1 through FIND-5
- [README](ledger-truth/) — design decisions, methodology, limits

```bash
cd ledger-truth && uv run python scripts/demo.py
```

## Not being built

Deliberately scoped out, with reasons, rather than left as open ambition:

- **Service reputation benchmarking** — Scale Labs' MCP-Atlas, Accenture's
  MCP-Bench and a published 100-server study already occupy this, with a
  200-server follow-up scheduled.
- **Agent-to-agent labour markets** — entirely simulated today; no real data is
  obtainable, so any result would measure the simulation.
- **Merchant agent-readiness scanning** — requires grading agents against a mess
  authored by the same person doing the grading.

Each was assessed and dropped on evidence. One project, finished, beats four
started.
