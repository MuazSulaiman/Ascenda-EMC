# app_pages/quotation_request.py
"""Rep-facing Quotations page: submit new quotations, track/withdraw/resubmit own ones."""
import datetime
import uuid
from decimal import Decimal

import pandas as pd
import streamlit as st

from ui import section_header, form_section
from widgets import customer_quick_find_module, customer_cascading_selectors
from db_ops import query_df
from app_pages.admin_targets_db import (
    get_business_units, get_product_categories, get_business_lines, get_articles,
)
from app_pages.change_request_helpers import _norm
from app_pages.quotation_helpers import (
    VALIDITY_OPTIONS, DELIVERY_OPTIONS, PAYMENT_TERMS_OPTIONS,
    compute_line_total, compute_header_totals,
    submit_quotation, resubmit_quotation, withdraw_quotation,
    render_quotation_detail, render_revision_diff,
    render_quotation_list,
    _load_quotation_header, _load_revisions, _load_revision_lines,
)


PAGE_NS = "quotation_request"
MAX_LINES = 14
ALLOWED_ROLES = ("rep", "sales manager", "biomedical manager", "admin")

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

_ZERO_TOTALS = {"subtotal": Decimal("0.00"), "vat_amount": Decimal("0.00"), "grand_total": Decimal("0.00")}


def page_quotation_request():
    u = st.session_state.get("user")
    role = (u.get("role") or "").lower().strip() if u else ""
    if role not in ALLOWED_ROLES:
        st.error("Access denied.")
        st.stop()

    section_header("Quotations", "Submit and track your quotation requests")

    qid_param = st.query_params.get("quotation_id")
    if qid_param:
        _show_my_quotation_detail(qid_param, u)
        return

    active_tab = st.radio(
        "Quotations Section", ["New Quotation", "My Quotations"],
        key=f"{PAGE_NS}_active_tab", horizontal=True, label_visibility="collapsed",
    )
    if active_tab == "New Quotation":
        _render_new_quotation_tab(u)
    else:
        _render_my_quotations_tab(u)


def _show_my_quotation_detail(qid_param: str, u) -> None:
    try:
        qid = int(qid_param)
    except (ValueError, TypeError):
        st.error("Invalid quotation ID.")
        return

    if st.button("← Back to My Quotations", key=f"{PAGE_NS}_detail_back"):
        st.query_params.pop("quotation_id", None)
        st.rerun()

    uid = int(u.get("user_id") or u.get("id"))
    header = _load_quotation_header(qid)
    if not header or int(header.get("rep_user_id") or -1) != uid:
        st.error("Quotation not found or you don't have permission to view it.")
        return

    _render_my_quotation_detail_body(u, header)


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
# get_product_categories, get_business_lines, get_articles). Only the
# product_id -> BU/Category/Business Line inference (needed to prefill the
# cascade when editing an existing line) is new: no such by-product lookup
# existed elsewhere, so a small fresh query is written below, following the
# same query_df/style used throughout admin_targets_db.py.
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
        "price": f"{ns}_row{row_id}_price",
        "disc": f"{ns}_row{row_id}_disc",
    }


def _clear_line_row_state(ns: str, row_id: str) -> None:
    for key in _line_row_keys(ns, row_id).values():
        st.session_state.pop(key, None)


