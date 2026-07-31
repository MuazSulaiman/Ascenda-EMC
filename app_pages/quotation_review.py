# app_pages/quotation_review.py
"""Sales-manager-facing Quotations page: review pending quotations and manage approved ones."""
import pandas as pd
import streamlit as st

from ui import section_header
from db_ops import query_df
from app_pages.change_request_helpers import _norm
from app_pages.quotation_helpers import (
    manager_approve, manager_reject, manager_request_edit, manager_return_for_revision,
    render_quotation_detail, render_revision_diff, render_quotation_list, render_print_button,
    _load_quotation_header, _load_revisions, _load_revision_lines,
)


PAGE_NS = "quotation_review"
ALLOWED_ROLES = ("sales manager", "admin")

_DECISION_STATUS_LABELS = {
    "EDIT_REQUESTED": "Edit Requested",
    "APPROVED": "Approved",
    "DONE": "Done",
    "REJECTED": "Rejected",
    "WITHDRAWN": "Withdrawn",
}
_DECISION_STATUS_ORDER = ["EDIT_REQUESTED", "APPROVED", "DONE", "REJECTED", "WITHDRAWN"]


def page_quotation_review():
    u = st.session_state.get("user")
    role = (u.get("role") or "").lower().strip() if u else ""
    if role not in ALLOWED_ROLES:
        st.error("Access denied.")
        st.stop()

    uid = int(u.get("user_id") or u.get("id"))

    section_header("Review Quotations", "Approve, reject, or request edits on submitted quotations")

    qid_param = st.query_params.get("quotation_id")
    if qid_param:
        _show_quotation_detail(qid_param, uid, role)
        return

    active_tab = st.radio(
        "Review Section", ["Pending Review", "Approved", "My Decisions"],
        key=f"{PAGE_NS}_active_tab", horizontal=True, label_visibility="collapsed",
    )
    if active_tab == "Pending Review":
        _render_pending_tab(uid, role)
    elif active_tab == "Approved":
        _render_approved_tab(uid, role)
    else:
        _render_my_decisions_tab(uid)


def _show_quotation_detail(qid_param: str, uid: int, role: str) -> None:
    try:
        qid = int(qid_param)
    except (ValueError, TypeError):
        st.error("Invalid quotation ID.")
        return

    header = _load_quotation_header(qid)

    back_col, print_col = st.columns([5, 1])
    with back_col:
        if st.button("← Back", key=f"{PAGE_NS}_detail_back"):
            st.query_params.pop("quotation_id", None)
            st.rerun()
    if header:
        with print_col:
            render_print_button(header, ns=f"{PAGE_NS}_detail_{qid}")

    if not header:
        st.warning("Quotation not found.")
        return

    status = _norm(header.get("status"))
    is_self = role != "admin" and int(header.get("rep_user_id") or -1) == uid

    render_quotation_detail(qid)
    _render_revision_comparison(qid)

    st.markdown("---")
    if status == "IN_REVIEW":
        if is_self:
            st.info("You submitted this quotation — another manager or admin needs to review it.")
        else:
            _render_pending_actions(uid, qid)
    elif status == "APPROVED":
        if is_self:
            st.info("You submitted this quotation — another manager or admin can return it for revision.")
        else:
            _render_return_for_revision_section(uid, qid)
    # REJECTED / EDIT_REQUESTED / DONE / WITHDRAWN — read-only, no manager actions here.


# ─────────────────────────────────────────────────────────────────────────────
# Queue loaders
# ─────────────────────────────────────────────────────────────────────────────

def _load_pending_queue(uid: int, role: str) -> pd.DataFrame:
    base = """
        SELECT qr.*, c.account_name, u.name AS rep_name
        FROM quotation_requests qr
        JOIN customers c ON c.customer_id = qr.customer_id
        JOIN users u ON u.user_id = qr.rep_user_id
        WHERE qr.status = 'IN_REVIEW' {extra}
        ORDER BY qr.submitted_at
    """
    if role == "admin":
        return query_df(base.format(extra=""))
    return query_df(base.format(extra="AND qr.rep_user_id != :uid"), {"uid": uid})


def _load_approved_queue(uid: int, role: str) -> pd.DataFrame:
    base = """
        SELECT qr.*, c.account_name, u.name AS rep_name
        FROM quotation_requests qr
        JOIN customers c ON c.customer_id = qr.customer_id
        JOIN users u ON u.user_id = qr.rep_user_id
        WHERE qr.status = 'APPROVED' {extra}
        ORDER BY qr.manager_decided_at
    """
    if role == "admin":
        return query_df(base.format(extra=""))
    return query_df(base.format(extra="AND qr.rep_user_id != :uid"), {"uid": uid})


