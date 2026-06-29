# pages/submit_visit.py
import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import folium
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from streamlit_folium import st_folium

from auth import resolve_session_user, set_url_param
from config import TIMEZONE, DUP_MINUTES
from db_ops import query_df, exec_sql, insert_visit_atomic, recent_visit_minutes
from utils import _utcnow, _utcnow_iso, _local_now_str, push_visit_to_pbi, _client_ip
from widgets import (
    customer_quick_find_module,
    customer_cascading_selectors,
    get_location_block,
    nearby_customers_block,
    _on_customer_change,
    _on_bu_change,
    _on_line_change,
    _reset_geo_on_user_or_page_change,
    set_current_page,
)
from ui import section_header, required_legend, form_section, form_subsection

try:
    from psycopg.errors import UniqueViolation
except Exception:
    UniqueViolation = None


@st.cache_data(ttl=300)
def _fetch_departments() -> list:
    df = query_df(
        "SELECT DISTINCT department FROM target_audiences"
        " WHERE department IS NOT NULL AND trim(department) <> '' ORDER BY department"
    )
    return df["department"].astype(str).str.strip().tolist() if not df.empty else []


@st.cache_data(ttl=300)
def _fetch_positions() -> list:
    df = query_df(
        "SELECT DISTINCT position FROM target_audiences"
        " WHERE position IS NOT NULL AND trim(position) <> '' ORDER BY position"
    )
    return df["position"].astype(str).str.strip().tolist() if not df.empty else []


