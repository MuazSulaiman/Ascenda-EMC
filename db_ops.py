# db_ops.py
from datetime import datetime, timezone
from typing import List, Optional

import pandas as pd
from sqlalchemy import text

from db import engine


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def query_df(sql: str, params: Optional[dict] = None) -> pd.DataFrame:
    """Run a read query and return a DataFrame (PostgreSQL)."""
    with engine.begin() as conn:
        return pd.read_sql_query(text(sql), conn, params=params or {})


def exec_sql(sql: str, params: Optional[dict] = None):
    """Execute a write DDL/DML statement (PostgreSQL)."""
    with engine.begin() as conn:
        conn.execute(text(sql), params or {})


def insert_visit_returning_id(row: dict) -> int:
    """
    Insert a visit row into PostgreSQL and return the new visit_id via RETURNING.
    `row` is a dict of column -> value.
    """
    cols = list(row.keys())
    named = [f":{c}" for c in cols]
    sql = f"INSERT INTO visits ({', '.join(cols)}) VALUES ({', '.join(named)}) RETURNING visit_id"
    with engine.begin() as conn:
        vid = conn.execute(text(sql), row).scalar_one()
    return int(vid)


def insert_visit_atomic(
    visit_row: dict,
    home_visit: Optional[dict] = None,
    shelf_lines: Optional[List[dict]] = None,
) -> int:
    """
    Insert a visit and the optional related entities atomically (PostgreSQL).
    Returns the new visit_id. Rolls back everything on any failure.
    """
    visit_cols = list(visit_row.keys())
    visit_vals_named = [f":{c}" for c in visit_cols]
    sql_visit = f"""
        INSERT INTO visits ({', '.join(visit_cols)})
        VALUES ({', '.join(visit_vals_named)})
        RETURNING visit_id
    """

    with engine.begin() as conn:
        vid = conn.execute(text(sql_visit), visit_row).scalar_one()

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
