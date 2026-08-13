# app_pages/quotation_helpers.py
import base64
import os
import tempfile
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import quote_plus

from datetime import datetime, timezone

import fitz  # PyMuPDF
import pandas as pd
import streamlit as st
from num2words import num2words
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from db import engine
from db_ops import query_df, query_scalar, exec_sql
from app_pages.change_request_helpers import _norm, _sql_val
from app_pages.notification_helpers import notify_role, notify_users
from ui import compare_row, status_badge, visit_card
from utils import to_local, to_local_str


VALIDITY_OPTIONS = [30, 60, 90, 180, 365]
DELIVERY_OPTIONS = ["Immediate", "15 Days", "30 Days", "45 Days", "60 Days", "90 Days", "120 Days"]
PAYMENT_TERMS_OPTIONS = [
    "Cash in Advance", "Cash on Delivery", "15 Days", "30 Days",
    "60 Days", "90 Days", "120 Days", "As per Agreed Policy",
]


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


def fmt_money(value) -> str:
    """Comma-grouped 2dp display string, e.g. Decimal('50000') -> '50,000.00'.
    Display-only — never use this for stored or computed amounts."""
    return f"{Decimal(value):,.2f}"


# ─────────────────────────────────────────────────────────────────────────────
# Authorization / duplicate-reference guards
# ─────────────────────────────────────────────────────────────────────────────

def _require_role(actor_uid: int, allowed_roles: set[str]) -> str:
    """Re-query the actor's current role from the DB. Raises ValueError if not allowed."""
    role = query_scalar("SELECT role FROM users WHERE user_id = :uid", {"uid": actor_uid})
    role = _norm(role)
    if role not in allowed_roles:
        raise ValueError(f"Role '{role}' is not permitted to perform this action.")
    return role


def _odoo_reference_exists(ref: str) -> bool:
    ref = _norm(ref)
    if not ref:
        return False
    row = query_scalar(
        "SELECT 1 FROM quotation_requests WHERE odoo_reference = :ref", {"ref": ref}
    )
    return row is not None


# ─────────────────────────────────────────────────────────────────────────────
# Atomic quotation-number assignment
# ─────────────────────────────────────────────────────────────────────────────

def _next_quotation_number(conn, year: int) -> str:
    result = conn.execute(
        text("""
            INSERT INTO quotation_number_counters (year, last_seq) VALUES (:yr, 1)
            ON CONFLICT (year) DO UPDATE SET last_seq = quotation_number_counters.last_seq + 1
            RETURNING last_seq
        """),
        {"yr": year},
    )
    seq = result.scalar_one()
    return f"QT-{year}-{seq:06d}"


# ─────────────────────────────────────────────────────────────────────────────
# Read helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_quotation_header(quotation_id: int) -> dict | None:
    df = query_df("SELECT * FROM quotation_requests WHERE quotation_id = :id", {"id": quotation_id})
    return df.iloc[0].to_dict() if not df.empty else None


def _load_quotation_lines(quotation_id: int) -> pd.DataFrame:
    return query_df(
        "SELECT * FROM quotation_lines WHERE quotation_id = :id ORDER BY line_no",
        {"id": quotation_id},
    )


def _load_status_events(quotation_id: int) -> pd.DataFrame:
    return query_df(
        "SELECT * FROM quotation_status_events WHERE quotation_id = :id ORDER BY at_utc",
        {"id": quotation_id},
    )


def _load_revisions(quotation_id: int) -> pd.DataFrame:
    return query_df(
        "SELECT * FROM quotation_revisions WHERE quotation_id = :id ORDER BY revision_no",
        {"id": quotation_id},
    )


