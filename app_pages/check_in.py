# pages/check_in.py
from datetime import datetime, timezone
from typing import Optional

import folium
import pandas as pd
import streamlit as st
from sqlalchemy import text
from streamlit_folium import st_folium

from auth import resolve_session_user
from config import TIMEZONE, DUP_MINUTES
from db_ops import query_df, exec_sql, insert_visit_atomic
from utils import _utcnow_iso, _local_now_str, _client_ip, _utcnow
from widgets import get_location_block, nearby_customers_block, _reset_geo_on_user_or_page_change, set_current_page, customer_quick_find_module, customer_cascading_selectors
from ui import section_header, required_legend, form_section, form_subsection


def page_check_in():
    section_header("Check-In", "Log a quick customer check-in")

    st.markdown(required_legend(), unsafe_allow_html=True)

    PAGE_NS = "check_in"
    nonce_key         = f"_{PAGE_NS}_form_nonce"
    saved_ok_key      = f"_{PAGE_NS}_saved_ok"
    geo_nonce_key     = f"_{PAGE_NS}_geo_nonce"
    geo_captured_key  = f"_{PAGE_NS}_geo_captured"
    busy_key          = f"_{PAGE_NS}_busy"
    intent_key        = f"_{PAGE_NS}_submit_intent"

    # Customer search / lock state (fixed keys)
    cid_locked_key     = f"_{PAGE_NS}_cid_locked"

    # ✅ request flags so we can clear/set BEFORE widgets instantiate
    req_clear_customer_key = f"_{PAGE_NS}_req_clear_customer"
    req_clear_acct_key     = f"_{PAGE_NS}_req_clear_acct"

    # ✅ request "set account id to uppercase" safely on next run
    req_set_acct_key       = f"_{PAGE_NS}_req_set_acct"
    acct_set_value_key     = f"_{PAGE_NS}_acct_set_value"

    # Persist quick-find message across reruns
    qf_msg_key        = f"_{PAGE_NS}_qf_msg"
    qf_msg_type_key   = f"_{PAGE_NS}_qf_msg_type"  # "error" | "success" | ""

    st.session_state.setdefault(nonce_key, 0)
    st.session_state.setdefault(geo_nonce_key, 0)
    st.session_state.setdefault(busy_key, False)
    st.session_state.setdefault(intent_key, False)

    st.session_state.setdefault(cid_locked_key, False)
    st.session_state.setdefault(req_clear_customer_key, False)
    st.session_state.setdefault(req_clear_acct_key, False)

    st.session_state.setdefault(req_set_acct_key, False)
    st.session_state.setdefault(acct_set_value_key, "")

    st.session_state.setdefault(qf_msg_key, "")
    st.session_state.setdefault(qf_msg_type_key, "")

    # ------------------------------
    # Fixed keys for customer widgets (DO NOT use nonce)
    # ------------------------------
    KEY_ACCT   = f"{PAGE_NS}/acct_search"
    KEY_REGION = f"{PAGE_NS}/region_sel"
    KEY_CITY   = f"{PAGE_NS}/city_sel"
    KEY_SECTOR = f"{PAGE_NS}/sector_sel"
    KEY_CUST   = f"{PAGE_NS}/cust_sel"
    KEY_CUSTID = f"{PAGE_NS}/customer_id_resolved"

    # Use nonce ONLY for non-customer widgets (notes, button keys, etc.)
    def k(name: str) -> str:
        return f"{PAGE_NS}/{name}_{st.session_state[nonce_key]}"

    set_current_page(PAGE_NS)

    # --- Resolve logged-in user ---
    u = st.session_state.get("user") or resolve_session_user()
    if not u:
        st.warning("Please sign in to continue.")
        st.stop()

    uid = int(u.get("user_id") or u.get("id"))
    _reset_geo_on_user_or_page_change(PAGE_NS, uid)

    # --- Header ---
    display_name   = u.get("name") or u.get("email") or f"User #{u.get('user_id', '?')}"
    display_region = u.get("region") or "—"
    display_role   = u.get("role") or "—"
    if st.session_state.pop(saved_ok_key, False):
        st.success("Checked in. Fields cleared — ready for the next entry.")

    # =====================================================
    # SECTION 1 — Location (REQUIRED)
    # =====================================================
    st.markdown(form_section(1, "Check-In Location"), unsafe_allow_html=True)
    lat, lon, acc = get_location_block(k)

    if lat is None or lon is None:
        st.warning("Location is required before you can check in.")
        return

    nearby_customers_block(lat, lon, KEY_REGION, KEY_CITY, KEY_SECTOR, KEY_CUST)

    # =====================================================
    # SECTION 2 — Customer
    # =====================================================
    st.markdown(form_section(2, "Customer"), unsafe_allow_html=True)

    # 2.1 Quick Find (fills + locks)
    _ = customer_quick_find_module(
        page_ns=PAGE_NS,
        query_df=query_df,
        customers_table="customers",
        KEY_ACCT=KEY_ACCT,
        KEY_REGION=KEY_REGION,
        KEY_CITY=KEY_CITY,
        KEY_SECTOR=KEY_SECTOR,
        KEY_CUST=KEY_CUST,
        KEY_CUSTID=KEY_CUSTID,
        cid_locked_key=cid_locked_key,
        req_clear_customer_key=req_clear_customer_key,
        req_clear_acct_key=req_clear_acct_key,
        req_set_acct_key=req_set_acct_key,
        acct_set_value_key=acct_set_value_key,
        qf_msg_key=qf_msg_key,
        qf_msg_type_key=qf_msg_type_key,
    )

    # 2.2 Cascading selectors (respects lock)
    customer_id = customer_cascading_selectors(
        query_df=query_df,
        customers_table="customers",
        KEY_REGION=KEY_REGION,
        KEY_CITY=KEY_CITY,
        KEY_SECTOR=KEY_SECTOR,
        KEY_CUST=KEY_CUST,
        KEY_CUSTID=KEY_CUSTID,
        cid_locked_key=cid_locked_key,
        qf_msg_key=qf_msg_key,
        qf_msg_type_key=qf_msg_type_key,
    )

    # Keep these variables for your validation logic
    locked = bool(st.session_state.get(cid_locked_key, False))
    region_choice = (st.session_state.get(KEY_REGION) or "")
    city_choice   = (st.session_state.get(KEY_CITY) or "")
    sector_choice = (st.session_state.get(KEY_SECTOR) or "")

    # ✅ NEW: other customer name required (stored into visits.other_customer_name)
    cust_choice = (st.session_state.get(KEY_CUST) or "")
    is_other_customer = bool(cust_choice and cust_choice.strip().lower() == "other")
    other_customer_name = None

    if is_other_customer:
        st.markdown(form_subsection("New Customer Details"), unsafe_allow_html=True)
        other_customer_name = st.text_input(
            "Customer Name *",
            key=k("other_customer_name"),
            help="Enter the real legal customer name when you selected 'Other'.",
        )

    # =====================================================
    # SECTION 3 — Notes
    # =====================================================
    st.markdown(form_section(3, "Notes"), unsafe_allow_html=True)
    notes = st.text_area("Notes (optional)", key=k("notes"))

    # =====================================================
    # Check In button
    # =====================================================
    CHECKIN_OBJECTIVE_ID = 18

    st.markdown(
        '<div style="margin-top:1rem;padding-top:1rem;border-top:1px solid var(--color-border);"></div>',
        unsafe_allow_html=True,
    )
    click = st.button(
        "Check In",
        type="primary",
        key=k("checkin_btn"),
        disabled=st.session_state[busy_key],
        use_container_width=True,
        help="Saves immediately. You'll see a spinner while saving.",
    )

    if click and not st.session_state[busy_key]:
        st.session_state[intent_key] = True
        st.session_state[busy_key] = True
        st.rerun()

    if not st.session_state[intent_key]:
        return

    with st.spinner("Saving check-in…"):
        errors = []
        if not is_other_customer and not region_choice:
            errors.append("Please choose a **Region**.")
        if not is_other_customer and not city_choice:
            errors.append("Please choose a **City**.")
        if not is_other_customer and not sector_choice:
            errors.append("Please choose a **Sector**.")
        if not customer_id and not is_other_customer:
            errors.append("Please choose a **Customer**.")

        # ✅ NEW: validate Other customer name
        if is_other_customer:
            if not other_customer_name or not other_customer_name.strip():
                errors.append("For **Other Customer**, please enter **Customer Name**.")

        if locked:
            acct_now = (st.session_state.get(KEY_ACCT) or "").strip()
            if not acct_now or not st.session_state.get(KEY_CUSTID):
                errors.append("Please use **Find** to select a valid Account ID (or click **Clear** and select manually).")

        if errors:
            for e in errors:
                st.error(e)
            st.session_state[intent_key] = False
            st.session_state[busy_key] = False
            return

        visit_row = {
            "user_id":            uid,
            "submitted_at_utc":   _utcnow(),
            "submitted_at_local": _local_now_str(),
            "latitude":           lat,
            "longitude":          lon,
            "accuracy_m":         acc,
            "customer_id":        int(customer_id),
            "objective_id":       int(CHECKIN_OBJECTIVE_ID),
            "visit_type":         "Actual Visit",
            "notes":              (notes.strip() if notes else None),

            # ✅ NEW: store typed name if customer == Other
            "other_customer_name": (other_customer_name.strip() if (is_other_customer and other_customer_name) else None),
        }

        try:
            _ = insert_visit_atomic(visit_row, home_visit=None, shelf_lines=None)

            st.session_state[nonce_key] += 1
            st.session_state[geo_nonce_key] += 1
            st.session_state.pop(geo_captured_key, None)

            st.session_state[saved_ok_key] = True
            st.session_state[intent_key] = False
            st.session_state[busy_key] = False

            # clear customer fields after successful save (doesn't touch location)
            st.session_state[req_clear_customer_key] = True
            st.session_state[req_clear_acct_key] = True

            # clear quick-find message (since _clear_qf_msg is now inside the module)
            st.session_state[qf_msg_key] = ""
            st.session_state[qf_msg_type_key] = ""

            # clear Other customer input after successful save
            st.session_state.pop(k("other_customer_name"), None)

            st.rerun()

        except Exception as e:
            st.error("Could not save your check-in.")
            st.caption(str(e))
            st.session_state[intent_key] = False
            st.session_state[busy_key] = False
