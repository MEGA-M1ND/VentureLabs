# Feasibility findings

Running log of the load-bearing assumptions, and whether they survived contact.

---

## F-1: Can the chaos proxy sit between the MCP server and the Razorpay API?

**Status: RESOLVED — yes, two viable paths, no TLS interception required.**

The concern was that `razorpay-mcp-server` might pin its API host in a way that
left no interception point, forcing a TLS man-in-the-middle with a locally
trusted CA — fiddly, and a bad look in a repo about trustworthy verification.

It does not. `razorpay-mcp-server` (Go 1.24) depends on `razorpay/razorpay-go
v1.4.0`, whose `requests.Request` struct carries:

```go
type Request struct {
    ...
    HTTPClient *http.Client
    BaseURL    string
}
```

`BaseURL` is exported and settable, populated from `constants.BASE_URL` only at
construction, and consumed at every call site as:

```go
url := fmt.Sprintf("%s%s", request.BaseURL, path)
```

`Client` embeds `*requests.Request`, so `client.BaseURL = "http://127.0.0.1:9099"`
redirects the entire SDK.

### Path A (preferred): configurable base URL, via a fork

Add an env var — `RAZORPAY_API_BASE_URL`, defaulting to the real host — and set
`client.BaseURL` from it. Roughly a five-line change.

The proxy then runs as an ordinary HTTP reverse proxy on loopback, forwarding to
`https://api.razorpay.com`. The hop we inspect is plaintext on localhost, so we
get full request/response visibility with **no TLS MITM and no custom CA**.

This unlocks the complete fault set:

| Fault | Path A | Path B |
|---|---|---|
| Response dropped after commit | yes | yes |
| Response delayed past agent timeout | yes | yes |
| Duplicate request delivery | yes | no |
| Stale read after write | yes | no |

**Secondary benefit:** a configurable API base URL is a legitimately useful
feature for anyone testing against a mock, so this is a natural first pull
request to `razorpay/razorpay-mcp-server` — a substantive contribution rather
than a contrived one, and a far better first contact with that team than a cold
message.

### Path B (fallback): `HTTPS_PROXY`, no fork

`razorpay-go` builds its client as `&http.Client{Timeout: ...}`, leaving
`Transport` nil. Go then falls back to `http.DefaultTransport`, which uses
`ProxyFromEnvironment` — so `HTTPS_PROXY` is honoured with no code change.

The proxy sees only a `CONNECT` tunnel, so it cannot read or rewrite bodies. It
can still **drop the connection after forwarding the request bytes but before
relaying the response**, which is precisely the "committed but unacknowledged"
failure. It can also delay arbitrarily.

### Consequence for the experiment design

Duplicate money movement does **not** need to be injected directly. The honest
causal chain is:

```
response dropped after commit  ->  agent believes the refund failed
                               ->  agent retries
                               ->  two refunds exist
```

Inducing duplicates through a dropped response is strictly more faithful than
fabricating a duplicate, and it keeps the fault set to pure transport failures.
Path B alone is therefore sufficient for the headline result; Path A is an
upgrade that widens coverage.

---

## F-2: Toolchain

**Status: OPEN — action required.**

This machine has Python 3.12.10, uv, git, Node 24. It has **no Docker, no Go,
and no WSL**.

`create_refund` is excluded from Razorpay's hosted remote MCP server and is
available only in local deployment, so a local build is mandatory.

Recommendation: **install Go** rather than Docker Desktop. The MCP server builds
to a native Windows binary with no virtualization, whereas Docker Desktop on
Windows 11 Home pulls in a WSL2 install. Go is also required for Path A above.

---

## F-3: Razorpay test-mode access

**Status: PASSED — 2026-08-16.** No Stripe fallback needed.

Verified against live test mode with `rzp_test_…` credentials:

| Check | Result |
|---|---|
| Authentication | `200` |
| Order creation | `200` |
| Captured payment obtainable | yes — see [FIND-2](findings.md) |
| Refund creation | `200`, refund id returned |
| Refund read-back (`GET /v1/refunds/{id}`) | `200` |
| Refunds-for-payment listing | `200` |
| Settlements (`GET /v1/settlements`) | `200` |
| Recon (`GET /v1/settlements/recon/combined`) | `200` |
| Refund idempotency | supported — see [FIND-1](findings.md) |

Every read the provider adapter needs is available, and every write the mission
suite needs is available.

**Still unverified:** whether test-mode **webhooks** fire. Not blocking — the
verifier reads state directly rather than depending on webhook delivery — but it
is needed if the mission suite is to include webhook-driven reconciliation
tasks. Check before designing those missions.

Reproduce with:

```bash
uv run python scripts/spike_feasibility.py
```
