# pages/create_project.py
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

import folium
import pandas as pd
import streamlit as st
from sqlalchemy import text
from streamlit_folium import st_folium

from auth import resolve_session_user
from config import TIMEZONE
from db_ops import query_df, exec_sql, insert_project
from utils import _utcnow_iso, _local_now_str, _utcnow
from widgets import (
    customer_quick_find_module,
    customer_cascading_selectors,
    get_location_block,
    _on_customer_change,
    _reset_geo_on_user_or_page_change,
    set_current_page,
)
from ui import section_header


def page_create_project():
    section_header("Create Project", "Define a new sales or biomedical project")
    set_current_page("create_project")

    PAGE_NS = "create_project"
    nonce_key    = f"_{PAGE_NS}_form_nonce"
    saved_ok_key = f"_{PAGE_NS}_saved_ok"
    busy_key     = f"_{PAGE_NS}_busy"
    intent_key   = f"_{PAGE_NS}_submit_intent"

    st.session_state.setdefault(nonce_key, 0)
    st.session_state.setdefault(busy_key, False)
    st.session_state.setdefault(intent_key, False)

    def k(name: str) -> str:
        return f"{PAGE_NS}/{name}_{st.session_state[nonce_key]}"

    # ---- cascade clear helpers ----
    def _on_region_change():
        for n in ("city_sel", "sector_sel", "cust_sel"):
            st.session_state.pop(k(n), None)

    def _on_city_change():
        for n in ("sector_sel", "cust_sel"):
            st.session_state.pop(k(n), None)

    def _on_sector_change():
        for n in ("cust_sel",):
            st.session_state.pop(k(n), None)

    def _on_bu_change():
        for n in ("cat_sel", "bl_sel", "prod_sel"):
            st.session_state.pop(k(n), None)

    def _on_line_change():
        for n in ("prod_sel",):
            st.session_state.pop(k(n), None)

    # ---- Resolve logged-in user (manager) ---
    u = st.session_state.get("user") or resolve_session_user()
    if not u:
        st.warning("Please sign in to continue.")
        st.stop()

    manager_id     = int(u.get("user_id") or u.get("id"))
    display_name   = u.get("name") or u.get("email") or f"User #{manager_id}"
    display_role   = u.get("role") or "—"
    display_region = u.get("region") or "—"

    st.caption(
        f"Logged in as **{display_name}** · Region: **{display_region}** · Role: **{display_role}**"
    )

    if st.session_state.pop(saved_ok_key, False):
        st.success("Project created ✅")

    # ---- Form fields ----
    st.markdown(
        '<div style="margin:.25rem 0 1rem 0;">'
        'Fields marked with <span style="color:#d00000;font-weight:700">*</span> are required.'
        '</div>',
        unsafe_allow_html=True,
    )

    # ---------------- Basic Info ----------------
    name = st.text_input("Project Name *", key=k("name"))
    description = st.text_area("Description (optional)", key=k("desc"))

    # Assign to (Reps only)
    reps_df = query_df("""
        SELECT user_id, name, email
        FROM users
        WHERE is_active IS TRUE
          AND role = 'rep'
        ORDER BY name
    """)
    rep_labels = [""]
    rep_map = {}
    for r in reps_df.itertuples(index=False):
        lbl = f"{r.name} ({r.email})" if getattr(r, "email", None) else r.name
        rep_labels.append(lbl)
        rep_map[lbl] = int(r.user_id)

    assign_to_label = st.selectbox(
        "Assign To (Rep) *",
        rep_labels,
        index=0,
        key=k("rep_sel")
    )
    assigned_to_id = rep_map.get(assign_to_label)

    # Dates
    planned_start_date = st.date_input("Planned Start Date *", key=k("psd"))
    planned_end_date   = st.date_input("Planned End Date *", key=k("ped"))

    # ---------------- Region → City → Sector → Customer ----------------
    reg_df = query_df("""
        SELECT DISTINCT region
        FROM customers
        WHERE is_active IS TRUE
          AND region IS NOT NULL AND trim(region) <> ''
        ORDER BY region
    """)
    region_opts = [""] + reg_df["region"].tolist()
    region_choice = st.selectbox(
        "Region *",
        region_opts,
        index=0,
        key=k("region_sel"),
        on_change=_on_region_change
    )

    if region_choice:
        city_df = query_df("""
            SELECT DISTINCT city
            FROM customers
            WHERE is_active IS TRUE
              AND region = :r
              AND city IS NOT NULL AND trim(city) <> ''
            ORDER BY city
        """, {"r": region_choice})
        city_opts = [""] + city_df["city"].tolist()
    else:
        city_df = pd.DataFrame(columns=["city"])
        city_opts = [""]

    city_choice = st.selectbox(
        "City *",
        city_opts,
        index=0,
        key=k("city_sel"),
        disabled=(not region_choice),
        on_change=_on_city_change,
        help=None if region_choice else "Select a Region first",
    )

    if region_choice and city_choice:
        sec_df = query_df("""
            SELECT DISTINCT sector
            FROM customers
            WHERE is_active IS TRUE
              AND region = :r
              AND city   = :c
              AND sector IS NOT NULL AND trim(sector) <> ''
            ORDER BY sector
        """, {"r": region_choice, "c": city_choice})
        sector_opts = [""] + sec_df["sector"].tolist()
    else:
        sec_df = pd.DataFrame(columns=["sector"])
        sector_opts = [""]

    sector_choice = st.selectbox(
        "Sector *",
        sector_opts,
        index=0,
        key=k("sector_sel"),
        disabled=(not (region_choice and city_choice)),
        on_change=_on_sector_change,
        help=None if (region_choice and city_choice) else "Select a City first",
    )

    # Customer
    if region_choice and city_choice and sector_choice:
        cust_df = query_df("""
            SELECT customer_id, account_name
            FROM customers
            WHERE is_active IS TRUE
              AND region = :r
              AND city   = :c
              AND sector = :s
            ORDER BY account_name
        """, {"r": region_choice, "c": city_choice, "s": sector_choice})
        cust_opts = [""] + cust_df["account_name"].tolist()
    else:
        cust_df = pd.DataFrame(columns=["customer_id", "account_name"])
        cust_opts = [""]

    cust_choice = st.selectbox(
        "Customer *",
        cust_opts,
        index=0,
        key=k("cust_sel"),
        disabled=(not (region_choice and city_choice and sector_choice)),
        help=None if (region_choice and city_choice and sector_choice) else "Select Sector first",
    )

    customer_id = None
    if cust_choice:
        match = cust_df.loc[cust_df["account_name"] == cust_choice, "customer_id"]
        customer_id = int(match.iloc[0]) if not match.empty else None

    # ---------------- Business Unit → Category → Business Line → Article ----------------
    bu_df = query_df("""
        SELECT business_unit_id, name
        FROM business_units
        WHERE is_active IS TRUE
        ORDER BY name
    """)
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

    # Category
    if bu_id:
        cat_df = query_df("""
            SELECT DISTINCT category
            FROM business_lines
            WHERE is_active IS TRUE
              AND business_unit_id = :bid
              AND category IS NOT NULL
              AND trim(category) <> ''
            ORDER BY category
        """, {"bid": bu_id})
        cat_names = [""] + cat_df["category"].tolist()
    else:
        cat_df = pd.DataFrame(columns=["category"])
        cat_names = [""]

    cat_choice = st.selectbox(
        "Category *",
        cat_names,
        index=0,
        key=k("cat_sel"),
        disabled=(bu_id is None),
        help=None if bu_id else "Select a Business Unit first",
    )

    # Business Line
    bl_df = pd.DataFrame()
    bl_names = [""]
    bl_choice = ""
    business_line_id = None
    if bu_id and cat_choice:
        bl_df = query_df("""
            SELECT business_line_id, name
            FROM business_lines
            WHERE is_active IS TRUE
              AND business_unit_id = :bid
              AND category = :cat
            ORDER BY name
        """, {"bid": bu_id, "cat": cat_choice})
        bl_names = [""] + bl_df["name"].tolist()

    bl_choice = st.selectbox(
        "Business Line *",
        bl_names,
        index=0,
        key=k("bl_sel"),
        disabled=(bu_id is None or not cat_choice),
        on_change=_on_line_change,
        help=None if (bu_id and cat_choice) else "Select a Category first",
    )
    if bu_id and cat_choice and bl_choice:
        match = bl_df.loc[bl_df["name"] == bl_choice, "business_line_id"]
        business_line_id = int(match.iloc[0]) if not match.empty else None

    # Article Number (optional)
    prod_labels = [""]
    prod_df = pd.DataFrame()
    product_id = None
    if business_line_id:
        prod_df = query_df("""
            SELECT product_id, article_number, description
            FROM items
            WHERE is_active IS TRUE
              AND business_line_id = :blid
            ORDER BY COALESCE(article_number, product_id)
        """, {"blid": business_line_id})
        for r in prod_df.itertuples(index=False):
            label = (
                f"{(r.article_number or r.product_id)} — {r.description}"
                if pd.notna(r.description) and str(r.description).strip()
                else f"{(r.article_number or r.product_id)}"
            )
            prod_labels.append(label)

    prod_choice = st.selectbox(
        "Article Number (Product) — optional",
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
        product_id = label_to_pid.get(prod_choice)  # may be None (optional)

    # ---------------- Project Objective (with "Other" last & inactive custom) ----------------
    pobj_df = query_df("""
        SELECT project_objective_id, name
        FROM project_objectives
        WHERE COALESCE(is_active, TRUE) IS TRUE
        ORDER BY name
    """)
    existing_obj_names = pobj_df["name"].tolist()

    # "" (blank) + all active objectives + "Other" at the END
    pobj_names = [""] + existing_obj_names + ["Other"]

    pobj_choice = st.selectbox(
        "Project Objective *",
        pobj_names,
        index=0,
        key=k("pobj_sel"),
    )

    project_objective_id = None
    custom_objective_text = None

    if pobj_choice == "Other":
        custom_objective_text = st.text_input(
            "Specify Objective *",
            key=k("custom_obj"),
            placeholder="Enter custom project objective..."
        )
    elif pobj_choice:
        match = pobj_df.loc[pobj_df["name"] == pobj_choice, "project_objective_id"]
        project_objective_id = int(match.iloc[0]) if not match.empty else None

    # ---------------- Submit button (sticky / dedupe) ----------------
    inline_click = st.button(
        "Create Project",
        type="primary",
        key=k("submit_btn"),
        disabled=st.session_state[busy_key],
        help="Saves immediately. You'll see a spinner while saving."
    )

    if inline_click and not st.session_state[busy_key]:
        st.session_state[intent_key] = True
        st.session_state[busy_key]   = True
        st.rerun()

    if not st.session_state[intent_key]:
        return

    # ---------------- Validation + Save ----------------
    errors = []

    if not name.strip():
        errors.append("Please enter a **Project Name**.")
    if assigned_to_id is None:
        errors.append("Please choose an **Assign To (Rep)**.")
    if planned_end_date < planned_start_date:
        errors.append("**Planned End Date** cannot be before **Planned Start Date**.")
    if not region_choice:
        errors.append("Please choose a **Region**.")
    if not city_choice:
        errors.append("Please choose a **City**.")
    if not sector_choice:
        errors.append("Please choose a **Sector**.")
    if not customer_id:
        errors.append("Please choose a **Customer**.")
    if not bu_id:
        errors.append("Please choose a **Business Unit**.")
    if not cat_choice:
        errors.append("Please choose a **Category**.")
    if not business_line_id:
        errors.append("Please choose a **Business Line**.")

    # Objective validation
    if pobj_choice == "":
        errors.append("Please choose a **Project Objective**.")
    elif pobj_choice == "Other" and (not custom_objective_text or not custom_objective_text.strip()):
        errors.append("Please enter the **custom Objective**.")
    elif pobj_choice != "Other" and project_objective_id is None:
        errors.append("Please choose a valid **Project Objective**.")

    if errors:
        for msg in errors:
            st.error(msg)
        st.session_state[busy_key] = False
        st.session_state[intent_key] = False
        return

    with st.spinner("Creating project…"):
        # If manager entered custom objective, insert it into project_objectives as inactive
        if pobj_choice == "Other":
            try:
                new_obj_name = custom_objective_text.strip()
                # Avoid duplicates (case-insensitive, any active/inactive)
                existing = query_df(
                    """
                    SELECT project_objective_id
                    FROM project_objectives
                    WHERE lower(trim(name)) = lower(trim(:nm))
                    """,
                    {"nm": new_obj_name},
                )
                if not existing.empty:
                    project_objective_id = int(existing["project_objective_id"].iloc[0])
                else:
                    res = query_df(
                        """
                        INSERT INTO project_objectives (name, is_active)
                        VALUES (:n, FALSE)
                        RETURNING project_objective_id
                        """,
                        {"n": new_obj_name},
                    )
                    project_objective_id = int(res.iloc[0]["project_objective_id"])
            except Exception as e:
                st.error("Could not save the custom project objective.")
                st.caption(str(e))
                st.session_state[intent_key] = False
                st.session_state[busy_key] = False
                return

        project_row = {
            "name": name.strip(),
            "description": (description.strip() if description else None),
            "assigned_by_id": manager_id,
            "assigned_to_id": assigned_to_id,
            "business_line_id": int(business_line_id),
            "product_id": product_id,  # can be None
            "customer_id": int(customer_id),
            "planned_start_date": planned_start_date,
            "planned_end_date": planned_end_date,
            "actual_end_date": None,
            "status": "Not Started",
            "project_objective_id": int(project_objective_id),
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

        try:
            pid = insert_project(project_row)  # returns project_id
            st.session_state[nonce_key]    += 1
            st.session_state[saved_ok_key]  = True
            st.session_state[intent_key]    = False
            st.session_state[busy_key]      = False
            st.success("Project created successfully ✅")
            st.rerun()
        except Exception as e:
            st.error("Could not create the project.")
            st.caption(str(e))
            st.session_state[intent_key] = False
            st.session_state[busy_key] = False
