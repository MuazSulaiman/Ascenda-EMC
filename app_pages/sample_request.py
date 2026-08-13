# app_pages/sample_request.py
"""Rep-facing Sample Requests page: submit new sample requests, track/withdraw/resubmit own ones.

Mirrors app_pages/quotation_request.py's structure exactly, minus all money
math (no unit_price/discount_pct/VAT/totals — sample request lines only
carry product_id / quantity), and with up to 50 line items instead of 14.
Delivery date is a single request-level field (alongside request date), not
per line.
"""
import datetime
import uuid

import pandas as pd
import streamlit as st

from ui import section_header, form_section
from widgets import customer_quick_find_module, customer_cascading_selectors
from db_ops import query_df
from app_pages.admin_targets_db import (
    get_business_units, get_product_categories, get_business_lines, get_articles,
)
from app_pages.change_request_helpers import _norm
from app_pages.sample_request_helpers import (
    submit_sample_request, resubmit_sample_request, withdraw_sample_request,
    render_sample_request_detail, render_sample_request_list,
    _load_sample_request_header, _load_revisions, _load_revision_lines,
)


PAGE_NS = "sample_request"
ALLOWED_ROLES = ("rep", "sales manager", "biomedical manager", "admin")

MAX_LINES = 50

_STATUS_LABELS = {
    "IN_REVIEW": "In Review",
    "EDIT_REQUESTED": "Edit Requested",
    "APPROVED": "Approved",
    "DONE": "Done",
    "REJECTED": "Rejected",
    "WITHDRAWN": "Withdrawn",
}
# Order that surfaces things needing rep action first.
_STATUS_ORDER = ["EDIT_REQUESTED", "IN_REVIEW", "APPROVED", "DONE", "REJECTED", "WITHDRAWN"]


def page_sample_request():
    u = st.session_state.get("user")
    role = (u.get("role") or "").lower().strip() if u else ""
    if role not in ALLOWED_ROLES:
        st.error("Access denied.")
        st.stop()

    section_header("Sample Requests", "Submit and track your sample requests")

    sid_param = st.query_params.get("sample_request_id")
    if sid_param:
        _show_my_sample_request_detail(sid_param, u)
        return

    active_tab = st.radio(
        "Sample Requests Section", ["New Sample Request", "My Sample Requests"],
        key=f"{PAGE_NS}_active_tab", horizontal=True, label_visibility="collapsed",
    )
    if active_tab == "New Sample Request":
        _render_new_sample_request_tab(u)
    else:
        _render_my_sample_requests_tab(u)


def _show_my_sample_request_detail(sid_param: str, u) -> None:
    try:
        sid = int(sid_param)
    except (ValueError, TypeError):
        st.error("Invalid sample request ID.")
        return

    uid = int(u.get("user_id") or u.get("id"))
    role = (u.get("role") or "").lower().strip()
    header = _load_sample_request_header(sid)
    access_ok = bool(header) and (
        int(header.get("rep_user_id") or -1) == uid or role == "admin"
    )

    if st.button("← Back to My Sample Requests", key=f"{PAGE_NS}_detail_back"):
        st.query_params.pop("sample_request_id", None)
        st.rerun()

    if not access_ok:
        st.error("Sample request not found or you don't have permission to view it.")
        return

    _render_my_sample_request_detail_body(u, header)


# ─────────────────────────────────────────────────────────────────────────────
# Customer picker (reuses widgets.py verbatim — no reimplementation)
# ─────────────────────────────────────────────────────────────────────────────

def _customer_keys(ns: str) -> dict:
    return {
        "cid_locked_key": f"{ns}_cid_locked",
        "req_clear_customer_key": f"{ns}_req_clear_cust",
        "req_clear_acct_key": f"{ns}_req_clear_acct",
        "req_set_acct_key": f"{ns}_req_set_acct",
        "acct_set_value_key": f"{ns}_acct_set_val",
        "qf_msg_key": f"{ns}_qf_msg",
        "qf_msg_type_key": f"{ns}_qf_msg_type",
        "KEY_ACCT": f"{ns}/acct_search",
        "KEY_REGION": f"{ns}/region_sel",
        "KEY_CITY": f"{ns}/city_sel",
        "KEY_SECTOR": f"{ns}/sector_sel",
        "KEY_CUST": f"{ns}/cust_sel",
        "KEY_CUSTID": f"{ns}/customer_id_resolved",
    }


