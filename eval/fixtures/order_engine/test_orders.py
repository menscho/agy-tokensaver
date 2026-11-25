"""Baseline smoke tests for order_engine fixture."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import unittest

import inventory
import ledger
import pricing
import promotions


class OrderEngineSmokeTests(unittest.TestCase):
    def test_smoke_single_item_pricing_and_shipping(self) -> None:
        items = [
            {
                "sku": "ELC-001",
                "qty": 2,
                "unit_price": "20.00",
                "category": "electronics",
                "weight_kg": "1.0",
            }
        ]
        subtotal = pricing.calculate_subtotal(items)
        self.assertEqual(subtotal, Decimal("40.00"))
        shipping = pricing.calculate_shipping(subtotal, items)
        self.assertEqual(shipping, Decimal("8.00"))
        tax = pricing.compute_tax(subtotal, category="electronics")
        self.assertEqual(tax, Decimal("3.30"))

    def test_smoke_welcome10_promo_on_eligible_cart(self) -> None:
        items = [
            {
                "sku": "HOM-010",
                "qty": 2,
                "unit_price": "20.00",
                "category": "home",
                "weight_kg": "0.5",
            }
        ]
        res = promotions.best_promo_combo(
            items,
            ["WELCOME10"],
            now_iso="2026-06-01T12:00:00Z",
        )
        self.assertEqual(res["applied_codes"], ["WELCOME10"])
        self.assertEqual(res["discount_amount"], Decimal("4.00"))
        self.assertEqual(res["discounted_subtotal"], Decimal("36.00"))

    def test_smoke_inventory_and_ledger_and_catalog(self) -> None:
        catalog_path = Path(__file__).with_name("seed_catalog.json")
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(catalog["skus"]), 120)
        self.assertEqual(
            catalog["warehouses"],
            ["eu-central", "us-east", "us-west"],
        )

        store = inventory.InventoryStore(catalog["stock_by_wh"])
        allocs = store.reserve_order(
            "ORD-SMOKE-1",
            [{"sku": "ELC-001", "qty": 2}],
        )
        self.assertEqual(sum(a["qty"] for a in allocs), 2)

        svc = ledger.LedgerService()
        pay = svc.post_payment("idem-smoke-1", "ORD-SMOKE-1", 5412, 412, batch_id="B1")
        self.assertEqual(pay["status"], "posted")
        self.assertTrue(svc.reconcile_order("ORD-SMOKE-1"))
        self.assertEqual(len(svc.settlement_digest("B1")), 64)


if __name__ == "__main__":
    unittest.main()