def _load_my_decisions_queue(uid: int) -> pd.DataFrame:
    return query_df(
        """
        SELECT qr.*, c.account_name, u.name AS rep_name
        FROM quotation_requests qr
        JOIN customers c ON c.customer_id = qr.customer_id
        JOIN users u ON u.user_id = qr.rep_user_id
        WHERE qr.manager_user_id = :uid
          AND qr.status IN ('APPROVED', 'REJECTED', 'EDIT_REQUESTED', 'DONE', 'WITHDRAWN')
        ORDER BY qr.manager_decided_at DESC
        """,
        {"uid": uid},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Shared: revision comparison
# ─────────────────────────────────────────────────────────────────────────────

def _render_revision_comparison(qid: int) -> None:
    revisions_df = _load_revisions(qid)
    if len(revisions_df) < 2:
        return
    st.markdown("##### Revision Comparison (latest two)")
    rev_a = revisions_df.iloc[-2].to_dict()
    rev_b = revisions_df.iloc[-1].to_dict()
    rev_a["lines"] = _load_revision_lines(int(rev_a["revision_id"])).to_dict("records")
    rev_b["lines"] = _load_revision_lines(int(rev_b["revision_id"])).to_dict("records")
    render_revision_diff(rev_a, rev_b)


# ─────────────────────────────────────────────────────────────────────────────
# "Pending Review" tab
# ─────────────────────────────────────────────────────────────────────────────

def _render_pending_tab(uid: int, role: str) -> None:
    df = _load_pending_queue(uid, role)

    render_quotation_list(
        f"{PAGE_NS}_pending", df,
        page_name="Review Quotations",
        status_labels={"IN_REVIEW": "In Review"},
        status_order=["IN_REVIEW"],
        search_text_cols=("quotation_number", "account_name", "rep_name"),
        date_col="submitted_at",
        search_placeholder="Search by quotation #, customer, or rep…",
    )


def _render_pending_actions(uid: int, qid: int) -> None:
    ns = f"{PAGE_NS}_pending_{qid}"
    col_approve, col_reject, col_edit = st.columns(3)

    with col_approve:
        st.markdown("**Approve**")
        if st.button("Approve", type="primary", key=f"{ns}_approve_btn"):
            ok, err = manager_approve(qid, manager_uid=uid)
            if ok:
                st.toast("Quotation approved.", icon="✅")
                st.rerun()
            else:
                st.error(err)

    with col_reject:
        st.markdown("**Reject**")
        reject_reason = st.text_area(
            "Rejection reason (required) *",
            key=f"{ns}_reject_reason",
            placeholder="Explain why this quotation is rejected.",
        )
        if st.button("Reject", type="secondary", key=f"{ns}_reject_btn"):
            if not (reject_reason or "").strip():
                st.error("A rejection reason is required.")
            else:
                ok, err = manager_reject(qid, manager_uid=uid, reason=reject_reason.strip())
                if ok:
                    st.toast("Quotation rejected.", icon="✅")
                    st.rerun()
                else:
                    st.error(err)

    with col_edit:
        st.markdown("**Request Edit**")
        edit_comment = st.text_area(
            "Comment (required) *",
            key=f"{ns}_edit_comment",
            placeholder="Explain what needs to change.",
        )
        if st.button("Request Edit", type="secondary", key=f"{ns}_edit_btn"):
            if not (edit_comment or "").strip():
                st.error("A comment explaining the requested edit is required.")
            else:
                ok, err = manager_request_edit(qid, manager_uid=uid, comment=edit_comment.strip())
                if ok:
                    st.toast("Edit requested.", icon="✅")
                    st.rerun()
                else:
                    st.error(err)


# ─────────────────────────────────────────────────────────────────────────────
# "Approved" tab
# ─────────────────────────────────────────────────────────────────────────────

def _render_approved_tab(uid: int, role: str) -> None:
    df = _load_approved_queue(uid, role)

    render_quotation_list(
        f"{PAGE_NS}_approved", df,
        page_name="Review Quotations",
        status_labels={"APPROVED": "Approved"},
        status_order=["APPROVED"],
        search_text_cols=("quotation_number", "account_name", "rep_name"),
        date_col="manager_decided_at",
        search_placeholder="Search by quotation #, customer, or rep…",
    )


def _render_return_for_revision_section(uid: int, qid: int) -> None:
    ns = f"{PAGE_NS}_return_{qid}"
    with st.expander("Return to Rep for Revision", expanded=False):
        st.warning(
            "This will send the quotation back to the rep for revision. It will re-enter "
            "the review queue once the rep resubmits it."
        )
        reason = st.text_area("Reason for return (required) *", key=f"{ns}_reason")
        confirm = st.checkbox("I confirm I want to return this quotation for revision", key=f"{ns}_confirm")
        return_enabled = bool((reason or "").strip()) and confirm

        if st.button(
            "Return to Rep for Revision", key=f"{ns}_btn", type="secondary", disabled=not return_enabled
        ):
            ok, err = manager_return_for_revision(qid, manager_uid=uid, reason=(reason or "").strip())
            if ok:
                st.toast("Quotation returned to rep for revision.", icon="✅")
                st.rerun()
            else:
                st.error(err)


# ─────────────────────────────────────────────────────────────────────────────
# "My Decisions" tab — read-only history of quotations this manager decided on
# ─────────────────────────────────────────────────────────────────────────────

def _render_my_decisions_tab(uid: int) -> None:
    df = _load_my_decisions_queue(uid)

    render_quotation_list(
        f"{PAGE_NS}_decisions", df,
        page_name="Review Quotations",
        status_labels=_DECISION_STATUS_LABELS,
        status_order=_DECISION_STATUS_ORDER,
        search_text_cols=("quotation_number", "account_name", "rep_name"),
        date_col="manager_decided_at",
        search_placeholder="Search by quotation #, customer, or rep…",
    )
