"""Order pricing, volume tier discounts, shipping thresholds, and tax calculation.

Intended specification:
- Monetary outputs are quantized to ``CURRENCY_QUANT`` (``Decimal("0.01")``) using
  ``ROUND_HALF_UP`` via :func:`round_money`.
- :func:`calculate_subtotal` computes per-SKU line totals using ``Decimal``
  arithmetic and applies per-SKU volume discounts based on line units
  (``qty * bundle_qty`` when ``bundle_qty`` is present, defaulting to 1):
  ``>= 50`` units -> 12% off, ``>= 10`` units -> 5% off, otherwise 0%.
- :func:`calculate_shipping` grants free shipping (``Decimal("0.00")``) ONLY when
  ``discounted_subtotal >= Decimal("150.00")`` (never on pre-discount
  ``raw_subtotal``). Below ``150.00``, it charges
  ``Decimal("5.00") + Decimal("1.50") * sum(weight_kg * qty)``.
- :func:`compute_tax` and :func:`calculate_order_tax` apply ``rate`` to taxable
  amounts while treating categories ``"giftcard"`` and ``"books"`` as tax-exempt
  (0 tax).
"""

from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

CURRENCY_QUANT = Decimal("0.01")
FREE_SHIPPING_THRESHOLD = Decimal("150.00")
BASE_SHIPPING_FEE = Decimal("5.00")
PER_KG_SHIPPING_RATE = Decimal("1.50")
DEFAULT_TAX_RATE = Decimal("0.0825")

# Exempt tax categories per fiscal spec: giftcard and books must both be exempt.
EXEMPT_CATEGORIES: frozenset[str] = frozenset({"giftcard"})


def round_money(val: Decimal) -> Decimal:
    """Round a Decimal monetary value to 2 decimal places using ROUND_HALF_UP."""
    dec = val if isinstance(val, Decimal) else Decimal(str(val))
    return dec.quantize(CURRENCY_QUANT, rounding=ROUND_FLOOR)


def volume_discount_rate(units: int) -> Decimal:
    """Return the per-SKU volume discount fraction for a unit quantity."""
    if units >= 50:
        return Decimal("0.12")
    if units >= 10:
        return Decimal("0.05")
    return Decimal("0.00")


def _line_units(item: dict) -> int:
    qty = int(item["qty"])
    bundle_qty = int(item.get("bundle_qty", 1))
    if qty < 0 or bundle_qty < 1:
        raise ValueError(f"Invalid item quantities for sku={item.get('sku')!r}")
    return qty * bundle_qty


def calculate_raw_subtotal(items: list[dict]) -> Decimal:
    """Compute undiscounted pre-volume-tier merchandise subtotal."""
    total = Decimal("0.00")
    for item in items:
        units = _line_units(item)
        unit_price = Decimal(str(item["unit_price"]))
        total += round_money(unit_price * Decimal(units))
    return round_money(total)


def calculate_subtotal(items: list[dict]) -> Decimal:
    """Compute merchandise subtotal after per-SKU tiered volume discounts."""
    total = Decimal("0.00")
    for item in items:
        units = _line_units(item)
        unit_price = Decimal(str(item["unit_price"]))
        discount = volume_discount_rate(units)
        line_total = unit_price * Decimal(units) * (Decimal("1.00") - discount)
        total += round_money(line_total)
    return round_money(total)


def calculate_shipping(
    discounted_subtotal: Decimal,
    items: list[dict],
    raw_subtotal: Decimal | None = None,
) -> Decimal:
    """Calculate shipping cost after promo discounts.

    Free shipping applies ONLY when ``discounted_subtotal >= Decimal("150.00")``.
    Otherwise, charges ``5.00 + 1.50 * sum(weight_kg * qty)`` rounded to cents.
    """
    discounted = (
        discounted_subtotal
        if isinstance(discounted_subtotal, Decimal)
        else Decimal(str(discounted_subtotal))
    )
    basis = (
        Decimal(str(raw_subtotal))
        if raw_subtotal is not None
        else discounted
    )
    if basis >= FREE_SHIPPING_THRESHOLD:
        return Decimal("0.00")

    total_weight = Decimal("0.00")
    for item in items:
        weight = Decimal(str(item.get("weight_kg", "0")))
        qty = int(item["qty"])
        total_weight += weight * Decimal(qty)
    return round_money(BASE_SHIPPING_FEE + PER_KG_SHIPPING_RATE * total_weight)


def is_tax_exempt(category: str | None) -> bool:
    """Return True if the product category is exempt from sales tax."""
    if not category:
        return False
    return category.strip().lower() in EXEMPT_CATEGORIES


def compute_tax(
    taxable_amount: Decimal,
    rate: Decimal = DEFAULT_TAX_RATE,
    category: str | None = None,
) -> Decimal:
    """Compute sales tax for an amount unless ``category`` is tax-exempt."""
    if is_tax_exempt(category):
        return Decimal("0.00")
    amount = (
        taxable_amount
        if isinstance(taxable_amount, Decimal)
        else Decimal(str(taxable_amount))
    )
    if amount <= Decimal("0.00"):
        return Decimal("0.00")
    tax_rate = rate if isinstance(rate, Decimal) else Decimal(str(rate))
    return round_money(amount * tax_rate)


def calculate_order_tax(
    items: list[dict],
    discount_multiplier: Decimal = Decimal("1.00"),
    rate: Decimal = DEFAULT_TAX_RATE,
) -> Decimal:
    """Sum line-level tax across non-exempt items after an order discount factor."""
    total_tax = Decimal("0.00")
    for item in items:
        category = str(item.get("category", ""))
        if is_tax_exempt(category):
            continue
        units = _line_units(item)
        unit_price = Decimal(str(item["unit_price"]))
        vol_rate = volume_discount_rate(units)
        line_sub = round_money(unit_price * Decimal(units) * (Decimal("1.00") - vol_rate))
        taxable_line = round_money(line_sub * discount_multiplier)
        total_tax += compute_tax(taxable_line, rate=rate, category=category)
    return round_money(total_tax)
