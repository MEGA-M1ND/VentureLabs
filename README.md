# VentureLabs

Experiments in agent reliability infrastructure.

## Active

### [`ledger-truth/`](ledger-truth/) — independent outcome verification for agents that move money

When an LLM agent operates a real payment API through MCP, how often does it
report success while the ledger is in a different state than intended — and does
independent verification catch it?

Tracing answers *what the system did*. Durable execution answers *whether
execution survived*. Neither answers *whether the world is now in the intended
state*. That last question is what this measures.

Runs against **Razorpay test mode** via Razorpay's own MCP server. Ground truth
is always the real provider ledger; the injected faults are transport-layer only
(dropped-after-commit responses, delays, duplicate delivery), so the failure
modes are properties of networks rather than bugs authored for this repo.

**Status:** verification core built and tested; test-mode feasibility gate
passed; no agent measurements taken yet.

First finding, before the harness exists —
[`docs/findings.md`](ledger-truth/docs/findings.md):

> Razorpay's refund API supports idempotency via `X-Refund-Idempotency`, but the
> official MCP server passes `nil` for extra headers and never sends it, while
> describing `receipt` (the alternative idempotency key) only as "a unique
> identifier provided by you for your internal reference". An agent that retries
> after a dropped response therefore double-refunds by default.

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
