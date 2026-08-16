"""Provider-facing contract.

A LedgerReader turns live provider state into a normalized LedgerSnapshot. It is
the only part of the package that knows a specific payment processor exists, and
it has one hard obligation:

    **Never raise on a read failure.**

A reader that throws when an endpoint is down forces the caller to guess whether
the intent succeeded. Instead, failures are recorded into `snapshot.unreadable`
and `snapshot.read_errors`, and the verifier degrades that invariant to
INDETERMINATE. Distinguishing "the object is not there" from "we could not look"
is the reader's job, and it is the whole reason the verdict has three values.
"""

from __future__ import annotations

from typing import Protocol

from ..ledger import LedgerSnapshot


class LedgerReader(Protocol):
    def snapshot_for_payment(self, payment_id: str) -> LedgerSnapshot:
        """Independently read a payment and its refunds.

        Must not raise for network, auth or HTTP errors. Must distinguish an
        absent object (readable, definitively not there) from an unreadable one.
        """
        ...