def _init_customer_state(keys: dict) -> None:
    st.session_state.setdefault(keys["cid_locked_key"], False)
    st.session_state.setdefault(keys["req_clear_customer_key"], False)
    st.session_state.setdefault(keys["req_clear_acct_key"], False)
    st.session_state.setdefault(keys["req_set_acct_key"], False)
    st.session_state.setdefault(keys["acct_set_value_key"], "")
    st.session_state.setdefault(keys["qf_msg_key"], "")
    st.session_state.setdefault(keys["qf_msg_type_key"], "")


def _render_customer_picker(ns: str):
    """Renders Quick-Find + cascading Region/City/Sector/Customer. Returns resolved customer_id."""
    keys = _customer_keys(ns)
    _init_customer_state(keys)

    customer_quick_find_module(
        page_ns=ns,
        query_df=query_df,
        KEY_ACCT=keys["KEY_ACCT"],
        KEY_REGION=keys["KEY_REGION"],
        KEY_CITY=keys["KEY_CITY"],
        KEY_SECTOR=keys["KEY_SECTOR"],
        KEY_CUST=keys["KEY_CUST"],
        KEY_CUSTID=keys["KEY_CUSTID"],
        cid_locked_key=keys["cid_locked_key"],
        req_clear_customer_key=keys["req_clear_customer_key"],
        req_clear_acct_key=keys["req_clear_acct_key"],
        req_set_acct_key=keys["req_set_acct_key"],
        acct_set_value_key=keys["acct_set_value_key"],
        qf_msg_key=keys["qf_msg_key"],
        qf_msg_type_key=keys["qf_msg_type_key"],
    )
    return customer_cascading_selectors(
        query_df=query_df,
        KEY_REGION=keys["KEY_REGION"],
        KEY_CITY=keys["KEY_CITY"],
        KEY_SECTOR=keys["KEY_SECTOR"],
        KEY_CUST=keys["KEY_CUST"],
        KEY_CUSTID=keys["KEY_CUSTID"],
        cid_locked_key=keys["cid_locked_key"],
        qf_msg_key=keys["qf_msg_key"],
        qf_msg_type_key=keys["qf_msg_type_key"],
        allow_other=False,
    )


def _prefill_customer_state(ns: str, customer_id: int) -> None:
    """Prefill region/city/sector/customer widget state (unlocked) from an existing customer_id."""
    keys = _customer_keys(ns)
    _init_customer_state(keys)
    cust_df = query_df(
        "SELECT account_name, region, city, sector FROM customers WHERE customer_id = :cid",
        {"cid": customer_id},
    )
    if cust_df.empty:
        return
    row = cust_df.iloc[0]
    st.session_state[keys["KEY_REGION"]] = _norm(row.get("region"))
    st.session_state[keys["KEY_CITY"]] = _norm(row.get("city"))
    st.session_state[keys["KEY_SECTOR"]] = _norm(row.get("sector"))
    st.session_state[keys["KEY_CUST"]] = _norm(row.get("account_name"))
    st.session_state[keys["KEY_CUSTID"]] = int(customer_id)
    st.session_state[keys["cid_locked_key"]] = False


# ─────────────────────────────────────────────────────────────────────────────
# Product picker — BU -> Category -> Business Line -> Product cascading loaders
# reused verbatim from app_pages/admin_targets_db.py (get_business_units,
# get_product_categories, get_business_lines, get_articles), same as
# quotation_request.py. No unit_price/discount_pct here — sample requests
# carry no pricing.
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_id(df: pd.DataFrame, name_col: str, id_col: str, choice: str):
    if not choice or df is None or df.empty:
        return None
    match = df.loc[df[name_col] == choice, id_col]
    return int(match.iloc[0]) if not match.empty else None


def _product_label(article_number, description) -> str:
    art = _norm(article_number)
    desc = _norm(description)
    if art and desc:
        return f"{art} — {desc}"
    return art or desc


