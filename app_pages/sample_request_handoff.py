# app_pages/sample_request_handoff.py
"""Sales-coordinator-facing Sample Requests page: mark approved sample
requests as handed off (fulfilled) and export the warehouse pick sheet.

Mirrors app_pages/quotation_handoff.py's structure, Mark Done form UX, and
live _odoo_reference_exists() re-check-on-every-rerun hard-block pattern
almost exactly, against the sample_requests family of tables instead of
quotation_requests. The one new piece is the "Download Warehouse Pick
Sheet" button, which has no quotation equivalent (quotations render a
print-ready PDF instead) — it is gated the same way
quotation_helpers.render_print_button gates on PDF_PRINTABLE_STATUSES,
adapted to sample_pick_sheet's own APPROVED/DONE status set so a coordinator
can still re-download the sheet after marking a request Done.
"""
import pandas as pd
import streamlit as st

from ui import section_header
from db_ops import query_df, query_scalar
from app_pages.change_request_helpers import _norm
from app_pages.sample_request_helpers import (
    coordinator_mark_done, render_sample_request_detail, render_sample_request_list,
    generate_sample_pick_sheet, _odoo_reference_exists, _load_sample_request_header,
)


PAGE_NS = "sample_request_handoff"
ALLOWED_ROLES = ("sales coordinator", "admin")

# Statuses for which the warehouse pick sheet can be (re-)downloaded — mirrors
# quotation_helpers.PDF_PRINTABLE_STATUSES, adapted to this feature's status set.
PICK_SHEET_STATUSES = ("APPROVED", "DONE")


def page_sample_request_handoff():
    u = st.session_state.get("user")
    role = (u.get("role") or "").lower().strip() if u else ""
    if role not in ALLOWED_ROLES:
        st.error("Access denied.")
        st.stop()

    uid = int(u.get("user_id") or u.get("id"))

    section_header("Sample Request Handoff (Odoo)", "Mark approved sample requests as entered into Odoo")

    sid_param = st.query_params.get("sample_request_id")
    if sid_param:
        _show_sample_request_detail(sid_param, uid)
        return

    section = st.radio(
        "Handoff Section", ["Awaiting Handoff", "Done"],
        key=f"{PAGE_NS}_active_section", horizontal=True, label_visibility="collapsed",
    )
    if section == "Awaiting Handoff":
        _render_approved_queue(uid)
    else:
        _render_done_queue(uid)


def _show_sample_request_detail(sid_param: str, uid: int) -> None:
    try:
        sid = int(sid_param)
    except (ValueError, TypeError):
        st.error("Invalid sample request ID.")
        return

    header = _load_sample_request_header(sid)

    back_col, dl_col = st.columns([5, 1])
    with back_col:
        if st.button("← Back", key=f"{PAGE_NS}_detail_back"):
            st.query_params.pop("sample_request_id", None)
            st.rerun()
    if header:
        with dl_col:
            _render_pick_sheet_button(header, ns=f"{PAGE_NS}_detail_{sid}")

    if not header:
        st.warning("Sample request not found.")
        return

    render_sample_request_detail(sid)

    if _norm(header.get("status")) == "APPROVED":
        st.markdown("---")
        _render_mark_done_form(uid, sid)
    # DONE (or anything else) — read-only, no handoff form.


# ─────────────────────────────────────────────────────────────────────────────
# Warehouse pick-sheet download button
# ─────────────────────────────────────────────────────────────────────────────

def _render_pick_sheet_button(header: dict, ns: str) -> None:
    """
    Download button for the warehouse pick sheet — enabled only when status
    is APPROVED or DONE, disabled (with an explanatory tooltip) otherwise.
    Mirrors quotation_helpers.render_print_button's gating pattern; there is
    no shared helper in sample_request_helpers.py to reuse since this page
    is the only place the pick sheet is offered.
    """
    status_val = _norm(header.get("status"))
    if status_val in PICK_SHEET_STATUSES:
        sheet_bytes = generate_sample_pick_sheet(int(header["sample_request_id"]))
        st.download_button(
            "Download Warehouse Pick Sheet",
            data=sheet_bytes,
            file_name=f"{_norm(header.get('request_number')) or 'sample_request'}_pick_sheet.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"{ns}_pick_sheet_btn",
            use_container_width=True,
        )
    else:
        st.button(
            "Download Warehouse Pick Sheet",
            disabled=True,
            key=f"{ns}_pick_sheet_btn_disabled",
            use_container_width=True,
            help="The pick sheet is available once the sample request is Approved or Done.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Queue loaders
# ─────────────────────────────────────────────────────────────────────────────

def _load_approved_queue() -> pd.DataFrame:
    return query_df(
        """
        SELECT sr.*, c.account_name, u.name AS rep_name
        FROM sample_requests sr
        JOIN customers c ON c.customer_id = sr.customer_id
        JOIN users u ON u.user_id = sr.rep_user_id
        WHERE sr.status = 'APPROVED'
        ORDER BY sr.manager_decided_at
        """
    )


def _load_done_queue() -> pd.DataFrame:
    return query_df(
        """
        SELECT sr.*, c.account_name, u.name AS rep_name
        FROM sample_requests sr
        JOIN customers c ON c.customer_id = sr.customer_id
        JOIN users u ON u.user_id = sr.rep_user_id
        WHERE sr.status = 'DONE'
        ORDER BY sr.coordinator_done_at DESC
        """
    )


# ─────────────────────────────────────────────────────────────────────────────
# "Approved" (awaiting handoff) queue — small working queue, unfiltered
# ─────────────────────────────────────────────────────────────────────────────

def _render_approved_queue(uid: int) -> None:
    df = _load_approved_queue()

    render_sample_request_list(
        f"{PAGE_NS}_approved", df,
        page_name="Sample Request Handoff (Odoo)",
        status_labels={"APPROVED": "Approved"},
        status_order=["APPROVED"],
        search_text_cols=("request_number", "account_name", "rep_name"),
        date_col="manager_decided_at",
        search_placeholder="Search by request #, customer, or rep…",
    )


# ─────────────────────────────────────────────────────────────────────────────
# "Done" queue — permanent record, searchable (request #, customer, Odoo ref)
# ─────────────────────────────────────────────────────────────────────────────

def _render_done_queue(uid: int) -> None:
    df = _load_done_queue()

    render_sample_request_list(
        f"{PAGE_NS}_done", df,
        page_name="Sample Request Handoff (Odoo)",
        status_labels={"DONE": "Done"},
        status_order=["DONE"],
        search_text_cols=("request_number", "account_name", "odoo_reference"),
        date_col="coordinator_done_at",
        search_placeholder="Search by request #, customer, or Odoo reference…",
    )


def _render_mark_done_form(uid: int, sid: int) -> None:
    ns = f"{PAGE_NS}_{sid}"
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
            "SELECT request_number FROM sample_requests WHERE odoo_reference = :ref",
            {"ref": ref},
        )

    if conflict_number is not None:
        st.error(
            f"Odoo reference '{ref}' is already used by sample request "
            f"{_norm(conflict_number) or '(unknown)'}."
        )

    if st.button(
        "Mark Done", type="primary", key=f"{ns}_mark_done_btn", disabled=conflict_number is not None
    ):
        ok, err = coordinator_mark_done(
            sid, coordinator_uid=uid, odoo_reference=ref or None, note=_norm(note) or None
        )
        if ok:
            st.toast("Sample request marked done.", icon="✅")
            st.rerun()
        else:
            st.error(err)
