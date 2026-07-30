# app_pages/quotation_review.py
"""Sales-manager-facing Quotations page: review pending quotations and manage approved ones."""
import pandas as pd
import streamlit as st

from ui import section_header, status_badge
from db_ops import query_df
from app_pages.change_request_helpers import _norm
from app_pages.quotation_helpers import (
    manager_approve, manager_reject, manager_request_edit, manager_return_for_revision,
    render_quotation_detail, render_revision_diff,
    _load_revisions, _load_revision_lines,
)


PAGE_NS = "quotation_review"
ALLOWED_ROLES = ("sales manager", "admin")


def page_quotation_review():
    u = st.session_state.get("user")
    role = (u.get("role") or "").lower().strip() if u else ""
    if role not in ALLOWED_ROLES:
        st.error("Access denied.")
        st.stop()

    uid = int(u.get("user_id") or u.get("id"))

    section_header("Review Quotations", "Approve, reject, or request edits on submitted quotations")

    active_tab = st.radio(
        "Review Section", ["Pending Review", "Approved"],
        key=f"{PAGE_NS}_active_tab", horizontal=True, label_visibility="collapsed",
    )
    if active_tab == "Pending Review":
        _render_pending_tab(uid, role)
    else:
        _render_approved_tab(uid, role)


# ─────────────────────────────────────────────────────────────────────────────
# Queue loaders
# ─────────────────────────────────────────────────────────────────────────────

def _load_pending_queue(uid: int, role: str) -> pd.DataFrame:
    if role == "admin":
        return query_df(
            "SELECT * FROM quotation_requests WHERE status = 'IN_REVIEW' ORDER BY submitted_at"
        )
    return query_df(
        """
        SELECT * FROM quotation_requests
        WHERE status = 'IN_REVIEW' AND rep_user_id != :uid
        ORDER BY submitted_at
        """,
        {"uid": uid},
    )


def _load_approved_queue(uid: int, role: str) -> pd.DataFrame:
    if role == "admin":
        return query_df(
            "SELECT * FROM quotation_requests WHERE status = 'APPROVED' ORDER BY manager_decided_at"
        )
    return query_df(
        """
        SELECT * FROM quotation_requests
        WHERE status = 'APPROVED' AND rep_user_id != :uid
        ORDER BY manager_decided_at
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

    if df.empty:
        st.info("No quotations pending review.")
        return

    count = len(df)
    st.markdown(
        status_badge(f"{count} quotation{'s' if count != 1 else ''} pending review", "warning"),
        unsafe_allow_html=True,
    )
    st.markdown("")

    for _, row in df.iterrows():
        _render_pending_card(uid, row.to_dict())


def _render_pending_card(uid: int, header: dict) -> None:
    qid = int(header["quotation_id"])
    ns = f"{PAGE_NS}_pending_{qid}"
    label = f"{_norm(header.get('quotation_number'))} — In Review"

    with st.expander(label, expanded=False):
        render_quotation_detail(qid)
        _render_revision_comparison(qid)

        st.markdown("---")
        col_approve, col_reject, col_edit = st.columns(3)

        with col_approve:
            st.markdown("**Approve**")
            if st.button("Approve", type="primary", key=f"{ns}_approve_btn"):
                ok, err = manager_approve(qid, manager_uid=uid)
                if ok:
                    st.success("Quotation approved.")
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
                        st.success("Quotation rejected.")
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
                        st.success("Edit requested.")
                        st.rerun()
                    else:
                        st.error(err)


# ─────────────────────────────────────────────────────────────────────────────
# "Approved" tab
# ─────────────────────────────────────────────────────────────────────────────

def _render_approved_tab(uid: int, role: str) -> None:
    df = _load_approved_queue(uid, role)

    if df.empty:
        st.info("No approved quotations.")
        return

    st.markdown(f"#### Approved ({len(df)})")
    for _, row in df.iterrows():
        _render_approved_card(uid, row.to_dict())


def _render_approved_card(uid: int, header: dict) -> None:
    qid = int(header["quotation_id"])
    label = f"{_norm(header.get('quotation_number'))} — Approved"

    with st.expander(label, expanded=False):
        render_quotation_detail(qid)
        _render_revision_comparison(qid)

        st.markdown("---")
        _render_return_for_revision_section(uid, qid)


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
                st.success("Quotation returned to rep for revision.")
                st.rerun()
            else:
                st.error(err)