def _infer_bu_cat_bl_for_product(product_id: str):
    df = query_df(
        """
        SELECT bu.name AS bu_name, pc.name AS cat_name, bl.name AS bl_name,
               i.article_number, i.description
        FROM items i
        JOIN business_lines bl ON bl.business_line_id = i.business_line_id
        JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
        LEFT JOIN product_categories pc ON pc.product_category_id = bl.product_category_id
        WHERE i.product_id = :pid
        """,
        {"pid": product_id},
    )
    if df.empty:
        return None
    row = df.iloc[0]
    return {
        "bu_name": _norm(row.get("bu_name")),
        "cat_name": _norm(row.get("cat_name")),
        "bl_name": _norm(row.get("bl_name")),
        "prod_label": _product_label(row.get("article_number"), row.get("description")),
    }


def _line_row_ids_key(ns: str) -> str:
    return f"{ns}_row_ids"


def _new_row_id() -> str:
    return uuid.uuid4().hex[:8]


def _line_row_keys(ns: str, row_id: str) -> dict:
    return {
        "bu": f"{ns}_row{row_id}_bu",
        "cat": f"{ns}_row{row_id}_cat",
        "bl": f"{ns}_row{row_id}_bl",
        "prod": f"{ns}_row{row_id}_prod",
        "qty": f"{ns}_row{row_id}_qty",
    }


def _clear_line_row_state(ns: str, row_id: str) -> None:
    for key in _line_row_keys(ns, row_id).values():
        st.session_state.pop(key, None)


def _prefill_line_rows(ns: str, lines: list) -> None:
    """Prefill row widget state from prior line dicts (product_id/quantity).

    Builds a fresh list of stable row IDs (one per prior line, at least one),
    then seeds each row's widget-backed session-state keys by that row's own
    ID — never by position, so there is nothing to shift later.
    """
    row_ids = [_new_row_id() for _ in range(len(lines) if lines else 1)]
    st.session_state[_line_row_ids_key(ns)] = row_ids

    for row_id, line in zip(row_ids, lines):
        keys = _line_row_keys(ns, row_id)
        product_id = line.get("product_id")
        infer = _infer_bu_cat_bl_for_product(product_id) if product_id else None
        if infer:
            st.session_state[keys["bu"]] = infer["bu_name"]
            st.session_state[keys["cat"]] = infer["cat_name"]
            st.session_state[keys["bl"]] = infer["bl_name"]
            st.session_state[keys["prod"]] = infer["prod_label"]
        try:
            st.session_state[keys["qty"]] = int(line.get("quantity") or 0)
        except (TypeError, ValueError):
            st.session_state[keys["qty"]] = 0


def _remove_line_row(ns: str, row_id: str) -> None:
    """Remove a single row by its stable ID. Every other row keeps its own
    untouched widget keys — no shifting of values between positional slots,
    so this never writes into a key whose widget was already instantiated
    earlier in this script run."""
    ids_key = _line_row_ids_key(ns)
    row_ids = st.session_state.get(ids_key, [])
    st.session_state[ids_key] = [rid for rid in row_ids if rid != row_id]
    _clear_line_row_state(ns, row_id)


