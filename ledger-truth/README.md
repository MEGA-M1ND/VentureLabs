# ledgertruth

Independent outcome verification for LLM agents that move money.

When an agent operates a real payment API, "the tool returned an error" and
"the work didn't happen" are different statements. This measures how different.

---

## The result

Claude Opus 5 driving **Razorpay's own MCP server** against **live test mode**,
with transport faults injected between the server and the API. 16 runs.

| Fault injected | n | Agent's claim correct | Tool response correct | Ledger verdict | Duplicate refunds |
|---|--:|--:|--:|--:|--:|
| none | 5 | 5/5 | 5/5 | 5 VERIFIED | 0 |
| response dropped **after** the refund committed | 8 | **8/8** | **0/8** | 8 VERIFIED | 0 |
| refund rejected **before** it committed | 3 | 3/3 | 3/3 | 3 FAILED | 0 |

**The tool response was wrong on 8 of 16 runs. The agent's own report was wrong
on none of them.**

That inverts the usual framing. The danger measured here was not an agent
claiming success it hadn't earned — it was the *tooling* reporting failure for
work that had already succeeded. A harness that retries on that signal issues a
second refund.

### Why the tool response cannot be fixed by retrying harder

The two faults are indistinguishable to the caller and require opposite responses:

| | dropped after commit | rejected before commit |
|---|---|---|
| Refund actually created | **yes** | **no** |
| What the tool reports | error | error |
| Correct response | **do nothing** | **retry** |
| Cost of the wrong choice | duplicate refund | unrefunded customer |

No retry policy separates these, because the distinguishing information is not
in the tool response. Reading the ledger is the only thing that tells them apart
— which is what this library does.

---

## How it works

You declare what must be true if the intent actually happened. The verifier
reads live ledger state, independently of the path the agent used, and checks it.

```python
from ledgertruth import full_refund

verdict = full_refund("pay_JK2m9Qb").verify(snapshot)
print(verdict.explain())
```

```text
FAILED: fully refund payment pay_JK2m9Qb
  [FAILED] refunds never exceed the payment: expected <= 1000.00 INR, observed 2000.00 INR (2 refund(s): rfnd_1, rfnd_2)
  [VERIFIED] refund is complete: expected >= 1000.00 INR, observed 2000.00 INR (2 refund(s): rfnd_1, rfnd_2)
  [FAILED] no duplicate money movement: expected <= 1, observed 2 (rfnd_1, rfnd_2)
  [VERIFIED] payment marked refunded: expected == refunded, observed refunded
  recommended action: escalate
```

Note the middle line: *"refund is complete"* passes, and the payment is
correctly marked refunded. An agent checking whether it met its goal — and a
contract that only asserted completeness — would both conclude success. The
failure is visible only because "complete" and "not excessive" are separate
claims.

### Four design decisions that carry the weight

**Money is an integer count of minor units, never a float.** Cross-currency
arithmetic raises rather than coercing. A verifier that introduces its own
rounding error has no standing to judge anyone.

**Verdicts are three-valued: `VERIFIED` / `FAILED` / `INDETERMINATE`.** An object
that is *absent* ("no refund exists") is a real answer. One that is *unreadable*
("the endpoint timed out") is not. Collapsing those is how a verification layer
starts lying in the same way the thing it audits does. When combining checks, a
proven failure dominates an unreadable sibling.

**Over-refund and under-refund are separate invariants**, because they demand
opposite remediations — retry is safe for one and unsafe for the other. A
contract asserting `refund_total == payment_amount` collapses both and discards
exactly the information the repair layer needs.

**The repair layer reads before it writes.** Under a dropped response the write
already landed; retrying is what creates the duplicate. Writes live in a
separate module from the reader, so the component arm C trusts can never mutate
the state it later attests to.

---

## The four arms

Each answers "did it work?" from a different vantage. The disagreements are the
measurement.

| Arm | Source of truth |
|---|---|
| A | the agent's own final report |
| B | the tool responses |
| C | an independent read of the ledger |
| D | C, plus bounded repair |

Arm D's rules, each asserted in `tests/test_repair.py` rather than left to
review: re-read before writing; never auto-act on excess money movement; never
write on `INDETERMINATE`; never invent an amount to refund.

Across 4 repair runs it wrote 3 times, no-opped once (the ledger was already
correct), escalated 0 times, and ended `VERIFIED` every time. **Zero duplicate
refunds across all 16 runs.**

---

## Methodology

The faults are **transport-layer only** — a response lost after the request
committed, a request rejected before it committed, delays, duplicate delivery.
These are properties of networks, not bugs authored for this repo. Ground truth
is always the real Razorpay ledger, read by a path the agent never used.

Duplicates are **induced**, never fabricated: the proxy drops a response after
the refund has genuinely committed, and whatever the agent does next is the
measurement. A harness that injects its own bugs and then catches them with its
own verifier has measured nothing but its own configuration.

Seeding and verification bypass the proxy. The unit test suite never touches the
network.

## Limits

16 runs, one model (`claude-opus-5`), one effort level (`high`), one provider,
four mission types. This establishes that the behaviours exist and repeat under
these conditions. It does not establish rates, and the central claim of
[FIND-4](docs/findings.md) — that correct recovery is *behavioural* rather than
guaranteed by the platform — predicts that a weaker model, a lower effort
setting, or a terser prompt would change the result. That sweep is the obvious
next experiment.

## Reproducing

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
uv run pytest                                          # 83 tests, no network
uv run python scripts/spike_feasibility.py             # verify test-mode access
uv run python scripts/seed_payments.py --count 10      # mint captured payments
uv run python scripts/run_missions.py --chaos drop_refund_response --repeats 2
```

Needs Razorpay **test-mode** keys and a locally built `razorpay-mcp-server`
(`create_refund` is excluded from the hosted remote server). A guardrail rejects
any key not prefixed `rzp_test_`.

## Findings

Full write-ups in [docs/findings.md](docs/findings.md), feasibility notes in
[docs/feasibility.md](docs/feasibility.md).

- **FIND-1** — Razorpay's refund API supports idempotency via
  `X-Refund-Idempotency`, but the official MCP server never sends it and exposes
  no parameter for one. Filed upstream as
  [razorpay/razorpay-mcp-server#128](https://github.com/razorpay/razorpay-mcp-server/pull/128).
- **FIND-2** — Captured test payments can be seeded entirely server-side.
- **FIND-3** — On the clean path, all arms agreed on every run.
- **FIND-4** — The agent recovered from every dropped response by re-reading
  rather than retrying; arm B was wrong on all 8.
- **FIND-5** — The two faults are indistinguishable to the tool and require
  opposite responses.

## Licence

Apache-2.0
