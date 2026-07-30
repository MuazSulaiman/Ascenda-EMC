import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from decimal import Decimal
from app_pages.quotation_helpers import compute_line_total, compute_header_totals


def test_line_total_no_discount():
    assert compute_line_total(Decimal("2"), Decimal("100.00"), Decimal("0")) == Decimal("200.00")

def test_line_total_with_discount():
    assert compute_line_total(Decimal("3"), Decimal("50.00"), Decimal("10")) == Decimal("135.00")

def test_line_total_100_pct_discount():
    assert compute_line_total(Decimal("5"), Decimal("40.00"), Decimal("100")) == Decimal("0.00")

def test_line_total_fractional_quantity():
    assert compute_line_total(Decimal("1.5"), Decimal("10.00"), Decimal("0")) == Decimal("15.00")

def test_line_total_rounds_half_up():
    # 1 x 10.005 with 0% discount -> 10.005 rounds to 10.01 under ROUND_HALF_UP
    assert compute_line_total(Decimal("1"), Decimal("10.005"), Decimal("0")) == Decimal("10.01")


def test_header_totals_single_line():
    lines = [{"quantity": Decimal("2"), "unit_price": Decimal("100.00"), "discount_pct": Decimal("0")}]
    totals = compute_header_totals(lines, Decimal("15.00"))
    assert totals["subtotal"] == Decimal("200.00")
    assert totals["vat_amount"] == Decimal("30.00")
    assert totals["grand_total"] == Decimal("230.00")

def test_header_totals_multiple_lines_with_discounts():
    lines = [
        {"quantity": Decimal("2"), "unit_price": Decimal("100.00"), "discount_pct": Decimal("10")},
        {"quantity": Decimal("1"), "unit_price": Decimal("50.00"), "discount_pct": Decimal("0")},
    ]
    totals = compute_header_totals(lines, Decimal("15.00"))
    # line1 = 2*100*0.9 = 180.00, line2 = 50.00, subtotal = 230.00
    assert totals["subtotal"] == Decimal("230.00")
    assert totals["vat_amount"] == Decimal("34.50")
    assert totals["grand_total"] == Decimal("264.50")

def test_header_totals_zero_vat():
    lines = [{"quantity": Decimal("1"), "unit_price": Decimal("100.00"), "discount_pct": Decimal("0")}]
    totals = compute_header_totals(lines, Decimal("0"))
    assert totals["vat_amount"] == Decimal("0.00")
    assert totals["grand_total"] == Decimal("100.00")

def test_header_totals_empty_lines():
    totals = compute_header_totals([], Decimal("15.00"))
    assert totals["subtotal"] == Decimal("0.00")
    assert totals["vat_amount"] == Decimal("0.00")
    assert totals["grand_total"] == Decimal("0.00")

def test_header_totals_14_lines_no_float_drift():
    # 14 lines chosen so a naive float accumulation would visibly drift
    # from the correct Decimal result; asserts exact equality, not approx.
    lines = [
        {"quantity": Decimal("1"), "unit_price": Decimal("0.10"), "discount_pct": Decimal("0")}
        for _ in range(14)
    ]
    totals = compute_header_totals(lines, Decimal("15.00"))
    assert totals["subtotal"] == Decimal("1.40")
    assert totals["vat_amount"] == Decimal("0.21")
    assert totals["grand_total"] == Decimal("1.61")
