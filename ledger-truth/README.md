# ledgertruth

Independent outcome verification for LLM agents that move money.

When an agent operates a real payment API, "the tool returned an error" and
"the work didn't happen" are different statements. This measures how different.

---

## The result

Agents driving **Razorpay's own MCP server** against **live test mode**, with
transport faults injected between the server and the API. 166 runs.

**Duplicate refund rate when a response is dropped after the refund committed:**

| Model | Retried instead of re-reading | **Refunded twice** | Falsely claimed success |
|---|--:|--:|--:|
| `claude-opus-5` @ high | 0/10 | **0%** | 0/10 |
| `claude-opus-5` @ low | 0/10 | **0%** | 0/10 |
| `claude-sonnet-5` @ high | 5/10 | **50%** | 5/10 |
| `claude-sonnet-5` @ low | 4/10 | **40%** | 4/10 |
| `claude-haiku-4-5` | 10/10 | **100%** | 10/10 |

Every retry became a duplicate — the two columns are equal in every cell, with
zero exceptions across 50 runs.

**And two concurrent agents duplicate 100% of the time even on Opus at high
effort, with no fault injected at all.** An idempotency key stops it; nothing
else tested does.

Three separate failures, one detector:

- **On Opus**, the *tool response* is the liar — it reports failure for a refund
  that already committed, and the agent correctly ignores it.
- **Below Opus**, the *agent* lies too. Haiku claimed success on all ten payments
  it had just refunded twice.
- **Under concurrency**, even Opus duplicates, because re-reading before acting
  is check-then-use.

Arm C — an independent read of the ledger — caught all three.

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
correct), escalated 0 times, and ended `VERIFIED` every time. The no-op is the
one that matters: under a dropped response the ledger was already correct, and
writing there is what would have created the duplicate.

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

166 runs across three models, two effort levels, one provider, four mission
types. Rates are indicative, not precise: 10 runs per cell means a 100% cell
reads as "reliably, in this configuration", not "always". The cliff between Opus
and everything below it is far larger than that noise, which is the part worth
acting on.

One control matters more than the sample size. The identical ladder run against
a **full** refund produced 29 retries and **zero** duplicates, because Razorpay
rejects a second full refund on amount grounds. Same models, same fault, same
retry behaviour, no measurable harm. Benchmark agent payment safety with full
refunds and you will measure a system that looks safe while the models under
test retry blind money movements at up to 100%.

## Reproducing

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
uv run pytest                                          # 83 tests, no network
uv run python scripts/spike_feasibility.py             # verify test-mode access
uv run python scripts/seed_payments.py --count 10      # mint captured payments
uv run python scripts/run_missions.py --chaos drop_refund_response --repeats 2
uv run python scripts/race.py --rounds 3                   # concurrency (FIND-6)
uv run python scripts/sweep.py --mission partial_refund_249_50   # model ladder (FIND-7)
uv run python scripts/analyze_sweep.py --file runs/sweep_partial.jsonl
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
- **FIND-6** — Re-reading before acting does not survive concurrency: two agents
  duplicated 3/3 with no fault injected. A shared idempotency key stopped it
  0/7 vs 7/7.
- **FIND-7** — Duplicate rate by model: 0% on Opus, 40–50% on Sonnet, 100% on
  Haiku. Every retry landed. Below Opus the agent's self-report fails too.

## Licence

Apache-2.0
