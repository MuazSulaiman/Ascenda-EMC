# app_pages/quotation_handoff.py
"""Sales-coordinator-facing Quotations page: mark approved quotations as handed off to Odoo."""
import pandas as pd
import streamlit as st

from ui import section_header
from db_ops import query_df, query_scalar
from app_pages.change_request_helpers import _norm
from app_pages.quotation_helpers import (
    coordinator_mark_done, render_quotation_detail, _odoo_reference_exists,
    render_quotation_list, _load_quotation_header,
)


PAGE_NS = "quotation_handoff"
ALLOWED_ROLES = ("sales coordinator", "admin")


def page_quotation_handoff():
    u = st.session_state.get("user")
    role = (u.get("role") or "").lower().strip() if u else ""
    if role not in ALLOWED_ROLES:
        st.error("Access denied.")
        st.stop()

    uid = int(u.get("user_id") or u.get("id"))

    section_header("Quotation Handoff (Odoo)", "Mark approved quotations as entered into Odoo")

    qid_param = st.query_params.get("quotation_id")
    if qid_param:
        _show_quotation_detail(qid_param, uid)
        return

    section = st.radio(
        "Handoff Section", ["Awaiting Handoff", "Done"],
        key=f"{PAGE_NS}_active_section", horizontal=True, label_visibility="collapsed",
    )
    if section == "Awaiting Handoff":
        _render_approved_queue(uid)
    else:
        _render_done_queue(uid)


def _show_quotation_detail(qid_param: str, uid: int) -> None:
    try:
        qid = int(qid_param)
    except (ValueError, TypeError):
        st.error("Invalid quotation ID.")
        return

    if st.button("← Back", key=f"{PAGE_NS}_detail_back"):
        st.query_params.pop("quotation_id", None)
        st.rerun()

    header = _load_quotation_header(qid)
    if not header:
        st.warning("Quotation not found.")
        return

    render_quotation_detail(qid)

    if _norm(header.get("status")) == "APPROVED":
        st.markdown("---")
        _render_mark_done_form(uid, qid)
    # DONE (or anything else) — read-only, no handoff form.


# ─────────────────────────────────────────────────────────────────────────────
# Queue loaders
# ─────────────────────────────────────────────────────────────────────────────

def _load_approved_queue() -> pd.DataFrame:
    return query_df(
        """
        SELECT qr.*, c.account_name, u.name AS rep_name
        FROM quotation_requests qr
        JOIN customers c ON c.customer_id = qr.customer_id
        JOIN users u ON u.user_id = qr.rep_user_id
        WHERE qr.status = 'APPROVED'
        ORDER BY qr.manager_decided_at
        """
    )


def _load_done_queue() -> pd.DataFrame:
    return query_df(
        """
        SELECT qr.*, c.account_name, u.name AS rep_name
        FROM quotation_requests qr
        JOIN customers c ON c.customer_id = qr.customer_id
        JOIN users u ON u.user_id = qr.rep_user_id
        WHERE qr.status = 'DONE'
        ORDER BY qr.coordinator_done_at DESC
        """
    )


# ─────────────────────────────────────────────────────────────────────────────
# "Approved" (awaiting handoff) queue — small working queue, unfiltered
# ─────────────────────────────────────────────────────────────────────────────

def _render_approved_queue(uid: int) -> None:
    df = _load_approved_queue()

    render_quotation_list(
        f"{PAGE_NS}_approved", df,
        page_name="Quotation Handoff (Odoo)",
        status_labels={"APPROVED": "Approved"},
        status_order=["APPROVED"],
        search_text_cols=("quotation_number", "account_name", "rep_name"),
        date_col="manager_decided_at",
        search_placeholder="Search by quotation #, customer, or rep…",
    )


# ─────────────────────────────────────────────────────────────────────────────
# "Done" queue — permanent record, searchable (quotation #, customer, Odoo ref)
# ─────────────────────────────────────────────────────────────────────────────

def _render_done_queue(uid: int) -> None:
    df = _load_done_queue()

    render_quotation_list(
        f"{PAGE_NS}_done", df,
        page_name="Quotation Handoff (Odoo)",
        status_labels={"DONE": "Done"},
        status_order=["DONE"],
        search_text_cols=("quotation_number", "account_name", "odoo_reference"),
        date_col="coordinator_done_at",
        search_placeholder="Search by quotation #, customer, or Odoo reference…",
    )


def _render_mark_done_form(uid: int, qid: int) -> None:
    ns = f"{PAGE_NS}_{qid}"
    st.markdown("**Mark Done**")

    ref_input = st.text_input(
        "Odoo Reference (optional)",
        key=f"{ns}_odoo_ref",
        placeholder="e.g. SO0001234",
    )
    note = st.text_area("Note (optional)", key=f"{ns}_note")

    # Re-derive the conflict check from the *current* widget value on every
    # rerun (not a cached/stale result) so the button's disabled state and
    # the error message always reflect what's in the box right now.
    ref = _norm(ref_input)
    conflict_number = None
    if ref and _odoo_reference_exists(ref):
        conflict_number = query_scalar(
            "SELECT quotation_number FROM quotation_requests WHERE odoo_reference = :ref",
            {"ref": ref},
        )

    if conflict_number is not None:
        st.error(
            f"Odoo reference '{ref}' is already used by quotation "
            f"{_norm(conflict_number) or '(unknown)'}."
        )

    if st.button(
        "Mark Done", type="primary", key=f"{ns}_mark_done_btn", disabled=conflict_number is not None
    ):
        ok, err = coordinator_mark_done(
            qid, coordinator_uid=uid, odoo_reference=ref or None, note=_norm(note) or None
        )
        if ok:
            st.toast("Quotation marked done.", icon="✅")
            st.rerun()
        else:
            st.error(err)
