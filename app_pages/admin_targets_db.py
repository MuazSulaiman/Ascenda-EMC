# app_pages/admin_targets_db.py
from typing import Optional


# ── Pure business logic ────────────────────────────────────────────────────────

def calc_productivity(amount: float, visits: int) -> Optional[float]:
    """Return amount/visits rounded to 2dp, or None if visits is zero."""
    if visits and visits > 0:
        return round(float(amount) / int(visits), 2)
    return None


def derive_breakdown_level(
    article_id: Optional[str],
    business_line_id: Optional[int],
    product_category_id: Optional[int],
    business_unit_id: Optional[int],
    customer_id: Optional[int],
) -> str:
    """Return the deepest non-null dimension as the breakdown level string."""
    if article_id:
        return "article"
    if business_line_id:
        return "business_line"
    if product_category_id:
        return "product_category"
    if business_unit_id:
        return "business_unit"
    if customer_id:
        return "customer"
    return "rep"
