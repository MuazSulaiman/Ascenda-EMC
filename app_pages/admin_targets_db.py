# app_pages/admin_targets_db.py
from typing import Optional
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text

from db import engine
from db_ops import query_df, query_scalar, exec_sql


def _now():
    return datetime.now(timezone.utc)


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


# ── Year helpers ───────────────────────────────────────────────────────────────

def get_all_years() -> pd.DataFrame:
    return query_df("SELECT * FROM v_target_year_summary ORDER BY year DESC")


def get_year(year: int) -> Optional[dict]:
    df = query_df("SELECT * FROM target_year WHERE year = :y", {"y": year})
    return df.iloc[0].to_dict() if not df.empty else None


def create_year(year: int, amount: float, visits: int, created_by: int) -> None:
    exec_sql(
        """
        INSERT INTO target_year (year, target_amount, target_visits, created_by, updated_by)
        VALUES (:year, :amount, :visits, :cb, :cb)
        """,
        {"year": year, "amount": amount, "visits": visits, "cb": created_by},
    )


def update_year(year: int, amount: float, visits: int, updated_by: int) -> None:
    exec_sql(
        """
        UPDATE target_year
        SET target_amount = :amount, target_visits = :visits,
            updated_by = :ub, updated_at = NOW()
        WHERE year = :year
        """,
        {"year": year, "amount": amount, "visits": visits, "ub": updated_by},
    )