def _prefill_line_rows(ns: str, lines: list) -> None:
    """Prefill row widget state from prior line dicts (product_id/quantity/unit_price/discount_pct).

    Builds a fresh list of stable row IDs (one per prior line, at least one),
    then seeds each row's widget-backed session-state keys by that row's own
    ID — never by position, so there is nothing to shift later.
    """
    row_ids = [_new_row_id() for _ in range(max(1, min(MAX_LINES, len(lines))) if lines else 1)]
    st.session_state[_line_row_ids_key(ns)] = row_ids

    for row_id, line in zip(row_ids, lines[:MAX_LINES]):
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
        try:
            st.session_state[keys["price"]] = float(line.get("unit_price") or 0)
        except (TypeError, ValueError):
            st.session_state[keys["price"]] = 0.0
        try:
            st.session_state[keys["disc"]] = float(line.get("discount_pct") or 0)
        except (TypeError, ValueError):
            st.session_state[keys["disc"]] = 0.0


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
    # selectbox. Guard every level the same way admin_change_requests.py does for
    # cat_sel/bl_sel/prod_sel (~lines 768-787): reset to "" if the current session-state
    # value isn't among the live options, before the widget is instantiated.

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
    """Dynamic 1-14 row line-items editor, keyed by stable per-row IDs (not
    positional index) so removing a row never has to shift another row's
    already-instantiated widget state. Returns (lines: list[dict], all_valid: bool)."""
    ids_key = _line_row_ids_key(ns)
    st.session_state.setdefault(ids_key, [_new_row_id()])
    row_ids = st.session_state[ids_key]

    lines = []
    rows_valid = True
    for display_no, row_id in enumerate(row_ids, start=1):
        st.markdown(f"**Line {display_no}**")
        product_id = _render_product_picker_row(ns, row_id)
        keys = _line_row_keys(ns, row_id)

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            qty = st.number_input(
                "Quantity", min_value=0, step=1, key=keys["qty"],
            )
        with c2:
            price = st.number_input(
                "Unit Price", min_value=0.0, step=0.01, format="%.2f", key=keys["price"],
            )
        with c3:
            disc = st.number_input(
                "Discount %", min_value=0.0, max_value=100.0, step=0.5, format="%.2f", key=keys["disc"],
            )
        with c4:
            if product_id and qty > 0:
                line_total = compute_line_total(Decimal(str(qty)), Decimal(str(price)), Decimal(str(disc)))
                st.metric("Line Total", f"{line_total}")
            else:
                st.metric("Line Total", "—")

        row_valid = bool(product_id) and qty > 0
        rows_valid = rows_valid and row_valid
        if row_valid:
            lines.append({
                "product_id": product_id,
                "quantity": Decimal(str(qty)),
                "unit_price": Decimal(str(price)),
                "discount_pct": Decimal(str(disc)),
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
        st.caption(f"Maximum {MAX_LINES} lines reached.")

    all_valid = rows_valid and len(lines) > 0
    return lines, all_valid


def _clear_ns_state(ns: str) -> None:
    for k in list(st.session_state.keys()):
        if k.startswith(f"{ns}_") or k.startswith(f"{ns}/"):
            del st.session_state[k]


def _render_totals_preview(lines: list, vat_rate: Decimal) -> None:
    totals = compute_header_totals(lines, vat_rate) if lines else _ZERO_TOTALS
    st.markdown(
        f"**Subtotal:** {totals['subtotal']} &nbsp;·&nbsp; "
        f"**VAT:** {totals['vat_amount']} &nbsp;·&nbsp; "
        f"**Grand Total:** {totals['grand_total']}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# "New Quotation" tab
# ─────────────────────────────────────────────────────────────────────────────

def _render_new_quotation_tab(u):
    ns = f"{PAGE_NS}_new"
    uid = int(u.get("user_id") or u.get("id"))

    success_msg = st.session_state.pop(f"{ns}_success_msg", None)
    if success_msg:
        st.success(success_msg)

    st.markdown(form_section(1, "Customer", first=True), unsafe_allow_html=True)
    customer_id = _render_customer_picker(ns)

    st.markdown(form_section(2, "Quotation Details"), unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    with c1:
        quotation_date = st.date_input("Quotation Date *", value=datetime.date.today(), key=f"{ns}_quotation_date")
        vat_rate = st.number_input(
            "VAT Rate (%) *", min_value=0.0, max_value=100.0, value=0.0, step=0.5,
            format="%.2f", key=f"{ns}_vat_rate",
        )
    with c2:
        validity_choice = st.selectbox(
            "Validity (days) *", [str(v) for v in VALIDITY_OPTIONS], key=f"{ns}_validity",
        )
        delivery_terms = st.selectbox("Delivery Terms *", DELIVERY_OPTIONS, key=f"{ns}_delivery")
        payment_terms = st.selectbox("Payment Terms *", PAYMENT_TERMS_OPTIONS, key=f"{ns}_payment")
    remarks = st.text_area("Remarks", key=f"{ns}_remarks")

    st.markdown(form_section(3, "Line Items"), unsafe_allow_html=True)
    lines, lines_valid = _render_line_items_editor(ns)

    st.markdown(form_section(4, "Totals"), unsafe_allow_html=True)
    vat_rate_dec = Decimal(str(vat_rate))
    _render_totals_preview(lines, vat_rate_dec)

    can_submit = (
        bool(customer_id) and lines_valid
        and bool(validity_choice) and bool(delivery_terms) and bool(payment_terms)
    )
    if not can_submit:
        st.caption("Select a customer and complete at least one line item to enable submission.")

    if st.button("Submit Quotation", key=f"{ns}_submit_btn", type="primary", disabled=not can_submit):
        header = {
            "customer_id": customer_id,
            "quotation_date": quotation_date,
            "vat_rate": vat_rate_dec,
            "remarks": remarks,
            "validity_days": int(validity_choice),
            "delivery_terms": delivery_terms,
            "payment_terms": payment_terms,
        }
        try:
            quotation_id, quotation_number = submit_quotation(header, lines, actor_uid=uid)
        except Exception as e:
            st.error(f"Submission failed: {e}")
        else:
            _clear_ns_state(ns)
            st.session_state[f"{ns}_success_msg"] = f"Quotation {quotation_number} submitted for review."
            st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# "My Quotations" tab
# ─────────────────────────────────────────────────────────────────────────────

def _render_my_quotations_tab(u):
    uid = int(u.get("user_id") or u.get("id"))

    df = query_df(
        """
        SELECT qr.*, c.account_name
        FROM quotation_requests qr
        JOIN customers c ON c.customer_id = qr.customer_id
        WHERE qr.rep_user_id = :uid
        ORDER BY qr.submitted_at DESC
        """,
        {"uid": uid},
    )

    render_quotation_list(
        f"{PAGE_NS}_my_quotations", df,
        page_name="Quotations",
        status_labels=_STATUS_LABELS,
        status_order=_STATUS_ORDER,
        search_text_cols=("quotation_number", "account_name"),
        date_col="submitted_at",
        search_placeholder="Search by quotation # or customer…",
    )


def _render_my_quotation_detail_body(u, header: dict) -> None:
    uid = int(u.get("user_id") or u.get("id"))
    qid = int(header["quotation_id"])
    status = _norm(header.get("status"))
    is_owner = int(header.get("rep_user_id")) == uid

    render_quotation_detail(qid)

    revisions_df = _load_revisions(qid)
    if len(revisions_df) >= 2:
        st.markdown("##### Revision Comparison (latest two)")
        rev_a = revisions_df.iloc[-2].to_dict()
        rev_b = revisions_df.iloc[-1].to_dict()
        rev_a["lines"] = _load_revision_lines(int(rev_a["revision_id"])).to_dict("records")
        rev_b["lines"] = _load_revision_lines(int(rev_b["revision_id"])).to_dict("records")
        render_revision_diff(rev_a, rev_b)

    if status == "EDIT_REQUESTED":
        manager_comment = _norm(header.get("manager_comment"))
        if manager_comment:
            st.warning(f"**Manager comment:** {manager_comment}")
        if is_owner:
            st.markdown("---")
            _render_edit_resubmit_section(uid, header)
            _render_withdraw_section(uid, qid)
    elif status == "IN_REVIEW" and is_owner:
        st.markdown("---")
        _render_withdraw_section(uid, qid)
    # APPROVED / DONE / REJECTED / WITHDRAWN — read-only, no action buttons.


def _prefill_resubmit_form(ns: str, qid: int, header: dict) -> None:
    revisions_df = _load_revisions(qid)
    if revisions_df.empty:
        latest = header
        prior_lines = []
    else:
        latest = revisions_df.iloc[-1].to_dict()
        prior_lines = _load_revision_lines(int(latest["revision_id"])).to_dict("records")

    qd_raw = latest.get("quotation_date")
    if isinstance(qd_raw, str):
        qd = datetime.date.fromisoformat(qd_raw)
    elif isinstance(qd_raw, datetime.datetime):
        qd = qd_raw.date()
    elif isinstance(qd_raw, datetime.date):
        qd = qd_raw
    elif hasattr(qd_raw, "date"):
        qd = qd_raw.date()
    else:
        qd = datetime.date.today()
    st.session_state[f"{ns}_quotation_date"] = qd

    _vat_rate = latest.get("vat_rate")
    _vat_rate_is_na = _vat_rate is None or (isinstance(_vat_rate, float) and pd.isna(_vat_rate))
    try:
        st.session_state[f"{ns}_vat_rate"] = 0.0 if _vat_rate_is_na else float(_vat_rate)
    except (TypeError, ValueError):
        st.session_state[f"{ns}_vat_rate"] = 0.0
    st.session_state[f"{ns}_remarks"] = _norm(latest.get("remarks"))

    validity = latest.get("validity_days")
    validity_norm = _norm(validity)
    st.session_state[f"{ns}_validity"] = validity_norm.split(".")[0] if validity_norm else str(VALIDITY_OPTIONS[0])
    st.session_state[f"{ns}_delivery"] = _norm(latest.get("delivery_terms")) or DELIVERY_OPTIONS[0]
    st.session_state[f"{ns}_payment"] = _norm(latest.get("payment_terms")) or PAYMENT_TERMS_OPTIONS[0]

    customer_id = latest.get("customer_id")
    if customer_id is None or (isinstance(customer_id, float) and pd.isna(customer_id)):
        customer_id = header.get("customer_id")
    if customer_id is not None and not (isinstance(customer_id, float) and pd.isna(customer_id)):
        _prefill_customer_state(ns, int(customer_id))

    _prefill_line_rows(ns, prior_lines)


def _render_edit_resubmit_section(uid: int, header: dict) -> None:
    qid = int(header["quotation_id"])
    ns = f"{PAGE_NS}_resub_{qid}"
    edit_flag = f"{ns}_editing"
    init_flag = f"{ns}_initialized"

    if not st.session_state.get(edit_flag):
        if st.button("Edit & Resubmit", key=f"{ns}_start_btn"):
            st.session_state[edit_flag] = True
            st.rerun()
        return

    if not st.session_state.get(init_flag):
        _prefill_resubmit_form(ns, qid, header)
        st.session_state[init_flag] = True

    st.markdown("##### Edit & Resubmit")
    customer_id = _render_customer_picker(ns)

    c1, c2 = st.columns(2)
    with c1:
        quotation_date = st.date_input("Quotation Date *", key=f"{ns}_quotation_date")
        vat_rate = st.number_input(
            "VAT Rate (%) *", min_value=0.0, max_value=100.0, step=0.5, format="%.2f", key=f"{ns}_vat_rate",
        )
    with c2:
        validity_choice = st.selectbox(
            "Validity (days) *", [str(v) for v in VALIDITY_OPTIONS], key=f"{ns}_validity",
        )
        delivery_terms = st.selectbox("Delivery Terms *", DELIVERY_OPTIONS, key=f"{ns}_delivery")
        payment_terms = st.selectbox("Payment Terms *", PAYMENT_TERMS_OPTIONS, key=f"{ns}_payment")
    remarks = st.text_area("Remarks", key=f"{ns}_remarks")

    lines, lines_valid = _render_line_items_editor(ns)

    vat_rate_dec = Decimal(str(vat_rate))
    _render_totals_preview(lines, vat_rate_dec)

    can_submit = (
        bool(customer_id) and lines_valid
        and bool(validity_choice) and bool(delivery_terms) and bool(payment_terms)
    )

    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("Resubmit", key=f"{ns}_resubmit_btn", type="primary", disabled=not can_submit):
            new_header = {
                "customer_id": customer_id,
                "quotation_date": quotation_date,
                "vat_rate": vat_rate_dec,
                "remarks": remarks,
                "validity_days": int(validity_choice),
                "delivery_terms": delivery_terms,
                "payment_terms": payment_terms,
            }
            ok, err = resubmit_quotation(
                qid, new_header, lines, actor_uid=uid, expected_version=int(header.get("version") or 0),
            )
            if ok:
                _clear_ns_state(ns)
                st.toast("Quotation resubmitted for review.", icon="✅")
                st.rerun()
            else:
                st.error(err)
    with col_b:
        if st.button("Cancel", key=f"{ns}_cancel_btn"):
            _clear_ns_state(ns)
            st.rerun()


def _render_withdraw_section(uid: int, qid: int) -> None:
    ns = f"{PAGE_NS}_withdraw_{qid}"
    with st.expander("Withdraw Request", expanded=False):
        st.warning(
            "This will permanently withdraw this quotation request. It cannot be resubmitted afterward."
        )
        reason = st.text_area("Withdrawal reason (required) *", key=f"{ns}_reason")
        confirm = st.checkbox("I confirm I want to withdraw this quotation request", key=f"{ns}_confirm")
        withdraw_enabled = bool((reason or "").strip()) and confirm

        if st.button("Withdraw Request", key=f"{ns}_btn", type="secondary", disabled=not withdraw_enabled):
            ok, err = withdraw_quotation(qid, rep_uid=uid, reason=(reason or "").strip())
            if ok:
                _clear_ns_state(ns)
                st.toast("Quotation withdrawn.", icon="✅")
                st.rerun()
            else:
                st.error(err)