def _render_product_picker_row(ns: str, row_id: str):
    """Renders BU -> Category -> Business Line -> Product cascading selects. Returns product_id or None."""
    keys = _line_row_keys(ns, row_id)

    def _reset_below_bu():
        st.session_state.pop(keys["cat"], None)
        st.session_state.pop(keys["bl"], None)
        st.session_state.pop(keys["prod"], None)

    def _reset_below_cat():
        st.session_state.pop(keys["bl"], None)
        st.session_state.pop(keys["prod"], None)

    def _reset_below_bl():
        st.session_state.pop(keys["prod"], None)

    c1, c2, c3, c4 = st.columns(4)

    # Each level's prefilled session-state value (set by _prefill_line_rows from a prior
    # revision snapshot) may reference a BU/Category/Business Line/Product that has since
    # been deactivated or renamed. get_business_units/get_product_categories/
    # get_business_lines/get_articles all filter WHERE is_active = TRUE, so a stale value
    # would not appear in `options` and Streamlit would raise StreamlitAPIException on that
    # selectbox. Guard every level the same way quotation_request.py does.

    bu_df = get_business_units()
    bu_options = [""] + bu_df["name"].tolist()
    if st.session_state.get(keys["bu"]) not in bu_options:
        st.session_state[keys["bu"]] = ""
    with c1:
        bu_choice = st.selectbox(
            "Business Unit", bu_options, key=keys["bu"], on_change=_reset_below_bu,
        )
    bu_id = _resolve_id(bu_df, "name", "business_unit_id", bu_choice)

    cat_df = get_product_categories(bu_id) if bu_id else pd.DataFrame(columns=["product_category_id", "name"])
    cat_options = [""] + cat_df["name"].tolist()
    if st.session_state.get(keys["cat"]) not in cat_options:
        st.session_state[keys["cat"]] = ""
    with c2:
        cat_choice = st.selectbox(
            "Category", cat_options, key=keys["cat"],
            disabled=not bu_id, on_change=_reset_below_cat,
        )
    cat_id = _resolve_id(cat_df, "name", "product_category_id", cat_choice)

    bl_df = get_business_lines(cat_id) if cat_id else pd.DataFrame(columns=["business_line_id", "name"])
    bl_options = [""] + bl_df["name"].tolist()
    if st.session_state.get(keys["bl"]) not in bl_options:
        st.session_state[keys["bl"]] = ""
    with c3:
        bl_choice = st.selectbox(
            "Business Line", bl_options, key=keys["bl"],
            disabled=not cat_id, on_change=_reset_below_bl,
        )
    bl_id = _resolve_id(bl_df, "name", "business_line_id", bl_choice)

    art_df = get_articles(bl_id) if bl_id else pd.DataFrame(columns=["product_id", "article_number", "description"])
    art_df = art_df.copy()
    if not art_df.empty:
        art_df["label"] = [
            _product_label(r["article_number"], r["description"]) for _, r in art_df.iterrows()
        ]
    prod_options = [""] + (art_df["label"].tolist() if not art_df.empty else [])
    if st.session_state.get(keys["prod"]) not in prod_options:
        st.session_state[keys["prod"]] = ""
    with c4:
        prod_choice = st.selectbox(
            "Product", prod_options,
            key=keys["prod"], disabled=not bl_id,
        )

    if prod_choice and not art_df.empty:
        match = art_df.loc[art_df["label"] == prod_choice, "product_id"]
        return str(match.iloc[0]) if not match.empty else None
    return None


def _render_line_items_editor(ns: str):
    """Dynamic 1-50 row line-items editor, keyed by stable per-row IDs (not
    positional index) so removing a row never has to shift another row's
    already-instantiated widget state. Returns (lines: list[dict], all_valid: bool)."""
    ids_key = _line_row_ids_key(ns)
    st.session_state.setdefault(ids_key, [_new_row_id()])
    row_ids = st.session_state[ids_key]

    lines = []
    rows_valid = True
    seen_products: dict = {}
    duplicate_found = False
    for display_no, row_id in enumerate(row_ids, start=1):
        st.markdown(f"**Line {display_no}**")
        product_id = _render_product_picker_row(ns, row_id)

        if product_id:
            if product_id in seen_products:
                duplicate_found = True
                st.warning(
                    f"⚠️ Same product as Line {seen_products[product_id]} — "
                    f"remove or change one before submitting."
                )
            else:
                seen_products[product_id] = display_no

        keys = _line_row_keys(ns, row_id)

        qty = st.number_input(
            "Quantity", min_value=0, step=1, value=None, placeholder="e.g. 2", key=keys["qty"],
        )
        qty = qty or 0

        row_valid = bool(product_id) and qty > 0
        rows_valid = rows_valid and row_valid
        if row_valid:
            lines.append({
                "product_id": product_id,
                "quantity": int(qty),
            })

        if len(row_ids) > 1:
            if st.button("Remove Line", key=f"{ns}_remove_{row_id}"):
                _remove_line_row(ns, row_id)
                st.rerun()
        st.markdown("---")

    if len(row_ids) < MAX_LINES:
        if st.button("+ Add Line", key=f"{ns}_add_line"):
            st.session_state[ids_key] = row_ids + [_new_row_id()]
            st.rerun()
    else:
        st.caption(f"Maximum of {MAX_LINES} lines reached.")

    all_valid = rows_valid and len(lines) > 0 and not duplicate_found
    return lines, all_valid


def _clear_ns_state(ns: str) -> None:
    for k in list(st.session_state.keys()):
        if k.startswith(f"{ns}_") or k.startswith(f"{ns}/"):
            del st.session_state[k]


