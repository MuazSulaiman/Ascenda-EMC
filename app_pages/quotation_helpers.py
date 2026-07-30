# app_pages/quotation_helpers.py
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping


TWO_PLACES = Decimal("0.01")


def _q(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def compute_line_total(quantity: Decimal, unit_price: Decimal, discount_pct: Decimal) -> Decimal:
    """qty x price x (1 - discount%), quantized to 2dp with ROUND_HALF_UP."""
    gross = Decimal(quantity) * Decimal(unit_price)
    net = gross * (Decimal("1") - Decimal(discount_pct) / Decimal("100"))
    return _q(net)


def compute_header_totals(lines: Iterable[Mapping], vat_rate: Decimal) -> dict:
    """Sum per-line totals into subtotal/vat_amount/grand_total, all Decimal, 2dp."""
    subtotal = Decimal("0.00")
    for line in lines:
        subtotal += compute_line_total(
            Decimal(line["quantity"]), Decimal(line["unit_price"]), Decimal(line["discount_pct"])
        )
    subtotal = _q(subtotal)
    vat_amount = _q(subtotal * Decimal(vat_rate) / Decimal("100"))
    grand_total = _q(subtotal + vat_amount)
    return {"subtotal": subtotal, "vat_amount": vat_amount, "grand_total": grand_total}
