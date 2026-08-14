# app_pages/sample_request_review.py
"""Sales-manager-facing Sample Requests page: review pending sample requests
and manage approved ones.

Mirrors app_pages/quotation_review.py's pending-queue/approved-queue
structure, self-review-block SQL condition, and approve/reject/request-edit
button idiom almost exactly (dropping the "My Decisions" tab, which is not
called for in this feature's brief), against the sample_requests family of
tables instead of quotation_requests. The one genuinely new piece is the
manager decision-support **insights panel** rendered for every pending
request, before the decision buttons: two side-by-side columns built from
Task 3's get_rep_sample_stats/get_customer_sample_stats aggregates, so a
manager can see how many samples this rep and this customer have already
received (all-time / this month) before deciding on the request in front of
them.
"""
import pandas as pd
import streamlit as st

from ui import section_header, guarded_button
from db_ops import query_df
from utils import to_local_str
from app_pages.change_request_helpers import _norm
from app_pages.sample_request_helpers import (
    manager_approve, manager_reject, manager_request_edit, manager_return_for_revision,
    render_sample_request_detail, render_sample_request_list,
    get_rep_sample_stats, get_customer_sample_stats,
    _load_sample_request_header,
)


PAGE_NS = "sample_request_review"
ALLOWED_ROLES = ("sales manager", "admin")


def page_sample_request_review():
    u = st.session_state.get("user")
    role = (u.get("role") or "").lower().strip() if u else ""
    if role not in ALLOWED_ROLES:
        st.error("Access denied.")
        st.stop()

    uid = int(u.get("user_id") or u.get("id"))

    section_header("Review Sample Requests", "Approve, reject, or request edits on submitted sample requests")

    sid_param = st.query_params.get("sample_request_id")
    if sid_param:
        _show_sample_request_detail(sid_param, uid, role)
        return

    active_tab = st.radio(
        "Review Section", ["Pending Review", "Approved"],
        key=f"{PAGE_NS}_active_tab", horizontal=True, label_visibility="collapsed",
    )
    if active_tab == "Pending Review":
        _render_pending_tab(uid, role)
    else:
        _render_approved_tab(uid, role)


def _show_sample_request_detail(sid_param: str, uid: int, role: str) -> None:
    try:
        sid = int(sid_param)
    except (ValueError, TypeError):
        st.error("Invalid sample request ID.")
        return

    header = _load_sample_request_header(sid)

    if st.button("← Back", key=f"{PAGE_NS}_detail_back"):
        st.query_params.pop("sample_request_id", None)
        st.rerun()

    if not header:
        st.warning("Sample request not found.")
        return

    status = _norm(header.get("status"))
    is_self = role != "admin" and int(header.get("rep_user_id") or -1) == uid

    render_sample_request_detail(sid)

    st.markdown("---")
    if status == "IN_REVIEW":
        if is_self:
            st.info("You submitted this sample request — another manager or admin needs to review it.")
        else:
            _render_insights_panel(header)
            _render_pending_actions(uid, sid)
    elif status == "APPROVED":
        if is_self:
            st.info("You submitted this sample request — another manager or admin can return it for revision.")
        else:
            _render_return_for_revision_section(uid, sid)
    # REJECTED / EDIT_REQUESTED / DONE / WITHDRAWN — read-only, no manager actions here.


# ─────────────────────────────────────────────────────────────────────────────
# Queue loaders
# ─────────────────────────────────────────────────────────────────────────────

def _load_pending_queue(uid: int, role: str) -> pd.DataFrame:
    base = """
        SELECT sr.*, c.account_name, u.name AS rep_name
        FROM sample_requests sr
        JOIN customers c ON c.customer_id = sr.customer_id
        JOIN users u ON u.user_id = sr.rep_user_id
        WHERE sr.status = 'IN_REVIEW' {extra}
        ORDER BY sr.submitted_at
    """
    if role == "admin":
        return query_df(base.format(extra=""))
    return query_df(base.format(extra="AND sr.rep_user_id != :uid"), {"uid": uid})


def _load_approved_queue(uid: int, role: str) -> pd.DataFrame:
    base = """
        SELECT sr.*, c.account_name, u.name AS rep_name
        FROM sample_requests sr
        JOIN customers c ON c.customer_id = sr.customer_id
        JOIN users u ON u.user_id = sr.rep_user_id
        WHERE sr.status = 'APPROVED' {extra}
        ORDER BY sr.manager_decided_at
    """
    if role == "admin":
        return query_df(base.format(extra=""))
    return query_df(base.format(extra="AND sr.rep_user_id != :uid"), {"uid": uid})


# ─────────────────────────────────────────────────────────────────────────────
# "Pending Review" tab
# ─────────────────────────────────────────────────────────────────────────────