# ─────────────────────────────────────────────────────────────────────────────
# "New Sample Request" tab
# ─────────────────────────────────────────────────────────────────────────────

def _render_new_sample_request_tab(u):
    ns = f"{PAGE_NS}_new"
    uid = int(u.get("user_id") or u.get("id"))

    success_msg = st.session_state.pop(f"{ns}_success_msg", None)
    if success_msg:
        st.success(success_msg)

    st.markdown(form_section(1, "Customer", first=True), unsafe_allow_html=True)
    customer_id = _render_customer_picker(ns)

    st.markdown(form_section(2, "Request Details"), unsafe_allow_html=True)
    request_date = st.date_input("Request Date *", value=datetime.date.today(), key=f"{ns}_request_date")
    delivery_date = st.date_input(
        "Delivery Date (optional)", value=None, key=f"{ns}_delivery_date",
    )
    dates_valid = delivery_date is None or delivery_date > request_date
    if not dates_valid:
        st.caption("⚠️ Delivery date must be after the request date.")
    remarks = st.text_area("Remarks", key=f"{ns}_remarks")

    st.markdown(form_section(3, "Line Items"), unsafe_allow_html=True)
    lines, lines_valid = _render_line_items_editor(ns)

    can_submit = bool(customer_id) and lines_valid and dates_valid
    if not (bool(customer_id) and lines_valid):
        st.caption("Select a customer and complete at least one line item to enable submission.")

    if st.button("Submit Sample Request", key=f"{ns}_submit_btn", type="primary", disabled=not can_submit):
        header = {
            "customer_id": customer_id,
            "request_date": request_date,
            "delivery_date": delivery_date,
            "remarks": remarks,
        }
        try:
            sample_request_id, request_number = submit_sample_request(header, lines, actor_uid=uid)
        except Exception as e:
            st.error(f"Submission failed: {e}")
        else:
            _clear_ns_state(ns)
            st.session_state[f"{ns}_success_msg"] = f"Sample request {request_number} submitted for review."
            st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# "My Sample Requests" tab
# ─────────────────────────────────────────────────────────────────────────────

def _render_my_sample_requests_tab(u):
    uid = int(u.get("user_id") or u.get("id"))
    role = (u.get("role") or "").lower().strip()

    if role == "admin":
        df = query_df(
            """
            SELECT sr.*, c.account_name, u.name AS rep_name
            FROM sample_requests sr
            JOIN customers c ON c.customer_id = sr.customer_id
            JOIN users u ON u.user_id = sr.rep_user_id
            ORDER BY sr.submitted_at DESC
            """
        )
    else:
        df = query_df(
            """
            SELECT sr.*, c.account_name, u.name AS rep_name
            FROM sample_requests sr
            JOIN customers c ON c.customer_id = sr.customer_id
            JOIN users u ON u.user_id = sr.rep_user_id
            WHERE sr.rep_user_id = :uid
            ORDER BY sr.submitted_at DESC
            """,
            {"uid": uid},
        )

    render_sample_request_list(
        f"{PAGE_NS}_my_sample_requests", df,
        page_name="Sample Requests",
        status_labels=_STATUS_LABELS,
        status_order=_STATUS_ORDER,
        search_text_cols=("request_number", "account_name"),
        date_col="submitted_at",
        search_placeholder="Search by request # or customer…",
    )


def _render_my_sample_request_detail_body(u, header: dict) -> None:
    uid = int(u.get("user_id") or u.get("id"))
    sid = int(header["sample_request_id"])
    status = _norm(header.get("status"))
    is_owner = int(header.get("rep_user_id")) == uid

    render_sample_request_detail(sid)

    if status == "EDIT_REQUESTED":
        manager_comment = _norm(header.get("manager_comment"))
        if manager_comment:
            st.warning(f"**Manager comment:** {manager_comment}")
        if is_owner:
            st.markdown("---")
            _render_edit_resubmit_section(uid, header)
            _render_withdraw_section(uid, sid)
    elif status == "IN_REVIEW" and is_owner:
        st.markdown("---")
        _render_withdraw_section(uid, sid)
    # APPROVED / DONE / REJECTED / WITHDRAWN — read-only, no action buttons.