def _load_revision_lines(revision_id: int) -> pd.DataFrame:
    return query_df(
        "SELECT * FROM quotation_revision_lines WHERE revision_id = :id ORDER BY line_no",
        {"id": revision_id},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Guarded state-transition functions
#
# Idiom matched from app_pages/admin_change_requests.py::_apply_changes /
# _reject_request: a single `engine.begin()` block performs one guarded
# `UPDATE ... WHERE quotation_id = :qid AND status = '<expected>' [...]`.
# Where the transaction needs to fail loudly on a stale/foreign row, we raise
# ValueError *inside* the `with engine.begin()` block (which rolls the whole
# transaction back) and catch it in an outer try/except that converts it to
# `(False, str(e))`, exactly like `_apply_changes` does. Manager/coordinator
# functions call `_require_role(...)` as the very first line, before the
# transaction opens, and let a role failure propagate as a raised ValueError
# (defense-in-depth below the page-level role gate — this is not expected to
# be hit through the normal UI, so it is not converted to a soft return).
# ─────────────────────────────────────────────────────────────────────────────

def submit_quotation(header: dict, lines: list[dict], actor_uid: int) -> tuple[int, str]:
    """
    Insert a brand-new quotation: header (status='IN_REVIEW', version=0),
    lines, revision #1 (+ revision lines), and a SUBMITTED status event, all
    inside one atomic transaction. Pure insert — no status guard needed.
    Returns (quotation_id, quotation_number).
    """
    quotation_date = header.get("quotation_date")
    if isinstance(quotation_date, str):
        year = datetime.fromisoformat(quotation_date).year
    elif quotation_date is not None and hasattr(quotation_date, "year"):
        year = quotation_date.year
    else:
        year = datetime.now(timezone.utc).year

    vat_rate = Decimal(str(header.get("vat_rate")))
    totals = compute_header_totals(lines, vat_rate)

    with engine.begin() as conn:
        quotation_number = _next_quotation_number(conn, year)

        quotation_id = conn.execute(
            text("""
                INSERT INTO quotation_requests
                    (quotation_number, customer_id, rep_user_id, quotation_date,
                     vat_rate, remarks, customer_reference, validity_days, delivery_terms, payment_terms,
                     status, version, submitted_by)
                VALUES
                    (:quotation_number, :customer_id, :actor_uid, :quotation_date,
                     :vat_rate, :remarks, :customer_reference, :validity_days, :delivery_terms, :payment_terms,
                     'IN_REVIEW', 0, :actor_uid)
                RETURNING quotation_id
            """),
            {
                "quotation_number": quotation_number,
                "customer_id": header.get("customer_id"),
                "actor_uid": actor_uid,
                "quotation_date": header.get("quotation_date"),
                "vat_rate": vat_rate,
                "remarks": _norm(header.get("remarks")) or None,
                "customer_reference": _norm(header.get("customer_reference")) or None,
                "validity_days": header.get("validity_days"),
                "delivery_terms": header.get("delivery_terms"),
                "payment_terms": header.get("payment_terms"),
            },
        ).scalar_one()

        for i, line in enumerate(lines, start=1):
            conn.execute(
                text("""
                    INSERT INTO quotation_lines
                        (quotation_id, line_no, product_id, quantity, unit_price, discount_pct)
                    VALUES (:qid, :line_no, :product_id, :quantity, :unit_price, :discount_pct)
                """),
                {
                    "qid": quotation_id,
                    "line_no": i,
                    "product_id": line["product_id"],
                    "quantity": Decimal(str(line["quantity"])),
                    "unit_price": Decimal(str(line["unit_price"])),
                    "discount_pct": Decimal(str(line.get("discount_pct") or 0)),
                },
            )

        revision_id = conn.execute(
            text("""
                INSERT INTO quotation_revisions
                    (quotation_id, revision_no, created_by, customer_id, quotation_date,
                     vat_rate, remarks, customer_reference, validity_days, delivery_terms, payment_terms,
                     subtotal, vat_amount, grand_total)
                VALUES
                    (:qid, 1, :actor_uid, :customer_id, :quotation_date,
                     :vat_rate, :remarks, :customer_reference, :validity_days, :delivery_terms, :payment_terms,
                     :subtotal, :vat_amount, :grand_total)
                RETURNING revision_id
            """),
            {
                "qid": quotation_id,
                "actor_uid": actor_uid,
                "customer_id": header.get("customer_id"),
                "quotation_date": header.get("quotation_date"),
                "vat_rate": vat_rate,
                "remarks": _norm(header.get("remarks")) or None,
                "customer_reference": _norm(header.get("customer_reference")) or None,
                "validity_days": header.get("validity_days"),
                "delivery_terms": header.get("delivery_terms"),
                "payment_terms": header.get("payment_terms"),
                "subtotal": totals["subtotal"],
                "vat_amount": totals["vat_amount"],
                "grand_total": totals["grand_total"],
            },
        ).scalar_one()

        for i, line in enumerate(lines, start=1):
            item_row = conn.execute(
                text("SELECT article_number, description FROM items WHERE product_id = :pid"),
                {"pid": line["product_id"]},
            ).mappings().one_or_none()
            quantity = Decimal(str(line["quantity"]))
            unit_price = Decimal(str(line["unit_price"]))
            discount_pct = Decimal(str(line.get("discount_pct") or 0))
            conn.execute(
                text("""
                    INSERT INTO quotation_revision_lines
                        (revision_id, line_no, product_id, article_number_snapshot, description_snapshot,
                         quantity, unit_price, discount_pct, line_total)
                    VALUES
                        (:rid, :line_no, :product_id, :article_number, :description,
                         :quantity, :unit_price, :discount_pct, :line_total)
                """),
                {
                    "rid": revision_id,
                    "line_no": i,
                    "product_id": line["product_id"],
                    "article_number": item_row["article_number"] if item_row else None,
                    "description": item_row["description"] if item_row else None,
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "discount_pct": discount_pct,
                    "line_total": compute_line_total(quantity, unit_price, discount_pct),
                },
            )

        conn.execute(
            text("""
                INSERT INTO quotation_status_events
                    (quotation_id, event_type, actor_user_id, from_status, to_status, revision_id)
                VALUES (:qid, 'SUBMITTED', :actor_uid, NULL, 'IN_REVIEW', :rid)
            """),
            {"qid": quotation_id, "actor_uid": actor_uid, "rid": revision_id},
        )

        notify_role(
            conn, ["sales manager", "admin"], exclude_user_id=actor_uid,
            category="quotation", event_type="SUBMITTED",
            title=f"New quotation {quotation_number} awaiting review",
            link_page="Review Quotations", link_params={"quotation_id": quotation_id},
            actor_user_id=actor_uid,
        )

    return int(quotation_id), quotation_number


def resubmit_quotation(
    quotation_id: int, header: dict, lines: list[dict], actor_uid: int, expected_version: int
) -> tuple[bool, str | None]:
    """
    Rep resubmits an EDIT_REQUESTED quotation. Single guarded UPDATE combines
    the version bump, status guard, and rep-ownership guard in one WHERE
    clause — the only mechanism that decides whether this call wins the race.
    Everything (line replace, header update, new revision snapshot, event)
    happens in the same transaction; any failure rolls all of it back.
    """
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text("""
                    UPDATE quotation_requests
                    SET version = version + 1, status = 'IN_REVIEW'
                    WHERE quotation_id = :qid AND status = 'EDIT_REQUESTED'
                      AND version = :expected_version AND rep_user_id = :actor_uid
                    RETURNING version, quotation_number
                """),
                {"qid": quotation_id, "expected_version": expected_version, "actor_uid": actor_uid},
            )
            row = result.fetchone()
            if row is None:
                raise ValueError("This quotation was already changed elsewhere. Refresh and try again.")
            new_version = row[0]
            quotation_number = row[1]

            conn.execute(text("DELETE FROM quotation_lines WHERE quotation_id = :qid"), {"qid": quotation_id})

            for i, line in enumerate(lines, start=1):
                conn.execute(
                    text("""
                        INSERT INTO quotation_lines
                            (quotation_id, line_no, product_id, quantity, unit_price, discount_pct)
                        VALUES (:qid, :line_no, :product_id, :quantity, :unit_price, :discount_pct)
                    """),
                    {
                        "qid": quotation_id,
                        "line_no": i,
                        "product_id": line["product_id"],
                        "quantity": Decimal(str(line["quantity"])),
                        "unit_price": Decimal(str(line["unit_price"])),
                        "discount_pct": Decimal(str(line.get("discount_pct") or 0)),
                    },
                )

            vat_rate = Decimal(str(header.get("vat_rate")))
            conn.execute(
                text("""
                    UPDATE quotation_requests
                    SET customer_id = :customer_id,
                        quotation_date = :quotation_date, vat_rate = :vat_rate, remarks = :remarks,
                        customer_reference = :customer_reference,
                        validity_days = :validity_days, delivery_terms = :delivery_terms,
                        payment_terms = :payment_terms
                    WHERE quotation_id = :qid
                """),
                {
                    "qid": quotation_id,
                    "customer_id": header.get("customer_id"),
                    "quotation_date": header.get("quotation_date"),
                    "vat_rate": vat_rate,
                    "remarks": _norm(header.get("remarks")) or None,
                    "customer_reference": _norm(header.get("customer_reference")) or None,
                    "validity_days": header.get("validity_days"),
                    "delivery_terms": header.get("delivery_terms"),
                    "payment_terms": header.get("payment_terms"),
                },
            )

            totals = compute_header_totals(lines, vat_rate)
            revision_no = new_version + 1
            revision_id = conn.execute(
                text("""
                    INSERT INTO quotation_revisions
                        (quotation_id, revision_no, created_by, customer_id, quotation_date,
                         vat_rate, remarks, customer_reference, validity_days, delivery_terms, payment_terms,
                         subtotal, vat_amount, grand_total)
                    VALUES
                        (:qid, :revision_no, :actor_uid, :customer_id, :quotation_date,
                         :vat_rate, :remarks, :customer_reference, :validity_days, :delivery_terms, :payment_terms,
                         :subtotal, :vat_amount, :grand_total)
                    RETURNING revision_id
                """),
                {
                    "qid": quotation_id,
                    "revision_no": revision_no,
                    "actor_uid": actor_uid,
                    "customer_id": header.get("customer_id"),
                    "quotation_date": header.get("quotation_date"),
                    "vat_rate": vat_rate,
                    "remarks": _norm(header.get("remarks")) or None,
                    "customer_reference": _norm(header.get("customer_reference")) or None,
                    "validity_days": header.get("validity_days"),
                    "delivery_terms": header.get("delivery_terms"),
                    "payment_terms": header.get("payment_terms"),
                    "subtotal": totals["subtotal"],
                    "vat_amount": totals["vat_amount"],
                    "grand_total": totals["grand_total"],
                },
            ).scalar_one()

            for i, line in enumerate(lines, start=1):
                item_row = conn.execute(
                    text("SELECT article_number, description FROM items WHERE product_id = :pid"),
                    {"pid": line["product_id"]},
                ).mappings().one_or_none()
                quantity = Decimal(str(line["quantity"]))
                unit_price = Decimal(str(line["unit_price"]))
                discount_pct = Decimal(str(line.get("discount_pct") or 0))
                conn.execute(
                    text("""
                        INSERT INTO quotation_revision_lines
                            (revision_id, line_no, product_id, article_number_snapshot, description_snapshot,
                             quantity, unit_price, discount_pct, line_total)
                        VALUES
                            (:rid, :line_no, :product_id, :article_number, :description,
                             :quantity, :unit_price, :discount_pct, :line_total)
                    """),
                    {
                        "rid": revision_id,
                        "line_no": i,
                        "product_id": line["product_id"],
                        "article_number": item_row["article_number"] if item_row else None,
                        "description": item_row["description"] if item_row else None,
                        "quantity": quantity,
                        "unit_price": unit_price,
                        "discount_pct": discount_pct,
                        "line_total": compute_line_total(quantity, unit_price, discount_pct),
                    },
                )

            conn.execute(
                text("""
                    INSERT INTO quotation_status_events
                        (quotation_id, event_type, actor_user_id, from_status, to_status, revision_id)
                    VALUES (:qid, 'RESUBMITTED', :actor_uid, 'EDIT_REQUESTED', 'IN_REVIEW', :rid)
                """),
                {"qid": quotation_id, "actor_uid": actor_uid, "rid": revision_id},
            )

            notify_role(
                conn, ["sales manager", "admin"], exclude_user_id=actor_uid,
                category="quotation", event_type="RESUBMITTED",
                title=f"Quotation {quotation_number} resubmitted for review",
                link_page="Review Quotations", link_params={"quotation_id": quotation_id},
                actor_user_id=actor_uid,
            )
        return True, None
    except Exception as e:
        return False, str(e)