def _render_pending_tab(uid: int, role: str) -> None:
    df = _load_pending_queue(uid, role)

    render_sample_request_list(
        f"{PAGE_NS}_pending", df,
        page_name="Review Sample Requests",
        status_labels={"IN_REVIEW": "In Review"},
        status_order=["IN_REVIEW"],
        search_text_cols=("request_number", "account_name", "rep_name"),
        date_col="submitted_at",
        search_placeholder="Search by request #, customer, or rep…",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Manager decision-support insights panel
# ─────────────────────────────────────────────────────────────────────────────

def _render_recent_table(recent: list, *, other_col: str, other_label: str) -> None:
    """Small drill-down table for one side of the insights panel. `other_col`
    is "customer_name" for the This-Rep column (each of a rep's recent DONE
    requests went to some customer) and "rep_name" for the This-Customer
    column (each of a customer's recent DONE requests was handled by some
    rep) — mirrors the extra field get_customer_sample_stats adds per the
    Task 3 docstring."""
    if not recent:
        st.caption("No fulfilled sample requests yet.")
        return

    rows = []
    for r in recent:
        rows.append({
            "Request #": r.get("request_number"),
            "Date": to_local_str(r.get("coordinator_done_at"), fmt="%d %b %Y"),
            other_label: r.get(other_col) or "—",
            "Items": r.get("item_summary") or "—",
            "Qty": r.get("total_qty"),
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _render_insights_panel(header: dict) -> None:
    st.markdown("##### Manager Insights")
    col_rep, col_cust = st.columns(2)

    with col_rep:
        st.markdown("**This Rep**")
        rep_stats = get_rep_sample_stats(int(header["rep_user_id"]))
        st.metric("All-Time Units", rep_stats["all_time_qty"])
        st.metric("This Month", rep_stats["this_month_qty"])
        with st.expander("Recent Requests"):
            _render_recent_table(rep_stats["recent"], other_col="customer_name", other_label="Customer")

    with col_cust:
        st.markdown("**This Customer**")
        cust_stats = get_customer_sample_stats(int(header["customer_id"]))
        st.metric("All-Time Units", cust_stats["all_time_qty"])
        st.metric("This Month", cust_stats["this_month_qty"])
        with st.expander("Recent Requests"):
            _render_recent_table(cust_stats["recent"], other_col="rep_name", other_label="Rep")

    st.markdown("---")


def _render_pending_actions(uid: int, sid: int) -> None:
    ns = f"{PAGE_NS}_pending_{sid}"
    col_approve, col_reject, col_edit = st.columns(3)

    with col_approve:
        st.markdown("**Approve**")
        approve_ns = f"{ns}_approve"
        if guarded_button("Approve", approve_ns, type="primary"):
            ok, err = manager_approve(sid, manager_uid=uid)
            st.session_state[f"_{approve_ns}_busy"] = False
            if ok:
                st.toast("Sample request approved.", icon="✅")
                st.rerun()
            else:
                st.error(err)

    with col_reject:
        st.markdown("**Reject**")
        reject_reason = st.text_area(
            "Rejection reason (required) *",
            key=f"{ns}_reject_reason",
            placeholder="Explain why this sample request is rejected.",
        )
        reject_ns = f"{ns}_reject"
        if guarded_button("Reject", reject_ns, type="secondary"):
            if not (reject_reason or "").strip():
                st.error("A rejection reason is required.")
                st.session_state[f"_{reject_ns}_busy"] = False
            else:
                ok, err = manager_reject(sid, manager_uid=uid, reason=reject_reason.strip())
                st.session_state[f"_{reject_ns}_busy"] = False
                if ok:
                    st.toast("Sample request rejected.", icon="✅")
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
        edit_ns = f"{ns}_edit"
        if guarded_button("Request Edit", edit_ns, type="secondary"):
            if not (edit_comment or "").strip():
                st.error("A comment explaining the requested edit is required.")
                st.session_state[f"_{edit_ns}_busy"] = False
            else:
                ok, err = manager_request_edit(sid, manager_uid=uid, comment=edit_comment.strip())
                st.session_state[f"_{edit_ns}_busy"] = False
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

    render_sample_request_list(
        f"{PAGE_NS}_approved", df,
        page_name="Review Sample Requests",
        status_labels={"APPROVED": "Approved"},
        status_order=["APPROVED"],
        search_text_cols=("request_number", "account_name", "rep_name"),
        date_col="manager_decided_at",
        search_placeholder="Search by request #, customer, or rep…",
    )


def _render_return_for_revision_section(uid: int, sid: int) -> None:
    ns = f"{PAGE_NS}_return_{sid}"
    with st.expander("Return to Rep for Revision", expanded=False):
        st.warning(
            "This will send the sample request back to the rep for revision. It will re-enter "
            "the review queue once the rep resubmits it."
        )
        reason = st.text_area("Reason for return (required) *", key=f"{ns}_reason")
        confirm = st.checkbox("I confirm I want to return this sample request for revision", key=f"{ns}_confirm")
        return_enabled = bool((reason or "").strip()) and confirm

        return_ns = f"{ns}_return"
        if guarded_button(
            "Return to Rep for Revision", return_ns, type="secondary", disabled=not return_enabled
        ):
            ok, err = manager_return_for_revision(sid, manager_uid=uid, reason=(reason or "").strip())
            st.session_state[f"_{return_ns}_busy"] = False
            if ok:
                st.toast("Sample request returned to rep for revision.", icon="✅")
                st.rerun()
            else:
                st.error(err)
