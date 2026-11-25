"""Multi-warehouse inventory reservation and priority backorder queue.

Intended specification:
- Warehouses are checked in priority order ``["eu-central", "us-east", "us-west"]``.
- :meth:`InventoryStore.reserve_order` MUST be atomic across all order lines:
  if any line cannot be satisfied (raising :class:`OutOfStockError`), no stock
  counters in ``stock_by_wh`` may be modified for earlier lines.
- When ``allow_split=False`` on a line, the entire ``qty`` MUST be allocated
  from a single warehouse with ``stock >= qty``; if none qualifies, raise
  :class:`OutOfStockError`.
- :meth:`InventoryStore.pop_backorder_batch` pops queued backorders for ``sku``
  ordered by highest ``priority`` first (descending), breaking ties by lowest
  ``created_seq`` (ascending FIFO), filling items whose ``qty <= remaining``.
"""

from __future__ import annotations

from copy import deepcopy

WAREHOUSE_ORDER: tuple[str, ...] = ("eu-central", "us-east", "us-west")


class OutOfStockError(Exception):
    """Raised when warehouse stock cannot satisfy an order reservation."""


class InventoryStore:
    """In-memory multi-warehouse inventory and backorder store."""

    def __init__(
        self,
        stock_by_wh: dict[str, dict[str, int]],
        backorders: list[dict] | None = None,
    ) -> None:
        self.stock_by_wh: dict[str, dict[str, int]] = deepcopy(stock_by_wh)
        self.backorders: list[dict] = list(backorders) if backorders is not None else []
        self.reservations: dict[str, list[dict]] = {}

    def _ordered_warehouses(self) -> list[str]:
        ordered = [wh for wh in WAREHOUSE_ORDER if wh in self.stock_by_wh]
        for wh in self.stock_by_wh:
            if wh not in ordered:
                ordered.append(wh)
        return ordered

    def available_stock(self, sku: str) -> int:
        """Return total available units for ``sku`` across all warehouses."""
        return sum(
            max(0, int(wh_stock.get(sku, 0)))
            for wh_stock in self.stock_by_wh.values()
        )

    def reserve_order(self, order_id: str, lines: list[dict]) -> list[dict]:
        """Reserve stock for all lines of ``order_id`` atomically."""
        if not order_id:
            raise ValueError("order_id must be non-empty")

        allocations: list[dict] = []
        warehouses = self._ordered_warehouses()

        for line in lines:
            sku = str(line["sku"])
            qty = int(line["qty"])
            if qty <= 0:
                raise ValueError(f"Line qty must be positive for sku={sku!r}")
            allow_split = bool(line.get("allow_split", True))
            remaining = qty

            for wh in warehouses:
                avail = int(self.stock_by_wh[wh].get(sku, 0))
                if avail <= 0:
                    continue
                take = min(avail, remaining)
                self.stock_by_wh[wh][sku] = avail - take
                allocations.append(
                    {
                        "order_id": order_id,
                        "sku": sku,
                        "warehouse": wh,
                        "qty": take,
                        "allow_split": allow_split,
                    }
                )
                remaining -= take
                if remaining == 0:
                    break

            if remaining > 0:
                raise OutOfStockError(
                    f"Insufficient stock for sku={sku} (short {remaining} units)"
                )

        self.reservations[order_id] = allocations
        return allocations

    def enqueue_backorder(
        self,
        order_id: str,
        sku: str,
        qty: int,
        priority: int,
        created_seq: int,
    ) -> None:
        """Add a backorder entry to the queue."""
        self.backorders.append(
            {
                "order_id": order_id,
                "sku": sku,
                "qty": int(qty),
                "priority": int(priority),
                "created_seq": int(created_seq),
            }
        )

    def pop_backorder_batch(self, sku: str, max_qty: int) -> list[dict]:
        """Pop up to ``max_qty`` units of backorders for ``sku``.

        Orders are prioritized by highest ``priority`` first, then lowest
        ``created_seq`` (FIFO) on ties.
        """
        if max_qty <= 0:
            return []

        candidates = [b for b in self.backorders if b.get("sku") == sku]
        candidates.sort(key=lambda b: (int(b["priority"]), int(b["created_seq"])))

        picked: list[dict] = []
        picked_ids: set[int] = set()
        remaining = int(max_qty)

        for entry in candidates:
            entry_qty = int(entry["qty"])
            if entry_qty <= remaining:
                picked.append(dict(entry))
                picked_ids.add(id(entry))
                remaining -= entry_qty
                if remaining == 0:
                    break

        self.backorders = [b for b in self.backorders if id(b) not in picked_ids]
        return picked
