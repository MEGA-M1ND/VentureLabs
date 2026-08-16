# Findings

Substantive discoveries, as distinct from the go/no-go gates in
[feasibility.md](feasibility.md).

---

## FIND-1: Razorpay's official MCP server sends no idempotency key on `create_refund`

**Confirmed 2026-08-16, against test mode and against the published source.**

### The API supports idempotency

Two independent mechanisms exist, and both work. Verified empirically against
`api.razorpay.com` in test mode:

| Mechanism | Behaviour on replay | Observed |
|---|---|---|
| `X-Refund-Idempotency` header | returns the **same** refund | two calls → one refund, identical id |
| `receipt` body field | **rejects** the second | `400 BAD_REQUEST_ERROR: Duplicate receipt found for this refund request.` |

Note the semantics differ: the header replays the stored response, while
`receipt` errors. Both prevent a duplicate; only the header is safe to fire
blindly on retry.

### The MCP server uses neither

`pkg/razorpay/refunds.go` builds the call as:

```go
refund, err := client.Payment.Refund(
    payload["payment_id"].(string),
    int(payload["amount"].(float64)), data, nil)
```

The SDK signature is:

```go
func (p *Payment) Refund(paymentID string, amount int,
    data map[string]interface{}, extraHeaders map[string]string) (...)
```

That trailing `nil` is `extraHeaders`. **`X-Refund-Idempotency` is never sent.**

`receipt` *is* exposed as an optional tool parameter, but its description reads
in full:

> "A unique identifier provided by you for your internal reference."

Nothing in the tool surface tells a model that `receipt` prevents duplicate
refunds. An LLM has no reason to populate it, and every reason to omit an
optional field described as being for someone else's bookkeeping.

### Why this matters

The duplicate-refund failure is therefore not hypothetical — it is the default
behaviour of the current tool surface:

```
agent calls create_refund
  -> refund commits server-side
  -> response dropped / times out in transit
  -> agent observes failure
  -> agent retries
  -> no idempotency key on either call
  -> two refunds exist, twice the money has left the merchant
```

Every link in that chain is now verified except the agent's retry decision,
which is what the experiment measures.

### Consequences for this project

1. **The thesis is validated before the harness exists.** The gap between "the
   platform supports safe retries" and "the agent-facing tool uses them" is
   exactly the gap `ledgertruth` was built to detect.
2. **`RETRY_IDEMPOTENT` remediation must supply the header itself.** Our repair
   layer cannot assume the agent's tooling provided one. It must send
   `X-Refund-Idempotency` explicitly, or read-then-decide.
3. **This is the first PR.** Setting `extraHeaders` from a tool-level
   idempotency parameter — and amending the `receipt` description to say what it
   actually does — is a small, self-contained, obviously-correct contribution to
   `razorpay/razorpay-mcp-server`.

### Caveat

Verified in **test mode** on a single account, on 2026-08-16. Before publishing,
confirm the same `extraHeaders: nil` on the then-current `main`, and re-run the
header and `receipt` probes — this is the kind of thing that gets fixed quietly.

---

## FIND-2: Captured payments can be seeded entirely server-side

**Confirmed 2026-08-16.** Removes the need for browser automation.

Server-to-server payment creation (`/v1/payments/create/upi`,
`/v1/payments/create/json`) returns `404` on a standard test account — those
require explicit enablement.

But the endpoints the browser checkout uses authenticate with a **public
`key_id` in the request body rather than HTTP basic auth**, which is why they
returned `401` rather than `404` when probed with basic auth. Called the way
checkout calls them, they work:

```
POST /v1/orders                    (basic auth)      -> order_id
POST /v1/payments/create/ajax      (key_id in body,  -> pay_...
                                    form-encoded,
                                    upi[vpa]=success@razorpay)
```

The test VPA `success@razorpay` auto-succeeds, and the payment was observed
**`status=captured, captured=True`** on the first poll — no capture step, no
3-D Secure, no browser.

This makes seeding ~40 independent refundable payments a scripted, repeatable
setup step rather than a manual chore.