def manager_approve(quotation_id: int, manager_uid: int) -> tuple[bool, str | None]:
    role = _require_role(manager_uid, {"sales manager", "admin"})
    self_review_clause = "" if role == "admin" else "AND rep_user_id != :actor_uid"
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text(f"""
                    UPDATE quotation_requests
                    SET status = 'APPROVED', manager_user_id = :actor_uid, manager_decided_at = NOW()
                    WHERE quotation_id = :qid AND status = 'IN_REVIEW' {self_review_clause}
                    RETURNING quotation_id, rep_user_id, quotation_number
                """),
                {"qid": quotation_id, "actor_uid": manager_uid},
            )
            row = result.fetchone()
            if row is None:
                raise ValueError(
                    "This quotation was already resolved elsewhere, or you cannot review your "
                    "own submission. Refresh and try again."
                )
            rep_user_id, quotation_number = row[1], row[2]
            conn.execute(
                text("""
                    INSERT INTO quotation_status_events
                        (quotation_id, event_type, actor_user_id, from_status, to_status, revision_id)
                    VALUES (:qid, 'APPROVED', :actor_uid, 'IN_REVIEW', 'APPROVED', NULL)
                """),
                {"qid": quotation_id, "actor_uid": manager_uid},
            )

            notify_users(
                conn, [rep_user_id], category="quotation", event_type="APPROVED",
                title=f"Your quotation {quotation_number} was approved",
                link_page="Quotations", link_params={"quotation_id": quotation_id},
                actor_user_id=manager_uid,
            )
            notify_role(
                conn, ["sales coordinator", "admin"], category="quotation", event_type="APPROVED",
                title=f"Quotation {quotation_number} approved, ready for handoff",
                link_page="Quotation Handoff (Odoo)", link_params={"quotation_id": quotation_id},
                actor_user_id=manager_uid,
            )
        return True, None
    except Exception as e:
        return False, str(e)


def manager_reject(quotation_id: int, manager_uid: int, reason: str) -> tuple[bool, str | None]:
    role = _require_role(manager_uid, {"sales manager", "admin"})
    if not (reason or "").strip():
        return False, "A rejection reason is required."
    self_review_clause = "" if role == "admin" else "AND rep_user_id != :actor_uid"
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text(f"""
                    UPDATE quotation_requests
                    SET status = 'REJECTED', manager_user_id = :actor_uid, manager_decided_at = NOW(),
                        manager_comment = :comment
                    WHERE quotation_id = :qid AND status = 'IN_REVIEW' {self_review_clause}
                    RETURNING quotation_id, rep_user_id, quotation_number
                """),
                {"qid": quotation_id, "actor_uid": manager_uid, "comment": reason.strip()},
            )
            row = result.fetchone()
            if row is None:
                raise ValueError(
                    "This quotation was already resolved elsewhere, or you cannot review your "
                    "own submission. Refresh and try again."
                )
            rep_user_id, quotation_number = row[1], row[2]
            conn.execute(
                text("""
                    INSERT INTO quotation_status_events
                        (quotation_id, event_type, actor_user_id, from_status, to_status, comment, revision_id)
                    VALUES (:qid, 'REJECTED', :actor_uid, 'IN_REVIEW', 'REJECTED', :comment, NULL)
                """),
                {"qid": quotation_id, "actor_uid": manager_uid, "comment": reason.strip()},
            )

            notify_users(
                conn, [rep_user_id], category="quotation", event_type="REJECTED",
                title=f"Your quotation {quotation_number} was rejected",
                link_page="Quotations", link_params={"quotation_id": quotation_id},
                actor_user_id=manager_uid,
            )
        return True, None
    except Exception as e:
        return False, str(e)


def manager_request_edit(quotation_id: int, manager_uid: int, comment: str) -> tuple[bool, str | None]:
    role = _require_role(manager_uid, {"sales manager", "admin"})
    if not (comment or "").strip():
        return False, "A comment explaining the requested edit is required."
    self_review_clause = "" if role == "admin" else "AND rep_user_id != :actor_uid"
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text(f"""
                    UPDATE quotation_requests
                    SET status = 'EDIT_REQUESTED', manager_user_id = :actor_uid, manager_decided_at = NOW(),
                        manager_comment = :comment
                    WHERE quotation_id = :qid AND status = 'IN_REVIEW' {self_review_clause}
                    RETURNING quotation_id, rep_user_id, quotation_number
                """),
                {"qid": quotation_id, "actor_uid": manager_uid, "comment": comment.strip()},
            )
            row = result.fetchone()
            if row is None:
                raise ValueError(
                    "This quotation was already resolved elsewhere, or you cannot review your "
                    "own submission. Refresh and try again."
                )
            rep_user_id, quotation_number = row[1], row[2]
            conn.execute(
                text("""
                    INSERT INTO quotation_status_events
                        (quotation_id, event_type, actor_user_id, from_status, to_status, comment, revision_id)
                    VALUES (:qid, 'EDIT_REQUESTED', :actor_uid, 'IN_REVIEW', 'EDIT_REQUESTED', :comment, NULL)
                """),
                {"qid": quotation_id, "actor_uid": manager_uid, "comment": comment.strip()},
            )

            notify_users(
                conn, [rep_user_id], category="quotation", event_type="EDIT_REQUESTED",
                title=f"Edit requested on your quotation {quotation_number}",
                link_page="Quotations", link_params={"quotation_id": quotation_id},
                actor_user_id=manager_uid,
            )
        return True, None
    except Exception as e:
        return False, str(e)


def manager_return_for_revision(quotation_id: int, manager_uid: int, reason: str) -> tuple[bool, str | None]:
    role = _require_role(manager_uid, {"sales manager", "admin"})
    if not (reason or "").strip():
        return False, "A reason for returning this quotation is required."
    self_review_clause = "" if role == "admin" else "AND rep_user_id != :actor_uid"
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text(f"""
                    UPDATE quotation_requests
                    SET status = 'EDIT_REQUESTED', manager_user_id = :actor_uid, manager_decided_at = NOW(),
                        manager_comment = :comment
                    WHERE quotation_id = :qid AND status = 'APPROVED' {self_review_clause}
                    RETURNING quotation_id, rep_user_id, quotation_number
                """),
                {"qid": quotation_id, "actor_uid": manager_uid, "comment": reason.strip()},
            )
            row = result.fetchone()
            if row is None:
                raise ValueError(
                    "This quotation was already resolved elsewhere, or you cannot review your "
                    "own submission. Refresh and try again."
                )
            rep_user_id, quotation_number = row[1], row[2]
            conn.execute(
                text("""
                    INSERT INTO quotation_status_events
                        (quotation_id, event_type, actor_user_id, from_status, to_status, comment, revision_id)
                    VALUES (:qid, 'RETURNED_FOR_REVISION', :actor_uid, 'APPROVED', 'EDIT_REQUESTED', :comment, NULL)
                """),
                {"qid": quotation_id, "actor_uid": manager_uid, "comment": reason.strip()},
            )

            notify_users(
                conn, [rep_user_id], category="quotation", event_type="RETURNED_FOR_REVISION",
                title=f"Your quotation {quotation_number} was returned for revision",
                link_page="Quotations", link_params={"quotation_id": quotation_id},
                actor_user_id=manager_uid,
            )
        return True, None
    except Exception as e:
        return False, str(e)


