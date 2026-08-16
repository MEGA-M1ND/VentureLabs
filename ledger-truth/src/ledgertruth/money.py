"""Money as integer minor units.

Payment ledgers are exact. Floats are not. Every amount in this package is an
integer count of the currency's minor unit (paise for INR, cents for USD), and
arithmetic across currencies raises rather than silently coercing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self


class CurrencyMismatch(ValueError):
    """Raised when two amounts in different currencies are combined or compared."""


@dataclass(frozen=True, order=False)
class Money:
    minor: int
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.minor, int) or isinstance(self.minor, bool):
            raise TypeError(f"Money.minor must be int, got {type(self.minor).__name__}")
        if not self.currency or len(self.currency) != 3:
            raise ValueError(f"currency must be a 3-letter code, got {self.currency!r}")
        object.__setattr__(self, "currency", self.currency.upper())

    @classmethod
    def zero(cls, currency: str) -> Self:
        return cls(0, currency)

    def _check(self, other: Money) -> None:
        if not isinstance(other, Money):
            raise TypeError(f"expected Money, got {type(other).__name__}")
        if self.currency != other.currency:
            raise CurrencyMismatch(f"{self.currency} vs {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.minor - other.minor, self.currency)

    def __lt__(self, other: Money) -> bool:
        self._check(other)
        return self.minor < other.minor

    def __le__(self, other: Money) -> bool:
        self._check(other)
        return self.minor <= other.minor

    def __gt__(self, other: Money) -> bool:
        self._check(other)
        return self.minor > other.minor

    def __ge__(self, other: Money) -> bool:
        self._check(other)
        return self.minor >= other.minor

    def __str__(self) -> str:
        sign = "-" if self.minor < 0 else ""
        whole, frac = divmod(abs(self.minor), 100)
        return f"{sign}{whole}.{frac:02d} {self.currency}"


def inr(rupees: str | int) -> Money:
    """Build INR from whole rupees. Kept explicit so no decimal string ever
    reaches a float. `inr("249.50")` -> 24950 paise."""
    text = str(rupees)
    if "." in text:
        whole, _, frac = text.partition(".")
        if len(frac) > 2:
            raise ValueError(f"INR has 2 minor digits, got {text!r}")
        frac = frac.ljust(2, "0")
    else:
        whole, frac = text, "00"
    negative = whole.startswith("-")
    whole = whole.lstrip("-") or "0"
    if not (whole.isdigit() and frac.isdigit()):
        raise ValueError(f"not a decimal amount: {text!r}")
    minor = int(whole) * 100 + int(frac)
    return Money(-minor if negative else minor, "INR")
