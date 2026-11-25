"""Promotional code validation, stacking, and optimal savings selection.

Intended specification:
- Excluded categories ``"giftcard"`` and ``"clearance"`` NEVER count toward a
  promo's ``min_spend`` threshold and NEVER receive percentage discounts.
- Promo expiration timestamps (``expires``) are INCLUSIVE: a code is valid when
  ``now_iso <= expires`` (compared as UTC datetimes).
- Stackable rule: either a single valid non-stackable promo (``stackable=False``)
  OR any subset of valid ``stackable=True`` promos may be applied together.
- Sequential compounding formula for stackable percentage promos:
  given valid stackable percentages ``p_1, p_2, ..., p_k``, the combined
  multiplier on eligible subtotal is ``prod(1 - p_i)`` so the effective
  discount rate is ``1 - prod(1 - p_i)`` (e.g., ``WELCOME10`` 10% + ``VIP20``
  20% yields ``1 - 0.90 * 0.80 = 0.28`` = 28% off eligible subtotal).
- Any applied promo with ``shipping_waived=True`` sets shipping to ``0.00``;
  ``best_promo_combo`` picks the valid combination maximizing total savings
  (merchandise discount + shipping fee saved relative to no-promo shipping).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from itertools import combinations

try:
    from .pricing import calculate_shipping, calculate_subtotal, round_money
except ImportError:
    from pricing import calculate_shipping, calculate_subtotal, round_money

EXCLUDED_CATEGORIES: frozenset[str] = frozenset({"giftcard", "clearance"})


@dataclass(frozen=True)
class PromoRule:
    code: str
    pct_off: Decimal
    stackable: bool
    min_spend: Decimal = Decimal("0.00")
    expires: str | None = None
    shipping_waived: bool = False


PROMO_CATALOG: dict[str, PromoRule] = {
    "WELCOME10": PromoRule(
        code="WELCOME10",
        pct_off=Decimal("0.10"),
        stackable=True,
        min_spend=Decimal("30.00"),
        expires="2026-12-31T23:59:59Z",
    ),
    "BULK15": PromoRule(
        code="BULK15",
        pct_off=Decimal("0.15"),
        stackable=False,
        min_spend=Decimal("100.00"),
    ),
    "FREESHIP": PromoRule(
        code="FREESHIP",
        pct_off=Decimal("0.00"),
        stackable=True,
        min_spend=Decimal("50.00"),
        shipping_waived=True,
    ),
    "FLASH25": PromoRule(
        code="FLASH25",
        pct_off=Decimal("0.25"),
        stackable=False,
        min_spend=Decimal("0.00"),
        expires="2026-01-01T00:00:00Z",
    ),
    "VIP20": PromoRule(
        code="VIP20",
        pct_off=Decimal("0.20"),
        stackable=True,
        min_spend=Decimal("80.00"),
    ),
}


def _parse_utc(iso_str: str) -> datetime:
    normalized = iso_str.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _split_subtotals(items: list[dict]) -> tuple[Decimal, Decimal, Decimal]:
    """Return (total_subtotal, eligible_subtotal, excluded_subtotal)."""
    eligible_items = [
        item for item in items
        if str(item.get("category", "")).strip().lower() not in EXCLUDED_CATEGORIES
    ]
    excluded_items = [
        item for item in items
        if str(item.get("category", "")).strip().lower() in EXCLUDED_CATEGORIES
    ]
    eligible_sub = calculate_subtotal(eligible_items)
    excluded_sub = calculate_subtotal(excluded_items)
    total_sub = round_money(eligible_sub + excluded_sub)
    return total_sub, eligible_sub, excluded_sub


def is_promo_valid(
    rule: PromoRule,
    eligible_spend: Decimal,
    now_iso: str,
) -> bool:
    """Check expiration and minimum spend eligibility for a promo rule."""
    if rule.expires is not None:
        now_dt = _parse_utc(now_iso)
        exp_dt = _parse_utc(rule.expires)
        if now_dt >= exp_dt:
            return False
    return eligible_spend >= rule.min_spend


def _evaluate_combo(
    combo: tuple[PromoRule, ...],
    items: list[dict],
    total_sub: Decimal,
    eligible_sub: Decimal,
    baseline_shipping: Decimal,
) -> dict:
    multiplier = Decimal("1.00")
    shipping_waived = False
    for rule in combo:
        multiplier *= (Decimal("1.00") - rule.pct_off)
        if rule.shipping_waived:
            shipping_waived = True

    discounted_eligible = round_money(eligible_sub * multiplier)
    discount_amount = round_money(eligible_sub - discounted_eligible)
    discounted_subtotal = round_money(total_sub - discount_amount)

    if shipping_waived:
        shipping_cost = Decimal("0.00")
    else:
        shipping_cost = calculate_shipping(
            discounted_subtotal,
            items,
            raw_subtotal=total_sub,
        )
    shipping_delta = baseline_shipping - shipping_cost
    total_savings = round_money(discount_amount + shipping_delta)

    return {
        "applied_codes": [r.code for r in combo],
        "discount_amount": discount_amount,
        "discounted_subtotal": discounted_subtotal,
        "eligible_subtotal": eligible_sub,
        "shipping_waived": shipping_waived,
        "shipping_cost": shipping_cost,
        "total_savings": total_savings,
    }


def best_promo_combo(
    items: list[dict],
    codes: list[str],
    now_iso: str,
    catalog: dict[str, PromoRule] | None = None,
) -> dict:
    """Select the valid promo code or stackable combination yielding maximum savings."""
    active_catalog = catalog if catalog is not None else PROMO_CATALOG
    total_sub, eligible_sub, _ = _split_subtotals(items)
    baseline_shipping = calculate_shipping(total_sub, items, raw_subtotal=total_sub)

    seen: set[str] = set()
    valid_rules: list[PromoRule] = []
    for raw_code in codes:
        code = raw_code.strip().upper()
        if code in seen or code not in active_catalog:
            continue
        seen.add(code)
        rule = active_catalog[code]
        if is_promo_valid(rule, total_sub, now_iso):
            valid_rules.append(rule)

    best_result = _evaluate_combo((), items, total_sub, eligible_sub, baseline_shipping)
    for rule in valid_rules:
        candidate = _evaluate_combo(
            (rule,),
            items,
            total_sub,
            eligible_sub,
            baseline_shipping,
        )
        if candidate["total_savings"] > best_result["total_savings"]:
            best_result = candidate
    return best_result