def withdraw_quotation(quotation_id: int, rep_uid: int, reason: str) -> tuple[bool, str | None]:
    """
    Owning rep withdraws from IN_REVIEW or EDIT_REQUESTED. The guard allows two
    possible prior statuses, so the accurate `from_status` for the audit event
    is captured with a `FOR UPDATE` CTE folded into the *same* guarded UPDATE
    statement (Postgres evaluates the CTE against the pre-UPDATE snapshot) —
    this is still exactly one guarded UPDATE, not a separate SELECT-then-UPDATE:
    if the WHERE guard doesn't match, zero rows come back and nothing else in
    the transaction runs.
    """
    if not (reason or "").strip():
        return False, "A withdrawal reason is required."
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text("""
                    WITH prior AS (
                        SELECT status FROM quotation_requests WHERE quotation_id = :qid FOR UPDATE
                    )
                    UPDATE quotation_requests
                    SET status = 'WITHDRAWN', withdrawn_at = NOW(), withdrawn_by = :actor_uid,
                        withdrawal_reason = :reason
                    FROM prior
                    WHERE quotation_requests.quotation_id = :qid
                      AND prior.status IN ('IN_REVIEW', 'EDIT_REQUESTED')
                      AND quotation_requests.rep_user_id = :actor_uid
                    RETURNING quotation_requests.quotation_id, prior.status AS prior_status
                """),
                {"qid": quotation_id, "actor_uid": rep_uid, "reason": reason.strip()},
            )
            row = result.fetchone()
            if row is None:
                raise ValueError(
                    "This quotation could not be withdrawn — it may have already been resolved, "
                    "or it does not belong to you. Refresh and try again."
                )
            prior_status = row[1]

            conn.execute(
                text("""
                    INSERT INTO quotation_status_events
                        (quotation_id, event_type, actor_user_id, from_status, to_status, comment, revision_id)
                    VALUES (:qid, 'WITHDRAWN', :actor_uid, :from_status, 'WITHDRAWN', :reason, NULL)
                """),
                {"qid": quotation_id, "actor_uid": rep_uid, "from_status": prior_status, "reason": reason.strip()},
            )
        return True, None
    except Exception as e:
        return False, str(e)


def coordinator_mark_done(
    quotation_id: int, coordinator_uid: int, odoo_reference: str | None, note: str | None
) -> tuple[bool, str | None]:
    """
    Coordinator marks an APPROVED quotation DONE. `_odoo_reference_exists()` is
    the primary defense (caller/UI should already have checked it and disabled
    the button); the DB-level guarded UPDATE plus the unique partial index
    `idx_qr_odoo_ref_unique` is the backstop for the true concurrent-race case,
    caught here as `IntegrityError` and converted to a soft `(False, ...)`.
    """
    _require_role(coordinator_uid, {"sales coordinator", "admin"})
    ref = _norm(odoo_reference) or None
    note_val = _norm(note) or None

    if ref and _odoo_reference_exists(ref):
        return False, f"Odoo reference '{ref}' is already used by another quotation."

    try:
        with engine.begin() as conn:
            result = conn.execute(
                text("""
                    UPDATE quotation_requests
                    SET status = 'DONE', coordinator_user_id = :actor_uid, coordinator_done_at = NOW(),
                        coordinator_note = :note, odoo_reference = :ref
                    WHERE quotation_id = :qid AND status = 'APPROVED'
                    RETURNING quotation_id, rep_user_id, manager_user_id, quotation_number
                """),
                {"qid": quotation_id, "actor_uid": coordinator_uid, "note": note_val, "ref": ref},
            )
            row = result.fetchone()
            if row is None:
                raise ValueError("This quotation was already resolved elsewhere. Refresh and try again.")
            rep_user_id, manager_user_id, quotation_number = row[1], row[2], row[3]
            conn.execute(
                text("""
                    INSERT INTO quotation_status_events
                        (quotation_id, event_type, actor_user_id, from_status, to_status, comment, revision_id)
                    VALUES (:qid, 'MARKED_DONE', :actor_uid, 'APPROVED', 'DONE', :note, NULL)
                """),
                {"qid": quotation_id, "actor_uid": coordinator_uid, "note": note_val},
            )

            notify_users(
                conn, [rep_user_id], category="quotation", event_type="MARKED_DONE",
                title=f"Quotation {quotation_number} marked Done",
                link_page="Quotations", link_params={"quotation_id": quotation_id},
                actor_user_id=coordinator_uid,
            )
            notify_users(
                conn, [manager_user_id], category="quotation", event_type="MARKED_DONE",
                title=f"Quotation {quotation_number} marked Done",
                link_page="Review Quotations", link_params={"quotation_id": quotation_id},
                actor_user_id=coordinator_uid,
            )
        return True, None
    except IntegrityError:
        return False, "That Odoo reference was just used by another quotation."
    except Exception as e:
        return False, str(e)


# ─────────────────────────────────────────────────────────────────────────────
# Shared rendering helpers
# ─────────────────────────────────────────────────────────────────────────────

def render_search_date_filter(ns: str, *, search_placeholder: str = "Search…", show_date: bool = True):
    """
    Shared search + optional date-range controls, matching the pattern already
    used in app_pages/my_submissions.py (text_input + two date_input(value=None)).
    Returns (search_q, date_from, date_to) for use with filter_quotations_df.
    """
    search_q = st.text_input(
        "", placeholder=search_placeholder, key=f"{ns}_search", label_visibility="collapsed"
    )
    date_from = date_to = None
    if show_date:
        c1, c2 = st.columns(2)
        with c1:
            date_from = st.date_input("From", value=None, key=f"{ns}_date_from")
        with c2:
            date_to = st.date_input("To", value=None, key=f"{ns}_date_to")
    return search_q, date_from, date_to


def filter_quotations_df(
    df: pd.DataFrame,
    *,
    search_q: str = "",
    date_from=None,
    date_to=None,
    date_col: str = "submitted_at",
    text_cols: tuple = ("quotation_number", "account_name"),
) -> pd.DataFrame:
    """Client-side filter of an already-loaded quotations DataFrame — no new queries."""
    if df.empty:
        return df
    out = df

    if (date_from or date_to) and date_col in out.columns:
        flt_date = pd.to_datetime(out[date_col], errors="coerce").dt.date
        if date_from:
            out = out[flt_date >= date_from]
            flt_date = flt_date[out.index]
        if date_to:
            out = out[flt_date <= date_to]

    q = (search_q or "").strip().lower()
    if q:
        mask = pd.Series(False, index=out.index)
        for col in text_cols:
            if col in out.columns:
                mask = mask | out[col].astype(str).str.lower().str.contains(q, na=False, regex=False)
        out = out[mask]

    return out


def quotation_detail_href(page_name: str, quotation_id: int) -> str:
    """URL to a single quotation's routed detail view (?page=...&quotation_id=...)."""
    return f"?page={quote_plus(page_name)}&quotation_id={int(quotation_id)}"