def _prefill_resubmit_form(ns: str, sid: int, header: dict) -> None:
    revisions_df = _load_revisions(sid)
    if revisions_df.empty:
        latest = header
        prior_lines = []
    else:
        latest = revisions_df.iloc[-1].to_dict()
        prior_lines = _load_revision_lines(int(latest["revision_id"])).to_dict("records")

    def _parse_date_like(raw, default=None):
        if isinstance(raw, str) and raw:
            try:
                return datetime.date.fromisoformat(raw)
            except ValueError:
                return default
        if isinstance(raw, datetime.datetime):
            return raw.date()
        if isinstance(raw, datetime.date):
            return raw
        if raw is not None and hasattr(raw, "date") and pd.notna(raw):
            return raw.date()
        return default

    st.session_state[f"{ns}_request_date"] = _parse_date_like(
        latest.get("request_date"), default=datetime.date.today()
    )
    st.session_state[f"{ns}_delivery_date"] = _parse_date_like(latest.get("delivery_date"))

    st.session_state[f"{ns}_remarks"] = _norm(latest.get("remarks"))

    customer_id = latest.get("customer_id")
    if customer_id is None or (isinstance(customer_id, float) and pd.isna(customer_id)):
        customer_id = header.get("customer_id")
    if customer_id is not None and not (isinstance(customer_id, float) and pd.isna(customer_id)):
        _prefill_customer_state(ns, int(customer_id))

    _prefill_line_rows(ns, prior_lines)


def _render_edit_resubmit_section(uid: int, header: dict) -> None:
    sid = int(header["sample_request_id"])
    ns = f"{PAGE_NS}_resub_{sid}"
    edit_flag = f"{ns}_editing"
    init_flag = f"{ns}_initialized"

    if not st.session_state.get(edit_flag):
        if st.button("Edit & Resubmit", key=f"{ns}_start_btn"):
            st.session_state[edit_flag] = True
            st.rerun()
        return

    if not st.session_state.get(init_flag):
        _prefill_resubmit_form(ns, sid, header)
        st.session_state[init_flag] = True

    st.markdown("##### Edit & Resubmit")
    customer_id = _render_customer_picker(ns)

    request_date = st.date_input("Request Date *", key=f"{ns}_request_date")
    delivery_date = st.date_input(
        "Delivery Date (optional)", key=f"{ns}_delivery_date",
    )
    dates_valid = delivery_date is None or delivery_date > request_date
    if not dates_valid:
        st.caption("⚠️ Delivery date must be after the request date.")
    remarks = st.text_area("Remarks", key=f"{ns}_remarks")

    lines, lines_valid = _render_line_items_editor(ns)

    can_submit = bool(customer_id) and lines_valid and dates_valid

    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("Resubmit", key=f"{ns}_resubmit_btn", type="primary", disabled=not can_submit):
            new_header = {
                "customer_id": customer_id,
                "request_date": request_date,
                "delivery_date": delivery_date,
                "remarks": remarks,
            }
            ok, err = resubmit_sample_request(
                sid, new_header, lines, actor_uid=uid, expected_version=int(header.get("version") or 0),
            )
            if ok:
                _clear_ns_state(ns)
                st.toast("Sample request resubmitted for review.", icon="✅")
                st.rerun()
            else:
                st.error(err)
    with col_b:
        if st.button("Cancel", key=f"{ns}_cancel_btn"):
            _clear_ns_state(ns)
            st.rerun()


def _render_withdraw_section(uid: int, sid: int) -> None:
    ns = f"{PAGE_NS}_withdraw_{sid}"
    with st.expander("Withdraw Request", expanded=False):
        st.warning(
            "This will permanently withdraw this sample request. It cannot be resubmitted afterward."
        )
        reason = st.text_area("Withdrawal reason (required) *", key=f"{ns}_reason")
        confirm = st.checkbox("I confirm I want to withdraw this sample request", key=f"{ns}_confirm")
        withdraw_enabled = bool((reason or "").strip()) and confirm

        if st.button("Withdraw Request", key=f"{ns}_btn", type="secondary", disabled=not withdraw_enabled):
            ok, err = withdraw_sample_request(sid, rep_uid=uid, reason=(reason or "").strip())
            if ok:
                _clear_ns_state(ns)
                st.toast("Sample request withdrawn.", icon="✅")
                st.rerun()
            else:
                st.error(err)
