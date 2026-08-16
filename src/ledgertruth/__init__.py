"""ledgertruth -- independent outcome verification for agents that move money.

The question this package answers is not "what did the agent do?" (tracing) or
"did the call survive?" (durable execution), but "is the ledger now in the state
the agent claims it is in?"
"""

from .contracts import full_refund, no_refund_occurred, partial_refund
from .intent import Intent
from .invariants import (
    Comparison,
    Literal,
    PaymentAmount,
    PaymentStatusOf,
    RefundCount,
    RefundTotal,
)
from .ledger import (
    LedgerSnapshot,
    Lookup,
    Order,
    Payment,
    PaymentStatus,
    Presence,
    Refund,
    RefundStatus,
)
from .money import CurrencyMismatch, Money, inr
from .verdict import Action, CheckResult, Outcome, Verdict

__all__ = [
    "Action",
    "CheckResult",
    "Comparison",
    "CurrencyMismatch",
    "Intent",
    "LedgerSnapshot",
    "Literal",
    "Lookup",
    "Money",
    "Order",
    "Outcome",
    "Payment",
    "PaymentAmount",
    "PaymentStatus",
    "PaymentStatusOf",
    "Presence",
    "Refund",
    "RefundCount",
    "RefundStatus",
    "RefundTotal",
    "Verdict",
    "full_refund",
    "inr",
    "no_refund_occurred",
    "partial_refund",
]