def page_submit_visit():
    section_header("Submit Visit", "Log a new customer visit.")

    st.markdown(required_legend(), unsafe_allow_html=True)

    PAGE_NS = "submit_visit"
    nonce_key           = f"_{PAGE_NS}_form_nonce"
    saved_ok_key        = f"_{PAGE_NS}_saved_ok"
    geo_nonce_key       = f"_{PAGE_NS}_geo_nonce"
    geo_captured_key    = f"_{PAGE_NS}_geo_captured"
    busy_key            = f"_{PAGE_NS}_busy"
    intent_key          = f"_{PAGE_NS}_submit_intent"
    pending_errors_key  = f"_{PAGE_NS}_pending_errors"

    TITLE_OPTIONS = ["", "Dr.", "Mr.", "Ms.", "Mrs.", "Prof.", "Eng.", "Other"]

    st.session_state.setdefault(nonce_key, 0)
    st.session_state.setdefault(geo_nonce_key, 0)
    st.session_state.setdefault(busy_key, False)
    st.session_state.setdefault(intent_key, False)

    # =====================================================
    # Customer Quick-Find (fixed keys) — Submit Visit
    # =====================================================
    cid_locked_key         = f"_{PAGE_NS}_cid_locked"
    req_clear_customer_key = f"_{PAGE_NS}_req_clear_customer"
    req_clear_acct_key     = f"_{PAGE_NS}_req_clear_acct"
    req_set_acct_key       = f"_{PAGE_NS}_req_set_acct"
    acct_set_value_key     = f"_{PAGE_NS}_acct_set_value"
    qf_msg_key             = f"_{PAGE_NS}_qf_msg"
    qf_msg_type_key        = f"_{PAGE_NS}_qf_msg_type"

    st.session_state.setdefault(cid_locked_key, False)
    st.session_state.setdefault(req_clear_customer_key, False)
    st.session_state.setdefault(req_clear_acct_key, False)
    st.session_state.setdefault(req_set_acct_key, False)
    st.session_state.setdefault(acct_set_value_key, "")
    st.session_state.setdefault(qf_msg_key, "")
    st.session_state.setdefault(qf_msg_type_key, "")

    # Fixed keys for customer widgets (DO NOT use nonce here)
    KEY_ACCT   = f"{PAGE_NS}/acct_search"
    KEY_REGION = f"{PAGE_NS}/region_sel"
    KEY_CITY   = f"{PAGE_NS}/city_sel"
    KEY_SECTOR = f"{PAGE_NS}/sector_sel"
    KEY_CUST   = f"{PAGE_NS}/cust_sel"
    KEY_CUSTID = f"{PAGE_NS}/customer_id_resolved"

    def k(name: str) -> str:
        return f"{PAGE_NS}/{name}_{st.session_state[nonce_key]}"

    # ---- cascade clear helpers (nonce'd keys for non-customer fields) ----
    def _on_bu_change():
        for n in ("cat_sel", "bl_sel", "prod_sel"):
            st.session_state.pop(k(n), None)

    def _on_line_change():
        for n in ("prod_sel",):
            st.session_state.pop(k(n), None)

    set_current_page(PAGE_NS)

    # --- Resolve logged-in user safely ---
    u = st.session_state.get("user") or resolve_session_user()
    if not u:
        st.warning("Please sign in to continue.")
        st.stop()

    uid  = int(u.get("user_id") or u.get("id"))
    role = (u.get("role") or "").lower().strip()

    # --- Display info ---
    display_name   = u.get("name") or u.get("email") or f"User #{u.get('user_id', '?')}"
    display_region = u.get("region") or "—"
    display_role   = u.get("role") or "—"
    _reset_geo_on_user_or_page_change(PAGE_NS, uid)

    if st.session_state.pop(saved_ok_key, False):
        st.success("Visit saved. Fields cleared — ready for the next entry.")

    # =====================================================
    # SECTION 1 — Visit Location (REQUIRED)
    # =====================================================
    st.markdown(form_section(1, "Visit Location", first=True), unsafe_allow_html=True)
    lat, lon, acc = get_location_block(k)
    if lat is None or lon is None:
        st.warning("Location is required before you can submit.")
        return

    nearby_customers_block(lat, lon, KEY_REGION, KEY_CITY, KEY_SECTOR, KEY_CUST)

    # =====================================================
    # SECTION 2 — Customer & Target Audience
    # =====================================================
    st.markdown(form_section(2, "Customer & Target Audience"), unsafe_allow_html=True)

    # Quick Find (Account ID)
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

    # Cascading selectors (respects lock)
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

    # Keep these for validation / duplicate banner
    region_choice = (st.session_state.get(KEY_REGION) or "")
    city_choice   = (st.session_state.get(KEY_CITY) or "")
    sector_choice = (st.session_state.get(KEY_SECTOR) or "")
    cust_choice   = (st.session_state.get(KEY_CUST) or "")

    # ✅ NEW: "Other" customer required name (stored into visits.other_customer_name)
    other_customer_name = None
    is_other_customer = bool(cust_choice and cust_choice.strip().lower() == "other")

    if is_other_customer:
        st.markdown(form_subsection("New Customer Details"), unsafe_allow_html=True)
        other_customer_name = st.text_input(
            "Customer Name *",
            key=k("other_customer_name"),
            help="Enter the real legal customer name when you selected 'Other'.",
        )

    # ---------------- Target Audience (with "Other") ----------------
    audience_id      = None
    aud_choice_label = ""
    aud_choice_name  = None

    aud_labels: list[str] = [""]
    aud_rows   = []  # (label, id, raw_name)

    other_ta_title      = None
    other_ta_name       = None
    other_ta_department = None
    other_ta_position   = None
    other_ta_phone      = None
    other_ta_email      = None

    dept_choices_base: list[str] = []
    pos_choices_base:  list[str] = []

    dept_choices_base = _fetch_departments()
    pos_choices_base  = _fetch_positions()

    if customer_id:
        aud_df = query_df(
            """
            SELECT audience_id, title, name, department, position
            FROM target_audiences
            WHERE is_active IS TRUE AND customer_id=:cid
            ORDER BY name
            """,
            {"cid": int(customer_id)},
        )

        def _fmt_audience(row) -> str:
            parts = []
            title = (str(row.title).strip() + " ") if pd.notna(row.title) and str(row.title).strip() else ""
            name  = str(row.name).strip()          if pd.notna(row.name)  else ""
            parts.append((title + name).strip())
            if pd.notna(row.department) and str(row.department).strip():
                parts.append(str(row.department).strip())
            if pd.notna(row.position) and str(row.position).strip():
                parts.append(str(row.position).strip())
            parts = [p for p in parts if p]
            return " · ".join(parts) if parts else name

        for r in aud_df.itertuples(index=False):
            label = _fmt_audience(r)
            aud_rows.append((label, int(r.audience_id), str(r.name).strip() if pd.notna(r.name) else ""))

        aud_labels = [""] + [x[0] for x in aud_rows]

        if len(aud_labels) == 1:
            st.warning("This customer has no Target Audiences.")

        aud_labels.append("Other")

    elif is_other_customer:
        # No real customer yet — lock audience to "Other"
        aud_labels = ["Other"]

    aud_choice_label = st.selectbox(
        "Target Audience *",
        aud_labels,
        index=0,
        key=k("aud_sel"),
        disabled=is_other_customer or (customer_id is None and not is_other_customer),
        help=("Auto-filled — new customer" if is_other_customer
              else (None if customer_id else "Select a Customer first")),
    )

    if customer_id and aud_choice_label and aud_choice_label not in ("", "Other"):
        for lbl, aid, raw_name in aud_rows:
            if lbl == aud_choice_label:
                audience_id     = aid
                aud_choice_name = raw_name
                break

    if (customer_id or is_other_customer) and aud_choice_label == "Other":
        st.markdown(form_subsection("New Target Audience Details"), unsafe_allow_html=True)

        other_ta_title = st.selectbox(
            "Title (optional)",
            TITLE_OPTIONS,
            index=0,
            key=k("other_ta_title"),
        )

        other_ta_name = st.text_input(
            "Target Audience Name *",
            key=k("other_ta_name"),
            help="Name of the person you are meeting.",
        )

        dept_opts = [""] + dept_choices_base + ["Other"]
        pos_opts  = [""] + pos_choices_base  + ["Other"]

        other_ta_department = st.selectbox(
            "Department *",
            dept_opts,
            index=0,
            key=k("other_ta_dept_sel"),
            help="Select the department or choose 'Other'.",
        )

        other_ta_position = st.selectbox(
            "Position *",
            pos_opts,
            index=0,
            key=k("other_ta_pos_sel"),
            help="Select the position or choose 'Other'.",
        )

        other_ta_phone = st.text_input(
            "Phone # (optional)",
            key=k("other_ta_phone"),
            help="Optional – KSA mobile like 05XXXXXXXX.",
        )

        other_ta_email = st.text_input(
            "Email (optional)",
            key=k("other_ta_email"),
            help="Optional – must be a valid email address.",
        )

    # -------- Home Visit block --------
    is_home_visit  = bool(aud_choice_label and aud_choice_label.strip().lower().startswith("home visit"))
    patient_name   = None
    patient_phone  = None
    serial_no      = None
    if is_home_visit:
        with st.container():
            patient_name  = st.text_input("Patient Name *", key=k("pat_name"))
            patient_phone = st.text_input("Patient Phone # *", key=k("pat_phone"))
            serial_no     = st.text_input("Device Serial # *", key=k("serial_no"))

    # =====================================================
    # SECTION 3 — Product Details
    # =====================================================
    st.markdown(form_section(3, "Product Details"), unsafe_allow_html=True)

    # ---- Business Unit ----
    bu_df = query_df(
        """
        SELECT business_unit_id, name
        FROM business_units
        WHERE is_active IS TRUE
        ORDER BY name
        """
    )
    bu_names = [""] + bu_df["name"].tolist()

    bu_choice = st.selectbox(
        "Business Unit *",
        bu_names,
        index=0,
        key=k("bu_sel"),
        on_change=_on_bu_change,
    )

    bu_id = None
    if bu_choice:
        match = bu_df.loc[bu_df["name"] == bu_choice, "business_unit_id"]
        bu_id = int(match.iloc[0]) if not match.empty else None

    # ---- Category ----
    cat_df    = pd.DataFrame()
    cat_names = [""]

    if bu_id:
        cat_df = query_df(
            """
            SELECT DISTINCT category
            FROM business_lines
            WHERE is_active IS TRUE
            AND business_unit_id = :bid
            AND category IS NOT NULL
            AND trim(category) <> ''
            ORDER BY category
            """,
            {"bid": bu_id},
        )
        cat_names = [""] + cat_df["category"].tolist()

    cat_choice = st.selectbox(
        "Category *",
        cat_names,
        index=0,
        key=k("cat_sel"),
        disabled=(bu_id is None),
        help=None if bu_id else "Select a Business Unit first",
    )

    # ---- Business Line ----
    bl_df    = pd.DataFrame()
    bl_names = [""]

    bl_choice = ""
    business_line_id = None

    if bu_id and cat_choice:
        bl_df = query_df(
            """
            SELECT business_line_id, name
            FROM business_lines
            WHERE is_active IS TRUE
            AND business_unit_id = :bid
            AND category = :cat
            ORDER BY name
            """,
            {"bid": bu_id, "cat": cat_choice},
        )
        bl_names = [""] + bl_df["name"].tolist()

    bl_choice = st.selectbox(
        "Business Line *",
        bl_names,
        index=0,
        key=k("bl_sel"),
        disabled=(bu_id is None) or (not cat_choice),
        on_change=_on_line_change,
        help=None if (bu_id and cat_choice) else "Select a Category first",
    )

    if bu_id and cat_choice and bl_choice:
        match = bl_df.loc[bl_df["name"] == bl_choice, "business_line_id"]
        business_line_id = int(match.iloc[0]) if not match.empty else None

    # ---- Product (Article Number) ----
    prod_labels: list[str] = [""]
    prod_df = pd.DataFrame()
    product_id  = None
    prod_choice = ""

    if business_line_id:
        prod_df = query_df(
            """
            SELECT product_id, article_number, description
            FROM items
            WHERE is_active IS TRUE
            AND business_line_id = :blid
            ORDER BY COALESCE(article_number, product_id)
            """,
            {"blid": business_line_id},
        )

        prod_labels = [""] + [
            (
                f"{(r.article_number or r.product_id)} — {r.description}"
                if pd.notna(r.description) and str(r.description).strip()
                else f"{(r.article_number or r.product_id)}"
            )
            for r in prod_df.itertuples(index=False)
        ]

    prod_choice = st.selectbox(
        "Product / Article # (optional)",
        prod_labels,
        index=0,
        key=k("prod_sel"),
        disabled=(business_line_id is None),
        help=None if business_line_id else "Select Business Line first",
    )

    if business_line_id and prod_choice:
        label_to_pid = {}
        for r in prod_df.itertuples(index=False):
            label = (
                f"{(r.article_number or r.product_id)} — {r.description}"
                if pd.notna(r.description) and str(r.description).strip()
                else f"{(r.article_number or r.product_id)}"
            )
            label_to_pid[label] = r.product_id
        product_id = label_to_pid.get(prod_choice)

    # =====================================================
    # SECTION 4 — Visit Details & Outcome
    # =====================================================
    st.markdown(form_section(4, "Visit Details & Outcome"), unsafe_allow_html=True)

    visit_type_choice = st.radio(
        "Visit Type *",
        ["Actual Visit", "Phone Call"],
        index=0,
        horizontal=True,
        key=k("visit_type_sel"),
    )

    if role in {"admin"}:
        obj_df = query_df(
            """
            SELECT objective_id, name
            FROM objectives
            WHERE COALESCE(is_active, TRUE) IS TRUE
            ORDER BY name
            """
        )
    else:
        obj_df = query_df(
            """
            SELECT o.objective_id, o.name
            FROM objectives o
            JOIN role_objectives ro
              ON ro.objective_id = o.objective_id
            WHERE COALESCE(o.is_active, TRUE) IS TRUE
              AND COALESCE(ro.is_active, TRUE) IS TRUE
              AND ro.role = :role
            ORDER BY o.name
            """,
            {"role": role},
        )

    obj_names  = [""] + obj_df["name"].tolist()
    obj_choice = st.selectbox("Business Objective *", obj_names, index=0, key=k("obj_sel"))

    objective_id = None
    if obj_choice:
        match = obj_df.loc[obj_df["name"] == obj_choice, "objective_id"]
        objective_id = int(match.iloc[0]) if not match.empty else None

    is_shelf_movement = bool(obj_choice and ("shelf movement" in obj_choice.strip().lower()))
    notes             = st.text_area("Notes (optional)", key=k("notes"))

    allowed_evals = {"Positive", "Negative", "Neutral"}
    evaluation_choice = st.selectbox(
        "Evaluation *",
        [""] + sorted(list(allowed_evals)),
        index=0,
        key=k("eval_sel"),
    )
    evaluation_val = evaluation_choice if evaluation_choice in allowed_evals else None

    # ---------------- Shelf Movement grid ----------------
    shelf_df    = pd.DataFrame()
    shelf_editor = None
    if is_shelf_movement:
        st.markdown(form_subsection("Shelf Movement — Quantities Checked"), unsafe_allow_html=True)
        if not bu_id:
            st.info("Select a Business Unit to load items.")
        elif not cat_choice:
            st.info("Select a Category to load items.")
        else:
            shelf_df = query_df(
                """
                SELECT i.product_id,
                       COALESCE(i.article_number, i.product_id) AS article_number,
                       COALESCE(i.description, '') AS description
                FROM items i
                JOIN business_lines bl ON bl.business_line_id = i.business_line_id
                WHERE i.is_active IS TRUE
                  AND bl.is_active IS TRUE
                  AND bl.business_unit_id = :bid
                  AND bl.category = :cat
                ORDER BY COALESCE(i.article_number, i.product_id)
                """,
                {"bid": bu_id, "cat": cat_choice},
            )
            if shelf_df.empty:
                st.warning("No active items found for this Category.")
            else:
                shelf_df = shelf_df.assign(qty_checked=pd.Series([None] * len(shelf_df)))
                shelf_editor = st.data_editor(
                    shelf_df,
                    key=k("sm_editor"),
                    width='stretch',
                    hide_index=True,
                    num_rows="fixed",
                    column_config={
                        "product_id":     st.column_config.TextColumn("Product ID", disabled=True),
                        "article_number": st.column_config.TextColumn("Article #",  disabled=True),
                        "description":    st.column_config.TextColumn("Description", disabled=True),
                        "qty_checked":    st.column_config.NumberColumn(
                            "Qty Checked",
                            help="Leave blank if not checked. Enter 0 if none on shelf.",
                            min_value=0,
                            step=1,
                        ),
                    },
                )

    # ---------------- Potential duplicate banner ----------------
    if customer_id:
        mins = recent_visit_minutes(uid, customer_id)
        if mins is not None and mins < DUP_MINUTES:
            st.info(f"You submitted for **{cust_choice}** {mins} minutes ago — potential duplicate.")

    # ---------------- Validation errors (shown near submit so the user sees them) ----------------
    pending_errors = st.session_state.get(pending_errors_key, [])
    if pending_errors:
        for msg in pending_errors:
            st.error(msg)

    # ---------------- Submit button ----------------
    st.markdown(
        '<div style="margin-top:2rem;padding-top:1.25rem;border-top:1px solid var(--color-border);"></div>',
        unsafe_allow_html=True,
    )
    inline_click = st.button(
        "Submit Visit",
        type="primary",
        key=k("submit_btn_inline"),
        disabled=st.session_state[busy_key],
        use_container_width=True,
        help="Saves immediately. You'll see a spinner while saving.",
    )

    if inline_click and not st.session_state[busy_key]:
        st.session_state[intent_key] = True
        st.session_state[busy_key]   = True
        st.rerun()

    if not st.session_state[intent_key]:
        return

    # ---------------- Process submission ----------------
    with st.spinner("Saving your visit…"):
        errors: list[str] = []

        if not is_other_customer and not region_choice:
            errors.append("Please choose a **Region**.")
        if not is_other_customer and not city_choice:
            errors.append("Please choose a **City**.")
        if not is_other_customer and not sector_choice:
            errors.append("Please choose a **Sector**.")
        if not customer_id and not is_other_customer:
            errors.append("Please choose a **Customer**.")

        # ✅ NEW: Other Customer validation
        if is_other_customer:
            if not other_customer_name or not other_customer_name.strip():
                errors.append("For **Other Customer**, please enter **Customer Name**.")

        # Target audience validation
        if not aud_choice_label:
            errors.append("Please choose a **Target Audience** for the selected customer.")
        elif aud_choice_label == "Other":
            if not other_ta_name or not other_ta_name.strip():
                errors.append("For **Other Target Audience**, please enter **Target Audience Name**.")
            if not other_ta_department:
                errors.append("For **Other Target Audience**, please select a **Department**.")
            if not other_ta_position:
                errors.append("For **Other Target Audience**, please select a **Position**.")

            if other_ta_phone and other_ta_phone.strip():
                phone_clean = other_ta_phone.strip()
                if not re.fullmatch(r"(?:\+966|00966|0)?5\d{8}", phone_clean):
                    errors.append(
                        "For **Other Target Audience**, **Phone #** looks invalid "
                        "(expected KSA mobile like 05XXXXXXXX)."
                    )

            if other_ta_email and other_ta_email.strip():
                email_clean = other_ta_email.strip()
                if not re.fullmatch(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_clean):
                    errors.append("For **Other Target Audience**, **Email** looks invalid.")
        elif not audience_id:
            errors.append("Please choose a valid **Target Audience** for the selected customer.")

        if is_home_visit:
            if not patient_name:
                errors.append("For **Home Visit**, please enter **Patient Name**.")
            if not patient_phone:
                errors.append("For **Home Visit**, please enter **Patient Phone #**.")
            elif not re.fullmatch(r"(?:\+966|00966|0)?5\d{8}", patient_phone.strip()):
                errors.append("**Patient Phone #** looks invalid (expected KSA mobile like 05XXXXXXXX).")
            if not serial_no:
                errors.append("For **Home Visit**, please enter **Serial #**.")

        if not bu_id:
            errors.append("Please choose a **Business Unit**.")
        if not cat_choice:
            errors.append("Please choose a **Category**.")
        if not business_line_id:
            errors.append("Please choose a **Business Line**.")

        if objective_id is None:
            errors.append("Please choose a **Business Objective**.")
        if evaluation_val is None:
            errors.append("Please choose an **Evaluation** (Positive/Negative/Neutral).")

        shelf_lines_payload = None
        filled_rows = None

        # Shelf Movement validations
        if is_shelf_movement:
            if shelf_editor is None or shelf_editor.empty:
                errors.append(
                    "**Shelf Movement** grid is empty. Load items by selecting Business Unit and Category."
                )
            else:
                shelf_lines_payload = []
                any_qty = False
                invalid_qty_found = False
                negative_qty_found = False

                digit_map = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

                for _, row in shelf_editor.iterrows():
                    raw = row.get("qty_checked", None)
                    if raw is None:
                        continue

                    txt = str(raw).strip()
                    if txt == "" or txt.lower() == "none":
                        continue

                    txt = txt.translate(digit_map)
                    digits_only = "".join(ch for ch in txt if ch in "0123456789")

                    if digits_only == "":
                        invalid_qty_found = True
                        continue

                    qty = int(digits_only)

                    if qty < 0:
                        negative_qty_found = True
                        continue

                    any_qty = True
                    shelf_lines_payload.append(
                        {
                            "product_id": int(row["product_id"]),
                            "qty_checked": float(qty),
                        }
                    )

                filled_rows = pd.DataFrame(shelf_lines_payload)

                if negative_qty_found:
                    errors.append("Quantities in **Shelf Movement** cannot be negative.")

                if invalid_qty_found:
                    errors.append(
                        "Some values in **Shelf Movement** are not numeric or are out of range. "
                        "Please enter only valid numbers or leave blank."
                    )

                if not any_qty and not invalid_qty_found and not negative_qty_found:
                    errors.append(
                        "Enter at least **one** quantity in the **Shelf Movement** grid "
                        "(blank = not checked; 0 is allowed)."
                    )

        if errors:
            st.session_state[pending_errors_key] = errors
            st.session_state[busy_key]            = False
            st.session_state[intent_key]          = False
            st.rerun()

        # ----- All validations passed → persist -----
        visit_row = {
            "user_id":             uid,
            "submitted_at_utc":    _utcnow(),
            "submitted_at_local":  _local_now_str(),
            "latitude":            lat,
            "longitude":           lon,
            "accuracy_m":          acc,
            "customer_id":         int(customer_id) if customer_id is not None else None,
            "audience_id":         int(audience_id) if audience_id else None,
            "business_line_id":    int(business_line_id),
            "product_id":          (None if is_shelf_movement else product_id),
            "objective_id":        int(objective_id),
            "notes":               (notes.strip() if notes else None),
            "evaluation":          evaluation_val,
            "visit_type":          visit_type_choice,

            "is_other_customer":   is_other_customer,
            # ✅ NEW: store the typed name when customer is Other
            "other_customer_name": (other_customer_name.strip() if (is_other_customer and other_customer_name) else None),

            # New fields for "Other" Target Audience
            "other_audience_title":      (other_ta_title.strip() if other_ta_title else None) or None,
            "other_audience_name":       (other_ta_name.strip() if other_ta_name else None),
            "other_audience_department": (other_ta_department.strip() if other_ta_department else None),
            "other_audience_position":   (other_ta_position.strip() if other_ta_position else None),
            "other_audience_phone":      (other_ta_phone.strip() if other_ta_phone else None) or None,
            "other_audience_email":      (other_ta_email.strip() if other_ta_email else None) or None,
        }

        home_payload = None
        if is_home_visit:
            home_payload = {
                "patient_name":  patient_name,
                "patient_phone": patient_phone,
                "serial_no":     serial_no,
            }

        try:
            visit_id = insert_visit_atomic(visit_row, home_payload, shelf_lines_payload)

            # Power BI row
            def _article_from_label(lbl: str | None) -> str:
                if not lbl:
                    return ""
                return str(lbl).split(" — ", 1)[0].strip()

            shelf_lines_count = int(len(filled_rows)) if (is_shelf_movement and filled_rows is not None) else 0
            shelf_total_qty   = int(filled_rows["qty_checked"].sum()) if (is_shelf_movement and filled_rows is not None) else 0

            pbi_row = {
                "submitted_at_utc":   datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "submitted_at_local": datetime.now().isoformat(),
                "user_name":          str(u.get("name") or ""),
                "user_region":        str(u.get("region") or ""),
                "customer_name":      str(cust_choice or ""),
                "audience_name":      ("Home Visit" if is_home_visit else str(aud_choice_label or "")),
                "business_unit":      str(bu_choice or ""),
                "category":           str(cat_choice or ""),
                "business_line":      str(bl_choice or ""),
                "article_number": (
                    "" if is_shelf_movement else _article_from_label(
                        prod_choice if (business_line_id and prod_choice) else None
                    )
                ),
                "objective":          str(obj_choice or ""),
                "evaluation":         str(evaluation_val or ""),
                "latitude":           float(lat) if lat is not None else 0.0,
                "longitude":          float(lon) if lon is not None else 0.0,
                "accuracy_m": (
                    f"{acc:.1f}" if isinstance(acc, (int, float)) else (str(acc) if acc is not None else "")
                ),
                "notes":              (notes.strip() if notes else ""),
                "shelf_lines_count":  shelf_lines_count,
                "shelf_total_qty":    shelf_total_qty,
            }

            # (optional) include the typed Other customer name in Power BI payload too
            if is_other_customer and other_customer_name and other_customer_name.strip():
                pbi_row["other_customer_name"] = other_customer_name.strip()

            if is_home_visit:
                pbi_row.update({
                    "patient_name":  patient_name.strip(),
                    "patient_phone": patient_phone.strip(),
                    "serial_no":     serial_no.strip().upper(),
                })

            ok, err = push_visit_to_pbi(pbi_row)
            if not ok:
                st.warning(f"Saved, but Power BI push failed → {err}")
            else:
                st.toast("Pushed to Power BI ✅", icon="✅")

            # reset form
            st.session_state[nonce_key]        += 1
            st.session_state[geo_nonce_key]    += 1
            st.session_state.pop(geo_captured_key, None)
            st.session_state.pop(pending_errors_key, None)
            st.session_state[saved_ok_key]      = True
            st.session_state[intent_key]        = False
            st.session_state[busy_key]          = False

            # clear "Other Customer Name" field after save
            st.session_state.pop(k("other_customer_name"), None)

            # (optional but recommended) clear quick-find UI state after successful save
            st.session_state[req_clear_customer_key] = True
            st.session_state[req_clear_acct_key]     = True
            st.session_state[qf_msg_key]             = ""
            st.session_state[qf_msg_type_key]        = ""

            st.rerun()

        except IntegrityError as e:
            emsg = str(e).lower()
            if (
                UniqueViolation and isinstance(e.orig, UniqueViolation)
            ) or (
                "duplicate key value violates unique constraint" in emsg
            ) or (
                "unique constraint" in emsg and "home_visits_serial_no" in emsg
            ):
                st.error("Serial # already exists. Please verify and try again.")
            else:
                st.error("Could not save your submission.")
                st.caption(str(e))
            st.session_state[intent_key] = False
            st.session_state[busy_key]   = False

        except Exception as e:
            st.error("Could not save your submission.")
            st.caption(str(e))
            st.session_state[intent_key] = False
            st.session_state[busy_key]   = False
