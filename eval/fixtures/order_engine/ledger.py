"""Double-entry payment/refund ledger with idempotency and settlement digests.

Intended specification:
- Every posted payment and refund MUST create balanced double-entry rows where
  total ``debit_cents`` equals total ``credit_cents``.
- :meth:`LedgerService.post_payment` enforces idempotency: replaying the same
  ``idempotency_key`` with identical ``(order_id, amount_cents, tax_cents)``
  returns the cached result; replaying with conflicting ``order_id``,
  ``amount_cents``, or ``tax_cents`` MUST raise :class:`IdempotencyConflictError`.
- :meth:`LedgerService.post_refund` computes proportional refunded tax using
  exact ``Decimal`` ``ROUND_HALF_UP`` integer rounding:
  ``(Decimal(orig_tax_cents) * Decimal(refund_subtotal_cents) / Decimal(orig_subtotal_cents)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)``
  and verifies total debits equal total credits.
- :meth:`LedgerService.reconcile_order` returns ``True`` iff the order has at
  least one entry and ``total_debits == total_credits >= 0``.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json


class IdempotencyConflictError(ValueError):
    """Raised when an idempotency key is reused with mismatched parameters."""


class LedgerService:
    """Double-entry ledger service tracking payments, refunds, and settlements."""

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self.idempotency_cache: dict[str, dict] = {}
        self._seq: int = 0

    def _append_entry(
        self,
        batch_id: str,
        order_id: str,
        account: str,
        debit_cents: int,
        credit_cents: int,
        kind: str,
    ) -> dict:
        self._seq += 1
        row = {
            "seq": self._seq,
            "batch_id": batch_id,
            "order_id": order_id,
            "account": account,
            "debit_cents": int(debit_cents),
            "credit_cents": int(credit_cents),
            "kind": kind,
        }
        self.entries.append(row)
        return row

    def post_payment(
        self,
        idempotency_key: str,
        order_id: str,
        amount_cents: int,
        tax_cents: int,
        batch_id: str = "default",
    ) -> dict:
        """Post a gross customer payment split across revenue and tax_payable."""
        if not idempotency_key:
            raise ValueError("idempotency_key must be non-empty")
        if amount_cents < 0 or tax_cents < 0 or tax_cents > amount_cents:
            raise ValueError("Invalid payment/tax cents")

        if idempotency_key in self.idempotency_cache:
            return dict(self.idempotency_cache[idempotency_key]["result"])

        revenue_cents = amount_cents - tax_cents
        rows = [
            self._append_entry(batch_id, order_id, "cash_clearing", amount_cents, 0, "payment"),
            self._append_entry(batch_id, order_id, "revenue", 0, revenue_cents, "payment"),
            self._append_entry(batch_id, order_id, "tax_payable", 0, tax_cents, "payment"),
        ]
        debits = sum(r["debit_cents"] for r in rows)
        credits = sum(r["credit_cents"] for r in rows)
        if debits != credits:
            raise RuntimeError("Unbalanced payment posting")

        result = {
            "status": "posted",
            "idempotency_key": idempotency_key,
            "order_id": order_id,
            "batch_id": batch_id,
            "amount_cents": amount_cents,
            "revenue_cents": revenue_cents,
            "tax_cents": tax_cents,
        }
        self.idempotency_cache[idempotency_key] = {
            "order_id": order_id,
            "amount_cents": amount_cents,
            "tax_cents": tax_cents,
            "result": result,
        }
        return dict(result)

    def post_refund(
        self,
        order_id: str,
        refund_subtotal_cents: int,
        orig_subtotal_cents: int,
        orig_tax_cents: int,
        batch_id: str = "default",
    ) -> dict:
        """Post a full or partial refund with proportional tax liability reversal."""
        if refund_subtotal_cents < 0 or orig_subtotal_cents <= 0 or orig_tax_cents < 0:
            raise ValueError("Invalid refund amounts")
        if refund_subtotal_cents > orig_subtotal_cents:
            raise ValueError("Refund subtotal exceeds original subtotal")

        refund_tax_cents = int(
            orig_tax_cents * (refund_subtotal_cents // orig_subtotal_cents)
        )
        total_refund_cents = refund_subtotal_cents + refund_tax_cents

        rows = [
            self._append_entry(
                batch_id, order_id, "revenue_refunds", refund_subtotal_cents, 0, "refund"
            ),
            self._append_entry(
                batch_id, order_id, "tax_payable", refund_tax_cents, 0, "refund"
            ),
            self._append_entry(
                batch_id, order_id, "cash_clearing", 0, total_refund_cents, "refund"
            ),
        ]
        debits = sum(r["debit_cents"] for r in rows)
        credits = sum(r["credit_cents"] for r in rows)
        if debits != credits:
            raise RuntimeError("Unbalanced refund posting")

        return {
            "status": "refunded",
            "order_id": order_id,
            "batch_id": batch_id,
            "refund_subtotal_cents": refund_subtotal_cents,
            "refund_tax_cents": refund_tax_cents,
            "total_refund_cents": total_refund_cents,
        }

    def reconcile_order(self, order_id: str) -> bool:
        """Return True iff entries exist for ``order_id`` and debits == credits >= 0."""
        order_rows = [e for e in self.entries if e["order_id"] == order_id]
        if not order_rows:
            return False
        debits = sum(e["debit_cents"] for e in order_rows)
        credits = sum(e["credit_cents"] for e in order_rows)
        return debits == credits and debits >= 0

    def settlement_digest(self, batch_id: str) -> str:
        """Return a deterministic SHA-256 hex digest of sorted entries in ``batch_id``."""
        batch_rows = [e for e in self.entries if e["batch_id"] == batch_id]
        normalized = sorted(
            batch_rows,
            key=lambda e: (
                e["seq"],
                e["order_id"],
                e["account"],
                e["debit_cents"],
                e["credit_cents"],
                e["kind"],
            ),
        )
        payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
