# app_pages/quotation_handoff.py
"""Sales-coordinator-facing Quotations page: mark approved quotations as handed off to Odoo."""
import pandas as pd
import streamlit as st

from ui import section_header
from db_ops import query_df, query_scalar
from app_pages.change_request_helpers import _norm
from app_pages.quotation_helpers import (
    coordinator_mark_done, render_quotation_detail, _odoo_reference_exists,
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

    _render_approved_queue(uid)


# ─────────────────────────────────────────────────────────────────────────────
# Queue loader
# ─────────────────────────────────────────────────────────────────────────────

def _load_approved_queue() -> pd.DataFrame:
    return query_df(
        "SELECT * FROM quotation_requests WHERE status = 'APPROVED' ORDER BY manager_decided_at"
    )


# ─────────────────────────────────────────────────────────────────────────────
# "Approved" queue
# ─────────────────────────────────────────────────────────────────────────────

def _render_approved_queue(uid: int) -> None:
    df = _load_approved_queue()

    if df.empty:
        st.info("No approved quotations awaiting handoff.")
        return

    st.markdown(f"#### Approved ({len(df)})")
    for _, row in df.iterrows():
        _render_handoff_card(uid, row.to_dict())


def _render_handoff_card(uid: int, header: dict) -> None:
    qid = int(header["quotation_id"])
    ns = f"{PAGE_NS}_{qid}"
    label = f"{_norm(header.get('quotation_number'))} — Approved"

    with st.expander(label, expanded=False):
        render_quotation_detail(qid)

        st.markdown("---")
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
                st.success("Quotation marked done.")
                st.rerun()
            else:
                st.error(err)
