# ledgertruth

Independent outcome verification for LLM agents that move money.

> **Status: in development.** The verification core is built and tested. No
> agent measurements have been taken yet — the results section below is
> deliberately empty rather than filled with illustrative numbers.

## The problem

An agent issues a refund and reports success. The tool call returned `200`. The
trace looks clean. The durable-execution layer confirms the workflow completed.

None of those things are the question. The question is whether the merchant's
ledger is now in the state the agent claims it is in — and the three most
common answers in production are *no*, *partly*, and *twice*.

Existing tooling largely answers adjacent questions:

| Layer | Answers |
|---|---|
| Tracing / observability | What did the system do? |
| Durable execution | Did the execution survive failure? |
| Evals | Did the trajectory look right? |
| **Outcome verification** | **Is the external world in the intended state?** |

`ledgertruth` does the last one, for payment ledgers specifically, by reading
the ledger back independently and checking it against a declared contract.

## How it works

You declare what should be true if the intent actually happened. The verifier
reads live ledger state and checks it.

```python
from ledgertruth import full_refund, inr, partial_refund

intent = full_refund("pay_JK2m9Qb")
verdict = intent.verify(snapshot)

print(verdict.explain())
```

Against a ledger where the agent retried and refunded twice:

```text
FAILED: fully refund payment pay_JK2m9Qb
  [FAILED] refunds never exceed the payment: expected <= 1000.00 INR, observed 2000.00 INR (2 refund(s): rfnd_1, rfnd_2)
  [VERIFIED] refund is complete: expected >= 1000.00 INR, observed 2000.00 INR (2 refund(s): rfnd_1, rfnd_2)
  [FAILED] no duplicate money movement: expected <= 1, observed 2 (rfnd_1, rfnd_2)
  [VERIFIED] payment marked refunded: expected == refunded, observed refunded
  recommended action: escalate
```

Note what the middle line shows: *"refund is complete"* passes, and the payment
is correctly marked refunded. An agent checking whether it achieved its goal —
and a contract that only asserted completeness — would both conclude success.
The failure is visible only because "complete" and "not excessive" are separate
claims.

### Three design decisions that carry the weight

**1. Money is an integer count of minor units, never a float.** Cross-currency
arithmetic raises rather than coercing. A verifier that introduces its own
rounding error has no standing to judge anyone.

**2. Verdicts are three-valued: `VERIFIED` / `FAILED` / `INDETERMINATE`.**
An object that is *absent* ("no refund exists") is a real answer. An object that
is *unreadable* ("the refunds endpoint timed out") is not. Collapsing those two
is how a verification layer starts lying in exactly the way the agents do. When
combining checks, a proven failure dominates an unreadable sibling — unreadable
state cannot un-prove an established fact.

**3. Over-refund and under-refund are separate invariants.** They demand
opposite remediations:

- refunded **less** than intended → incomplete; an idempotency-keyed retry is safe
- refunded **more** than intended → money left that should not have; there is no
  safe automatic remedy, because clawing it back means charging a customer again

A contract that says `refund_total == payment_amount` collapses both into one
signal and discards the information the repair layer needs. Remediation
therefore defaults to `ESCALATE`; auto-retrying a money movement must be an
explicit choice by the contract author.

## What is being measured

The experiment this library exists to run:

> When an LLM agent operates a **real** payment API through MCP, how often does
> it report success while the ledger is in a different state than intended, and
> does independent verification catch it?

Four arms:

| Arm | Success determined by |
|---|---|
| A | Agent self-report |
| B | Tool response (HTTP 200) |
| C | Independent ledger verifier |
| D | Verifier + bounded repair |

Headline metrics: **false success rate** and **duplicate money movement rate**.

### On methodology

The failure modes under test are **transport-layer** faults — responses dropped
after the request committed, duplicated delivery, responses delayed past the
agent's timeout, stale reads. These are universal properties of networks, not
business bugs authored for this repo, and ground truth is always the real
provider ledger rather than a mock.

This distinction is the whole reason the results will mean anything. A harness
that injects its own bugs and then catches them with its own verifier has
measured nothing but its own configuration.

## Development

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run pytest
```

## Licence

Apache-2.0