def render_quotation_list(
    ns: str,
    df: pd.DataFrame,
    *,
    page_name: str,
    status_labels: dict,
    status_order: list,
    search_text_cols: tuple = ("quotation_number", "account_name"),
    date_col: str = "submitted_at",
    search_placeholder: str = "Search…",
    page_size: int = 10,
) -> None:
    """
    Shared paginated list view for a quotations queue: search + date filter,
    counted status tabs, and card rows (reusing ui.visit_card, which is fully
    generic) linking to a routed detail view via quotation_detail_href.
    Matches the pattern already established in app_pages/my_submissions.py.
    """
    if df.empty:
        st.info("No quotations to show.")
        return

    search_q, date_from, date_to = render_search_date_filter(ns, search_placeholder=search_placeholder)
    filtered = filter_quotations_df(
        df, search_q=search_q, date_from=date_from, date_to=date_to,
        date_col=date_col, text_cols=search_text_cols,
    )

    tab_labels = [f"All  {len(filtered)}"]
    for status in status_order:
        cnt = int((filtered["status"] == status).sum()) if "status" in filtered.columns else 0
        tab_labels.append(f"{status_labels.get(status, status)}  {cnt}")

    chosen_label = st.radio(
        "Status", tab_labels, key=f"{ns}_status_tab", horizontal=True, label_visibility="collapsed",
    )
    chosen = chosen_label.rsplit("  ", 1)[0].strip()

    if chosen == "All":
        visible = filtered
    else:
        inv_labels = {v: k for k, v in status_labels.items()}
        status_key = inv_labels.get(chosen, chosen)
        visible = filtered[filtered["status"] == status_key]

    if visible.empty:
        st.caption("No results — try a different search, date range, or status.")
        return

    total = len(visible)
    filter_key = (search_q, date_from, date_to, chosen_label)
    page_state_key = f"{ns}_page"
    filter_state_key = f"{ns}_filter_key"
    if st.session_state.get(filter_state_key) != filter_key:
        st.session_state[filter_state_key] = filter_key
        st.session_state[page_state_key] = 0

    current_page = st.session_state.get(page_state_key, 0)
    total_pages = max(1, (total + page_size - 1) // page_size)
    current_page = min(current_page, total_pages - 1)

    start_idx = current_page * page_size
    end_idx = min(start_idx + page_size, total)
    page_df = visible.iloc[start_idx:end_idx]

    st.caption(f"Showing {start_idx + 1}–{end_idx} of {total:,} quotation{'s' if total != 1 else ''}")

    cards_html = ""
    for _, row in page_df.iterrows():
        qid = int(row["quotation_id"])
        status_val = _norm(row.get("status"))
        status_label = status_labels.get(status_val, status_val)
        cards_html += visit_card(
            _norm(row.get("rep_name")),
            to_local(row.get(date_col)),
            _norm(row.get("account_name")) or "—",
            subtitle=_norm(row.get("quotation_number")),
            status=status_label,
            status_variant=_status_badge_variant(status_val),
            href=quotation_detail_href(page_name, qid),
        )
    st.markdown(cards_html, unsafe_allow_html=True)

    if total_pages > 1:
        pcol1, pcol2, pcol3 = st.columns([1, 2, 1])
        with pcol1:
            if st.button("← Prev", key=f"{ns}_prev_btn", disabled=current_page <= 0, use_container_width=True):
                st.session_state[page_state_key] = current_page - 1
                st.rerun()
        with pcol2:
            st.markdown(
                f'<p style="text-align:center;color:var(--color-text-subtle);font-size:0.85rem;margin-top:0.4rem;">'
                f'Page {current_page + 1} of {total_pages}</p>',
                unsafe_allow_html=True,
            )
        with pcol3:
            if st.button("Next →", key=f"{ns}_next_btn", disabled=current_page >= total_pages - 1, use_container_width=True):
                st.session_state[page_state_key] = current_page + 1
                st.rerun()


_REVISION_HEADER_FIELDS = [
    ("quotation_date", "Quotation Date"),
    ("vat_rate", "VAT Rate (%)"),
    ("remarks", "Remarks"),
    ("customer_reference", "Customer Reference No."),
    ("validity_days", "Validity (days)"),
    ("delivery_terms", "Delivery Terms"),
    ("payment_terms", "Payment Terms"),
    ("subtotal", "Subtotal"),
    ("vat_amount", "VAT Amount"),
    ("grand_total", "Grand Total"),
]


def render_revision_diff(revision_a: dict, revision_b: dict) -> None:
    """
    Header-field-by-field and line-by-line comparison of two quotation_revisions
    rows, using the same visual idiom as ui.py::compare_row /
    admin_change_requests.py::_render_diff_table (reused, not reimplemented).

    Each dict is a revision row (e.g. from `_load_revisions(...).iloc[i].to_dict()`)
    with an added "lines" key: a list of dicts from
    `_load_revision_lines(revision_id).to_dict("records")`.
    """
    label_a = f"Revision #{revision_a.get('revision_no', '?')}"
    label_b = f"Revision #{revision_b.get('revision_no', '?')}"

    header_rows = []
    for col, label in _REVISION_HEADER_FIELDS:
        old_val = _norm(revision_a.get(col))
        new_val = _norm(revision_b.get(col))
        header_rows.append(compare_row(label, old_val, new_val, changed=(old_val != new_val)))

    lines_a = {ln["line_no"]: ln for ln in (revision_a.get("lines") or [])}
    lines_b = {ln["line_no"]: ln for ln in (revision_b.get("lines") or [])}
    line_rows = []
    for line_no in sorted(set(lines_a) | set(lines_b)):
        la = lines_a.get(line_no)
        lb = lines_b.get(line_no)

        def _line_desc(ln):
            if not ln:
                return "(none)"
            art = _norm(ln.get("article_number_snapshot")) or _norm(ln.get("product_id"))
            return f"{art} — qty {ln.get('quantity')} @ {ln.get('unit_price')} ({ln.get('discount_pct')}% disc)"

        old_desc = _line_desc(la)
        new_desc = _line_desc(lb)
        line_rows.append(compare_row(f"Line {line_no}", old_desc, new_desc, changed=(old_desc != new_desc)))

    rows_html = "".join(header_rows) + "".join(line_rows)
    st.markdown(
        f"""
        <table style="width:100%;border-collapse:collapse;border:1px solid var(--color-border);
                      border-radius:10px;overflow:hidden;font-size:0.875rem;">
          <thead>
            <tr style="background:var(--color-surface-2);">
              <th style="padding:10px 12px;text-align:left;font-weight:600;color:var(--color-text-muted);
                         border-bottom:1px solid var(--color-border);width:30%;">Field</th>
              <th style="padding:10px 12px;text-align:left;font-weight:600;color:var(--color-text-muted);
                         border-bottom:1px solid var(--color-border);">{label_a}</th>
              <th style="padding:10px 12px;text-align:left;font-weight:600;color:var(--color-text-muted);
                         border-bottom:1px solid var(--color-border);">{label_b}</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
        """,
        unsafe_allow_html=True,
    )


def render_quotation_detail(quotation_id: int) -> None:
    """
    Shared read-only quotation view (header, lines, totals, status timeline)
    used identically by quotation_request.py / quotation_review.py /
    quotation_handoff.py so the detail view isn't copy-pasted three times.
    """
    header = _load_quotation_header(quotation_id)
    if not header:
        st.warning("Quotation not found.")
        return

    lines_df = query_df(
        """
        SELECT ql.line_no, ql.product_id, i.article_number, i.description, i.unit_of_measurement,
               ql.quantity, ql.unit_price, ql.discount_pct
        FROM quotation_lines ql
        JOIN items i ON i.product_id = ql.product_id
        WHERE ql.quotation_id = :id
        ORDER BY ql.line_no
        """,
        {"id": quotation_id},
    )
    totals = compute_header_totals(
        lines_df.to_dict("records") if not lines_df.empty else [],
        Decimal(str(header.get("vat_rate") or 0)),
    )

    status_val = _norm(header.get("status"))
    st.markdown(
        f"#### {_norm(header.get('quotation_number'))} &nbsp; "
        + status_badge(status_val, _status_badge_variant(status_val)),
        unsafe_allow_html=True,
    )

    cust_name = query_scalar(
        "SELECT account_name FROM customers WHERE customer_id = :cid", {"cid": header.get("customer_id")}
    )

    col1, col2 = st.columns(2)
    with col1:
        st.write(f"**Customer:** {_norm(cust_name) or '—'}")
        st.write(f"**Quotation Date:** {header.get('quotation_date') or '—'}")
        st.write(f"**VAT Rate:** {header.get('vat_rate')}%")
    with col2:
        st.write(f"**Validity:** {header.get('validity_days') or '—'} days")
        st.write(f"**Delivery Terms:** {_norm(header.get('delivery_terms')) or '—'}")
        st.write(f"**Payment Terms:** {_norm(header.get('payment_terms')) or '—'}")
        st.write(f"**Remarks:** {_norm(header.get('remarks')) or '—'}")
        st.write(f"**Customer Reference No.:** {_norm(header.get('customer_reference')) or '—'}")

    st.markdown("##### Line Items")
    if lines_df.empty:
        st.info("No line items.")
    else:
        display_df = lines_df.copy()
        display_df["line_total"] = [
            compute_line_total(
                Decimal(str(r["quantity"])), Decimal(str(r["unit_price"])), Decimal(str(r["discount_pct"]))
            )
            for _, r in display_df.iterrows()
        ]
        display_df["unit_of_measurement"] = display_df["unit_of_measurement"].fillna("")
        st.dataframe(
            display_df[
                ["line_no", "article_number", "description", "unit_of_measurement",
                 "quantity", "unit_price", "discount_pct", "line_total"]
            ],
            use_container_width=True,
            hide_index=True,
            column_config={"unit_of_measurement": "Unit"},
        )

    st.markdown(
        f"**Subtotal:** {fmt_money(totals['subtotal'])} &nbsp;·&nbsp; "
        f"**VAT:** {fmt_money(totals['vat_amount'])} &nbsp;·&nbsp; "
        f"**Grand Total:** {fmt_money(totals['grand_total'])}"
    )

    if status_val in ("APPROVED", "DONE"):
        odoo_ref = _norm(header.get("odoo_reference"))
        coord_note = _norm(header.get("coordinator_note"))
        st.markdown("##### Handoff to Odoo")
        if odoo_ref or coord_note:
            if odoo_ref:
                st.write(f"**Odoo Reference:** {odoo_ref}")
            if coord_note:
                st.write(f"**Coordinator Note:** {coord_note}")
        else:
            st.caption("No Odoo reference recorded yet.")

    st.markdown("##### Status Timeline")
    events_df = _load_status_events(quotation_id)
    if events_df.empty:
        st.caption("No status events recorded.")
        return

    name_map: dict = {}
    actor_ids = [int(x) for x in events_df["actor_user_id"].dropna().unique().tolist()]
    if actor_ids:
        names_df = query_df("SELECT user_id, name FROM users WHERE user_id = ANY(:ids)", {"ids": actor_ids})
        if not names_df.empty:
            name_map = dict(zip(names_df["user_id"], names_df["name"]))

    for _, ev in events_df.iterrows():
        actor_name = name_map.get(ev.get("actor_user_id"), "—")
        at_str = to_local_str(ev.get("at_utc"))
        comment = _norm(ev.get("comment"))
        from_s = _norm(ev.get("from_status")) or "—"
        to_s = _norm(ev.get("to_status"))
        event_type = _norm(ev.get("event_type"))
        badge_html = status_badge(f"{event_type}: {from_s} → {to_s}", _status_badge_variant(to_s))

        st.markdown(
            f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:4px;">'
            f'{badge_html}'
            f'<span style="font-size:0.8rem;color:var(--color-text-subtle);">{_norm(actor_name)} · {at_str}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
        if comment:
            st.caption(f'"{comment}"')


def _status_badge_variant(status: str) -> str:
    return {
        "IN_REVIEW": "info",
        "EDIT_REQUESTED": "warning",
        "REJECTED": "danger",
        "APPROVED": "success",
        "DONE": "success",
        "WITHDRAWN": "neutral",
    }.get(status, "neutral")


# ─────────────────────────────────────────────────────────────────────────────
# PDF export — print-ready quotation (Design C: compact/invoice-style)
# ─────────────────────────────────────────────────────────────────────────────

PDF_PRINTABLE_STATUSES = ("APPROVED", "DONE")

_PDF_COMPANY = {
    "name_en": "Excellence Medical Care Co.",
    "name_ar": "شركة تميز الرعاية الطبية",
    "addr_en": "(Wasel) P.O.Box 9397 Al Amir - Prince Mohammed bin Fahd Road - Al Tubayshi - Unit No: 2 - Dammam 32233 - 2325 - K.S.A",
    "phone1": "+966 13 8335536",
    "phone2": "+966 13 8333269",
    "email": "info@tamiozmed.com",
    "website": "www.tamiozmed.com",
    "vat": "310211040600003",
}


def _pdf_logo_b64() -> str:
    logo_path = Path(__file__).resolve().parent.parent / "static" / "EMC LOGO.png"
    try:
        with open(logo_path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except Exception:
        return ""


def _amount_in_words(amount: Decimal) -> tuple[str, str]:
    """(English, Arabic) sentence for a Decimal SAR amount, e.g.
    ("Seventeen Thousand, Five Hundred And Thirty-Seven Saudi Riyals and
    Fifty Halalas Only", "سبعة عشر ألفاً ... ريال سعودي وخمسون هللة فقط")."""
    q = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    riyals = int(q)
    halalas = int((q - riyals) * 100)

    en = f"{num2words(riyals, lang='en').title()} Saudi Riyals"
    ar = f"{num2words(riyals, lang='ar')} ريال سعودي"
    if halalas:
        en += f" and {num2words(halalas, lang='en').title()} Halalas"
        ar += f" و{num2words(halalas, lang='ar')} هللة"
    en += " Only"
    ar += " فقط"
    return en, ar


def render_print_button(header: dict, ns: str) -> None:
    """
    Print/download button for a quotation's detail view — enabled only when
    status is APPROVED or DONE, disabled (with an explanatory tooltip)
    otherwise. Shared across all 3 quotation pages so the gate and PDF
    generation call live in exactly one place.
    """
    status_val = _norm(header.get("status"))
    if status_val in PDF_PRINTABLE_STATUSES:
        pdf_bytes = generate_quotation_pdf(int(header["quotation_id"]))
        st.download_button(
            "🖶 Print",
            data=pdf_bytes,
            file_name=f"{_norm(header.get('quotation_number')) or 'quotation'}.pdf",
            mime="application/pdf",
            key=f"{ns}_print_btn",
            use_container_width=True,
        )
    else:
        st.button(
            "🖶 Print",
            disabled=True,
            key=f"{ns}_print_btn_disabled",
            use_container_width=True,
            help="Print is available once the quotation is Approved or Done.",
        )


def _place_quotation_stamp(page: fitz.Page, sig_line_width: int, website: str) -> None:
    """
    Overlay the company seal (static/Quotations_Stamp.png) beside the
    signature block on the given page. Anchored off the actual rendered
    "Prepared By" / "Approved By" labels (not the rep/manager names —
    those can repeat elsewhere on the page, e.g. the "Sales Rep" meta
    field, or collide with each other when the rep and manager are the
    same person) plus the totals box and footer band, so placement stays
    correct regardless of how much content pushed the signature block down
    the page. Raises on any lookup failure — callers should treat stamp
    placement as best-effort and catch broadly.
    """
    import io

    from PIL import Image

    stamp_path = Path(__file__).resolve().parent.parent / "static" / "Quotations_Stamp.png"

    label1_rect = page.search_for("Prepared By")[0]
    label2_rect = page.search_for("Approved By")[0]
    footer_top = max(r.y0 for r in page.search_for(website))
    totals_bottom = max(r.y1 for r in page.search_for("SAR"))

    im = Image.open(stamp_path).convert("RGBA")
    im = im.rotate(8, expand=True, resample=Image.BICUBIC)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    stamp_pix = fitz.Pixmap(buf.getvalue())
    aspect = im.height / im.width  # stamp_h / stamp_w

    # Short quotations (few line items, short remarks) leave much less
    # vertical room between the totals box and the footer than a long one
    # does, so the stamp scales down to fit that gap rather than skipping
    # outright — it should always appear, just smaller when space is tight.
    available = (footer_top - 14) - (totals_bottom + 10)
    if available < 45:
        return  # truly no room on this layout — skip rather than force an overlap
    stamp_w = min(118, available / aspect)
    stamp_h = stamp_w * aspect

    # Right of the (shortened) signature lines, vertically spanning the gap
    # between the two rows, clamped so it can never drift into the totals
    # box above or the footer band below.
    line_end_x = label1_rect.x0 + sig_line_width
    cx = line_end_x + 16 + stamp_w / 2
    cy = (label1_rect.y0 + label2_rect.y0) / 2 - 8

    max_cy = footer_top - 14 - stamp_h / 2
    min_cy = totals_bottom + 10 + stamp_h / 2
    cy = max(min_cy, min(cy, max_cy))

    stamp_rect = fitz.Rect(cx - stamp_w / 2, cy - stamp_h / 2, cx + stamp_w / 2, cy + stamp_h / 2)
    page.insert_image(stamp_rect, pixmap=stamp_pix, overlay=True)


def generate_quotation_pdf(quotation_id: int) -> bytes:
    """
    Render a quotation as a print-ready PDF (Design C layout). Callers are
    responsible for only offering this to the user when
    status in PDF_PRINTABLE_STATUSES — this function itself doesn't enforce
    that gate, it just renders whatever the quotation's current state is.
    """
    header = _load_quotation_header(quotation_id)
    if not header:
        raise ValueError("Quotation not found.")

    customer = query_df(
        "SELECT account_name, region, city, sector FROM customers WHERE customer_id = :cid",
        {"cid": header.get("customer_id")},
    )
    customer_name = _norm(customer.iloc[0]["account_name"]) if not customer.empty else ""
    customer_region = _norm(customer.iloc[0]["region"]) if not customer.empty else ""
    customer_city = _norm(customer.iloc[0]["city"]) if not customer.empty else ""
    customer_sector = _norm(customer.iloc[0]["sector"]) if not customer.empty else ""

    rep_name = query_scalar(
        "SELECT name FROM users WHERE user_id = :uid", {"uid": header.get("rep_user_id")}
    )
    manager_name = (
        query_scalar("SELECT name FROM users WHERE user_id = :uid", {"uid": header.get("manager_user_id")})
        if header.get("manager_user_id")
        else None
    )

    lines_df = query_df(
        """
        SELECT ql.line_no, i.article_number, i.description, i.unit_of_measurement,
               ql.quantity, ql.unit_price, ql.discount_pct
        FROM quotation_lines ql
        JOIN items i ON i.product_id = ql.product_id
        WHERE ql.quotation_id = :id
        ORDER BY ql.line_no
        """,
        {"id": quotation_id},
    )

    vat_rate = Decimal(str(header.get("vat_rate") or 0))
    totals = compute_header_totals(lines_df.to_dict("records") if not lines_df.empty else [], vat_rate)
    amount_words_en, amount_words_ar = _amount_in_words(totals["grand_total"])

    rows_html = ""
    for _, l in lines_df.iterrows():
        line_total = compute_line_total(
            Decimal(str(l["quantity"])), Decimal(str(l["unit_price"])), Decimal(str(l["discount_pct"]))
        )
        rows_html += f"""
        <tr>
          <td style="text-align:center; color:#8a93a6;">{int(l['line_no'])}</td>
          <td style="font-weight:700;">{_norm(l['article_number'])}</td>
          <td>{_norm(l['description'])}</td>
          <td style="text-align:center;">{_norm(l['unit_of_measurement'])}</td>
          <td style="text-align:center;">{l['quantity']}</td>
          <td style="text-align:right;">{fmt_money(l['unit_price'])}</td>
          <td style="text-align:center;">{Decimal(str(l['discount_pct'])):.2f}%</td>
          <td style="text-align:right; font-weight:700;">{fmt_money(line_total)}</td>
        </tr>"""

    quotation_date = header.get("quotation_date")
    quotation_date_str = quotation_date.strftime("%d %b %Y") if hasattr(quotation_date, "strftime") else _norm(quotation_date)

    odoo_ref = _norm(header.get("odoo_reference"))
    odoo_line = f"Odoo Ref: <b>{odoo_ref}</b>" if odoo_ref else ""

    customer_ref = _norm(header.get("customer_reference"))
    customer_ref_row = (
        f'<tr><td class="k">Customer Ref</td><td class="v">{customer_ref}</td></tr>' if customer_ref else ""
    )

    status_val = _norm(header.get("status"))
    logo_b64 = _pdf_logo_b64()
    c = _PDF_COMPANY
    rep_display = _norm(rep_name)
    manager_display = _norm(manager_name)
    SIG_LINE_WIDTH = 128  # px — shortened signature underline, leaves room for the stamp

    html = f"""
<html><head><style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: Arial, sans-serif; font-size: 8.6pt; color: #14171f; margin: 0; }}
  .ar {{ direction: rtl; text-align: right; font-family: Arial, sans-serif; }}
  table {{ width: 100%; border-collapse: collapse; }}

  .band {{ background: #12213E; padding: 10px 16px; }}
  .band td {{ vertical-align: middle; }}
  .band .name {{ color: #fff; font-size: 13pt; font-weight: 800; }}
  .band .name-ar {{ color: #B9C4DE; font-size: 8.5pt; margin-top: 1px; }}
  .band .detail {{ color: #8FA0C7; font-size: 7pt; margin-top: 3px; line-height: 1.4; }}
  .band .logo {{ width: 40px; height: auto; }}

  .strip {{ background: #E8ECF5; padding: 6px 16px; }}
  .strip td {{ font-size: 7.6pt; color: #4a5568; padding-top: 2px; }}
  .strip .qtitle {{ color: #12213E; font-weight: 800; font-size: 10pt; }}
  .strip .pill {{ display: inline-block; font-size: 7pt; font-weight: 700; color: #fff; background: #1c9a5b;
                  padding: 1px 8px; border-radius: 3px; }}

  .body {{ padding: 10px 16px 0; }}

  .meta {{ width: 100%; }}
  .meta td {{ padding: 1px 10px 1px 0; font-size: 7.8pt; white-space: nowrap; }}
  .meta .k {{ color: #8a93a6; }}
  .meta .v {{ font-weight: 700; }}

  .items {{ margin-top: 10px; }}
  .items th {{ border-top: 1.5px solid #12213E; border-bottom: 1.5px solid #12213E; padding: 4px 5px;
               font-size: 6.9pt; font-weight: 700; text-transform: uppercase; letter-spacing: 0.3px; text-align: left; color: #12213E; }}
  .items td {{ padding: 4px 5px; border-bottom: 1px solid #E8ECF5; font-size: 8pt; }}

  .bottomwrap {{ margin-top: 8px; }}
  .bottomwrap td {{ vertical-align: top; }}
  .words {{ font-size: 7.4pt; color: #8a93a6; font-style: italic; width: 55%; }}
  .words .ar {{ margin-top: 1px; }}
  .totalbox {{ width: 100%; background: #12213E; border-radius: 6px; padding: 8px 12px; }}
  .totalbox table {{ width: 100%; }}
  .totalbox td {{ padding: 2px 0; font-size: 8pt; color: #B9C4DE; white-space: nowrap; }}
  .totalbox .val {{ text-align: right; color: #fff; }}
  .totalbox .grandrow td {{ border-top: 1px solid #35426b; padding-top: 6px; font-size: 10.5pt; font-weight: 800; color: #fff; white-space: nowrap; }}

  .footwrap {{ margin-top: 10px; }}
  .footwrap td {{ vertical-align: top; width: 50%; padding-right: 12px; }}
  .terms table {{ width: 100%; }}
  .terms td {{ padding: 1px 0; font-size: 7.6pt; }}
  .terms .lbl {{ color: #8a93a6; white-space: nowrap; }}
  .terms .val {{ text-align: right; font-weight: 700; }}
  .remarksbox {{ font-size: 7.4pt; color: #4a5568; background: #F7F8FB; border: 1px solid #E8ECF5; border-radius: 4px; padding: 5px 7px; margin-top: 4px; }}

  .sig {{ margin-top: 6px; }}
  .sig table {{ width: 100%; }}
  .sig td {{ font-size: 7.6pt; padding: 0 8px 0 0; width: 50%; }}
  .sig .lbl {{ color: #8a93a6; font-size: 6.8pt; text-transform: uppercase; }}
  .sig .name {{ font-weight: 700; margin-top: 12px; border-top: 1px solid #14171f; padding-top: 3px; font-size: 7.8pt; }}

  .footer {{ margin-top: 10px; padding: 6px 16px; background: #12213E; color: #8FA0C7; text-align: center; font-size: 6.8pt; }}
</style></head>
<body>

  <table class="band">
    <tr>
      <td style="width:80%;">
        <div class="name">{c['name_en']}</div>
        <div class="name-ar ar">{c['name_ar']}</div>
        <div class="detail">
          <div>{c['addr_en']}</div>
          <div>{c['phone1']} | {c['phone2']} | {c['email']} | {c['website']} | VAT {c['vat']}</div>
        </div>
      </td>
      <td style="width:20%; text-align:right;">
        <img class="logo" src="data:image/png;base64,{logo_b64}" />
      </td>
    </tr>
  </table>

  <table class="strip">
    <tr>
      <td style="width:60%;"><span class="qtitle">QUOTATION / عرض اسعار</span></td>
      <td style="width:40%; text-align:right;"><b>{_norm(header.get('quotation_number'))}</b> &nbsp;&middot;&nbsp; {quotation_date_str}</td>
    </tr>
    <tr>
      <td style="width:60%;"><span class="pill">{status_val}</span></td>
      <td style="width:40%; text-align:right;">{odoo_line}</td>
    </tr>
  </table>

  <div class="body">

    <table>
      <tr>
        <td style="width:55%; vertical-align:top;">
          <div style="font-size:7.2pt; color:#8a93a6; font-weight:700;">CUSTOMER</div>
          <div style="font-size:10.5pt; font-weight:800; margin-top:1px;">{customer_name}</div>
          <div style="font-size:7.8pt; color:#8a93a6;">{customer_city} &middot; {customer_region} &middot; {customer_sector}</div>
        </td>
        <td style="width:45%; vertical-align:top;">
          <table class="meta">
            <tr><td class="k">Sales Rep / مندوب المبيعات</td><td class="v">{rep_display}</td></tr>
            <tr><td class="k">VAT Rate / نسبة الضريبة</td><td class="v">{vat_rate}%</td></tr>
            {customer_ref_row}
          </table>
        </td>
      </tr>
    </table>

    <table class="items">
      <tr>
        <th style="width:3%;">#</th>
        <th style="width:10%;">Model</th>
        <th style="width:33%;">Description</th>
        <th style="width:6%;">Unit</th>
        <th style="width:6%;">Qty</th>
        <th style="width:12%;">Price</th>
        <th style="width:9%;">Disc.</th>
        <th style="width:21%; text-align:right;">Total</th>
      </tr>
      {rows_html}
    </table>

    <table class="bottomwrap">
      <tr>
        <td class="words" style="width:40%;">
          <div>Amount In Words: {amount_words_en}</div>
          <div class="ar">المبلغ بالكلمات: {amount_words_ar}</div>
        </td>
        <td style="width:3%;"></td>
        <td style="width:57%;">
          <div class="totalbox">
            <table>
              <tr><td>Subtotal</td><td class="val">{fmt_money(totals['subtotal'])}</td></tr>
              <tr><td>VAT ({vat_rate}%)</td><td class="val">{fmt_money(totals['vat_amount'])}</td></tr>
              <tr class="grandrow"><td>Grand Total</td><td class="val">{fmt_money(totals['grand_total'])} SAR</td></tr>
            </table>
          </div>
        </td>
      </tr>
    </table>

    <table class="footwrap">
      <tr>
        <td>
          <div class="terms">
            <table>
              <tr><td class="lbl">Validity / صلاحية العرض</td><td class="val">{header.get('validity_days') or '—'} Days</td></tr>
              <tr><td class="lbl">Delivery / مدة التوصيل</td><td class="val">{_norm(header.get('delivery_terms')) or '—'}</td></tr>
              <tr><td class="lbl">Payment / طريقة السداد</td><td class="val">{_norm(header.get('payment_terms')) or '—'}</td></tr>
            </table>
            <div class="remarksbox"><b>Remarks:</b> {_norm(header.get('remarks')) or '—'}</div>
          </div>
        </td>
        <td>
          <div class="sig">
            <table>
              <tr>
                <td>
                  <div class="lbl">Prepared By / أُعد بواسطة</div>
                  <div class="name" style="width:{SIG_LINE_WIDTH}px;">{rep_display}</div>
                </td>
              </tr>
              <tr>
                <td>
                  <div class="lbl" style="margin-top:8px;">Approved By / اعتمد بواسطة</div>
                  <div class="name" style="width:{SIG_LINE_WIDTH}px;">{manager_display or '—'}</div>
                </td>
              </tr>
            </table>
          </div>
        </td>
      </tr>
    </table>

  </div>

  <div class="footer">
    {c['name_en']} &nbsp;&middot;&nbsp; {c['phone1']} &nbsp;&middot;&nbsp; {c['email']} &nbsp;&middot;&nbsp; {c['website']}
  </div>

</body></html>
"""

    story = fitz.Story(html=html)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pdf", prefix=f"quotation_{quotation_id}_")
    os.close(tmp_fd)
    writer = fitz.DocumentWriter(tmp_path)
    mediabox = fitz.paper_rect("a4")
    where = mediabox + (24, 16, -24, -16)
    more = True
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()

    doc = fitz.open(tmp_path)
    total_pages = len(doc)
    for i, page in enumerate(doc, start=1):
        rect = page.rect
        page.insert_textbox(
            fitz.Rect(0, rect.height - 20, rect.width, rect.height - 6),
            f"Page {i} of {total_pages}",
            fontsize=7, fontname="helv", color=(0.5, 0.5, 0.5),
            align=fitz.TEXT_ALIGN_CENTER,
        )

    # Company stamp — only once a manager has actually approved (a real
    # manager_display, not the "—" placeholder), placed beside the signature
    # block on the last page. Anchored off the rendered text positions
    # (rather than fixed coordinates) so it's correct regardless of how the
    # quotation paginates; wrapped defensively so a placement quirk on some
    # future layout can never break PDF generation itself.
    if manager_display:
        try:
            _place_quotation_stamp(doc[-1], SIG_LINE_WIDTH, c["website"])
        except Exception:
            pass

    doc.saveIncr()
    doc.close()

    with open(tmp_path, "rb") as f:
        pdf_bytes = f.read()
    try:
        os.remove(tmp_path)
    except OSError:
        pass

    return pdf_bytes
