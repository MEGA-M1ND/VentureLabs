"""The invariant DSL.

An invariant is a declarative claim about ledger state that must hold if the
agent's stated intent actually happened. Contracts are built from small
composable expressions rather than lambdas so they stay inspectable, printable
and serializable -- when a verdict is disputed, you need to show the reader the
claim, not a closure.

Note on syntax: comparisons are fluent methods (`.equals`, `.at_most`) rather
than overloaded `==` / `<=`. Overloading `==` on an expression node would make
these objects lie about equality everywhere else in the program, which is a poor
trade in a package whose entire purpose is not lying about state.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from .ledger import LedgerSnapshot, PaymentStatus, Presence
from .money import CurrencyMismatch, Money
from .verdict import Action, CheckResult, Outcome

Bindings = dict[str, str]


class _Absent:
    """Sentinel for 'this object is readable and definitively not there'.

    Distinct from unreadable (we could not look) and from zero (we looked and
    found nothing, which for a sum is a real number). Any comparison touching an
    absent object is a definitive failure, not a type error.
    """

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<absent>"


ABSENT = _Absent()


@dataclass(frozen=True)
class EvalResult:
    readable: bool
    value: Any = None
    rendered: str = "?"
    detail: str = ""

    @classmethod
    def unreadable(cls, detail: str) -> EvalResult:
        return cls(readable=False, rendered="<unreadable>", detail=detail)

    @classmethod
    def absent(cls, detail: str) -> EvalResult:
        return cls(readable=True, value=ABSENT, rendered="<absent>", detail=detail)


class Expr(ABC):
    """A value read out of a ledger snapshot."""

    @abstractmethod
    def evaluate(self, snap: LedgerSnapshot, bindings: Bindings) -> EvalResult: ...

    @abstractmethod
    def describe(self) -> str: ...

    def equals(self, other: Expr | Any) -> Comparison:
        return Comparison(self, _lift(other), "==")

    def at_most(self, other: Expr | Any) -> Comparison:
        return Comparison(self, _lift(other), "<=")

    def at_least(self, other: Expr | Any) -> Comparison:
        return Comparison(self, _lift(other), ">=")


def _lift(value: Expr | Any) -> Expr:
    return value if isinstance(value, Expr) else Literal(value)


@dataclass(frozen=True)
class Literal(Expr):
    value: Any

    def evaluate(self, snap: LedgerSnapshot, bindings: Bindings) -> EvalResult:
        return EvalResult(readable=True, value=self.value, rendered=_render(self.value))

    def describe(self) -> str:
        return _render(self.value)


def _render(value: Any) -> str:
    if isinstance(value, Money):
        return str(value)
    if isinstance(value, PaymentStatus):
        return value.value
    return str(value)


def _resolve(ref: str, bindings: Bindings) -> str | None:
    """A ref is either a literal id (`pay_abc`) or a `$name` binding."""
    if ref.startswith("$"):
        return bindings.get(ref[1:])
    return ref


# --------------------------------------------------------------------------
# Payment expressions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _PaymentExpr(Expr):
    payment: str

    def _lookup(self, snap: LedgerSnapshot, bindings: Bindings):
        pid = _resolve(self.payment, bindings)
        if pid is None:
            return None, EvalResult.unreadable(f"unbound reference {self.payment}")
        found = snap.payment(pid)
        if found.presence is Presence.UNREADABLE:
            return pid, EvalResult.unreadable(found.detail)
        if found.presence is Presence.ABSENT:
            # Absent is a real, readable answer: the payment does not exist.
            return pid, EvalResult.absent(found.detail)
        return pid, None


@dataclass(frozen=True)
class PaymentAmount(_PaymentExpr):
    def evaluate(self, snap: LedgerSnapshot, bindings: Bindings) -> EvalResult:
        pid, early = self._lookup(snap, bindings)
        if early is not None:
            return early
        payment = snap.payment(pid).value
        return EvalResult(readable=True, value=payment.amount, rendered=str(payment.amount))

    def describe(self) -> str:
        return f"amount({self.payment})"


@dataclass(frozen=True)
class PaymentStatusOf(_PaymentExpr):
    def evaluate(self, snap: LedgerSnapshot, bindings: Bindings) -> EvalResult:
        pid, early = self._lookup(snap, bindings)
        if early is not None:
            return early
        payment = snap.payment(pid).value
        return EvalResult(readable=True, value=payment.status, rendered=payment.status.value)

    def describe(self) -> str:
        return f"status({self.payment})"


# --------------------------------------------------------------------------
# Refund expressions -- where duplicate money movement is caught
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RefundTotal(Expr):
    """Sum of refunds against a payment.

    `include_pending` defaults to True: an in-flight refund has plausibly moved
    money, and treating it as nothing is exactly the mistake that produces
    double refunds when an agent retries.
    """

    payment: str
    include_pending: bool = True

    def evaluate(self, snap: LedgerSnapshot, bindings: Bindings) -> EvalResult:
        pid = _resolve(self.payment, bindings)
        if pid is None:
            return EvalResult.unreadable(f"unbound reference {self.payment}")
        found = snap.refunds_of(pid)
        if found.presence is Presence.UNREADABLE:
            return EvalResult.unreadable(found.detail)
        refunds = [r for r in found.value if self._counts(r)]
        if not refunds:
            # No refunds is not "unknown" -- it is exactly zero money moved. The
            # currency has to come from somewhere real, so take it from the
            # payment; without it there is nothing meaningful to compare against.
            payment = snap.payment(pid)
            if payment.presence is Presence.UNREADABLE:
                return EvalResult.unreadable(
                    f"no refunds against {pid} and the payment's currency could not be read"
                )
            if payment.presence is Presence.ABSENT:
                # The payment itself does not exist. That is a definitive answer,
                # not an unknown one -- propagate absence so the verdict says
                # FAILED rather than shrugging.
                return EvalResult.absent(payment.detail)
            zero = Money.zero(payment.value.amount.currency)
            return EvalResult(readable=True, value=zero, rendered=str(zero), detail="no refunds")
        total = refunds[0].amount
        for r in refunds[1:]:
            total = total + r.amount
        return EvalResult(
            readable=True,
            value=total,
            rendered=str(total),
            detail=f"{len(refunds)} refund(s): {', '.join(r.id for r in refunds)}",
        )

    def _counts(self, refund) -> bool:
        return refund.moved_money if self.include_pending else refund.status.value == "processed"

    @staticmethod
    def _currency_of(snap: LedgerSnapshot, payment_id: str) -> str | None:
        found = snap.payment(payment_id)
        if found.presence is Presence.FOUND:
            return found.value.amount.currency
        return None

    def describe(self) -> str:
        return f"refund_total({self.payment})"


@dataclass(frozen=True)
class RefundCount(Expr):
    payment: str
    include_pending: bool = True

    def evaluate(self, snap: LedgerSnapshot, bindings: Bindings) -> EvalResult:
        pid = _resolve(self.payment, bindings)
        if pid is None:
            return EvalResult.unreadable(f"unbound reference {self.payment}")
        found = snap.refunds_of(pid)
        if found.presence is Presence.UNREADABLE:
            return EvalResult.unreadable(found.detail)
        refunds = [
            r
            for r in found.value
            if (r.moved_money if self.include_pending else r.status.value == "processed")
        ]
        return EvalResult(
            readable=True,
            value=len(refunds),
            rendered=str(len(refunds)),
            detail=", ".join(r.id for r in refunds) or "none",
        )

    def describe(self) -> str:
        return f"refund_count({self.payment})"


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

_OPS = {
    "==": lambda a, b: a == b,
    "<=": lambda a, b: a <= b,
    ">=": lambda a, b: a >= b,
}


@dataclass(frozen=True)
class Comparison:
    left: Expr
    right: Expr
    op: str
    name: str | None = None
    #: What to do if this invariant fails. Defaults to ESCALATE deliberately:
    #: automatically re-driving a money movement must be an explicit decision by
    #: the contract author, never an inferred default.
    on_fail: Action = Action.ESCALATE

    @property
    def label(self) -> str:
        return self.name or f"{self.left.describe()} {self.op} {self.right.describe()}"

    def check(self, snap: LedgerSnapshot, bindings: Bindings) -> CheckResult:
        lhs = self.left.evaluate(snap, bindings)
        rhs = self.right.evaluate(snap, bindings)

        if not lhs.readable or not rhs.readable:
            detail = "; ".join(d for d in (lhs.detail, rhs.detail) if d)
            return CheckResult(
                invariant=self.label,
                outcome=Outcome.INDETERMINATE,
                expected=rhs.rendered,
                observed=lhs.rendered,
                detail=detail or "state could not be read",
            )

        if lhs.value is ABSENT or rhs.value is ABSENT:
            detail = "; ".join(d for d in (lhs.detail, rhs.detail) if d)
            return CheckResult(
                invariant=self.label,
                outcome=Outcome.FAILED,
                expected=rhs.rendered,
                observed=lhs.rendered,
                detail=detail or "referenced object does not exist",
            )

        try:
            passed = _OPS[self.op](lhs.value, rhs.value)
        except CurrencyMismatch as exc:
            return CheckResult(
                invariant=self.label,
                outcome=Outcome.INDETERMINATE,
                expected=rhs.rendered,
                observed=lhs.rendered,
                detail=f"currency mismatch: {exc}",
            )
        except TypeError as exc:
            # Comparing incompatible types means the *contract* is wrong, not
            # the ledger. Surface it loudly rather than scoring it as a failure.
            return CheckResult(
                invariant=self.label,
                outcome=Outcome.INDETERMINATE,
                expected=rhs.rendered,
                observed=lhs.rendered,
                detail=f"contract error: incomparable types ({exc})",
            )

        return CheckResult(
            invariant=self.label,
            outcome=Outcome.VERIFIED if passed else Outcome.FAILED,
            expected=f"{self.op} {rhs.rendered}",
            observed=lhs.rendered,
            detail=lhs.detail,
        )

    def named(self, name: str) -> Comparison:
        return Comparison(self.left, self.right, self.op, name, self.on_fail)

    def remediate(self, action: Action) -> Comparison:
        return Comparison(self.left, self.right, self.op, self.name, action)
