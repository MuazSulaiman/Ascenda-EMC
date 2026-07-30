# app_pages/quotation_helpers.py
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping

from datetime import datetime, timezone

import pandas as pd
import streamlit as st
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from db import engine
from db_ops import query_df, query_scalar, exec_sql
from app_pages.change_request_helpers import _norm, _sql_val
from ui import compare_row, status_badge


REQUEST_SOURCE_OPTIONS = ["Purchasing Dept.", "Procurement Dept.", "Sales Dept.", "Direct Request"]
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
                    (quotation_number, customer_id, rep_user_id, request_source, quotation_date,
                     vat_rate, remarks, validity_days, delivery_terms, payment_terms,
                     status, version, submitted_by)
                VALUES
                    (:quotation_number, :customer_id, :actor_uid, :request_source, :quotation_date,
                     :vat_rate, :remarks, :validity_days, :delivery_terms, :payment_terms,
                     'IN_REVIEW', 0, :actor_uid)
                RETURNING quotation_id
            """),
            {
                "quotation_number": quotation_number,
                "customer_id": header.get("customer_id"),
                "actor_uid": actor_uid,
                "request_source": header.get("request_source"),
                "quotation_date": header.get("quotation_date"),
                "vat_rate": vat_rate,
                "remarks": _norm(header.get("remarks")) or None,
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
                    (quotation_id, revision_no, created_by, customer_id, request_source, quotation_date,
                     vat_rate, remarks, validity_days, delivery_terms, payment_terms,
                     subtotal, vat_amount, grand_total)
                VALUES
                    (:qid, 1, :actor_uid, :customer_id, :request_source, :quotation_date,
                     :vat_rate, :remarks, :validity_days, :delivery_terms, :payment_terms,
                     :subtotal, :vat_amount, :grand_total)
                RETURNING revision_id
            """),
            {
                "qid": quotation_id,
                "actor_uid": actor_uid,
                "customer_id": header.get("customer_id"),
                "request_source": header.get("request_source"),
                "quotation_date": header.get("quotation_date"),
                "vat_rate": vat_rate,
                "remarks": _norm(header.get("remarks")) or None,
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
                    RETURNING version
                """),
                {"qid": quotation_id, "expected_version": expected_version, "actor_uid": actor_uid},
            )
            row = result.fetchone()
            if row is None:
                raise ValueError("This quotation was already changed elsewhere. Refresh and try again.")
            new_version = row[0]

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
                    SET customer_id = :customer_id, request_source = :request_source,
                        quotation_date = :quotation_date, vat_rate = :vat_rate, remarks = :remarks,
                        validity_days = :validity_days, delivery_terms = :delivery_terms,
                        payment_terms = :payment_terms
                    WHERE quotation_id = :qid
                """),
                {
                    "qid": quotation_id,
                    "customer_id": header.get("customer_id"),
                    "request_source": header.get("request_source"),
                    "quotation_date": header.get("quotation_date"),
                    "vat_rate": vat_rate,
                    "remarks": _norm(header.get("remarks")) or None,
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
                        (quotation_id, revision_no, created_by, customer_id, request_source, quotation_date,
                         vat_rate, remarks, validity_days, delivery_terms, payment_terms,
                         subtotal, vat_amount, grand_total)
                    VALUES
                        (:qid, :revision_no, :actor_uid, :customer_id, :request_source, :quotation_date,
                         :vat_rate, :remarks, :validity_days, :delivery_terms, :payment_terms,
                         :subtotal, :vat_amount, :grand_total)
                    RETURNING revision_id
                """),
                {
                    "qid": quotation_id,
                    "revision_no": revision_no,
                    "actor_uid": actor_uid,
                    "customer_id": header.get("customer_id"),
                    "request_source": header.get("request_source"),
                    "quotation_date": header.get("quotation_date"),
                    "vat_rate": vat_rate,
                    "remarks": _norm(header.get("remarks")) or None,
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
                    RETURNING quotation_id
                """),
                {"qid": quotation_id, "actor_uid": manager_uid},
            )
            if result.fetchone() is None:
                raise ValueError(
                    "This quotation was already resolved elsewhere, or you cannot review your "
                    "own submission. Refresh and try again."
                )
            conn.execute(
                text("""
                    INSERT INTO quotation_status_events
                        (quotation_id, event_type, actor_user_id, from_status, to_status, revision_id)
                    VALUES (:qid, 'APPROVED', :actor_uid, 'IN_REVIEW', 'APPROVED', NULL)
                """),
                {"qid": quotation_id, "actor_uid": manager_uid},
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
                    RETURNING quotation_id
                """),
                {"qid": quotation_id, "actor_uid": manager_uid, "comment": reason.strip()},
            )
            if result.fetchone() is None:
                raise ValueError(
                    "This quotation was already resolved elsewhere, or you cannot review your "
                    "own submission. Refresh and try again."
                )
            conn.execute(
                text("""
                    INSERT INTO quotation_status_events
                        (quotation_id, event_type, actor_user_id, from_status, to_status, comment, revision_id)
                    VALUES (:qid, 'REJECTED', :actor_uid, 'IN_REVIEW', 'REJECTED', :comment, NULL)
                """),
                {"qid": quotation_id, "actor_uid": manager_uid, "comment": reason.strip()},
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
                    RETURNING quotation_id
                """),
                {"qid": quotation_id, "actor_uid": manager_uid, "comment": comment.strip()},
            )
            if result.fetchone() is None:
                raise ValueError(
                    "This quotation was already resolved elsewhere, or you cannot review your "
                    "own submission. Refresh and try again."
                )
            conn.execute(
                text("""
                    INSERT INTO quotation_status_events
                        (quotation_id, event_type, actor_user_id, from_status, to_status, comment, revision_id)
                    VALUES (:qid, 'EDIT_REQUESTED', :actor_uid, 'IN_REVIEW', 'EDIT_REQUESTED', :comment, NULL)
                """),
                {"qid": quotation_id, "actor_uid": manager_uid, "comment": comment.strip()},
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
                    RETURNING quotation_id
                """),
                {"qid": quotation_id, "actor_uid": manager_uid, "comment": reason.strip()},
            )
            if result.fetchone() is None:
                raise ValueError(
                    "This quotation was already resolved elsewhere, or you cannot review your "
                    "own submission. Refresh and try again."
                )
            conn.execute(
                text("""
                    INSERT INTO quotation_status_events
                        (quotation_id, event_type, actor_user_id, from_status, to_status, comment, revision_id)
                    VALUES (:qid, 'RETURNED_FOR_REVISION', :actor_uid, 'APPROVED', 'EDIT_REQUESTED', :comment, NULL)
                """),
                {"qid": quotation_id, "actor_uid": manager_uid, "comment": reason.strip()},
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
                    RETURNING quotation_id
                """),
                {"qid": quotation_id, "actor_uid": coordinator_uid, "note": note_val, "ref": ref},
            )
            if result.fetchone() is None:
                raise ValueError("This quotation was already resolved elsewhere. Refresh and try again.")
            conn.execute(
                text("""
                    INSERT INTO quotation_status_events
                        (quotation_id, event_type, actor_user_id, from_status, to_status, comment, revision_id)
                    VALUES (:qid, 'MARKED_DONE', :actor_uid, 'APPROVED', 'DONE', :note, NULL)
                """),
                {"qid": quotation_id, "actor_uid": coordinator_uid, "note": note_val},
            )
        return True, None
    except IntegrityError:
        return False, "That Odoo reference was just used by another quotation."
    except Exception as e:
        return False, str(e)


