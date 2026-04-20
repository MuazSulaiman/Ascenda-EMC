# pages/admin_import.py
import io
import json
import re
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import streamlit as st
from sqlalchemy import text

from auth import resolve_session_user
from config import TIMEZONE
from db import engine
from db_ops import query_df, exec_sql
from utils import _utcnow_iso, _local_now_str, _utcnow
from widgets import set_current_page
from ui import section_header

def page_admin_import():
    section_header("Admin — Import Lookups", "Upload Excel/CSV files to populate lookup tables. Existing rows are kept; duplicates are skipped.")

    set_current_page("admin_import")

    # -----------------------
    # Progress UI helpers
    # -----------------------
    def _mk_status(title: str):
        """Return (status_ctx_or_spinner, progress_widget, line_widget, has_status)."""
        has_status = hasattr(st, "status")
        if has_status:
            sts = st.status(title, expanded=True)
            pb = st.progress(0)
            ln = st.empty()
            return (sts, pb, ln, True)
        sp = st.spinner(title + "…")
        pb = st.progress(0)
        ln = st.empty()
        return (sp, pb, ln, False)

    def _update_progress(pb, ln, i, total, inserted=0, updated=0, skipped=0, label_prefix=""):
        frac = max(0.0, min(1.0, (i / float(total)))) if total else 0.0
        pb.progress(frac)
        ln.write(f"{label_prefix} {i}/{total} · Inserted: {inserted} · Updated: {updated} · Skipped: {skipped}")

    def _finish_status(sts_or_spinner, has_status: bool, final_text: str, ok: bool = True):
        if has_status:
            state = "complete" if ok else "error"
            sts_or_spinner.update(label=final_text, state=state)
        else:
            (st.success if ok else st.error)(final_text)

    # -----------------------
    # Flash + utilities
    # -----------------------
    if "flash_admin" in st.session_state:
        level, msg = st.session_state.pop("flash_admin")
        getattr(st, level)(msg)

    def popout(label: str):
        """Used later for e.g. Danger Zone; safe wrapper around popover/expander."""
        if hasattr(st, "popover"):
            return st.popover(label)
        st.markdown(f"**{label}**")
        return st.expander(label, expanded=False)

    if "danger_nonce" not in st.session_state:
        st.session_state["danger_nonce"] = 0

    def _refcount(sql: str, params: dict) -> int:
        with engine.begin() as conn:
            r = conn.execute(text(sql), params).fetchone()
            return int(r[0]) if r and r[0] is not None else 0

    def _parts_join(*parts):
        return " - ".join([p for p in [str(x).strip() for x in parts] if p and p != "None"])

    def _norm_col(s: str) -> str:
        if s is None:
            return ""
        s = unicodedata.normalize("NFKC", str(s))
        s = s.replace("\u00A0", " ")
        s = s.strip().lower()
        s = re.sub(r"\s+", " ", s)
        s = s.replace(" ", "_")
        return s

    def _read_df_upload(file):
        if file.name.endswith(".xlsx"):
            df = pd.read_excel(file, dtype=str)
        else:
            df = pd.read_csv(file, dtype=str)
        df.columns = [_norm_col(c) for c in df.columns]
        return df

    def _norm_or_empty(v):
        return (v.strip() if isinstance(v, str) else v) or ""

    # =====================================================================
    # MAIN TABS FOR ENTITIES
    # =====================================================================
    main_tabs = st.tabs(
        ["Customers", "Target Audiences", "Business Units", "Business Lines", "Items", "Objectives"]
    )

    # =====================================================================
    # 1) CUSTOMERS
    # =====================================================================
    with main_tabs[0]:
        st.subheader("Customers")

        # Use radio instead of nested tabs so selection persists on rerun
        mode = st.radio(
            "Mode",
            ["➕ Add / Import", "📝 Manage"],
            index=0,
            key="cust_mode",
            horizontal=True,
        )

        # ---------------------------------------------------------------
        # Common dropdown data for sectors / regions (from existing data)
        # ---------------------------------------------------------------
        sec_df = query_df(
            """
            SELECT DISTINCT sector
            FROM customers
            WHERE sector IS NOT NULL AND sector <> ''
            ORDER BY sector
            """
        )
        sector_values = [str(r.sector).strip() for r in sec_df.itertuples(index=False) if str(r.sector).strip()]
        sector_options = [""] + sector_values + ["OTHER"]

        reg_df = query_df(
            """
            SELECT DISTINCT region
            FROM customers
            WHERE region IS NOT NULL AND region <> ''
            ORDER BY region
            """
        )
        region_values = [str(r.region).strip() for r in reg_df.itertuples(index=False) if str(r.region).strip()]
        region_options = [""] + region_values + ["OTHER"]

        # -------------------------
        # MODE 1: Add / Import
        # -------------------------
        if mode == "➕ Add / Import":
            st.markdown("### ➕ Add single customer")
            st.caption("Required field: **Account Name**. Sector, Region, and City are optional.")

            # init add-state
            st.session_state.setdefault("cust_add_acc", "")
            st.session_state.setdefault("cust_add_sector_opt", "")
            st.session_state.setdefault("cust_add_sector_other", "")
            st.session_state.setdefault("cust_add_region_opt", "")
            st.session_state.setdefault("cust_add_region_other", "")
            st.session_state.setdefault("cust_add_city_opt", "")
            st.session_state.setdefault("cust_add_city_other", "")

            # --- Account Name ---
            acc = st.text_input("Account Name *", key="cust_add_acc")

            # --- Sector ---
            if st.session_state["cust_add_sector_opt"] not in sector_options:
                st.session_state["cust_add_sector_opt"] = ""
            sec_idx = sector_options.index(st.session_state["cust_add_sector_opt"])

            sector_sel = st.selectbox(
                "Sector",
                sector_options,
                index=sec_idx,
                key="cust_add_sector_opt",
            )
            if sector_sel == "OTHER":
                sector_other = st.text_input("Other sector", key="cust_add_sector_other")
            else:
                sector_other = st.session_state.get("cust_add_sector_other", "")

            # --- Region ---
            if st.session_state["cust_add_region_opt"] not in region_options:
                st.session_state["cust_add_region_opt"] = ""
            reg_idx = region_options.index(st.session_state["cust_add_region_opt"])

            region_sel = st.selectbox(
                "Region",
                region_options,
                index=reg_idx,
                key="cust_add_region_opt",
            )
            if region_sel == "OTHER":
                region_other = st.text_input("Other region", key="cust_add_region_other")
            else:
                region_other = st.session_state.get("cust_add_region_other", "")

            # --- City (depends on region) ---
            if region_sel not in ("", "OTHER"):
                city_df = query_df(
                    """
                    SELECT DISTINCT city
                    FROM customers
                    WHERE region = :r
                      AND city IS NOT NULL AND city <> ''
                    ORDER BY city
                    """,
                    {"r": region_sel},
                )
                city_values = [str(r.city).strip() for r in city_df.itertuples(index=False) if str(r.city).strip()]
                city_options = [""] + city_values + ["OTHER"]
            else:
                city_options = ["", "OTHER"]

            if st.session_state["cust_add_city_opt"] not in city_options:
                st.session_state["cust_add_city_opt"] = ""
            city_idx = city_options.index(st.session_state["cust_add_city_opt"])

            city_sel = st.selectbox(
                "City",
                city_options,
                index=city_idx,
                key="cust_add_city_opt",
            )
            if city_sel == "OTHER":
                city_other = st.text_input("Other city", key="cust_add_city_other")
            else:
                city_other = st.session_state.get("cust_add_city_other", "")

            # --- Save button ---
            if st.button("Save Customer", type="primary", key="cust_add_save"):
                if not acc.strip():
                    st.error("Account Name is required.")
                else:
                    try:
                        acc_v = acc.strip()

                        # resolve sector
                        if sector_sel == "":
                            sector_v = None
                        elif sector_sel == "OTHER":
                            sector_v = (sector_other or "").strip() or None
                        else:
                            sector_v = sector_sel

                        # resolve region
                        if region_sel == "":
                            region_v = None
                        elif region_sel == "OTHER":
                            region_v = (region_other or "").strip() or None
                        else:
                            region_v = region_sel

                        # resolve city
                        if city_sel == "":
                            city_v = None
                        elif city_sel == "OTHER":
                            city_v = (city_other or "").strip() or None
                        else:
                            city_v = city_sel

                        with engine.begin() as conn:
                            res = conn.execute(
                                text(
                                    """
                                    INSERT INTO customers(account_name, sector, region, city)
                                    SELECT :acc, :sector, :region, :city
                                    WHERE NOT EXISTS (
                                        SELECT 1
                                        FROM customers c
                                        WHERE lower(coalesce(c.account_name, '')) = lower(coalesce(:acc, ''))
                                          AND lower(coalesce(c.sector,       '')) = lower(coalesce(:sector, ''))
                                          AND lower(coalesce(c.region,       '')) = lower(coalesce(:region, ''))
                                          AND lower(coalesce(c.city,         '')) = lower(coalesce(:city, ''))
                                    )
                                    """
                                ),
                                {"acc": acc_v, "sector": sector_v, "region": region_v, "city": city_v},
                            )

                        if (res.rowcount or 0) > 0:
                            st.success("Customer added ✅")
                            # reset form
                            for key in (
                                "cust_add_acc",
                                "cust_add_sector_opt",
                                "cust_add_sector_other",
                                "cust_add_region_opt",
                                "cust_add_region_other",
                                "cust_add_city_opt",
                                "cust_add_city_other",
                            ):
                                st.session_state.pop(key, None)
                        else:
                            st.info(
                                "A customer with the same **Name + Sector + Region + City** already exists — nothing added."
                            )
                    except Exception as e:
                        st.error("Could not add customer.")
                        st.caption(str(e))

            st.markdown("---")
            st.markdown("### ⬆️ Bulk upload customers (Excel/CSV)")
            st.write("Columns: **account_name**, sector, region, city")

            f1 = st.file_uploader(
                "Upload Customers file", type=["xlsx", "csv"], key="cust_upload"
            )
            if f1 is not None:
                df = _read_df_upload(f1)

                if "account_name" not in df.columns:
                    st.error("Missing required column: account_name")
                else:
                    total = len(df)
                    inserted = 0
                    skipped = 0
                    sts, pb, ln, has_status = _mk_status("Importing Customers…")

                    try:
                        with engine.begin() as conn:
                            for i, r in enumerate(df.itertuples(index=False), start=1):
                                acc_raw = getattr(r, "account_name", "")
                                acc_v = str(acc_raw).strip() if pd.notna(acc_raw) else ""
                                if not acc_v:
                                    skipped += 1
                                    if i % 200 == 0 or i == total:
                                        _update_progress(
                                            pb, ln, i, total, inserted, 0, skipped, label_prefix="Customers"
                                        )
                                    continue

                                sector_v = (
                                    str(getattr(r, "sector")).strip()
                                    if hasattr(r, "sector") and pd.notna(getattr(r, "sector"))
                                    else None
                                )
                                region_v = (
                                    str(getattr(r, "region")).strip()
                                    if hasattr(r, "region") and pd.notna(getattr(r, "region"))
                                    else None
                                )
                                city_v = (
                                    str(getattr(r, "city")).strip()
                                    if hasattr(r, "city") and pd.notna(getattr(r, "city"))
                                    else None
                                )

                                res = conn.execute(
                                    text(
                                        """
                                        INSERT INTO customers(account_name, sector, region, city)
                                        SELECT :acc, :sector, :region, :city
                                        WHERE NOT EXISTS (
                                            SELECT 1
                                            FROM customers c
                                            WHERE lower(coalesce(c.account_name, '')) = lower(coalesce(:acc, ''))
                                              AND lower(coalesce(c.sector,       '')) = lower(coalesce(:sector, ''))
                                              AND lower(coalesce(c.region,       '')) = lower(coalesce(:region, ''))
                                              AND lower(coalesce(c.city,         '')) = lower(coalesce(:city, ''))
                                        )
                                        """
                                    ),
                                    {"acc": acc_v, "sector": sector_v, "region": region_v, "city": city_v},
                                )

                                if (res.rowcount or 0) > 0:
                                    inserted += 1
                                else:
                                    skipped += 1

                                if i % 200 == 0 or i == total:
                                    _update_progress(
                                        pb, ln, i, total, inserted, 0, skipped, label_prefix="Customers"
                                    )
                                    time.sleep(0.001)

                        _finish_status(
                            sts,
                            has_status,
                            f"Customers import ✅ Inserted: {inserted} | Skipped: {skipped}",
                            ok=True,
                        )
                    except Exception as e:
                        _finish_status(sts, has_status, "Customers import failed ❌", ok=False)
                        st.caption(str(e))

        # -------------------------
        # MODE 2: Manage
        # -------------------------
        else:  # mode == "📝 Manage"
            st.markdown("### 📝 Manage customers")

            cdf = query_df(
                """
                SELECT customer_id,
                       account_name,
                       sector,
                       region,
                       city,
                       COALESCE(is_active, TRUE) AS is_active
                FROM customers
                ORDER BY account_name
                """
            )

            if cdf.empty:
                st.info("No customers yet.")
            else:
                options = [
                    _parts_join(r.customer_id, r.account_name, r.region, r.city)
                    + f" ({'active' if bool(r.is_active) else 'inactive'})"
                    for r in cdf.itertuples(index=False)
                ]
                options = [""] + options

                sel_label = st.selectbox(
                    "Select customer", options, index=0, key="mg_cust_sel"
                )

                if sel_label == "":
                    st.info("Please select a customer.")
                else:
                    row_idx = options.index(sel_label) - 1
                    row = cdf.iloc[row_idx]
                    cid = int(row["customer_id"])

                    # quick refs / status
                    colA, colB = st.columns([1, 1])
                    with colA:
                        v_cnt = _refcount("SELECT COUNT(*) FROM visits WHERE customer_id=:cid", {"cid": cid})
                        a_cnt = _refcount("SELECT COUNT(*) FROM target_audiences WHERE customer_id=:cid", {"cid": cid})
                        st.caption(f"Refs → Visits: **{v_cnt}** · Audiences: **{a_cnt}**")
                    with colB:
                        st.caption("Status")
                        st.write("✅ Active" if bool(row["is_active"]) else "🚫 Inactive")

                    st.markdown("---")
                    st.markdown("#### Edit customer")

                    base_key = f"mg_cust_{cid}"

                    # ----- Account Name FIRST -----
                    acc_edit = st.text_input(
                        "Account Name *",
                        value=row["account_name"] or "",
                        key=f"{base_key}_acc"
                    )

                    # ----- Sector -----
                    sec_key = base_key + "_sector_opt"
                    sec_other_key = base_key + "_sector_other"

                    existing_sec = (row["sector"] or "").strip() if row["sector"] else ""

                    if sec_key not in st.session_state:
                        if existing_sec and existing_sec in sector_options:
                            st.session_state[sec_key] = existing_sec
                        elif existing_sec:
                            st.session_state[sec_key] = "OTHER"
                            st.session_state[sec_other_key] = existing_sec
                        else:
                            st.session_state[sec_key] = ""

                    # keep state valid against current options
                    if st.session_state[sec_key] not in sector_options:
                        st.session_state[sec_key] = ""

                    sector_sel_edit = st.selectbox(
                        "Sector",
                        sector_options,
                        key=sec_key,   # ❗ no index here
                    )
                    if sector_sel_edit == "OTHER":
                        sector_other_edit = st.text_input("Other sector", key=sec_other_key)
                    else:
                        sector_other_edit = st.session_state.get(sec_other_key, "")

                    # ----- Region -----
                    reg_key = base_key + "_region_opt"
                    reg_other_key = base_key + "_region_other"

                    existing_reg = (row["region"] or "").strip() if row["region"] else ""

                    if reg_key not in st.session_state:
                        if existing_reg and existing_reg in region_options:
                            st.session_state[reg_key] = existing_reg
                        elif existing_reg:
                            st.session_state[reg_key] = "OTHER"
                            st.session_state[reg_other_key] = existing_reg
                        else:
                            st.session_state[reg_key] = ""

                    if st.session_state[reg_key] not in region_options:
                        st.session_state[reg_key] = ""

                    region_sel_edit = st.selectbox(
                        "Region",
                        region_options,
                        key=reg_key,   # ❗ no index here
                    )
                    if region_sel_edit == "OTHER":
                        region_other_edit = st.text_input("Other region", key=reg_other_key)
                    else:
                        region_other_edit = st.session_state.get(reg_other_key, "")

                    # ----- City (dependent) -----
                    city_key = base_key + "_city_opt"
                    city_other_key = base_key + "_city_other"

                    if region_sel_edit not in ("", "OTHER"):
                        city_df = query_df(
                            """
                            SELECT DISTINCT city
                            FROM customers
                            WHERE region = :r
                              AND city IS NOT NULL AND city <> ''
                            ORDER BY city
                            """,
                            {"r": region_sel_edit},
                        )
                        city_vals = [str(r.city).strip() for r in city_df.itertuples(index=False) if str(r.city).strip()]
                        city_options_edit = [""] + city_vals + ["OTHER"]
                    else:
                        city_options_edit = ["", "OTHER"]

                    existing_city = (row["city"] or "").strip() if row["city"] else ""

                    if city_key not in st.session_state:
                        if existing_city and existing_city in city_options_edit:
                            st.session_state[city_key] = existing_city
                        elif existing_city:
                            st.session_state[city_key] = "OTHER"
                            st.session_state[city_other_key] = existing_city
                        else:
                            st.session_state[city_key] = ""

                    if st.session_state[city_key] not in city_options_edit:
                        st.session_state[city_key] = ""

                    city_sel_edit = st.selectbox(
                        "City",
                        city_options_edit,
                        key=city_key,   # ❗ no index here
                    )
                    if city_sel_edit == "OTHER":
                        city_other_edit = st.text_input("Other city", key=city_other_key)
                    else:
                        city_other_edit = st.session_state.get(city_other_key, "")

                    # ----- Active toggle -----
                    active_flag = st.checkbox(
                        "Active",
                        value=bool(row["is_active"]),
                        key=f"{base_key}_active",
                        help="Uncheck to deactivate this customer.",
                    )

                    # ----- Save button -----
                    if st.button("Save changes", type="primary", key=f"{base_key}_save"):
                        acc_clean = acc_edit.strip()
                        if not acc_clean:
                            st.error("Account Name is required.")
                        else:
                            # resolve sector
                            if sector_sel_edit == "":
                                sector_v = None
                            elif sector_sel_edit == "OTHER":
                                sector_v = (sector_other_edit or "").strip() or None
                            else:
                                sector_v = sector_sel_edit

                            # resolve region
                            if region_sel_edit == "":
                                region_v = None
                            elif region_sel_edit == "OTHER":
                                region_v = (region_other_edit or "").strip() or None
                            else:
                                region_v = region_sel_edit

                            # resolve city
                            if city_sel_edit == "":
                                city_v = None
                            elif city_sel_edit == "OTHER":
                                city_v = (city_other_edit or "").strip() or None
                            else:
                                city_v = city_sel_edit

                            dup = query_df(
                                """
                                SELECT 1
                                FROM customers
                                WHERE lower(account_name)=lower(:n)
                                  AND customer_id<>:id
                                """,
                                {"n": acc_clean, "id": cid},
                            )
                            if not dup.empty:
                                st.error("Account Name already exists.")
                            else:
                                try:
                                    exec_sql(
                                        """
                                        UPDATE customers
                                        SET account_name=:acc,
                                            sector=:s,
                                            region=:r,
                                            city=:c,
                                            is_active=:b
                                        WHERE customer_id=:id
                                        """,
                                        {
                                            "acc": acc_clean,
                                            "s": sector_v,
                                            "r": region_v,
                                            "c": city_v,
                                            "b": bool(active_flag),
                                            "id": cid,
                                        },
                                    )
                                    st.success("Customer updated ✅")
                                except Exception as e:
                                    st.error("Could not update customer.")
                                    st.caption(str(e))

                    st.markdown("---")
                    st.markdown("#### 🔴 Danger Zone")
                    st.write("Delete this customer permanently (only if not referenced by visits/audiences).")

                    del_conf_key = f"{base_key}_del_conf"
                    del_confirm = st.checkbox(
                        "I understand this cannot be undone.",
                        key=del_conf_key,
                    )

                    if st.button(
                        "Delete Customer",
                        type="primary",
                        disabled=not del_confirm,
                        key=f"{base_key}_del",
                    ):
                        if v_cnt > 0 or a_cnt > 0:
                            st.error(
                                "Cannot delete: this customer is referenced by visits and/or target audiences. "
                                "Deactivate instead."
                            )
                        else:
                            try:
                                exec_sql("DELETE FROM customers WHERE customer_id=:id", {"id": cid})
                                st.success("Customer deleted ✅")

                                # reset customer selection safely
                                st.session_state.pop("mg_cust_sel", None)

                                # optional: refresh UI so the deleted customer disappears from lists
                                st.rerun()

                            except Exception as e:
                                st.error("Delete failed.")
                                st.caption(str(e))

    # =====================================================================
    # 2) TARGET AUDIENCES
    # =====================================================================
    with main_tabs[1]:
        st.subheader("Target Audiences")

        mode = st.radio(
            "Mode",
            ["➕ Add / Import", "📝 Manage"],
            index=0,
            key="aud_mode",
            horizontal=True,
        )

        # -----------------------------
        # Common lookup data
        # -----------------------------
        # Customers (for both add & manage)
        cust_df = query_df(
            """
            SELECT customer_id,
                   account_name,
                   region,
                   city,
                   COALESCE(is_active, TRUE) AS is_active
            FROM customers
            ORDER BY account_name
            """
        )

        # Distinct departments and positions (for dropdowns)
        dept_df = query_df(
            """
            SELECT DISTINCT department
            FROM target_audiences
            WHERE department IS NOT NULL AND department <> ''
            ORDER BY department
            """
        )
        dept_values = [str(r.department).strip() for r in dept_df.itertuples(index=False) if str(r.department).strip()]
        dept_options = [""] + dept_values + ["OTHER"]

        pos_df = query_df(
            """
            SELECT DISTINCT position
            FROM target_audiences
            WHERE position IS NOT NULL AND position <> ''
            ORDER BY position
            """
        )
        pos_values = [str(r.position).strip() for r in pos_df.itertuples(index=False) if str(r.position).strip()]
        pos_options = [""] + pos_values + ["OTHER"]

        TITLE_OPTIONS = ["", "Dr.", "Mr.", "Ms.", "Mrs.", "Prof.", "Eng.", "Other"]

        # Helper to build customer labels
        def _fmt_cust_label(r):
            base = _parts_join(r.customer_id, r.account_name, r.region, r.city)
            return base + f" ({'active' if bool(r.is_active) else 'inactive'})"

        # ==============================================================
        # MODE 1: Add / Import Target Audiences
        # ==============================================================
        if mode == "➕ Add / Import":
            st.markdown("### ➕ Add single target audience")

            if cust_df.empty:
                st.warning("No customers found. Please add customers first.")
            else:
                # -------- Customer select --------
                cust_labels = [""] + [_fmt_cust_label(r) for r in cust_df.itertuples(index=False)]
                cust_choice = st.selectbox(
                    "Customer *",
                    cust_labels,
                    index=0,
                    key="aud_add_cust",
                )

                if cust_choice:
                    cust_row = cust_df.iloc[cust_labels.index(cust_choice) - 1]
                    cid = int(cust_row["customer_id"])
                else:
                    cid = None

                # -------- Title dropdown --------
                title_choice = st.selectbox("Title", TITLE_OPTIONS, index=0, key="aud_add_title_opt")
                title_other_val = ""
                if title_choice == "Other":
                    title_other_val = st.text_input("Other title", key="aud_add_title_other")

                # -------- Name (required) --------
                name_val = st.text_input("Name *", key="aud_add_name")

                # -------- Department dropdown --------
                dept_choice = st.selectbox(
                    "Department",
                    dept_options,
                    index=0,
                    key="aud_add_dept_opt",
                )
                dept_other_val = ""
                if dept_choice == "OTHER":
                    dept_other_val = st.text_input("Other department", key="aud_add_dept_other")

                # -------- Position dropdown --------
                pos_choice = st.selectbox(
                    "Position",
                    pos_options,
                    index=0,
                    key="aud_add_pos_opt",
                )
                pos_other_val = ""
                if pos_choice == "OTHER":
                    pos_other_val = st.text_input("Other position", key="aud_add_pos_other")

                # -------- Other fields --------
                pot_val = st.text_input("Potentiality", key="aud_add_pot")
                loy_val = st.text_input("Loyalty", key="aud_add_loy")
                mob_val = st.text_input("Mobile", key="aud_add_mobile")
                land_val = st.text_input("Landline", key="aud_add_landline")
                ext_val = st.text_input("External Number", key="aud_add_ext")
                email_val = st.text_input("Email", key="aud_add_email")

                if st.button("Save Target Audience", type="primary", key="aud_add_save"):
                    if not cid:
                        st.error("Customer is required.")
                    elif not name_val.strip():
                        st.error("Name is required.")
                    else:
                        try:
                            # Resolve title
                            if title_choice == "":
                                title_v = None
                            elif title_choice == "Other":
                                title_v = (title_other_val or "").strip() or None
                            else:
                                title_v = title_choice

                            # Resolve department
                            if dept_choice == "":
                                dept_v = None
                            elif dept_choice == "OTHER":
                                dept_v = (dept_other_val or "").strip() or None
                            else:
                                dept_v = dept_choice

                            # Resolve position
                            if pos_choice == "":
                                pos_v = None
                            elif pos_choice == "OTHER":
                                pos_v = (pos_other_val or "").strip() or None
                            else:
                                pos_v = pos_choice

                            nm_clean = name_val.strip()

                            # Duplicate check: same (customer + name + dept + position)
                            dup = query_df(
                                """
                                SELECT 1
                                FROM target_audiences
                                WHERE customer_id=:cid
                                  AND lower(coalesce(name, '')) = lower(:nm)
                                  AND lower(coalesce(department, '')) = lower(coalesce(:dept, ''))
                                  AND lower(coalesce(position, '')) = lower(coalesce(:pos, ''))
                                LIMIT 1
                                """,
                                {"cid": cid, "nm": nm_clean, "dept": (dept_v or ""), "pos": (pos_v or "")},
                            )
                            if not dup.empty:
                                st.info(
                                    "This combination (Customer + Name + Department + Position) already exists — skipped."
                                )
                            else:
                                with engine.begin() as conn:
                                    conn.execute(
                                        text(
                                            """
                                            INSERT INTO target_audiences(
                                                customer_id, title, name, department, position,
                                                potentiality, loyalty,
                                                mobile, landline, external_number, email, is_active
                                            )
                                            VALUES (
                                                :cid, :title, :name, :dept, :pos,
                                                :pot, :loy,
                                                :mob, :land, :extn, :email, TRUE
                                            )
                                            """
                                        ),
                                        {
                                            "cid": cid,
                                            "title": title_v,
                                            "name": nm_clean,
                                            "dept": dept_v,
                                            "pos": pos_v,
                                            "pot": (pot_val.strip() or None),
                                            "loy": (loy_val.strip() or None),
                                            "mob": (mob_val.strip() or None),
                                            "land": (land_val.strip() or None),
                                            "extn": (ext_val.strip() or None),
                                            "email": (email_val.strip() or None),
                                        },
                                    )
                                st.success("Target audience added ✅")
                        except Exception as e:
                            st.error("Could not add target audience.")
                            st.caption(str(e))

            # -------- Bulk upload (same as before) --------
            st.markdown("---")
            st.markdown("### ⬆️ Bulk upload target audiences (Excel/CSV)")
            st.write("Columns: **customer_name**, name, title, department, position, potentiality, loyalty, mobile, landline, external_number, email")

            f2 = st.file_uploader("Upload Target Audiences", type=["xlsx", "csv"], key="aud_upload")
            if f2 is not None:
                df = _read_df_upload(f2)
                needed = {"customer_name", "name"}
                if not needed.issubset(df.columns):
                    st.error("Missing required columns: customer_name, name")
                else:
                    total = len(df)
                    inserted = 0
                    skipped = 0
                    sts, pb, ln, has_status = _mk_status("Importing Target Audiences…")

                    try:
                        with engine.begin() as conn:
                            cdf = pd.read_sql_query(
                                text("SELECT customer_id, account_name FROM customers"),
                                conn,
                            )
                            cmap = {
                                str(r.account_name).strip().lower(): int(r.customer_id)
                                for r in cdf.itertuples(index=False)
                            }

                            for i, r in enumerate(df.itertuples(index=False), start=1):
                                cname = str(getattr(r, "customer_name", "")).strip()
                                aname = str(getattr(r, "name", "")).strip()
                                if not (cname and aname):
                                    skipped += 1
                                    continue

                                cid = cmap.get(cname.lower())
                                if not cid:
                                    skipped += 1
                                    continue

                                title_v = (
                                    str(getattr(r, "title")).strip()
                                    if hasattr(r, "title") and pd.notna(getattr(r, "title"))
                                    else None
                                )
                                dept_v = (
                                    str(getattr(r, "department")).strip()
                                    if hasattr(r, "department") and pd.notna(getattr(r, "department"))
                                    else None
                                )
                                pos_v = (
                                    str(getattr(r, "position")).strip()
                                    if hasattr(r, "position") and pd.notna(getattr(r, "position"))
                                    else None
                                )
                                pot_v = (
                                    str(getattr(r, "potentiality")).strip()
                                    if hasattr(r, "potentiality") and pd.notna(getattr(r, "potentiality"))
                                    else None
                                )
                                loy_v = (
                                    str(getattr(r, "loyalty")).strip()
                                    if hasattr(r, "loyalty") and pd.notna(getattr(r, "loyalty"))
                                    else None
                                )
                                mob_v = (
                                    str(getattr(r, "mobile")).strip()
                                    if hasattr(r, "mobile") and pd.notna(getattr(r, "mobile"))
                                    else None
                                )
                                land_v = (
                                    str(getattr(r, "landline")).strip()
                                    if hasattr(r, "landline") and pd.notna(getattr(r, "landline"))
                                    else None
                                )
                                extn_v = (
                                    str(getattr(r, "external_number")).strip()
                                    if hasattr(r, "external_number") and pd.notna(getattr(r, "external_number"))
                                    else None
                                )
                                email_v = (
                                    str(getattr(r, "email")).strip()
                                    if hasattr(r, "email") and pd.notna(getattr(r, "email"))
                                    else None
                                )

                                dup = conn.execute(
                                    text(
                                        """
                                        SELECT 1
                                        FROM target_audiences
                                        WHERE customer_id = :cid
                                          AND lower(coalesce(name, '')) = lower(:name)
                                          AND lower(coalesce(department, '')) = lower(coalesce(:dept, ''))
                                          AND lower(coalesce(position, '')) = lower(coalesce(:pos, ''))
                                        LIMIT 1
                                        """
                                    ),
                                    {"cid": cid, "name": aname, "dept": (dept_v or ""), "pos": (pos_v or "")},
                                ).fetchone()

                                if dup:
                                    skipped += 1
                                    continue

                                conn.execute(
                                    text(
                                        """
                                        INSERT INTO target_audiences(
                                            customer_id, title, name, department, position,
                                            potentiality, loyalty,
                                            mobile, landline, external_number, email, is_active
                                        )
                                        VALUES (
                                            :cid, :title, :name, :dept, :pos,
                                            :pot, :loy,
                                            :mob, :land, :extn, :email, TRUE
                                        )
                                        """
                                    ),
                                    {
                                        "cid": cid,
                                        "title": title_v,
                                        "name": aname,
                                        "dept": dept_v,
                                        "pos": pos_v,
                                        "pot": pot_v,
                                        "loy": loy_v,
                                        "mob": mob_v,
                                        "land": land_v,
                                        "extn": extn_v,
                                        "email": email_v,
                                    },
                                )
                                inserted += 1

                                if i % 200 == 0 or i == total:
                                    _update_progress(
                                        pb, ln, i, total, inserted, 0, skipped, label_prefix="Audiences"
                                    )
                                    time.sleep(0.001)

                        _finish_status(
                            sts,
                            has_status,
                            f"✅ Target audiences import done. Inserted: {inserted} | Skipped: {skipped}",
                            ok=True,
                        )
                    except Exception as e:
                        _finish_status(sts, has_status, "❌ Target audiences import failed.", ok=False)
                        st.caption(str(e))

        # ==============================================================
        # MODE 2: Manage Target Audiences
        # ==============================================================
        else:  # mode == "📝 Manage"
            st.markdown("### 📝 Manage target audiences")

            if cust_df.empty:
                st.info("No customers yet.")
            else:
                # First select customer (same style as Customers tab)
                cust_labels = [""] + [_fmt_cust_label(r) for r in cust_df.itertuples(index=False)]
                cust_choice = st.selectbox(
                    "Select customer",
                    cust_labels,
                    index=0,
                    key="mg_aud_cust_sel",
                )

                if not cust_choice:
                    st.info("Please select a customer.")
                else:
                    cust_row = cust_df.iloc[cust_labels.index(cust_choice) - 1]
                    cid = int(cust_row["customer_id"])

                    # Load audiences for this customer
                    adf = query_df(
                        """
                        SELECT audience_id,
                               customer_id,
                               title,
                               name,
                               department,
                               position,
                               potentiality,
                               loyalty,
                               mobile,
                               landline,
                               external_number,
                               email,
                               COALESCE(is_active, TRUE) AS is_active
                        FROM target_audiences
                        WHERE customer_id = :cid
                        ORDER BY name
                        """,
                        {"cid": cid},
                    )

                    if adf.empty:
                        st.info("No target audiences for this customer yet.")
                    else:
                        def _fmt_aud_label(r):
                            # ID first, then (title + name), department, position – without customer name
                            title_name = (
                                ((str(r.title).strip() + " ") if r.title else "")
                                + (str(r.name).strip() if r.name else "")
                            ).strip()
                            parts = [str(r.audience_id), title_name]
                            if r.department and str(r.department).strip():
                                parts.append(str(r.department).strip())
                            if r.position and str(r.position).strip():
                                parts.append(str(r.position).strip())
                            base = " - ".join([p for p in parts if p])
                            return base + f" ({'active' if bool(r.is_active) else 'inactive'})"

                        aud_labels = [""] + [_fmt_aud_label(r) for r in adf.itertuples(index=False)]
                        aud_choice = st.selectbox(
                            "Select target audience",
                            aud_labels,
                            index=0,
                            key="mg_aud_sel",
                        )

                        if not aud_choice:
                            st.info("Please select a target audience.")
                        else:
                            row = adf.iloc[aud_labels.index(aud_choice) - 1]
                            aid = int(row["audience_id"])

                            # --- Refs & status ---
                            colA, colB = st.columns([1, 1])
                            with colA:
                                v_cnt = _refcount(
                                    "SELECT COUNT(*) FROM visits WHERE audience_id=:aid",
                                    {"aid": aid},
                                )
                                st.caption(f"Refs → Visits: **{v_cnt}**")
                            with colB:
                                st.caption("Status")
                                st.write("✅ Active" if bool(row["is_active"]) else "🚫 Inactive")

                            st.markdown("---")
                            st.markdown("#### Edit target audience")

                            base_key = f"mg_aud_{aid}"

                            # ----- Name (required) -----
                            name_edit = st.text_input(
                                "Name *",
                                value=row["name"] or "",
                                key=f"{base_key}_name",
                            )

                            # ----- Title dropdown -----
                            current_title = (row["title"] or "").strip() if row["title"] else ""
                            if current_title and current_title in TITLE_OPTIONS:
                                title_idx = TITLE_OPTIONS.index(current_title)
                                title_default_other = ""
                            elif current_title:
                                title_idx = TITLE_OPTIONS.index("Other")
                                title_default_other = current_title
                            else:
                                title_idx = 0
                                title_default_other = ""

                            title_sel = st.selectbox(
                                "Title",
                                TITLE_OPTIONS,
                                index=title_idx,
                                key=f"{base_key}_title_opt",
                            )
                            title_other_edit = ""
                            if title_sel == "Other":
                                title_other_edit = st.text_input(
                                    "Other title",
                                    value=title_default_other,
                                    key=f"{base_key}_title_other",
                                )

                            # ----- Department dropdown -----
                            current_dept = (row["department"] or "").strip() if row["department"] else ""
                            if current_dept and current_dept in dept_options:
                                dept_idx = dept_options.index(current_dept)
                                dept_default_other = ""
                            elif current_dept:
                                dept_idx = dept_options.index("OTHER")
                                dept_default_other = current_dept
                            else:
                                dept_idx = 0
                                dept_default_other = ""

                            dept_sel = st.selectbox(
                                "Department",
                                dept_options,
                                index=dept_idx,
                                key=f"{base_key}_dept_opt",
                            )
                            dept_other_edit = ""
                            if dept_sel == "OTHER":
                                dept_other_edit = st.text_input(
                                    "Other department",
                                    value=dept_default_other,
                                    key=f"{base_key}_dept_other",
                                )

                            # ----- Position dropdown -----
                            current_pos = (row["position"] or "").strip() if row["position"] else ""
                            if current_pos and current_pos in pos_options:
                                pos_idx = pos_options.index(current_pos)
                                pos_default_other = ""
                            elif current_pos:
                                pos_idx = pos_options.index("OTHER")
                                pos_default_other = current_pos
                            else:
                                pos_idx = 0
                                pos_default_other = ""

                            pos_sel = st.selectbox(
                                "Position",
                                pos_options,
                                index=pos_idx,
                                key=f"{base_key}_pos_opt",
                            )
                            pos_other_edit = ""
                            if pos_sel == "OTHER":
                                pos_other_edit = st.text_input(
                                    "Other position",
                                    value=pos_default_other,
                                    key=f"{base_key}_pos_other",
                                )

                            # ----- Other fields -----
                            pot_edit = st.text_input(
                                "Potentiality",
                                value=row["potentiality"] or "",
                                key=f"{base_key}_pot",
                            )
                            loy_edit = st.text_input(
                                "Loyalty",
                                value=row["loyalty"] or "",
                                key=f"{base_key}_loy",
                            )
                            mob_edit = st.text_input(
                                "Mobile",
                                value=row["mobile"] or "",
                                key=f"{base_key}_mob",
                            )
                            land_edit = st.text_input(
                                "Landline",
                                value=row["landline"] or "",
                                key=f"{base_key}_land",
                            )
                            ext_edit = st.text_input(
                                "External Number",
                                value=row["external_number"] or "",
                                key=f"{base_key}_ext",
                            )
                            email_edit = st.text_input(
                                "Email",
                                value=row["email"] or "",
                                key=f"{base_key}_email",
                            )

                            active_flag = st.checkbox(
                                "Active",
                                value=bool(row["is_active"]),
                                key=f"{base_key}_active",
                                help="Uncheck to deactivate this target audience.",
                            )

                            # ----- Save button -----
                            if st.button("Save changes", type="primary", key=f"{base_key}_save"):
                                nm_clean = name_edit.strip()
                                if not nm_clean:
                                    st.error("Name is required.")
                                else:
                                    # Resolve title
                                    if title_sel == "":
                                        title_v = None
                                    elif title_sel == "Other":
                                        title_v = (title_other_edit or "").strip() or None
                                    else:
                                        title_v = title_sel

                                    # Resolve department
                                    if dept_sel == "":
                                        dept_v = None
                                    elif dept_sel == "OTHER":
                                        dept_v = (dept_other_edit or "").strip() or None
                                    else:
                                        dept_v = dept_sel

                                    # Resolve position
                                    if pos_sel == "":
                                        pos_v = None
                                    elif pos_sel == "OTHER":
                                        pos_v = (pos_other_edit or "").strip() or None
                                    else:
                                        pos_v = pos_sel

                                    # Duplicate check (same customer + name + dept + position, excluding this audience)
                                    dup = query_df(
                                        """
                                        SELECT 1
                                        FROM target_audiences
                                        WHERE customer_id = :cid
                                          AND lower(coalesce(name, '')) = lower(:nm)
                                          AND lower(coalesce(department, '')) = lower(coalesce(:dept, ''))
                                          AND lower(coalesce(position, '')) = lower(coalesce(:pos, ''))
                                          AND audience_id <> :aid
                                        LIMIT 1
                                        """,
                                        {
                                            "cid": cid,
                                            "nm": nm_clean,
                                            "dept": (dept_v or ""),
                                            "pos": (pos_v or ""),
                                            "aid": aid,
                                        },
                                    )

                                    if not dup.empty:
                                        st.error(
                                            "Another target audience with the same (Name + Department + Position) already exists for this customer."
                                        )
                                    else:
                                        try:
                                            exec_sql(
                                                """
                                                UPDATE target_audiences
                                                SET title=:title,
                                                    name=:name,
                                                    department=:dept,
                                                    position=:pos,
                                                    potentiality=:pot,
                                                    loyalty=:loy,
                                                    mobile=:mob,
                                                    landline=:land,
                                                    external_number=:extn,
                                                    email=:email,
                                                    is_active=:b
                                                WHERE audience_id=:aid
                                                """,
                                                {
                                                    "title": title_v,
                                                    "name": nm_clean,
                                                    "dept": dept_v,
                                                    "pos": pos_v,
                                                    "pot": (pot_edit.strip() or None),
                                                    "loy": (loy_edit.strip() or None),
                                                    "mob": (mob_edit.strip() or None),
                                                    "land": (land_edit.strip() or None),
                                                    "extn": (ext_edit.strip() or None),
                                                    "email": (email_edit.strip() or None),
                                                    "b": bool(active_flag),
                                                    "aid": aid,
                                                },
                                            )
                                            st.success("Target audience updated ✅")
                                        except Exception as e:
                                            st.error("Could not update target audience.")
                                            st.caption(str(e))

                            st.markdown("---")
                            st.markdown("#### 🔴 Danger Zone")
                            st.write("Delete this target audience permanently (only if not referenced by visits).")

                            del_conf_key = f"{base_key}_del_conf"
                            del_confirm = st.checkbox(
                                "I understand this cannot be undone.",
                                key=del_conf_key,
                            )

                            if st.button(
                                "Delete Target Audience",
                                type="primary",
                                disabled=not del_confirm,
                                key=f"{base_key}_del",
                            ):
                                if v_cnt > 0:
                                    st.error(
                                        "Cannot delete: this target audience is referenced by visits. Deactivate instead."
                                    )
                                else:
                                    try:
                                        exec_sql(
                                            "DELETE FROM target_audiences WHERE audience_id=:id",
                                            {"id": aid},
                                        )
                                        st.success("Target audience deleted ✅")
                                        # reset the selection by removing the widget state
                                        for key in ("mg_aud_sel",):
                                            st.session_state.pop(key, None)

                                        # optional but nice: force UI refresh
                                        st.rerun()
                                    except Exception as e:
                                        st.error("Delete failed.")
                                        st.caption(str(e))

    # =====================================================================
    # 3) BUSINESS UNITS
    # =====================================================================
    with main_tabs[2]:
        st.subheader("Business Units")

        bu_mode = st.radio(
            "Mode",
            ["➕ Add / Import", "📝 Manage"],
            index=0,
            key="bu_mode",
            horizontal=True,
        )

        # -------------------------
        # MODE 1: Add / Import
        # -------------------------
        if bu_mode == "➕ Add / Import":
            st.markdown("### ➕ Add single business unit")
            st.caption("Required field: **Business Unit Name**.")

            st.session_state.setdefault("bu_add_name", "")

            bu_name = st.text_input(
                "Business Unit Name *",
                key="bu_add_name",
            )

            if st.button("Save Business Unit", type="primary", key="bu_add_save"):
                nm = bu_name.strip()
                if not nm:
                    st.error("Business Unit Name is required.")
                else:
                    try:
                        with engine.begin() as conn:
                            res = conn.execute(
                                text(
                                    """
                                    INSERT INTO business_units(name, is_active)
                                    VALUES (:name, TRUE)
                                    ON CONFLICT (name) DO NOTHING
                                    """
                                ),
                                {"name": nm},
                            )
                        if (res.rowcount or 0) > 0:
                            st.success("Business Unit added ✅")
                            # reset widget state safely
                            st.session_state.pop("bu_add_name", None)
                        else:
                            st.info("That Business Unit already exists — nothing added.")
                    except Exception as e:
                        st.error("Could not add Business Unit.")
                        st.caption(str(e))

            st.markdown("---")
            st.markdown("### ⬆️ Bulk upload business units (Excel/CSV)")
            st.write("Columns: **name**")

            fbu = st.file_uploader(
                "Upload Business Units file",
                type=["xlsx", "csv"],
                key="bu_upload",
            )
            if fbu is not None:
                df = _read_df_upload(fbu)
                if "name" not in df.columns:
                    st.error("Missing required column: name")
                else:
                    total = len(df)
                    inserted = 0
                    skipped = 0
                    sts, pb, ln, has_status = _mk_status("Importing Business Units…")

                    try:
                        with engine.begin() as conn:
                            for i, r in enumerate(df.itertuples(index=False), start=1):
                                nm_raw = getattr(r, "name", "")
                                nm = str(nm_raw).strip() if pd.notna(nm_raw) else ""
                                if not nm:
                                    skipped += 1
                                else:
                                    res = conn.execute(
                                        text(
                                            """
                                            INSERT INTO business_units(name, is_active)
                                            VALUES (:name, TRUE)
                                            ON CONFLICT (name) DO NOTHING
                                            """
                                        ),
                                        {"name": nm},
                                    )
                                    if (res.rowcount or 0) > 0:
                                        inserted += 1
                                    else:
                                        skipped += 1

                                if i % 200 == 0 or i == total:
                                    _update_progress(
                                        pb,
                                        ln,
                                        i,
                                        total,
                                        inserted,
                                        0,
                                        skipped,
                                        label_prefix="Business Units",
                                    )
                                    time.sleep(0.001)

                        _finish_status(
                            sts,
                            has_status,
                            f"Business Units import ✅ Inserted: {inserted} | Skipped: {skipped}",
                            ok=True,
                        )
                    except Exception as e:
                        _finish_status(
                            sts,
                            has_status,
                            "Business Units import failed ❌",
                            ok=False,
                        )
                        st.caption(str(e))

        # -------------------------
        # MODE 2: Manage
        # -------------------------
        else:  # bu_mode == "📝 Manage"
            st.markdown("### 📝 Manage business units")

            bdf = query_df(
                """
                SELECT business_unit_id,
                       name,
                       COALESCE(is_active, TRUE) AS is_active
                FROM business_units
                ORDER BY name
                """
            )

            if bdf.empty:
                st.info("No business units yet.")
            else:
                # show id + name + status
                bu_options = [
                    _parts_join(r.business_unit_id, r.name)
                    + f" ({'active' if bool(r.is_active) else 'inactive'})"
                    for r in bdf.itertuples(index=False)
                ]
                bu_options = [""] + bu_options

                sel_bu_label = st.selectbox(
                    "Select business unit",
                    bu_options,
                    index=0,
                    key="mg_bu_sel",
                )

                if sel_bu_label == "":
                    st.info("Please select a business unit.")
                else:
                    idx = bu_options.index(sel_bu_label) - 1
                    row = bdf.iloc[idx]
                    buid = int(row["business_unit_id"])

                    colA, colB = st.columns([1, 1])
                    with colA:
                        u_cnt = _refcount(
                            "SELECT COUNT(*) FROM users WHERE business_unit_id=:id",
                            {"id": buid},
                        )
                        bl_cnt = _refcount(
                            "SELECT COUNT(*) FROM business_lines WHERE business_unit_id=:id",
                            {"id": buid},
                        )
                        st.caption(
                            f"Refs → Users: **{u_cnt}** · Business Lines: **{bl_cnt}**"
                        )

                    with colB:
                        st.caption("Status")
                        st.write(
                            "✅ Active"
                            if bool(row["is_active"])
                            else "🚫 Inactive"
                        )

                    st.markdown("---")
                    st.markdown("#### Edit business unit")

                    base_key = f"mg_bu_{buid}"

                    bu_name_edit = st.text_input(
                        "Business Unit Name *",
                        value=row["name"] or "",
                        key=f"{base_key}_name",
                    )

                    active_flag = st.checkbox(
                        "Active",
                        value=bool(row["is_active"]),
                        key=f"{base_key}_active",
                        help="Uncheck to deactivate this business unit.",
                    )

                    if st.button("Save changes", type="primary", key=f"{base_key}_save"):
                        nm_clean = bu_name_edit.strip()
                        if not nm_clean:
                            st.error("Name is required.")
                        else:
                            dup = query_df(
                                """
                                SELECT 1
                                FROM business_units
                                WHERE lower(name)=lower(:n)
                                  AND business_unit_id<>:id
                                """,
                                {"n": nm_clean, "id": buid},
                            )
                            if not dup.empty:
                                st.error(
                                    "A business unit with that name already exists."
                                )
                            else:
                                try:
                                    exec_sql(
                                        """
                                        UPDATE business_units
                                        SET name=:n,
                                            is_active=:b
                                        WHERE business_unit_id=:id
                                        """,
                                        {
                                            "n": nm_clean,
                                            "b": bool(active_flag),
                                            "id": buid,
                                        },
                                    )
                                    st.success("Business Unit updated ✅")
                                except Exception as e:
                                    st.error("Could not update Business Unit.")
                                    st.caption(str(e))

                    st.markdown("---")
                    st.markdown("#### 🔴 Danger Zone")
                    st.write(
                        "Delete this business unit permanently (only if not referenced by users/business lines)."
                    )

                    del_conf_key = f"{base_key}_del_conf"
                    del_confirm = st.checkbox(
                        "I understand this cannot be undone.",
                        key=del_conf_key,
                    )

                    if st.button(
                        "Delete Business Unit",
                        type="primary",
                        disabled=not del_confirm,
                        key=f"{base_key}_del",
                    ):
                        if u_cnt > 0 or bl_cnt > 0:
                            st.error(
                                "Cannot delete: this business unit is referenced by users and/or business lines. "
                                "Deactivate instead."
                            )
                        else:
                            try:
                                exec_sql(
                                    "DELETE FROM business_units WHERE business_unit_id=:id",
                                    {"id": buid},
                                )
                                st.success("Business Unit deleted ✅")

                                # reset selection safely
                                st.session_state.pop("mg_bu_sel", None)

                                # optional: refresh UI so deleted BU disappears immediately
                                st.rerun()

                            except Exception as e:
                                st.error("Delete failed.")
                                st.caption(str(e))

    # =====================================================================
    # 4) BUSINESS LINES
    # =====================================================================
    with main_tabs[3]:
        st.subheader("Business Lines")

        bl_mode = st.radio(
            "Mode",
            ["➕ Add / Import", "📝 Manage"],
            index=0,
            key="bl_mode",
            horizontal=True,
        )

        # ---------------------------------------------------------------
        # Common dropdown data (from existing business_lines)
        # ---------------------------------------------------------------
        sup_df = query_df(
            """
            SELECT DISTINCT supplier
            FROM business_lines
            WHERE supplier IS NOT NULL AND supplier <> ''
            ORDER BY supplier
            """
        )
        supplier_values = [str(r.supplier).strip() for r in sup_df.itertuples(index=False) if str(r.supplier).strip()]
        supplier_options = [""] + supplier_values + ["OTHER"]

        cat_df = query_df(
            """
            SELECT DISTINCT category
            FROM business_lines
            WHERE category IS NOT NULL AND category <> ''
            ORDER BY category
            """
        )
        category_values = [str(r.category).strip() for r in cat_df.itertuples(index=False) if str(r.category).strip()]
        category_options = [""] + category_values + ["OTHER"]

        pg_df = query_df(
            """
            SELECT DISTINCT product_group
            FROM business_lines
            WHERE product_group IS NOT NULL AND product_group <> ''
            ORDER BY product_group
            """
        )
        prod_group_values = [str(r.product_group).strip() for r in pg_df.itertuples(index=False) if str(r.product_group).strip()]
        prod_group_options = [""] + prod_group_values + ["OTHER"]

        # -------------------------
        # MODE 1: Add / Import
        # -------------------------
        if bl_mode == "➕ Add / Import":
            st.markdown("### ➕ Add single business line")

            # ---- Business Units for selection ----
            bu_df_for_bl = query_df(
                """
                SELECT business_unit_id, name
                FROM business_units
                WHERE COALESCE(is_active, TRUE) IS TRUE
                ORDER BY name
                """
            )
            if bu_df_for_bl.empty:
                st.warning("No active Business Units found. Add a Business Unit first.")
            else:
                bu_labels = [""] + bu_df_for_bl["name"].tolist()
                bu_name_to_id = {r.name: int(r.business_unit_id) for r in bu_df_for_bl.itertuples(index=False)}

                # init add state
                st.session_state.setdefault("bl_add_bu", "")
                st.session_state.setdefault("bl_add_name", "")
                st.session_state.setdefault("bl_add_supplier_opt", "")
                st.session_state.setdefault("bl_add_supplier_other", "")
                st.session_state.setdefault("bl_add_category_opt", "")
                st.session_state.setdefault("bl_add_category_other", "")
                st.session_state.setdefault("bl_add_pg_opt", "")
                st.session_state.setdefault("bl_add_pg_other", "")

                # ---- Business Unit ----
                if st.session_state["bl_add_bu"] not in bu_labels:
                    st.session_state["bl_add_bu"] = ""
                bu_sel = st.selectbox(
                    "Business Unit *",
                    bu_labels,
                    key="bl_add_bu",
                )

                # ---- Line Name ----
                bl_name = st.text_input(
                    "Business Line Name *",
                    key="bl_add_name",
                )

                # ---- Supplier ----
                if st.session_state["bl_add_supplier_opt"] not in supplier_options:
                    st.session_state["bl_add_supplier_opt"] = ""
                sup_idx = supplier_options.index(st.session_state["bl_add_supplier_opt"])

                supplier_sel = st.selectbox(
                    "Supplier",
                    supplier_options,
                    index=sup_idx,
                    key="bl_add_supplier_opt",
                )
                if supplier_sel == "OTHER":
                    supplier_other = st.text_input("Other supplier", key="bl_add_supplier_other")
                else:
                    supplier_other = st.session_state.get("bl_add_supplier_other", "")

                # ---- Category (required) ----
                if st.session_state["bl_add_category_opt"] not in category_options:
                    st.session_state["bl_add_category_opt"] = ""
                cat_idx = category_options.index(st.session_state["bl_add_category_opt"])

                category_sel = st.selectbox(
                    "Category *",
                    category_options,
                    index=cat_idx,
                    key="bl_add_category_opt",
                    help="Category is required.",
                )
                if category_sel == "OTHER":
                    category_other = st.text_input("Other category", key="bl_add_category_other")
                else:
                    category_other = st.session_state.get("bl_add_category_other", "")

                # ---- Product Group ----
                if st.session_state["bl_add_pg_opt"] not in prod_group_options:
                    st.session_state["bl_add_pg_opt"] = ""
                pg_idx = prod_group_options.index(st.session_state["bl_add_pg_opt"])

                pg_sel = st.selectbox(
                    "Product Group",
                    prod_group_options,
                    index=pg_idx,
                    key="bl_add_pg_opt",
                )
                if pg_sel == "OTHER":
                    pg_other = st.text_input("Other product group", key="bl_add_pg_other")
                else:
                    pg_other = st.session_state.get("bl_add_pg_other", "")

                # ---- Save button ----
                if st.button("Save Business Line", type="primary", key="bl_add_save"):
                    if not bu_sel:
                        st.error("Business Unit is required.")
                    elif not bl_name.strip():
                        st.error("Business Line Name is required.")
                    elif category_sel == "" or (category_sel == "OTHER" and not (category_other or "").strip()):
                        st.error("Category is required.")
                    else:
                        try:
                            bu_id = bu_name_to_id.get(bu_sel)
                            if not bu_id:
                                st.error("Selected Business Unit not found.")
                            else:
                                # resolve supplier
                                if supplier_sel == "":
                                    supplier_v = None
                                elif supplier_sel == "OTHER":
                                    supplier_v = (supplier_other or "").strip() or None
                                else:
                                    supplier_v = supplier_sel

                                # resolve category
                                if category_sel == "":
                                    category_v = None  # covered by validation above
                                elif category_sel == "OTHER":
                                    category_v = (category_other or "").strip() or None
                                else:
                                    category_v = category_sel

                                # resolve product group
                                if pg_sel == "":
                                    pg_v = None
                                elif pg_sel == "OTHER":
                                    pg_v = (pg_other or "").strip() or None
                                else:
                                    pg_v = pg_sel

                                with engine.begin() as conn:
                                    res = conn.execute(
                                        text(
                                            """
                                            INSERT INTO business_lines(
                                                business_unit_id, name, supplier, category, product_group, is_active
                                            )
                                            VALUES (:bid, :name, :supplier, :category, :pg, TRUE)
                                            ON CONFLICT (business_unit_id, name) DO NOTHING
                                            """
                                        ),
                                        {
                                            "bid": bu_id,
                                            "name": bl_name.strip(),
                                            "supplier": supplier_v,
                                            "category": category_v,
                                            "pg": pg_v,
                                        },
                                    )

                                if (res.rowcount or 0) > 0:
                                    st.success("Business Line added ✅")
                                    for key in (
                                        "bl_add_bu",
                                        "bl_add_name",
                                        "bl_add_supplier_opt",
                                        "bl_add_supplier_other",
                                        "bl_add_category_opt",
                                        "bl_add_category_other",
                                        "bl_add_pg_opt",
                                        "bl_add_pg_other",
                                    ):
                                        st.session_state.pop(key, None)
                                    st.rerun()     # optional but nice
                                else:
                                    st.info("That Business Unit + Business Line Name already exists — nothing added.")
                        except Exception as e:
                            st.error("Could not add Business Line.")
                            st.caption(str(e))

            st.markdown("---")
            st.markdown("### ⬆️ Bulk upload business lines (Excel/CSV)")
            st.write("Columns: **business_unit**, **name**, **category**  (optional: supplier, product_group)")

            fbl = st.file_uploader("Upload Business Lines file", type=["xlsx", "csv"], key="blines")
            if fbl is not None:
                df = _read_df_upload(fbl)
                st.caption(f"Detected columns: {list(df.columns)}")
                needed = {"business_unit", "name", "category"}
                if not needed.issubset(set(df.columns)):
                    missing = sorted(list(needed - set(df.columns)))
                    st.error(f"Missing required columns: {', '.join(missing)}")
                else:
                    total = len(df)
                    inserted, skipped = 0, 0
                    sts, pb, ln, has_status = _mk_status("Importing Business Lines…")
                    try:
                        with engine.begin() as conn:
                            budf = pd.read_sql_query(text("SELECT business_unit_id, name FROM business_units"), conn)
                            bumap = {str(r.name).strip().lower(): int(r.business_unit_id) for r in budf.itertuples(index=False)}

                            for i, r in enumerate(df.itertuples(index=False), start=1):
                                bu_name_raw = (
                                    str(getattr(r, "business_unit")) if hasattr(r, "business_unit") and pd.notna(getattr(r, "business_unit")) else ""
                                ).strip()
                                bl_name_raw = (
                                    str(getattr(r, "name")) if hasattr(r, "name") and pd.notna(getattr(r, "name")) else ""
                                ).strip()
                                cat_raw = (
                                    str(getattr(r, "category")) if hasattr(r, "category") and pd.notna(getattr(r, "category")) else ""
                                ).strip()

                                if not (bu_name_raw and bl_name_raw and cat_raw):
                                    skipped += 1
                                    if i % 200 == 0 or i == total:
                                        _update_progress(pb, ln, i, total, inserted, 0, skipped, label_prefix="Business Lines")
                                    continue

                                bu_id_tmp = bumap.get(bu_name_raw.lower())
                                if not bu_id_tmp:
                                    skipped += 1
                                    if i % 200 == 0 or i == total:
                                        _update_progress(pb, ln, i, total, inserted, 0, skipped, label_prefix="Business Lines")
                                    continue

                                supplier_v = (
                                    str(getattr(r, "supplier")).strip()
                                    if hasattr(r, "supplier") and pd.notna(getattr(r, "supplier"))
                                    else None
                                )
                                prod_group_v = (
                                    str(getattr(r, "product_group")).strip()
                                    if hasattr(r, "product_group") and pd.notna(getattr(r, "product_group"))
                                    else None
                                )

                                res = conn.execute(
                                    text(
                                        """
                                        INSERT INTO business_lines(
                                            business_unit_id, name, supplier, category, product_group, is_active
                                        )
                                        VALUES (:bid, :name, :supplier, :category, :pg, TRUE)
                                        ON CONFLICT (business_unit_id, name) DO NOTHING
                                        """
                                    ),
                                    {
                                        "bid": bu_id_tmp,
                                        "name": bl_name_raw,
                                        "supplier": supplier_v,
                                        "category": cat_raw,
                                        "pg": prod_group_v,
                                    },
                                )
                                if (res.rowcount or 0) > 0:
                                    inserted += 1
                                else:
                                    skipped += 1

                                if i % 200 == 0 or i == total:
                                    _update_progress(pb, ln, i, total, inserted, 0, skipped, label_prefix="Business Lines")
                                    time.sleep(0.001)

                        _finish_status(
                            sts,
                            has_status,
                            f"Business Lines import ✅ Inserted: {inserted} | Skipped: {skipped}",
                            ok=True,
                        )
                    except Exception as e:
                        _finish_status(sts, has_status, "Business Lines import failed ❌", ok=False)
                        st.caption(str(e))

        # -------------------------
        # MODE 2: Manage
        # -------------------------
        else:  # bl_mode == "📝 Manage"
            st.markdown("### 📝 Manage business lines")

            bll = query_df(
                """
                SELECT bl.business_line_id,
                       bl.name,
                       bl.supplier,
                       bl.category,
                       bl.product_group,
                       COALESCE(bl.is_active, TRUE) AS is_active,
                       bl.business_unit_id,
                       bu.name AS business_unit
                FROM business_lines bl
                JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
                ORDER BY bu.name, bl.name
                """
            )

            if bll.empty:
                st.info("No business lines yet.")
            else:
                def _fmt_bl(r):
                    return " - ".join(
                        [
                            str(r.business_unit),
                            str(r.name),
                            str(r.category or ""),
                            str(r.product_group or ""),
                        ]
                    ).replace(" - None", "").replace("None", "").strip(" -")

                options = [
                    f"{r.business_line_id} - {_fmt_bl(r)}  ({'active' if bool(r.is_active) else 'inactive'})"
                    for r in bll.itertuples(index=False)
                ]
                options = [""] + options

                sel_label = st.selectbox("Select business line", options, index=0, key="mg_bl_sel")

                if sel_label == "":
                    st.info("Please select a business line.")
                else:
                    row_idx = options.index(sel_label) - 1
                    row = bll.iloc[row_idx]
                    blid = int(row["business_line_id"])

                    # quick refs / status
                    colA, colB = st.columns([1, 1])
                    with colA:
                        i_cnt = _refcount("SELECT COUNT(*) FROM items WHERE business_line_id=:id", {"id": blid})
                        v_cnt = _refcount("SELECT COUNT(*) FROM visits WHERE business_line_id=:id", {"id": blid})
                        st.caption(f"Refs → Items: **{i_cnt}** · Visits: **{v_cnt}**")
                    with colB:
                        st.caption("Status")
                        st.write("✅ Active" if bool(row["is_active"]) else "🚫 Inactive")

                    st.markdown("---")
                    st.markdown("#### Edit business line")

                    base_key = f"mg_bl_{blid}"

                    # ---- Business Unit dropdown ----
                    bu_df = query_df(
                        """
                        SELECT business_unit_id, name
                        FROM business_units
                        WHERE COALESCE(is_active, TRUE) IS TRUE
                        ORDER BY name
                        """
                    )
                    bu_labels = bu_df["name"].tolist()
                    if not bu_labels:
                        st.warning("No active Business Units found.")
                        bu_idx = 0
                    else:
                        bu_idx = 0
                        if pd.notna(row["business_unit_id"]):
                            for i, r2 in enumerate(bu_df.itertuples(index=False)):
                                if int(r2.business_unit_id) == int(row["business_unit_id"]):
                                    bu_idx = i
                                    break

                    sel_bu_label = st.selectbox(
                        "Business Unit *",
                        bu_labels if bu_labels else [],
                        index=bu_idx if bu_labels else 0,
                        key=f"{base_key}_bu",
                    )

                    if bu_labels:
                        new_bu_id = int(
                            bu_df.loc[bu_df["name"] == sel_bu_label, "business_unit_id"].iloc[0]
                        )
                    else:
                        new_bu_id = None

                    # ---- Name ----
                    bl_name_edit = st.text_input(
                        "Business Line Name *",
                        value=row["name"] or "",
                        key=f"{base_key}_name",
                    )

                    # ---- Supplier dropdown + OTHER ----
                    sup_key = base_key + "_supplier_opt"
                    sup_other_key = base_key + "_supplier_other"

                    if sup_key not in st.session_state:
                        existing_sup = (row["supplier"] or "").strip() if row["supplier"] else ""
                        if existing_sup and existing_sup in supplier_options:
                            st.session_state[sup_key] = existing_sup
                        elif existing_sup:
                            st.session_state[sup_key] = "OTHER"
                            st.session_state[sup_other_key] = existing_sup
                        else:
                            st.session_state[sup_key] = ""

                    if st.session_state[sup_key] not in supplier_options:
                        st.session_state[sup_key] = ""

                    sup_idx_edit = supplier_options.index(st.session_state[sup_key])
                    supplier_sel_edit = st.selectbox(
                        "Supplier",
                        supplier_options,
                        index=sup_idx_edit,
                        key=sup_key,
                    )
                    if supplier_sel_edit == "OTHER":
                        supplier_other_edit = st.text_input("Other supplier", key=sup_other_key)
                    else:
                        supplier_other_edit = st.session_state.get(sup_other_key, "")

                    # ---- Category dropdown + OTHER (required) ----
                    cat_key = base_key + "_category_opt"
                    cat_other_key = base_key + "_category_other"

                    if cat_key not in st.session_state:
                        existing_cat = (row["category"] or "").strip() if row["category"] else ""
                        if existing_cat and existing_cat in category_options:
                            st.session_state[cat_key] = existing_cat
                        elif existing_cat:
                            st.session_state[cat_key] = "OTHER"
                            st.session_state[cat_other_key] = existing_cat
                        else:
                            st.session_state[cat_key] = ""

                    if st.session_state[cat_key] not in category_options:
                        st.session_state[cat_key] = ""

                    cat_idx_edit = category_options.index(st.session_state[cat_key])
                    category_sel_edit = st.selectbox(
                        "Category *",
                        category_options,
                        index=cat_idx_edit,
                        key=cat_key,
                        help="Category is required.",
                    )
                    if category_sel_edit == "OTHER":
                        category_other_edit = st.text_input("Other category", key=cat_other_key)
                    else:
                        category_other_edit = st.session_state.get(cat_other_key, "")

                    # ---- Product Group dropdown + OTHER ----
                    pg_key = base_key + "_pg_opt"
                    pg_other_key = base_key + "_pg_other"

                    if pg_key not in st.session_state:
                        existing_pg = (row["product_group"] or "").strip() if row["product_group"] else ""
                        if existing_pg and existing_pg in prod_group_options:
                            st.session_state[pg_key] = existing_pg
                        elif existing_pg:
                            st.session_state[pg_key] = "OTHER"
                            st.session_state[pg_other_key] = existing_pg
                        else:
                            st.session_state[pg_key] = ""

                    if st.session_state[pg_key] not in prod_group_options:
                        st.session_state[pg_key] = ""

                    pg_idx_edit = prod_group_options.index(st.session_state[pg_key])
                    pg_sel_edit = st.selectbox(
                        "Product Group",
                        prod_group_options,
                        index=pg_idx_edit,
                        key=pg_key,
                    )
                    if pg_sel_edit == "OTHER":
                        pg_other_edit = st.text_input("Other product group", key=pg_other_key)
                    else:
                        pg_other_edit = st.session_state.get(pg_other_key, "")

                    # ---- Active toggle ----
                    active_flag = st.checkbox(
                        "Active",
                        value=bool(row["is_active"]),
                        key=f"{base_key}_active",
                        help="Uncheck to deactivate this business line.",
                    )

                    # ---- Save button ----
                    if st.button("Save changes", type="primary", key=f"{base_key}_save"):
                        if not new_bu_id:
                            st.error("Business Unit is required.")
                        elif not bl_name_edit.strip():
                            st.error("Business Line Name is required.")
                        elif category_sel_edit == "" or (category_sel_edit == "OTHER" and not (category_other_edit or "").strip()):
                            st.error("Category is required.")
                        else:
                            # resolve supplier
                            if supplier_sel_edit == "":
                                supplier_v = None
                            elif supplier_sel_edit == "OTHER":
                                supplier_v = (supplier_other_edit or "").strip() or None
                            else:
                                supplier_v = supplier_sel_edit

                            # resolve category
                            if category_sel_edit == "":
                                category_v = None
                            elif category_sel_edit == "OTHER":
                                category_v = (category_other_edit or "").strip() or None
                            else:
                                category_v = category_sel_edit

                            # resolve product group
                            if pg_sel_edit == "":
                                pg_v = None
                            elif pg_sel_edit == "OTHER":
                                pg_v = (pg_other_edit or "").strip() or None
                            else:
                                pg_v = pg_sel_edit

                            # check duplicate name within BU
                            dup = query_df(
                                """
                                SELECT 1
                                FROM business_lines
                                WHERE business_unit_id=:bid
                                  AND lower(name)=lower(:nm)
                                  AND business_line_id<>:id
                                """,
                                {"bid": new_bu_id, "nm": bl_name_edit.strip(), "id": blid},
                            )
                            if not dup.empty:
                                st.error("A business line with that name already exists in the selected Business Unit.")
                            else:
                                try:
                                    exec_sql(
                                        """
                                        UPDATE business_lines
                                        SET business_unit_id=:bid,
                                            name=:name,
                                            supplier=:supplier,
                                            category=:cat,
                                            product_group=:pg,
                                            is_active=:b
                                        WHERE business_line_id=:id
                                        """,
                                        {
                                            "bid": new_bu_id,
                                            "name": bl_name_edit.strip(),
                                            "supplier": supplier_v,
                                            "cat": category_v,
                                            "pg": pg_v,
                                            "b": bool(active_flag),
                                            "id": blid,
                                        },
                                    )
                                    st.success("Business Line updated ✅")
                                except Exception as e:
                                    st.error("Could not update Business Line.")
                                    st.caption(str(e))

                    st.markdown("---")
                    st.markdown("#### 🔴 Danger Zone")
                    st.write("Delete this business line permanently (only if not referenced by items/visits).")

                    del_conf_key = f"{base_key}_del_conf"
                    del_confirm = st.checkbox(
                        "I understand this cannot be undone.",
                        key=del_conf_key,
                    )

                    if st.button(
                        "Delete Business Line",
                        type="primary",
                        disabled=not del_confirm,
                        key=f"{base_key}_del",
                    ):
                        if i_cnt > 0 or v_cnt > 0:
                            st.error(
                                "Cannot delete: this business line is referenced by items and/or visits. "
                                "Deactivate instead."
                            )
                        else:
                            try:
                                exec_sql("DELETE FROM business_lines WHERE business_line_id=:id", {"id": blid})
                                st.success("Business Line deleted ✅")
                                st.session_state.pop("mg_bl_sel", None)
                                st.rerun()
                            except Exception as e:
                                st.error("Delete failed.")
                                st.caption(str(e))

    # =====================
    # 5) Items (Products)
    # =====================
    with main_tabs[4]:
        st.subheader("Items (Products)")
        st.caption("Items are tied to Business Lines, which are tied to Business Units.")

        item_mode = st.radio(
            "Mode",
            ["➕ Add / Import", "📝 Manage"],
            index=0,
            key="item_mode",
            horizontal=True,
        )

        # Common BU data (active only)
        bu_df_all = query_df(
            """
            SELECT business_unit_id, name
            FROM business_units
            WHERE COALESCE(is_active, TRUE) IS TRUE
            ORDER BY name
            """
        )

        # -------------------------
        # MODE 1: Add / Import
        # -------------------------
        if item_mode == "➕ Add / Import":
            st.markdown("### ➕ Add single item")
            st.caption("Required fields: **Product ID**, **Article Number**, **Business Unit**, **Business Line**.")

            if bu_df_all.empty:
                st.warning("No active Business Units found. Add one in the Business Units tab first.")
            else:
                # ---- Business Unit select ----
                bu_labels = [""] + bu_df_all["name"].tolist()
                bu_ids = [None] + bu_df_all["business_unit_id"].astype(int).tolist()

                bu_idx = st.selectbox(
                    "Business Unit *",
                    options=list(range(len(bu_labels))),
                    format_func=lambda i: bu_labels[i],
                    index=0,
                    key="item_add_bu_idx",
                )
                selected_bu_id = bu_ids[bu_idx]

                # ---- Business Line select (depends on BU) ----
                if selected_bu_id is not None:
                    bl_df = query_df(
                        """
                        SELECT business_line_id, name
                        FROM business_lines
                        WHERE COALESCE(is_active, TRUE) IS TRUE
                          AND business_unit_id = :bid
                        ORDER BY name
                        """,
                        {"bid": int(selected_bu_id)},
                    )
                else:
                    bl_df = pd.DataFrame(columns=["business_line_id", "name"])

                bl_labels = [""] + (bl_df["name"].tolist() if not bl_df.empty else [])
                bl_ids = [None] + (bl_df["business_line_id"].astype(int).tolist() if not bl_df.empty else [])

                bl_idx = st.selectbox(
                    "Business Line *",
                    options=list(range(len(bl_labels))),
                    format_func=lambda i: bl_labels[i],
                    index=0,
                    key="item_add_bl_idx",
                    help="Pick a Business Unit first to see its lines.",
                )
                selected_bl_id = bl_ids[bl_idx]

                # ---- Item fields ----
                pid = st.text_input("Product ID * (must be unique)", key="item_add_pid")
                article = st.text_input("Article Number *", key="item_add_article")
                desc = st.text_input("Description", key="item_add_desc")

                if st.button("Save Item", type="primary", key="item_add_save"):
                    if not pid.strip():
                        st.error("Product ID is required.")
                    elif not article.strip():
                        st.error("Article Number is required.")
                    elif selected_bu_id is None or selected_bl_id is None:
                        st.error("Business Unit and Business Line are required.")
                    else:
                        try:
                            with engine.begin() as conn:
                                res = conn.execute(
                                    text(
                                        """
                                        INSERT INTO items(
                                            product_id, article_number, description, business_line_id, is_active
                                        ) VALUES (
                                            :pid, :article, :desc, :blid, TRUE
                                        )
                                        ON CONFLICT (product_id) DO NOTHING
                                        """
                                    ),
                                    {
                                        "pid": pid.strip(),
                                        "article": article.strip(),
                                        "desc": (desc.strip() or None),
                                        "blid": int(selected_bl_id),
                                    },
                                )
                            if (res.rowcount or 0) > 0:
                                st.success("Item added ✅")
                                # reset widget-backed keys by removing them from session_state
                                for key in (
                                    "item_add_pid",
                                    "item_add_article",
                                    "item_add_desc",
                                    "item_add_bu_idx",
                                    "item_add_bl_idx",
                                ):
                                    st.session_state.pop(key, None)
                                st.rerun()
                            else:
                                st.error("That Product ID already exists.")
                        except Exception as e:
                            st.error("Could not add item.")
                            st.caption(str(e))

            st.markdown("---")
            st.markdown("### ⬆️ Bulk upload items (Excel/CSV)")
            st.write("Columns: **product_id**, **business_unit**, **business_line**, article_number, description")

            # Build resolver map for BU + BL name → business_line_id
            _bl_map_df = query_df(
                """
                SELECT bu.name AS bu_name,
                       bl.name AS bl_name,
                       bl.business_line_id AS bl_id
                FROM business_lines bl
                JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
                WHERE COALESCE(bu.is_active, TRUE) IS TRUE
                  AND COALESCE(bl.is_active, TRUE) IS TRUE
                ORDER BY bu.name, bl.name
                """
            )
            bu_to_bls = {}
            for r in _bl_map_df.itertuples(index=False):
                bu_to_bls.setdefault(str(r.bu_name).strip(), []).append((str(r.bl_name).strip(), int(r.bl_id)))

            f3 = st.file_uploader("Upload Items file", type=["xlsx", "csv"], key="items_upload")
            if f3 is not None:
                df = _read_df_upload(f3)
                needed = {"product_id", "business_unit", "business_line"}
                if not needed.issubset(df.columns):
                    st.error("Missing required columns: product_id, business_unit, business_line")
                else:
                    total = len(df)
                    inserted = 0
                    updated = 0
                    skipped = 0
                    sts, pb, ln, has_status = _mk_status("Importing Items…")
                    try:
                        with engine.begin() as conn:
                            existing = set(
                                pd.read_sql_query(text("SELECT product_id FROM items"), conn)[
                                    "product_id"
                                ].astype(str).tolist()
                            )

                            for i, r in enumerate(df.itertuples(index=False), start=1):
                                pid_raw = getattr(r, "product_id", None)
                                pid = (str(pid_raw).strip() if pd.notna(pid_raw) else "")

                                bu_name_raw = (
                                    str(getattr(r, "business_unit", "")).strip()
                                    if hasattr(r, "business_unit")
                                    else ""
                                )
                                bl_name_raw = (
                                    str(getattr(r, "business_line", "")).strip()
                                    if hasattr(r, "business_line")
                                    else ""
                                )

                                if not (pid and bu_name_raw and bl_name_raw):
                                    skipped += 1
                                    if i % 200 == 0 or i == total:
                                        _update_progress(
                                            pb, ln, i, total, inserted, updated, skipped, label_prefix="Items"
                                        )
                                    continue

                                # resolve BL from mapping
                                bl_id = None
                                if bu_name_raw in bu_to_bls:
                                    for name, _id in bu_to_bls[bu_name_raw]:
                                        if name == bl_name_raw:
                                            bl_id = _id
                                            break

                                if not bl_id:
                                    skipped += 1
                                    if i % 200 == 0 or i == total:
                                        _update_progress(
                                            pb, ln, i, total, inserted, updated, skipped, label_prefix="Items"
                                        )
                                    continue

                                article_v = (
                                    str(getattr(r, "article_number")).strip()
                                    if hasattr(r, "article_number") and pd.notna(getattr(r, "article_number"))
                                    else None
                                )
                                desc_v = (
                                    str(getattr(r, "description")).strip()
                                    if hasattr(r, "description") and pd.notna(getattr(r, "description"))
                                    else None
                                )

                                conn.execute(
                                    text(
                                        """
                                        INSERT INTO items(product_id, article_number, description, business_line_id, is_active)
                                        VALUES (:pid, :article, :desc, :blid, TRUE)
                                        ON CONFLICT (product_id) DO UPDATE
                                        SET article_number   = EXCLUDED.article_number,
                                            description      = EXCLUDED.description,
                                            business_line_id = EXCLUDED.business_line_id,
                                            is_active        = TRUE
                                        """
                                    ),
                                    {"pid": pid, "article": article_v, "desc": desc_v, "blid": int(bl_id)},
                                )

                                if pid in existing:
                                    updated += 1
                                else:
                                    inserted += 1

                                if i % 200 == 0 or i == total:
                                    _update_progress(
                                        pb, ln, i, total, inserted, updated, skipped, label_prefix="Items"
                                    )
                                    time.sleep(0.001)

                        _finish_status(
                            sts,
                            has_status,
                            f"Items import ✅ Inserted: {inserted} | Updated: {updated} | Skipped: {skipped}",
                            ok=True,
                        )
                    except Exception as e:
                        _finish_status(sts, has_status, "Items import failed ❌", ok=False)
                        st.caption(str(e))

        # -------------------------
        # MODE 2: Manage
        # -------------------------
        else:  # item_mode == "📝 Manage"
            st.markdown("### 📝 Manage items")

            idf = query_df(
                """
                SELECT i.product_id,
                       i.article_number,
                       i.description,
                       COALESCE(i.is_active, TRUE) AS is_active,
                       bl.business_line_id,
                       bl.name AS business_line,
                       bu.name AS business_unit
                FROM items i
                JOIN business_lines bl ON bl.business_line_id = i.business_line_id
                JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
                ORDER BY COALESCE(i.article_number, i.product_id)
                """
            )

            if idf.empty:
                st.info("No items yet.")
            else:
                def _fmt_item(r):
                    art = (str(r.article_number).strip()
                           if pd.notna(r.article_number) and str(r.article_number).strip()
                           else "")
                    bu = (str(r.business_unit).strip()
                          if pd.notna(r.business_unit) and str(r.business_unit).strip()
                          else "")
                    bl = (str(r.business_line).strip()
                          if pd.notna(r.business_line) and str(r.business_line).strip()
                          else "")
                    desc = (str(r.description).strip()
                            if pd.notna(r.description) and str(r.description).strip()
                            else "")
                    base = " - ".join([p for p in [art, bu, bl, desc] if p])
                    return base or str(r.product_id)

                options = [""] + [
                    f"{_fmt_item(r)}  ({'active' if r.is_active else 'inactive'})"
                    for r in idf.itertuples(index=False)
                ]

                sel_label = st.selectbox(
                    "Select item",
                    options,
                    index=0,
                    key="mg_item_sel",
                )

                if sel_label == "":
                    st.info("Please select an item.")
                else:
                    row_idx = options.index(sel_label) - 1
                    row = idf.iloc[row_idx]
                    pid = str(row["product_id"])

                    colA, colB = st.columns([1, 1])
                    with colA:
                        v_cnt = _refcount(
                            "SELECT COUNT(*) FROM visits WHERE product_id=:pid",
                            {"pid": pid},
                        )
                        st.caption(f"Refs → Visits: **{v_cnt}**")
                    with colB:
                        st.caption("Status")
                        st.write("✅ Active" if bool(row["is_active"]) else "🚫 Inactive")

                    st.markdown("---")
                    st.markdown("#### Edit item")

                    base_key = f"mg_item_{pid}"

                    # ---- Account / article / description ----
                    art_edit = st.text_input(
                        "Article Number (unique)",
                        value=row["article_number"] or "",
                        key=f"{base_key}_article",
                    )
                    desc_edit = st.text_input(
                        "Description",
                        value=row["description"] or "",
                        key=f"{base_key}_desc",
                    )

                    # ---- Business Unit dropdown ----
                    bu_df = query_df(
                        """
                        SELECT business_unit_id, name
                        FROM business_units
                        WHERE COALESCE(is_active, TRUE) IS TRUE
                        ORDER BY name
                        """
                    )
                    bu_labels = bu_df["name"].tolist()
                    bu_ids = bu_df["business_unit_id"].astype(int).tolist()

                    current_bu_name = row["business_unit"]
                    try:
                        bu_idx = bu_labels.index(current_bu_name)
                    except ValueError:
                        bu_idx = 0 if bu_labels else 0

                    bu_label = st.selectbox(
                        "Business Unit *",
                        bu_labels,
                        index=bu_idx if bu_labels else 0,
                        key=f"{base_key}_bu",
                    )
                    sel_bu_id = int(bu_df.loc[bu_df["name"] == bu_label, "business_unit_id"].iloc[0]) if not bu_df.empty else None

                    # ---- Business Line dropdown (depends on BU) ----
                    if sel_bu_id is not None:
                        bl_df = query_df(
                            """
                            SELECT business_line_id, name
                            FROM business_lines
                            WHERE COALESCE(is_active, TRUE) IS TRUE
                              AND business_unit_id = :bid
                            ORDER BY name
                            """,
                            {"bid": sel_bu_id},
                        )
                    else:
                        bl_df = pd.DataFrame(columns=["business_line_id", "name"])

                    bl_labels = bl_df["name"].tolist() if not bl_df.empty else []
                    bl_ids = bl_df["business_line_id"].astype(int).tolist() if not bl_df.empty else []

                    current_bl_id = int(row["business_line_id"])
                    bl_idx = 0
                    for i, r in enumerate(bl_df.itertuples(index=False)):
                        if int(r.business_line_id) == current_bl_id:
                            bl_idx = i
                            break

                    bl_label = st.selectbox(
                        "Business Line *",
                        bl_labels,
                        index=bl_idx if bl_labels else 0,
                        key=f"{base_key}_bl",
                    )
                    sel_bl_id = (
                        int(bl_df.loc[bl_df["name"] == bl_label, "business_line_id"].iloc[0])
                        if bl_labels
                        else None
                    )

                    # ---- Active flag ----
                    active_flag = st.checkbox(
                        "Active",
                        value=bool(row["is_active"]),
                        key=f"{base_key}_active",
                    )

                    if st.button("Save changes", type="primary", key=f"{base_key}_save"):
                        if not sel_bl_id:
                            st.error("Business Line is required.")
                        else:
                            try:
                                if art_edit.strip():
                                    dup = query_df(
                                        """
                                        SELECT 1
                                        FROM items
                                        WHERE lower(article_number)=lower(:a)
                                          AND product_id<>:pid
                                        """,
                                        {"a": art_edit.strip(), "pid": pid},
                                    )
                                    if not dup.empty:
                                        st.error("Article Number already exists.")
                                    else:
                                        exec_sql(
                                            """
                                            UPDATE items
                                            SET article_number=:a,
                                                description=:d,
                                                business_line_id=:bl,
                                                is_active=:b
                                            WHERE product_id=:pid
                                            """,
                                            {
                                                "a": art_edit.strip(),
                                                "d": (desc_edit.strip() or None),
                                                "bl": sel_bl_id,
                                                "b": bool(active_flag),
                                                "pid": pid,
                                            },
                                        )
                                        st.success("Item updated ✅")
                                else:
                                    exec_sql(
                                        """
                                        UPDATE items
                                        SET article_number=NULL,
                                            description=:d,
                                            business_line_id=:bl,
                                            is_active=:b
                                        WHERE product_id=:pid
                                        """,
                                        {
                                            "d": (desc_edit.strip() or None),
                                            "bl": sel_bl_id,
                                            "b": bool(active_flag),
                                            "pid": pid,
                                        },
                                    )
                                    st.success("Item updated ✅")
                            except Exception as e:
                                st.error("Could not update item.")
                                st.caption(str(e))

                    st.markdown("---")
                    st.markdown("#### 🔴 Danger Zone")
                    st.write("Delete this item permanently (only if not referenced by visits).")

                    del_conf_key = f"{base_key}_del_conf"
                    del_confirm = st.checkbox(
                        "I understand this cannot be undone.",
                        key=del_conf_key,
                    )

                    if st.button(
                        "Delete Item",
                        type="primary",
                        disabled=not del_confirm,
                        key=f"{base_key}_del",
                    ):
                        if v_cnt > 0:
                            st.error(
                                "Cannot delete: this item is referenced by visits. "
                                "Deactivate instead."
                            )
                        else:
                            try:
                                exec_sql("DELETE FROM items WHERE product_id=:pid", {"pid": pid})
                                st.success("Item deleted ✅")
                                st.session_state["mg_item_sel"] = ""
                            except Exception as e:
                                st.error("Delete failed.")
                                st.caption(str(e))

    # =====================================================================
    # 6) OBJECTIVES
    # =====================================================================
    with main_tabs[5]:
        st.subheader("Objectives")

        # Stable mode selector
        mode = st.radio(
            "Mode",
            ["➕ Add / Import", "📝 Manage"],
            index=0,
            key="obj_mode",
            horizontal=True
        )

        # ---------------------------------------------------
        # Load base category options
        # ---------------------------------------------------
        cat_df = query_df("""
            SELECT DISTINCT category
            FROM objectives
            WHERE category IS NOT NULL AND category <> ''
            ORDER BY category
        """)

        existing_cats = [str(r.category).strip() for r in cat_df.itertuples(index=False) if str(r.category).strip()]
        category_options = [""] + existing_cats + ["OTHER"]

        # ---------------------------------------------------
        # MODE 1 — ADD / IMPORT
        # ---------------------------------------------------
        if mode == "➕ Add / Import":
            st.markdown("### ➕ Add new Objective")
            st.caption("Required fields: **Name**. Category is optional.")

            # init state
            st.session_state.setdefault("obj_add_name", "")
            st.session_state.setdefault("obj_add_cat_opt", "")
            st.session_state.setdefault("obj_add_cat_other", "")

            # Name
            obj_name = st.text_input("Objective Name *", key="obj_add_name")

            # Category dropdown
            if st.session_state["obj_add_cat_opt"] not in category_options:
                st.session_state["obj_add_cat_opt"] = ""
            cat_idx = category_options.index(st.session_state["obj_add_cat_opt"])

            cat_sel = st.selectbox(
                "Category",
                category_options,
                index=cat_idx,
                key="obj_add_cat_opt"
            )
            if cat_sel == "OTHER":
                cat_other = st.text_input("Other category", key="obj_add_cat_other")
            else:
                cat_other = st.session_state.get("obj_add_cat_other", "")

            # Save objective
            if st.button("Save Objective", type="primary", key="obj_add_save"):
                if not obj_name.strip():
                    st.error("Objective name is required.")
                else:
                    name_v = obj_name.strip()

                    # resolve category
                    if cat_sel == "":
                        cat_v = None
                    elif cat_sel == "OTHER":
                        cat_v = (cat_other or "").strip() or None
                    else:
                        cat_v = cat_sel

                    try:
                        with engine.begin() as conn:
                            res = conn.execute(
                                text("""
                                    INSERT INTO objectives(name, category, is_active)
                                    SELECT :n, :c, TRUE
                                    WHERE NOT EXISTS (
                                        SELECT 1 FROM objectives
                                        WHERE lower(name)=lower(:n)
                                        AND lower(coalesce(category,''))=lower(coalesce(:c,''))
                                    )
                                """),
                                {"n": name_v, "c": cat_v}
                            )

                        if (res.rowcount or 0) > 0:
                            st.success("Objective added ✅")
                            # Reset widget-backed keys safely
                            for key in (
                                "obj_add_name",
                                "obj_add_cat_opt",
                                "obj_add_cat_other",
                            ):
                                st.session_state.pop(key, None)
                            st.rerun()
                        else:
                            st.info("This objective already exists — nothing added.")
                    except Exception as e:
                        st.error("Could not add objective.")
                        st.caption(str(e))

            # Bulk import
            st.markdown("---")
            st.markdown("### ⬆️ Bulk upload objectives (Excel/CSV)")
            st.write("Columns: **name**, category (optional)")

            fobj = st.file_uploader("Upload file", type=["xlsx", "csv"], key="obj_file")
            if fobj is not None:
                df = _read_df_upload(fobj)

                if "name" not in df.columns:
                    st.error("Missing required column: `name`")
                else:
                    total = len(df)
                    inserted = 0
                    skipped = 0
                    sts, pb, ln, has_status = _mk_status("Importing Objectives…")

                    try:
                        with engine.begin() as conn:
                            for i, r in enumerate(df.itertuples(index=False), start=1):
                                nm = str(getattr(r, "name", "")).strip()
                                cat_raw = getattr(r, "category", None)

                                if not nm:
                                    skipped += 1
                                else:
                                    cat_v = str(cat_raw).strip() if cat_raw and pd.notna(cat_raw) else None

                                    res = conn.execute(
                                        text("""
                                            INSERT INTO objectives(name, category, is_active)
                                            SELECT :n, :c, TRUE
                                            WHERE NOT EXISTS (
                                                SELECT 1 FROM objectives
                                                WHERE lower(name)=lower(:n)
                                                AND lower(coalesce(category,''))=lower(coalesce(:c,''))
                                            )
                                        """),
                                        {"n": nm, "c": cat_v}
                                    )
                                    if (res.rowcount or 0) > 0:
                                        inserted += 1
                                    else:
                                        skipped += 1

                                if i % 200 == 0 or i == total:
                                    _update_progress(pb, ln, i, total, inserted, 0, skipped, "Objectives")
                                    time.sleep(0.001)

                        _finish_status(
                            sts,
                            has_status,
                            f"Objectives import ✅ Inserted: {inserted} | Skipped: {skipped}",
                            True,
                        )

                    except Exception as e:
                        _finish_status(sts, has_status, "Objectives import failed ❌", False)
                        st.caption(str(e))

        # ---------------------------------------------------
        # MODE 2 — MANAGE
        # ---------------------------------------------------
        else:
            st.markdown("### 📝 Manage Objectives")

            odf = query_df("""
                SELECT objective_id,
                    name,
                    category,
                    COALESCE(is_active, TRUE) AS is_active
                FROM objectives
                ORDER BY name
            """)

            if odf.empty:
                st.info("No objectives yet.")
            else:
                # display format: [ID] Name (status)
                display = [""] + [
                    f"[{r.objective_id}] {r.name} ({'active' if bool(r.is_active) else 'inactive'})"
                    for r in odf.itertuples(index=False)
                ]

                sel = st.selectbox("Select objective", display, index=0, key="mg_obj_sel")

                if sel == "":
                    st.info("Select an objective to edit or update.")
                else:
                    row_idx = display.index(sel) - 1
                    row = odf.iloc[row_idx]
                    oid = int(row["objective_id"])

                    # reference count
                    v_cnt = _refcount(
                        "SELECT COUNT(*) FROM visits WHERE objective_id=:id", {"id": oid}
                    )

                    st.caption(f"Referenced in visits: **{v_cnt}**")
                    st.markdown("---")
                    st.markdown("#### Edit Objective")

                    base_key = f"mg_obj_{oid}"

                    # ---- Name ----
                    name_edit = st.text_input(
                        "Objective name *",
                        value=row["name"] or "",
                        key=f"{base_key}_name"
                    )

                    # ---- Category ----
                    cat_key = f"{base_key}_cat_opt"
                    cat_other_key = f"{base_key}_cat_other"

                    existing_cat = (row["category"] or "").strip()

                    if cat_key not in st.session_state:
                        if existing_cat and existing_cat in category_options:
                            st.session_state[cat_key] = existing_cat
                        elif existing_cat:
                            st.session_state[cat_key] = "OTHER"
                            st.session_state[cat_other_key] = existing_cat
                        else:
                            st.session_state[cat_key] = ""

                    if st.session_state[cat_key] not in category_options:
                        st.session_state[cat_key] = ""

                    cat_idx_edit = category_options.index(st.session_state[cat_key])
                    cat_sel_edit = st.selectbox(
                        "Category",
                        category_options,
                        index=cat_idx_edit,
                        key=cat_key
                    )

                    if cat_sel_edit == "OTHER":
                        cat_other_edit = st.text_input("Other category", key=cat_other_key)
                    else:
                        cat_other_edit = st.session_state.get(cat_other_key, "")

                    # ---- Active Flag ----
                    active_edit = st.checkbox(
                        "Active",
                        value=bool(row["is_active"]),
                        key=f"{base_key}_active",
                    )

                    # Save changes
                    if st.button("Save changes", type="primary", key=f"{base_key}_save"):
                        if not name_edit.strip():
                            st.error("Objective name is required.")
                        else:
                            nm_clean = name_edit.strip()

                            # resolve category
                            if cat_sel_edit == "":
                                cat_v = None
                            elif cat_sel_edit == "OTHER":
                                cat_v = (cat_other_edit or "").strip() or None
                            else:
                                cat_v = cat_sel_edit

                            # check duplicates
                            dup = query_df("""
                                SELECT 1
                                FROM objectives
                                WHERE lower(name)=lower(:n)
                                AND lower(coalesce(category,''))=lower(coalesce(:c,''))
                                AND objective_id<>:id
                            """, {"n": nm_clean, "c": cat_v, "id": oid})

                            if not dup.empty:
                                st.error("An objective with the same name and category already exists.")
                            else:
                                try:
                                    exec_sql("""
                                        UPDATE objectives
                                        SET name=:n,
                                            category=:c,
                                            is_active=:b
                                        WHERE objective_id=:id
                                    """, {
                                        "n": nm_clean,
                                        "c": cat_v,
                                        "b": bool(active_edit),
                                        "id": oid
                                    })
                                    st.success("Objective updated ✅")
                                except Exception as e:
                                    st.error("Could not update objective.")
                                    st.caption(str(e))

                    st.markdown("---")
                    st.markdown("#### 🔴 Danger Zone")

                    del_conf = st.checkbox(
                        "I understand this cannot be undone.",
                        key=f"{base_key}_del_conf"
                    )

                    if st.button("Delete Objective", type="primary", disabled=not del_conf, key=f"{base_key}_del"):
                        if v_cnt > 0:
                            st.error("Cannot delete: objective is referenced by visits.")
                        else:
                            try:
                                exec_sql("DELETE FROM objectives WHERE objective_id=:id", {"id": oid})
                                st.success("Objective deleted ✅")
                                st.session_state.pop("mg_obj_sel", None)
                                st.rerun()
                            except Exception as e:
                                st.error("Delete failed.")
                                st.caption(str(e))

# =============================
# Page — Admin: Data Browser
# =============================
