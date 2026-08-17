# VentureLabs

Experiments in agent reliability infrastructure.

## Active

### [`ledger-truth/`](ledger-truth/) — does the agent's refund actually happen?

Claude Opus 5 driving **Razorpay's own MCP server** against **live test mode**,
with transport faults injected between the server and the API. An independent
verifier reads the ledger by a path the agent never touches.

**Across 16 runs the tool responses were wrong 8 times. The agent's own reports
were wrong 0 times.**

The reason is the finding: two faults — a response dropped *after* the refund
committed, and a request rejected *before* it committed — are indistinguishable
to the caller and require opposite responses (do nothing vs. retry). Getting it
wrong means either a duplicate refund or an unrefunded customer. No retry policy
separates them, because the information isn't in the tool response.

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