def transition_year_status(
    year: int, new_status: str, updated_by: int, expected_status: str
) -> bool:
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                UPDATE target_year
                SET status = :status, updated_by = :ub, updated_at = NOW()
                WHERE year = :year AND status = :expected_status
                """
            ),
            {"year": year, "status": new_status, "ub": updated_by, "expected_status": expected_status},
        )
    return result.rowcount > 0


def get_year_allocated_totals(year: int) -> dict:
    row = query_df(
        """
        SELECT COALESCE(SUM(target_amount), 0) AS total_amount,
               COALESCE(SUM(target_visits), 0) AS total_visits
        FROM target_rep WHERE year = :y
        """,
        {"y": year},
    )
    return {"amount": float(row.iloc[0]["total_amount"]),
            "visits": int(row.iloc[0]["total_visits"])}


# ── Rep helpers ────────────────────────────────────────────────────────────────

def get_reps_for_year(year: int) -> pd.DataFrame:
    return query_df(
        "SELECT * FROM v_target_rep_summary WHERE year = :y ORDER BY rep_name",
        {"y": year},
    )


def get_rep(target_rep_id: int) -> Optional[dict]:
    df = query_df(
        "SELECT * FROM v_target_rep_summary WHERE target_rep_id = :id",
        {"id": target_rep_id},
    )
    return df.iloc[0].to_dict() if not df.empty else None


def upsert_rep(year: int, user_id: int, amount: float,
               visits: int, acting_user_id: int) -> None:
    exec_sql(
        """
        INSERT INTO target_rep (year, user_id, target_amount, target_visits,
                                created_by, updated_by)
        VALUES (:year, :uid, :amount, :visits, :cb, :cb)
        ON CONFLICT (year, user_id) DO UPDATE
        SET target_amount = EXCLUDED.target_amount,
            target_visits = EXCLUDED.target_visits,
            updated_by    = EXCLUDED.updated_by,
            updated_at    = NOW()
        """,
        {"year": year, "uid": user_id, "amount": amount,
         "visits": visits, "cb": acting_user_id},
    )


def remove_rep(target_rep_id: int) -> None:
    exec_sql("DELETE FROM target_rep WHERE id = :id", {"id": target_rep_id})


def get_rep_breakdown_count(target_rep_id: int) -> int:
    return int(query_scalar(
        "SELECT COUNT(*) FROM target_breakdown WHERE target_rep_id = :id",
        {"id": target_rep_id},
    ) or 0)


# ── Breakdown helpers ──────────────────────────────────────────────────────────

def get_breakdown_rows(target_rep_id: int) -> pd.DataFrame:
    return query_df(
        "SELECT * FROM v_target_breakdown_reporting WHERE target_rep_id = :id",
        {"id": target_rep_id},
    )


def get_breakdown_totals(target_rep_id: int) -> dict:
    row = query_df(
        """
        SELECT COALESCE(SUM(target_amount), 0) AS total_amount,
               COALESCE(SUM(target_visits), 0) AS total_visits
        FROM target_breakdown WHERE target_rep_id = :id
        """,
        {"id": target_rep_id},
    )
    return {"amount": float(row.iloc[0]["total_amount"]),
            "visits": int(row.iloc[0]["total_visits"])}


def check_duplicate_breakdown(target_rep_id: int, dims: dict) -> bool:
    row = query_scalar(
        """
        SELECT id FROM target_breakdown
        WHERE target_rep_id = :rep_id
          AND (customer_id          IS NOT DISTINCT FROM :customer_id)
          AND (business_unit_id     IS NOT DISTINCT FROM :bu_id)
          AND (product_category_id  IS NOT DISTINCT FROM :pc_id)
          AND (business_line_id     IS NOT DISTINCT FROM :bl_id)
          AND (article_id           IS NOT DISTINCT FROM :article_id)
        LIMIT 1
        """,
        {
            "rep_id":      target_rep_id,
            "customer_id": dims.get("customer_id"),
            "bu_id":       dims.get("business_unit_id"),
            "pc_id":       dims.get("product_category_id"),
            "bl_id":       dims.get("business_line_id"),
            "article_id":  dims.get("article_id"),
        },
    )
    return row is not None


def add_breakdown_row(row: dict, created_by: int) -> int:
    with engine.begin() as conn:
        result = conn.execute(
            text("""
                INSERT INTO target_breakdown (
                    target_rep_id, year, user_id, breakdown_level,
                    customer_id, business_unit_id, product_category_id,
                    business_line_id, article_id,
                    target_amount, target_visits, created_by, updated_by
                ) VALUES (
                    :target_rep_id, :year, :user_id, :breakdown_level,
                    :customer_id, :business_unit_id, :product_category_id,
                    :business_line_id, :article_id,
                    :target_amount, :target_visits, :cb, :cb
                ) RETURNING id
            """),
            {**row, "cb": created_by},
        )
        return int(result.scalar_one())


def delete_breakdown_row(breakdown_id: int) -> None:
    exec_sql("DELETE FROM target_breakdown WHERE id = :id", {"id": breakdown_id})


def get_contextual_gaps(target_rep_id: int) -> pd.DataFrame:
    """Return customer rows with sub-level sums less than the customer total — for gap warnings."""
    return query_df(
        """
        WITH customer_targets AS (
            SELECT customer_id, target_amount, target_visits
            FROM target_breakdown
            WHERE target_rep_id = :id AND breakdown_level = 'customer'
        ),
        sub_totals AS (
            SELECT customer_id,
                   SUM(target_amount) AS sub_amount,
                   SUM(target_visits) AS sub_visits
            FROM target_breakdown
            WHERE target_rep_id = :id AND breakdown_level != 'customer'
              AND customer_id IS NOT NULL
            GROUP BY customer_id
        )
        SELECT ct.customer_id, c.account_name,
               ct.target_amount, ct.target_visits,
               COALESCE(st.sub_amount, 0) AS sub_amount,
               COALESCE(st.sub_visits, 0) AS sub_visits,
               ct.target_amount - COALESCE(st.sub_amount, 0) AS gap_amount,
               ct.target_visits - COALESCE(st.sub_visits, 0) AS gap_visits
        FROM customer_targets ct
        JOIN customers c ON c.customer_id = ct.customer_id
        LEFT JOIN sub_totals st ON st.customer_id = ct.customer_id
        WHERE ct.target_amount - COALESCE(st.sub_amount, 0) > 0
        """,
        {"id": target_rep_id},
    )


# ── Lookup helpers for dropdowns ───────────────────────────────────────────────

def get_non_admin_users() -> pd.DataFrame:
    return query_df(
        """
        SELECT user_id, name, role, region
        FROM users
        WHERE role != 'admin' AND is_active = TRUE
        ORDER BY name
        """
    )


def get_customers() -> pd.DataFrame:
    return query_df(
        "SELECT customer_id, account_name FROM customers WHERE is_active = TRUE ORDER BY account_name"
    )


def get_business_units() -> pd.DataFrame:
    return query_df(
        "SELECT business_unit_id, name FROM business_units WHERE is_active = TRUE ORDER BY name"
    )


def get_product_categories(business_unit_id: Optional[int] = None) -> pd.DataFrame:
    if business_unit_id:
        return query_df(
            "SELECT product_category_id, name FROM product_categories WHERE business_unit_id = :buid AND is_active = TRUE ORDER BY name",
            {"buid": business_unit_id},
        )
    return query_df(
        "SELECT product_category_id, name FROM product_categories WHERE is_active = TRUE ORDER BY name"
    )


def get_business_lines(product_category_id: Optional[int] = None) -> pd.DataFrame:
    if product_category_id:
        return query_df(
            "SELECT business_line_id, name FROM business_lines WHERE product_category_id = :pcid AND is_active = TRUE ORDER BY name",
            {"pcid": product_category_id},
        )
    return query_df(
        "SELECT business_line_id, name FROM business_lines WHERE is_active = TRUE ORDER BY name"
    )


def get_articles(business_line_id: Optional[int] = None) -> pd.DataFrame:
    if business_line_id:
        return query_df(
            "SELECT product_id, article_number, description FROM items WHERE business_line_id = :blid AND is_active = TRUE ORDER BY article_number",
            {"blid": business_line_id},
        )
    return query_df(
        "SELECT product_id, article_number, description FROM items WHERE is_active = TRUE ORDER BY article_number"
    )
