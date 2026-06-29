# db_ops.py
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pandas as pd
from sqlalchemy import text

from db import engine

VISIT_INSERT_COLUMNS = (
    "user_id", "customer_id", "audience_id", "business_line_id",
    "product_id", "objective_id", "notes", "evaluation",
    "latitude", "longitude", "accuracy_m",
    "submitted_at_utc", "submitted_at_local",
    "project_id", "other_customer_name", "other_audience_title",
    "other_audience_name", "other_audience_department", "other_audience_position",
    "other_audience_phone", "other_audience_email",
    "region", "business_unit_id", "is_other_customer",
    "visit_type",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def query_df(sql: str, params: Optional[dict] = None) -> pd.DataFrame:
    """Run a read query and return a DataFrame (PostgreSQL)."""
    with engine.begin() as conn:
        return pd.read_sql_query(text(sql), conn, params=params or {})


def query_scalar(sql: str, params: Optional[dict] = None):
    """Run a query and return the first scalar value (e.g. for COUNT queries)."""
    with engine.begin() as conn:
        row = conn.execute(text(sql), params or {}).fetchone()
        return row[0] if row is not None else None


def exec_sql(sql: str, params: Optional[dict] = None):
    """Execute a write DDL/DML statement (PostgreSQL)."""
    with engine.begin() as conn:
        conn.execute(text(sql), params or {})


def insert_visit_atomic(
    visit_row: dict,
    home_visit: Optional[dict] = None,
    shelf_lines: Optional[List[dict]] = None,
) -> int:
    """
    Insert a visit and the optional related entities atomically (PostgreSQL).
    Returns the new visit_id. Rolls back everything on any failure.
    """
    visit_cols = [c for c in VISIT_INSERT_COLUMNS if c in visit_row]
    visit_vals_named = [f":{c}" for c in visit_cols]
    sql_visit = f"""
        INSERT INTO visits ({', '.join(visit_cols)})
        VALUES ({', '.join(visit_vals_named)})
        RETURNING visit_id
    """

    with engine.begin() as conn:
        vid = conn.execute(text(sql_visit), {c: visit_row[c] for c in visit_cols}).scalar_one()

        if home_visit:
            conn.execute(
                text("""
                INSERT INTO home_visits(visit_id, patient_name, patient_phone, serial_no)
                VALUES (:visit_id, :patient_name, :patient_phone, :serial_no)
                """),
                {
                    "visit_id": vid,
                    "patient_name": home_visit["patient_name"].strip(),
                    "patient_phone": home_visit["patient_phone"].strip(),
                    "serial_no": home_visit["serial_no"].strip().upper(),
                },
            )

        if shelf_lines:
            movement_id = conn.execute(
                text("""
                    INSERT INTO shelf_movement_headers(visit_id)
                    VALUES (:visit_id)
                    RETURNING movement_id
                """),
                {"visit_id": vid},
            ).scalar_one()

            for ln in shelf_lines:
                pid = str(ln["product_id"])
                qty = float(ln["qty_checked"])
                if qty < 0:
                    continue
                conn.execute(
                    text("""
                        INSERT INTO shelf_movement_lines(movement_id, product_id, qty_checked)
                        VALUES (:movement_id, :product_id, :qty_checked)
                    """),
                    {"movement_id": movement_id, "product_id": pid, "qty_checked": qty},
                )

    return int(vid)


def insert_project(project_row: dict) -> int:
    """
    Inserts a new project and returns its project_id.
    """
    with engine.begin() as conn:
        result = conn.execute(
            text("""
                INSERT INTO projects (
                    name, description, assigned_by_id, assigned_to_id,
                    business_line_id, product_id, customer_id,
                    planned_start_date, planned_end_date, actual_end_date,
                    status, project_objective_id, created_at, updated_at
                )
                VALUES (
                    :name, :description, :assigned_by_id, :assigned_to_id,
                    :business_line_id, :product_id, :customer_id,
                    :planned_start_date, :planned_end_date, :actual_end_date,
                    :status, :project_objective_id, :created_at, :updated_at
                )
                RETURNING project_id
            """),
            project_row,
        )
        return int(result.scalar_one())


def recent_visit_minutes(uid: int, customer_id: int) -> Optional[int]:
    """
    Return minutes since the most recent visit by user for given customer, or None.
    """
    with engine.begin() as conn:
        r = conn.execute(
            text("""
                SELECT submitted_at_utc
                FROM visits
                WHERE user_id = :uid AND customer_id = :cid
                ORDER BY visit_id DESC
                LIMIT 1
            """),
            {"uid": uid, "cid": customer_id},
        ).fetchone()
    if not r:
        return None

    last = r[0]
    if isinstance(last, str):
        try:
            last = datetime.fromisoformat(last)
        except Exception:
            return None
    last = last if getattr(last, "tzinfo", None) else last.replace(tzinfo=timezone.utc)
    delta = _utcnow() - last
    return int(delta.total_seconds() // 60)


# ─────────────────────────────────────────────────────────────────────────────
# Analytics helpers
# ─────────────────────────────────────────────────────────────────────────────

def _analytics_scope(user_id: int, role: str, date_from, date_to, filters: dict, rep_ids=None):
    """Return (joins_sql, where_sql, params) for analytics queries.

    All analytics queries JOIN business_units, objectives, and users so that
    name-based cross-filters (region, business_unit, objective) work uniformly.
    """
    joins = """
        LEFT JOIN business_lines bl  ON bl.business_line_id  = v.business_line_id
        LEFT JOIN business_units bu  ON bu.business_unit_id  = bl.business_unit_id
        LEFT JOIN objectives     o   ON o.objective_id       = v.objective_id
        LEFT JOIN users          u   ON u.user_id            = v.user_id
        LEFT JOIN customers      c   ON c.customer_id        = v.customer_id
    """
    clauses = ["v.is_deleted IS NOT TRUE"]
    params: dict = {}

    # Role-based row scoping
    if role in ("rep", "maintenance"):
        clauses.append("v.user_id = :an_uid")
        params["an_uid"] = user_id
    elif rep_ids:
        clauses.append("v.user_id = ANY(:an_reps)")
        params["an_reps"] = list(rep_ids)

    # Date range
    if date_from:
        clauses.append("DATE(v.submitted_at_local) >= :an_from")
        params["an_from"] = str(date_from)
    if date_to:
        clauses.append("DATE(v.submitted_at_local) <= :an_to")
        params["an_to"] = str(date_to)

    # Cross-filters
    if filters.get("region"):
        clauses.append("c.region = :an_region")
        params["an_region"] = filters["region"]
    if filters.get("business_unit"):
        clauses.append("bu.name = :an_bu")
        params["an_bu"] = filters["business_unit"]
    if filters.get("objective"):
        obj_val = filters["objective"]
        if obj_val in ("(No Objective)", "(Uncategorised)"):
            clauses.append("o.name IS NULL")
        else:
            clauses.append("o.name = :an_obj")
            params["an_obj"] = obj_val
    if filters.get("dow") is not None:
        clauses.append("EXTRACT(DOW FROM v.submitted_at_local)::int = :an_dow")
        params["an_dow"] = int(filters["dow"])
    if filters.get("hour") is not None:
        clauses.append("EXTRACT(HOUR FROM v.submitted_at_local)::int = :an_hour")
        params["an_hour"] = int(filters["hour"])
    if filters.get("city"):
        clauses.append("c.city = :an_city")
        params["an_city"] = filters["city"]
    if filters.get("sector"):
        clauses.append("c.sector = :an_sector")
        params["an_sector"] = filters["sector"]

    where = "WHERE " + " AND ".join(clauses)
    return joins, where, params


def get_analytics_kpis(user_id: int, role: str, date_from, date_to, filters: dict, rep_ids=None) -> dict:
    joins, where, params = _analytics_scope(user_id, role, date_from, date_to, filters, rep_ids)

    row = query_df(f"""
        WITH base AS (
            SELECT
                v.customer_id,
                v.audience_id,
                v.business_line_id,
                v.submitted_at_local,
                EXTRACT(DOW FROM v.submitted_at_local)::int AS dow
            FROM visits v {joins} {where}
        ),
        monthly AS (
            SELECT
                DATE_TRUNC('month', submitted_at_local) AS month,
                COUNT(DISTINCT customer_id)             AS mc,
                COUNT(DISTINCT business_line_id)        AS mbl
            FROM base
            GROUP BY 1
        ),
        daily_wd AS (
            SELECT DATE(submitted_at_local) AS d, COUNT(DISTINCT customer_id) AS dc
            FROM base
            WHERE dow = ANY(ARRAY[0,1,2,3,4])
            GROUP BY 1
        )
        SELECT
            (SELECT COUNT(*)                        FROM base)     AS total_visits,
            (SELECT COUNT(DISTINCT customer_id)     FROM base)     AS total_customers,
            (SELECT COUNT(DISTINCT audience_id)     FROM base)     AS total_audiences,
            (SELECT AVG(mc)                         FROM monthly)  AS avg_customers_per_month,
            (SELECT AVG(mbl)                        FROM monthly)  AS avg_bl_per_month,
            (SELECT AVG(dc)                         FROM daily_wd) AS customers_per_day
    """, params)

    _zero = {
        "total_visits": 0, "total_customers": 0, "total_audiences": 0,
        "visits_per_customer": 0.0, "audiences_per_customer": 0.0,
        "avg_customers_per_month": 0.0, "avg_bl_per_month": 0.0,
        "customers_per_day": 0.0,
    }
    kpis = row.iloc[0].to_dict() if not row.empty else dict(_zero)
    tv = int(kpis.get("total_visits") or 0)
    tc = int(kpis.get("total_customers") or 0)
    ta = int(kpis.get("total_audiences") or 0)
    kpis["total_visits"]            = tv
    kpis["total_customers"]         = tc
    kpis["total_audiences"]         = ta
    kpis["visits_per_customer"]     = tv / tc if tc else 0.0
    kpis["audiences_per_customer"]  = ta / tc if tc else 0.0
    kpis["avg_customers_per_month"] = float(kpis.get("avg_customers_per_month") or 0)
    kpis["avg_bl_per_month"]        = float(kpis.get("avg_bl_per_month") or 0)
    kpis["customers_per_day"]       = float(kpis.get("customers_per_day") or 0)
    return kpis


def get_analytics_kpis_previous_period(user_id: int, role: str, date_from, date_to,
                                        filters: dict, rep_ids=None) -> dict:
    """Same KPIs as get_analytics_kpis() but for the preceding period of equal length."""
    delta     = date_to - date_from
    prev_to   = date_from - timedelta(days=1)
    prev_from = prev_to   - delta
    return get_analytics_kpis(user_id, role, prev_from, prev_to, filters, rep_ids)


def get_analytics_coverage_rate(user_id: int, role: str, date_from, date_to,
                                  filters: dict, rep_ids=None) -> dict:
    """Returns visited_customers, total_active_customers, coverage_pct.

    total_active is scoped to region/city/sector filters when present.
    """
    joins, where, params = _analytics_scope(user_id, role, date_from, date_to, filters, rep_ids)

    active_clauses = ["c.is_active IS TRUE"]
    if filters.get("region"):
        active_clauses.append("c.region = :an_region")
    if filters.get("city"):
        active_clauses.append("c.city = :an_city")
    if filters.get("sector"):
        active_clauses.append("c.sector = :an_sector")
    active_where = "WHERE " + " AND ".join(active_clauses)

    row = query_df(f"""
        SELECT
            COUNT(DISTINCT v.customer_id)                          AS visited,
            (SELECT COUNT(*) FROM customers c {active_where})      AS total_active
        FROM visits v {joins} {where}
    """, params)

    if row.empty:
        return {"visited": 0, "total_active": 0, "coverage_pct": 0.0}
    visited      = int(row.iloc[0]["visited"] or 0)
    total_active = int(row.iloc[0]["total_active"] or 0)
    pct          = round(visited / total_active * 100, 1) if total_active else 0.0
    return {"visited": visited, "total_active": total_active, "coverage_pct": pct}


def get_analytics_new_vs_repeat(user_id: int, role: str, date_from, date_to,
                                  filters: dict, rep_ids=None) -> dict:
    """Count visits in period as 'New' (first-ever visit to a customer) vs 'Repeat'."""
    joins, where, params = _analytics_scope(user_id, role, date_from, date_to, filters, rep_ids)
    row = query_df(f"""
        WITH first_visits AS (
            SELECT user_id, customer_id, MIN(submitted_at_local) AS first_at
            FROM visits
            WHERE is_deleted IS NOT TRUE AND customer_id IS NOT NULL
            GROUP BY user_id, customer_id
        ),
        in_period AS (
            SELECT v.visit_id, v.submitted_at_local, fv.first_at
            FROM visits v {joins}
            LEFT JOIN first_visits fv
                ON fv.user_id = v.user_id AND fv.customer_id = v.customer_id
            {where}
        )
        SELECT
            SUM(CASE WHEN DATE(submitted_at_local) = DATE(first_at) THEN 1 ELSE 0 END) AS new_visits,
            SUM(CASE WHEN DATE(submitted_at_local) > DATE(first_at)  THEN 1 ELSE 0 END) AS repeat_visits
        FROM in_period
    """, params)
    if row.empty:
        return {"new_visits": 0, "repeat_visits": 0}
    return {
        "new_visits":    int(row.iloc[0]["new_visits"] or 0),
        "repeat_visits": int(row.iloc[0]["repeat_visits"] or 0),
    }


def get_analytics_target_vs_actual(year: int, rep_ids=None) -> pd.DataFrame:
    """Returns (rep, target_visits, actual_visits) for the given year.

    Joins target_rep → target_breakdown (SUM of target_visits) with actual
    visits for that year. Returns empty DataFrame if no target data exists.
    """
    params: dict = {"year": year}
    rep_clause = ""
    if rep_ids:
        rep_clause = "AND tr.user_id = ANY(:rids)"
        params["rids"] = list(rep_ids)

    return query_df(f"""
        SELECT
            u.name                                       AS rep,
            COALESCE(SUM(tb.target_visits), 0)           AS target_visits,
            COUNT(DISTINCT v.visit_id)                   AS actual_visits
        FROM target_rep tr
        JOIN users u ON u.user_id = tr.user_id
        LEFT JOIN target_breakdown tb
            ON tb.target_rep_id = tr.id
        LEFT JOIN visits v
            ON v.user_id = tr.user_id
           AND EXTRACT(YEAR FROM v.submitted_at_local) = tr.year
           AND v.is_deleted IS NOT TRUE
        WHERE tr.year = :year {rep_clause}
        GROUP BY u.name
        ORDER BY target_visits DESC
    """, params)


def get_analytics_customer_health(user_id: int, role: str, rep_ids=None) -> pd.DataFrame:
    """Returns active customers with days since last visit.

    Columns: customer_name, region, city, rep, last_visit_date, days_since_visit.
    No date-range filter — always uses lifetime data.
    Customers never visited show last_visit_date = None, days_since_visit = None.
    """
    clauses = ["c.is_active IS TRUE"]
    params: dict = {}

    if role in ("rep", "maintenance"):
        clauses.append("(lv.user_id = :chuid OR lv.user_id IS NULL)")
        params["chuid"] = user_id
    elif rep_ids:
        clauses.append("(lv.user_id = ANY(:chreps) OR lv.user_id IS NULL)")
        params["chreps"] = list(rep_ids)

    where = "WHERE " + " AND ".join(clauses)

    return query_df(f"""
        WITH last_v AS (
            SELECT
                v.customer_id,
                v.user_id,
                u.name                               AS rep,
                MAX(DATE(v.submitted_at_local))      AS last_visit_date
            FROM visits v
            LEFT JOIN users u ON u.user_id = v.user_id
            WHERE v.is_deleted IS NOT TRUE AND v.customer_id IS NOT NULL
            GROUP BY v.customer_id, v.user_id, u.name
        )
        SELECT
            COALESCE(c.account_name, '(Unknown)')  AS customer_name,
            c.region,
            c.city,
            lv.rep,
            lv.last_visit_date,
            CURRENT_DATE - lv.last_visit_date      AS days_since_visit
        FROM customers c
        LEFT JOIN last_v lv ON lv.customer_id = c.customer_id
        {where}
        ORDER BY days_since_visit DESC NULLS FIRST
        LIMIT 500
    """, params)


def get_analytics_time_series(user_id: int, role: str, date_from, date_to,
                               granularity: str, filters: dict, rep_ids=None) -> pd.DataFrame:
    joins, where, params = _analytics_scope(user_id, role, date_from, date_to, filters, rep_ids)
    # Use ISO-sortable string formats so ORDER BY period works alphabetically
    if granularity == "Year":
        period_expr = "EXTRACT(YEAR FROM v.submitted_at_local)::int::text"
    elif granularity == "Week":
        period_expr = (
            "TO_CHAR(v.submitted_at_local, 'IYYY') || '-W' || "
            "LPAD(TO_CHAR(v.submitted_at_local, 'IW'), 2, '0')"
        )
    else:  # Month — YYYY-MM sorts correctly and is reformatted in the UI
        period_expr = "TO_CHAR(v.submitted_at_local, 'YYYY-MM')"

    return query_df(f"""
        SELECT {period_expr} AS period, COUNT(*) AS visit_count
        FROM visits v {joins} {where}
        GROUP BY period
        ORDER BY period
    """, params)


def get_analytics_breakdowns(user_id: int, role: str, date_from, date_to,
                              filters: dict, rep_ids=None) -> dict:
    joins, where, params = _analytics_scope(user_id, role, date_from, date_to, filters, rep_ids)
    region_df = query_df(f"""
        SELECT u.region, COUNT(*) AS count
        FROM visits v {joins} {where} AND u.region IS NOT NULL
        GROUP BY u.region ORDER BY count DESC
    """, params)

    bu_df = query_df(f"""
        SELECT COALESCE(bu.name, '(Blank)') AS business_unit, COUNT(*) AS count
        FROM visits v {joins} {where}
        GROUP BY bu.name ORDER BY count DESC
    """, params)

    obj_df = query_df(f"""
        SELECT COALESCE(o.name, '(No Objective)') AS objective, COUNT(*) AS count
        FROM visits v {joins} {where}
        GROUP BY o.name ORDER BY count DESC
    """, params)

    return {"region": region_df, "business_unit": bu_df, "objective": obj_df}


def get_analytics_drilldown(user_id: int, role: str, date_from, date_to,
                              filters: dict, rep_ids=None) -> pd.DataFrame:
    """Flat table for building both treemaps.

    Columns: region, city, sector, customer_name,
             business_unit, product_category, rep, visit_count.
    """
    joins, where, params = _analytics_scope(user_id, role, date_from, date_to, filters, rep_ids)
    extra = "LEFT JOIN product_categories pc ON pc.product_category_id = bl.product_category_id"
    return query_df(f"""
        SELECT
            COALESCE(c.region,            '(No Region)')   AS region,
            COALESCE(c.city,              '(No City)')     AS city,
            COALESCE(c.sector,            '(No Sector)')   AS sector,
            COALESCE(c.account_name, v.other_customer_name, '(Unknown)') AS customer_name,
            COALESCE(bu.name,             '(No BU)')       AS business_unit,
            COALESCE(pc.name,             '(No Category)') AS product_category,
            COALESCE(u.name,              '(Unknown Rep)') AS rep,
            COUNT(*)                                        AS visit_count
        FROM visits v {joins} {extra} {where}
        GROUP BY c.region, c.city, c.sector, c.account_name, v.other_customer_name,
                 bu.name, pc.name, u.name
        ORDER BY visit_count DESC
    """, params)


def get_analytics_objective_categories(user_id: int, role: str, date_from, date_to,
                                        filters: dict, rep_ids=None) -> pd.DataFrame:
    """Returns (objective_category, objective_name, count) for the grouped objective bar chart."""
    joins, where, params = _analytics_scope(user_id, role, date_from, date_to, filters, rep_ids)
    return query_df(f"""
        SELECT
            COALESCE(o.category, 'Uncategorised') AS objective_category,
            COALESCE(o.name,     '(No Objective)') AS objective_name,
            COUNT(*) AS count
        FROM visits v {joins} {where}
        GROUP BY o.category, o.name
        ORDER BY count DESC
    """, params)


def get_analytics_kpis_per_rep(user_id: int, role: str, date_from, date_to,
                                filters: dict, rep_ids=None) -> dict:
    joins, where, params = _analytics_scope(user_id, role, date_from, date_to, filters, rep_ids)

    audience_df = query_df(f"""
        SELECT u.name AS rep, COUNT(DISTINCT v.audience_id) AS count
        FROM visits v {joins} {where} AND u.name IS NOT NULL
        GROUP BY u.name ORDER BY count DESC
    """, params)

    aud_per_cust_df = query_df(f"""
        SELECT u.name AS rep,
               COUNT(DISTINCT v.audience_id)::float /
               NULLIF(COUNT(DISTINCT v.customer_id), 0) AS ratio
        FROM visits v {joins} {where} AND u.name IS NOT NULL
        GROUP BY u.name ORDER BY ratio DESC
    """, params)

    avg_cust_month_df = query_df(f"""
        WITH monthly AS (
            SELECT u.name AS rep,
                   DATE_TRUNC('month', v.submitted_at_local) AS month,
                   COUNT(DISTINCT v.customer_id) AS mc
            FROM visits v {joins} {where} AND u.name IS NOT NULL
            GROUP BY rep, month
        )
        SELECT rep, AVG(mc) AS avg_monthly
        FROM monthly GROUP BY rep ORDER BY avg_monthly DESC
    """, params)

    region_df = query_df(f"""
        SELECT u.region, COUNT(*) AS count
        FROM visits v {joins} {where} AND u.region IS NOT NULL
        GROUP BY u.region ORDER BY count DESC
    """, params)

    return {
        "audience_count": audience_df,
        "audience_per_customer": aud_per_cust_df,
        "avg_customers_per_month": avg_cust_month_df,
        "region": region_df,
    }


def get_analytics_visits_per_rep(user_id: int, role: str, date_from, date_to,
                                  filters: dict, rep_ids=None) -> pd.DataFrame:
    """Returns (rep, total_visits, total_customers) for the leaderboard."""
    joins, where, params = _analytics_scope(user_id, role, date_from, date_to, filters, rep_ids)
    return query_df(f"""
        SELECT
            u.name                        AS rep,
            COUNT(*)                      AS total_visits,
            COUNT(DISTINCT v.customer_id) AS total_customers
        FROM visits v {joins} {where} AND u.name IS NOT NULL
        GROUP BY u.name
        ORDER BY total_visits DESC
    """, params)


def get_analytics_visits_detail(user_id: int, role: str, date_from, date_to,
                                 filters: dict, rep_ids=None) -> pd.DataFrame:
    joins, where, params = _analytics_scope(user_id, role, date_from, date_to, filters, rep_ids)
    return query_df(f"""
        SELECT
            v.visit_id                                        AS "Visit #",
            v.submitted_at_local                              AS "Date Local",
            u.name                                            AS "Frontline Name",
            COALESCE(c.account_name, v.other_customer_name)  AS "Customer Name",
            v.audience_id                                     AS "Audience ID",
            COALESCE(
                (SELECT ta.department FROM target_audiences ta WHERE ta.audience_id = v.audience_id),
                v.other_audience_department
            )                                                 AS "Department",
            COALESCE(
                (SELECT ta.position FROM target_audiences ta WHERE ta.audience_id = v.audience_id),
                v.other_audience_position
            )                                                 AS "Position"
        FROM visits v {joins} {where}
        ORDER BY v.visit_id DESC
        LIMIT 1000
    """, params)


def get_analytics_time_map(user_id: int, role: str, date_from, date_to,
                            filters: dict, rep_ids=None) -> pd.DataFrame:
    joins, where, params = _analytics_scope(user_id, role, date_from, date_to, filters, rep_ids)
    return query_df(f"""
        SELECT
            EXTRACT(DOW  FROM v.submitted_at_local)::int  AS dow,
            EXTRACT(HOUR FROM v.submitted_at_local)::int  AS hour,
            COALESCE(bu.name, '(Blank)')                  AS business_unit,
            COUNT(*)                                       AS visit_count
        FROM visits v {joins} {where}
        GROUP BY dow, hour, bu.name
        ORDER BY dow, hour
    """, params)


def get_analytics_today(user_id: int, role: str, today_date, rep_ids=None) -> pd.DataFrame:
    """Today's visit counts by rep (no cross-filters applied)."""
    clauses = ["v.is_deleted IS NOT TRUE", "DATE(v.submitted_at_local) = :td_date"]
    params: dict = {"td_date": str(today_date)}
    if role in ("rep", "maintenance"):
        clauses.append("v.user_id = :td_uid")
        params["td_uid"] = user_id
    elif rep_ids:
        clauses.append("v.user_id = ANY(:td_reps)")
        params["td_reps"] = list(rep_ids)
    where = "WHERE " + " AND ".join(clauses)
    return query_df(f"""
        SELECT u.name AS "Frontline Name", COUNT(*) AS "Visits"
        FROM visits v
        LEFT JOIN users u ON u.user_id = v.user_id
        {where}
        GROUP BY u.name ORDER BY "Visits" DESC
    """, params)


def get_analytics_attendance(user_id: int, role: str, date_from, date_to,
                               rep_ids=None) -> pd.DataFrame:
    """Returns (date, rep_name, visit_count) for the attendance pivot calendar.

    No cross-filters applied — date range + role scoping only.
    """
    clauses = [
        "v.is_deleted IS NOT TRUE",
        "DATE(v.submitted_at_local) >= :att_from",
        "DATE(v.submitted_at_local) <= :att_to",
    ]
    params: dict = {"att_from": str(date_from), "att_to": str(date_to)}

    if role in ("rep", "maintenance"):
        clauses.append("v.user_id = :att_uid")
        params["att_uid"] = user_id
    elif rep_ids:
        clauses.append("v.user_id = ANY(:att_reps)")
        params["att_reps"] = list(rep_ids)

    where = "WHERE " + " AND ".join(clauses)
    return query_df(f"""
        SELECT
            DATE(v.submitted_at_local)     AS date,
            COALESCE(u.name, '(Unknown)')  AS rep_name,
            COUNT(*)                       AS visit_count
        FROM visits v
        LEFT JOIN users u ON u.user_id = v.user_id
        {where}
        GROUP BY DATE(v.submitted_at_local), u.name
        ORDER BY date, rep_name
    """, params)


def get_customer_locations_for_map() -> pd.DataFrame:
    return query_df("""
        SELECT account_name, latitude, longitude, region, city
        FROM customers
        WHERE is_active IS TRUE
          AND latitude  IS NOT NULL
          AND longitude IS NOT NULL
    """)


def get_visit_locations_for_map(user_id: int, role: str, date_from, date_to,
                                 filters: dict, rep_ids=None) -> pd.DataFrame:
    joins, where, params = _analytics_scope(user_id, role, date_from, date_to, filters, rep_ids)
    return query_df(f"""
        SELECT v.latitude, v.longitude,
               COALESCE(c.account_name, v.other_customer_name) AS customer,
               u.name AS rep,
               v.submitted_at_local AS visit_time
        FROM visits v {joins} {where}
          AND v.latitude IS NOT NULL AND v.longitude IS NOT NULL
        LIMIT 2000
    """, params)


def get_all_reps() -> pd.DataFrame:
    return query_df("""
        SELECT user_id, name FROM users
        WHERE role IN ('rep', 'maintenance', 'sales manager', 'biomedical manager')
          AND is_active IS TRUE
        ORDER BY name
    """)