# ─────────────────────────────────────────────────────────────────────────────
# Shared rendering helpers
# ─────────────────────────────────────────────────────────────────────────────

_REVISION_HEADER_FIELDS = [
    ("request_source", "Request Source"),
    ("quotation_date", "Quotation Date"),
    ("vat_rate", "VAT Rate (%)"),
    ("remarks", "Remarks"),
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
        SELECT ql.line_no, ql.product_id, i.article_number, i.description,
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
        st.write(f"**Request Source:** {_norm(header.get('request_source')) or '—'}")
        st.write(f"**Quotation Date:** {header.get('quotation_date') or '—'}")
        st.write(f"**VAT Rate:** {header.get('vat_rate')}%")
    with col2:
        st.write(f"**Validity:** {header.get('validity_days') or '—'} days")
        st.write(f"**Delivery Terms:** {_norm(header.get('delivery_terms')) or '—'}")
        st.write(f"**Payment Terms:** {_norm(header.get('payment_terms')) or '—'}")
        st.write(f"**Remarks:** {_norm(header.get('remarks')) or '—'}")

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
        st.dataframe(
            display_df[
                ["line_no", "article_number", "description", "quantity", "unit_price", "discount_pct", "line_total"]
            ],
            use_container_width=True,
            hide_index=True,
        )

    st.markdown(
        f"**Subtotal:** {totals['subtotal']} &nbsp;·&nbsp; "
        f"**VAT:** {totals['vat_amount']} &nbsp;·&nbsp; "
        f"**Grand Total:** {totals['grand_total']}"
    )

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
        at_dt = pd.to_datetime(ev.get("at_utc"), errors="coerce")
        at_str = at_dt.strftime("%d %b %Y, %H:%M UTC") if pd.notna(at_dt) else "—"
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
