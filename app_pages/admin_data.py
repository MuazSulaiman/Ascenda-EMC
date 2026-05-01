# pages/admin_data.py
import hashlib
import io
import json
from datetime import date, timedelta

import pandas as pd
import streamlit as st
from sqlalchemy import text

from auth import resolve_session_user
from db_ops import query_df, query_scalar
from widgets import set_current_page
from ui import section_header, html_table

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_CHUNK = 200   # default row limit for large transactional tables
_CHUNK_LINES = 500   # shelf-movement lines grow faster

_PRESETS = ["Last 7 days", "Last 30 days", "Last 90 days", "This year", "All time", "Custom"]


def _preset_dates(preset: str):
    """Return (date_from, date_to) for a named preset. Custom → (None, None)."""
    today = date.today()
    mapping = {
        "Last 7 days":  (today - timedelta(days=7),  today),
        "Last 30 days": (today - timedelta(days=30), today),
        "Last 90 days": (today - timedelta(days=90), today),
        "This year":    (date(today.year, 1, 1),      today),
        "All time":     (None, None),
    }
    return mapping.get(preset, (None, None))


def _date_rep_cust_where(key_prefix: str, rep_names: list, cust_names: list):
    """
    Render date/rep/customer filter widgets and return (where_sql, params).
    Automatically resets the 'load all' flag when filters change.
    WHERE clause uses aliases: v (visits), u (users), c (customers).
    """
    today = date.today()
    fc1, fc2, fc3 = st.columns([2, 2, 2])

    with fc1:
        preset = st.selectbox("Period", _PRESETS, index=4, key=f"{key_prefix}_period")
        if preset == "Custom":
            date_from = st.date_input("From", value=today - timedelta(days=30), key=f"{key_prefix}_from")
            date_to   = st.date_input("To",   value=today,                      key=f"{key_prefix}_to")
        else:
            date_from, date_to = _preset_dates(preset)

    with fc2:
        sel_reps = st.multiselect("Rep", rep_names, key=f"{key_prefix}_reps")

    with fc3:
        sel_custs = st.multiselect("Customer", cust_names, key=f"{key_prefix}_custs")

    where_parts, params = [], {}
    if date_from:
        where_parts.append("v.submitted_at_local >= :df")
        params["df"] = str(date_from)
    if date_to:
        where_parts.append("v.submitted_at_local < :dt")
        params["dt"] = str(date_to + timedelta(days=1))
    if sel_reps:
        for i, r in enumerate(sel_reps):
            params[f"rep_{i}"] = r
        ph = ", ".join(f":rep_{i}" for i in range(len(sel_reps)))
        where_parts.append(f"u.name IN ({ph})")
    if sel_custs:
        for i, c in enumerate(sel_custs):
            params[f"cust_{i}"] = c
        ph = ", ".join(f":cust_{i}" for i in range(len(sel_custs)))
        where_parts.append(f"c.account_name IN ({ph})")

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    # Reset load-all when filter changes
    fkey = hashlib.md5(json.dumps(
        {"p": preset, "f": str(date_from), "t": str(date_to),
         "r": sorted(sel_reps), "c": sorted(sel_custs)},
        sort_keys=True,
    ).encode()).hexdigest()
    if st.session_state.get(f"{key_prefix}_fkey") != fkey:
        st.session_state[f"{key_prefix}_fkey"]     = fkey
        st.session_state[f"{key_prefix}_load_all"] = False

    return where_sql, params


def _transactional_table(
    key_prefix: str,
    count_sql: str,
    data_sql: str,
    params: dict,
    chunk: int,
    dl_filename: str,
    dl_key: str,
):
    """
    Count matching rows, show badge + optional 'Load all' button, render table,
    and offer a Download CSV button that always fetches the full filtered set.
    `data_sql` must NOT contain LIMIT — this helper appends it as needed.
    """
    total    = int(query_scalar(count_sql, params) or 0)
    load_all = st.session_state.get(f"{key_prefix}_load_all", False)
    showing  = total if load_all else min(chunk, total)

    bc, btnc, dlc = st.columns([4, 2, 2])
    with bc:
        st.markdown(f"**Showing {showing:,} of {total:,}**")
    with btnc:
        if not load_all and total > chunk:
            if st.button(f"Load all {total:,} rows", key=f"{key_prefix}_load_btn"):
                st.session_state[f"{key_prefix}_load_all"] = True
                st.rerun()

    paged_sql = data_sql if load_all else f"{data_sql} LIMIT {chunk}"
    df = query_df(paged_sql, params)
    st.markdown(html_table(df), unsafe_allow_html=True)

    with dlc:
        if total > 0:
            csv_df = query_df(data_sql, params) if not load_all else df
            st.download_button(
                f"Download CSV ({total:,})",
                csv_df.to_csv(index=False).encode("utf-8-sig"),
                dl_filename, "text/csv", key=dl_key,
            )


def _reference_table(df: pd.DataFrame, search_key: str, dl_filename: str, dl_key: str, key_prefix: str):
    """
    For static reference tables: free-text search, paged display (same structure
    as _transactional_table), and Download CSV.
    """
    q = st.text_input("Search", placeholder="type to filter…", key=search_key, label_visibility="collapsed")
    if q:
        mask = df.apply(lambda col: col.astype(str).str.contains(q, case=False, na=False)).any(axis=1)
        df   = df[mask]

    # Reset load-all when search changes
    if st.session_state.get(f"{key_prefix}_sq") != q:
        st.session_state[f"{key_prefix}_sq"]       = q
        st.session_state[f"{key_prefix}_load_all"] = False

    total    = len(df)
    load_all = st.session_state.get(f"{key_prefix}_load_all", False)
    showing  = total if load_all else min(_CHUNK, total)
    display  = df if load_all else df.head(_CHUNK)

    bc, btnc, dlc = st.columns([4, 2, 2])
    with bc:
        st.markdown(f"**Showing {showing:,} of {total:,}**")
    with btnc:
        if not load_all and total > _CHUNK:
            if st.button(f"Load all {total:,} rows", key=f"{key_prefix}_load_btn"):
                st.session_state[f"{key_prefix}_load_all"] = True
                st.rerun()

    st.markdown(html_table(display), unsafe_allow_html=True)

    with dlc:
        if total > 0:
            st.download_button(
                f"Download CSV ({total:,})",
                df.to_csv(index=False).encode("utf-8-sig"),
                dl_filename, "text/csv", key=dl_key,
            )


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------

def _get_export_tables() -> dict:
    """Fetch all tables needed for bulk export. Called by both export buttons."""
    return {
        "visits": query_df("""
            SELECT v.*, c.account_name AS customer_name,
                   i.article_number, i.description,
                   bl.name AS business_line, bu.name AS business_unit,
                   o.name AS objective_name,
                   hv.patient_name, hv.patient_phone, hv.serial_no,
                   COALESCE((SELECT COUNT(*) FROM shelf_movement_lines l
                       JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                       WHERE h.visit_id = v.visit_id), 0) AS shelf_lines_count,
                   COALESCE((SELECT SUM(l.qty_checked) FROM shelf_movement_lines l
                       JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                       WHERE h.visit_id = v.visit_id), 0) AS shelf_total_qty
            FROM visits v
            JOIN customers c        ON v.customer_id = c.customer_id
            LEFT JOIN items i        ON v.product_id = i.product_id
            LEFT JOIN business_lines bl   ON bl.business_line_id = v.business_line_id
            LEFT JOIN business_units bu   ON bu.business_unit_id = bl.business_unit_id
            JOIN objectives o        ON v.objective_id = o.objective_id
            LEFT JOIN home_visits hv ON hv.visit_id = v.visit_id
            WHERE COALESCE(v.is_deleted, FALSE) IS FALSE
            ORDER BY v.visit_id DESC
        """),
        "users":                   query_df("SELECT * FROM users ORDER BY user_id DESC"),
        "customers":               query_df("SELECT * FROM customers ORDER BY account_name"),
        "target_audiences":        query_df("SELECT * FROM target_audiences ORDER BY audience_id DESC"),
        "business_units":          query_df("SELECT * FROM business_units ORDER BY business_unit_id"),
        "business_lines":          query_df("""
            SELECT bl.*, bu.name AS business_unit
            FROM business_lines bl
            JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
            ORDER BY bu.name, bl.name
        """),
        "items":                   query_df("""
            SELECT i.product_id, i.article_number, i.description, i.is_active,
                   bl.name AS business_line, bu.name AS business_unit
            FROM items i
            JOIN business_lines bl ON bl.business_line_id = i.business_line_id
            JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
            ORDER BY COALESCE(i.article_number, i.product_id)
        """),
        "objectives":              query_df("SELECT * FROM objectives ORDER BY objective_id"),
        "home_visits":             query_df("""
            SELECT hv.*, v.submitted_at_local, u.name AS rep, c.account_name AS customer
            FROM home_visits hv
            JOIN visits v ON v.visit_id = hv.visit_id
            JOIN users u  ON u.user_id  = v.user_id
            JOIN customers c ON c.customer_id = v.customer_id
            ORDER BY hv.home_visit_id DESC
        """),
        "shelf_movement_headers":  query_df("SELECT * FROM shelf_movement_headers ORDER BY movement_id DESC"),
        "shelf_movement_lines":    query_df("SELECT * FROM shelf_movement_lines ORDER BY line_id DESC"),
    }


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def page_admin_data():
    u = st.session_state.get("user")
    if not u or (u.get("role") or "").lower().strip() != "admin":
        st.error("Access denied.")
        st.stop()

    section_header("Admin — Data Browser", "Browse and export all app data tables")
    set_current_page("admin_data")

    # Shared lookup lists for filter dropdowns (cheap queries, run once)
    _rep_names  = query_df("SELECT name FROM users ORDER BY name")["name"].tolist()
    _cust_names = query_df("SELECT account_name FROM customers ORDER BY account_name")["account_name"].tolist()

    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9, tab10 = st.tabs([
        "Visits", "Users", "Customers", "Target Audiences",
        "Business Units", "Business Lines",
        "Items", "Objectives", "Home Visits", "Shelf Movement",
    ])

    # ------------------------------------------------------------------ Visits
    with tab1:
        where_sql, params = _date_rep_cust_where("v", _rep_names, _cust_names)

        v_search = st.text_input("Search", placeholder="type to filter…", key="v_search", label_visibility="collapsed")
        if v_search:
            params["v_search"] = f"%{v_search}%"
            search_clause = (
                "(v.visit_id::text ILIKE :v_search OR u.name ILIKE :v_search "
                "OR c.account_name ILIKE :v_search OR o.name ILIKE :v_search "
                "OR v.notes ILIKE :v_search)"
            )
            where_sql = (where_sql + f" AND {search_clause}") if where_sql else f"WHERE {search_clause}"

        visit_where = ("WHERE COALESCE(v.is_deleted, FALSE) IS FALSE" +
                       (" AND " + where_sql[len("WHERE "):] if where_sql.startswith("WHERE ") else ""))

        _transactional_table(
            key_prefix  = "v",
            count_sql   = f"""
                SELECT COUNT(*) AS n
                FROM visits v
                JOIN users u     ON v.user_id     = u.user_id
                JOIN customers c ON v.customer_id = c.customer_id
                JOIN objectives o ON v.objective_id = o.objective_id
                {visit_where}
            """,
            data_sql    = f"""
                SELECT
                    v.visit_id,
                    v.submitted_at_local,
                    u.name              AS rep,
                    c.account_name      AS customer,
                    ta.name             AS audience,
                    i.article_number,
                    i.description,
                    bu.name             AS business_unit,
                    bl.name             AS business_line,
                    o.name              AS objective,
                    v.evaluation,
                    v.latitude,
                    v.longitude,
                    v.accuracy_m,
                    v.notes,
                    hv.patient_name,
                    hv.patient_phone,
                    hv.serial_no,
                    COALESCE((
                        SELECT COUNT(*) FROM shelf_movement_lines l
                        JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                        WHERE h.visit_id = v.visit_id
                    ), 0) AS shelf_lines_count,
                    COALESCE((
                        SELECT SUM(l.qty_checked) FROM shelf_movement_lines l
                        JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                        WHERE h.visit_id = v.visit_id
                    ), 0) AS shelf_total_qty
                FROM visits v
                JOIN users u                ON v.user_id       = u.user_id
                JOIN customers c            ON v.customer_id   = c.customer_id
                LEFT JOIN target_audiences ta ON v.audience_id = ta.audience_id
                LEFT JOIN items i            ON v.product_id   = i.product_id
                LEFT JOIN business_lines bl  ON bl.business_line_id = v.business_line_id
                LEFT JOIN business_units bu  ON bu.business_unit_id = bl.business_unit_id
                JOIN objectives o            ON v.objective_id = o.objective_id
                LEFT JOIN home_visits hv     ON hv.visit_id    = v.visit_id
                {visit_where}
                ORDER BY v.visit_id DESC
            """,
            params      = params,
            chunk       = _CHUNK,
            dl_filename = "visits.csv",
            dl_key      = "dl_visits",
        )

    # ------------------------------------------------------------------ Users
    with tab2:
        df = query_df("SELECT user_id, email, name, region, role, is_active FROM users ORDER BY user_id DESC")
        _reference_table(df, "search_users", "users.csv", "dl_users", "users")

    # --------------------------------------------------------------- Customers
    with tab3:
        df = query_df("SELECT * FROM customers ORDER BY account_name")

        # ---- Sector / Region / City filters ----
        all_sectors = sorted(df["sector"].dropna().str.strip().unique().tolist())
        all_regions = sorted(df["region"].dropna().str.strip().unique().tolist())

        fc1, fc2, fc3 = st.columns(3)
        with fc1:
            sel_sectors = st.multiselect("Sector", all_sectors, key="cust_filter_sector")
        with fc2:
            sel_regions = st.multiselect("Region", all_regions, key="cust_filter_region")
        with fc3:
            if sel_regions:
                city_pool = df[df["region"].isin(sel_regions)]
            else:
                city_pool = df
            all_cities = sorted(city_pool["city"].dropna().str.strip().unique().tolist())
            sel_cities = st.multiselect("City", all_cities, key="cust_filter_city")

        # Apply filters
        if sel_sectors:
            df = df[df["sector"].isin(sel_sectors)]
        if sel_regions:
            df = df[df["region"].isin(sel_regions)]
        if sel_cities:
            df = df[df["city"].isin(sel_cities)]

        _reference_table(df, "search_customers", "customers.csv", "dl_customers", "customers")

    # --------------------------------------------------- Target Audiences
    with tab4:
        df = query_df("""
            SELECT ta.audience_id, ta.customer_id, c.account_name,
                   ta.name, ta.department, ta.position, ta.is_active
            FROM target_audiences ta
            LEFT JOIN customers c ON c.customer_id = ta.customer_id
            ORDER BY ta.audience_id DESC
        """)

        # ---- Customer / Department / Position filters ----
        all_customers  = sorted(df["account_name"].dropna().str.strip().unique().tolist())
        all_depts      = sorted(df["department"].dropna().str.strip().unique().tolist())
        all_positions  = sorted(df["position"].dropna().str.strip().unique().tolist())

        fc1, fc2, fc3 = st.columns(3)
        with fc1:
            sel_ta_custs = st.multiselect("Customer", all_customers, key="ta_filter_customer")
        with fc2:
            sel_ta_depts = st.multiselect("Department", all_depts, key="ta_filter_dept")
        with fc3:
            sel_ta_pos   = st.multiselect("Position", all_positions, key="ta_filter_pos")

        if sel_ta_custs:
            df = df[df["account_name"].isin(sel_ta_custs)]
        if sel_ta_depts:
            df = df[df["department"].isin(sel_ta_depts)]
        if sel_ta_pos:
            df = df[df["position"].isin(sel_ta_pos)]

        _reference_table(df, "search_audiences", "target_audiences.csv", "dl_audiences", "audiences")

    # --------------------------------------------------- Business Units
    with tab5:
        df = query_df("SELECT business_unit_id, name, is_active FROM business_units ORDER BY name")
        _reference_table(df, "search_bus", "business_units.csv", "dl_business_units", "bus")

    # --------------------------------------------------- Business Lines
    with tab6:
        df = query_df("""
            SELECT bl.business_line_id, bu.name AS business_unit,
                   bl.name AS business_line, bl.category, bl.supplier,
                   bl.product_group, bl.is_active
            FROM business_lines bl
            JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
            ORDER BY bu.name, bl.name
        """)
        _reference_table(df, "search_bls", "business_lines.csv", "dl_business_lines", "bls")

    # --------------------------------------------------------------- Items
    with tab7:
        df = query_df("""
            SELECT i.product_id, i.article_number, i.description, i.is_active,
                   bl.name AS business_line, bu.name AS business_unit
            FROM items i
            JOIN business_lines bl ON bl.business_line_id = i.business_line_id
            JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
            ORDER BY COALESCE(i.article_number, i.product_id)
        """)
        _reference_table(df, "search_items", "items.csv", "dl_items", "items")

    # ----------------------------------------------------------- Objectives
    with tab8:
        df = query_df("SELECT * FROM objectives ORDER BY objective_id")
        _reference_table(df, "search_objectives", "objectives.csv", "dl_objectives", "objectives")

    # --------------------------------------------------------------- Home Visits
    with tab9:
        where_sql, params = _date_rep_cust_where("hv", _rep_names, _cust_names)

        _transactional_table(
            key_prefix  = "hv",
            count_sql   = f"""
                SELECT COUNT(*) AS n
                FROM home_visits hv
                JOIN visits v    ON v.visit_id     = hv.visit_id
                JOIN users u     ON u.user_id      = v.user_id
                JOIN customers c ON c.customer_id  = v.customer_id
                {where_sql}
            """,
            data_sql    = f"""
                SELECT hv.home_visit_id,
                       v.visit_id,
                       v.submitted_at_local,
                       u.name              AS rep,
                       c.account_name      AS customer,
                       hv.patient_name,
                       hv.patient_phone,
                       hv.serial_no,
                       v.latitude, v.longitude, v.accuracy_m,
                       o.name              AS objective
                FROM home_visits hv
                JOIN visits v     ON v.visit_id    = hv.visit_id
                JOIN users u      ON u.user_id     = v.user_id
                JOIN customers c  ON c.customer_id = v.customer_id
                JOIN objectives o ON o.objective_id = v.objective_id
                {where_sql}
                ORDER BY hv.home_visit_id DESC
            """,
            params      = params,
            chunk       = _CHUNK,
            dl_filename = "home_visits.csv",
            dl_key      = "dl_home_visits",
        )

    # --------------------------------------------------------- Shelf Movement
    with tab10:
        sub1, sub2 = st.tabs(["Headers (per visit)", "Lines (per product)"])

        with sub1:
            where_sql, params = _date_rep_cust_where("sm_h", _rep_names, _cust_names)

            _transactional_table(
                key_prefix  = "sm_h",
                count_sql   = f"""
                    SELECT COUNT(*) AS n
                    FROM shelf_movement_headers h
                    JOIN visits v    ON v.visit_id    = h.visit_id
                    JOIN users u     ON u.user_id     = v.user_id
                    JOIN customers c ON c.customer_id = v.customer_id
                    {where_sql}
                """,
                data_sql    = f"""
                    SELECT
                        h.movement_id,
                        v.visit_id,
                        v.submitted_at_local,
                        u.name             AS rep,
                        c.account_name     AS customer,
                        bu.name            AS business_unit,
                        bl.name            AS business_line,
                        o.name             AS objective,
                        COALESCE((SELECT COUNT(*) FROM shelf_movement_lines l
                                  WHERE l.movement_id = h.movement_id), 0) AS lines_count,
                        COALESCE((SELECT SUM(l.qty_checked) FROM shelf_movement_lines l
                                  WHERE l.movement_id = h.movement_id), 0) AS total_qty
                    FROM shelf_movement_headers h
                    JOIN visits v       ON v.visit_id     = h.visit_id
                    JOIN users u        ON u.user_id      = v.user_id
                    JOIN customers c    ON c.customer_id  = v.customer_id
                    JOIN business_lines bl ON bl.business_line_id = v.business_line_id
                    JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
                    JOIN objectives o   ON o.objective_id = v.objective_id
                    {where_sql}
                    ORDER BY h.movement_id DESC
                """,
                params      = params,
                chunk       = _CHUNK,
                dl_filename = "shelf_movement_headers.csv",
                dl_key      = "dl_sm_headers",
            )

        with sub2:
            where_sql, params = _date_rep_cust_where("sm_l", _rep_names, _cust_names)

            _transactional_table(
                key_prefix  = "sm_l",
                count_sql   = f"""
                    SELECT COUNT(*) AS n
                    FROM shelf_movement_lines l
                    JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                    JOIN visits v    ON v.visit_id    = h.visit_id
                    JOIN users u     ON u.user_id     = v.user_id
                    JOIN customers c ON c.customer_id = v.customer_id
                    {where_sql}
                """,
                data_sql    = f"""
                    SELECT
                        h.movement_id,
                        v.visit_id,
                        v.submitted_at_local,
                        u.name             AS rep,
                        c.account_name     AS customer,
                        COALESCE(i.article_number, i.product_id) AS article_number,
                        i.description,
                        bu.name            AS business_unit,
                        bl.name            AS business_line,
                        l.qty_checked
                    FROM shelf_movement_lines l
                    JOIN shelf_movement_headers h ON h.movement_id  = l.movement_id
                    JOIN visits v                 ON v.visit_id     = h.visit_id
                    JOIN users u                  ON u.user_id      = v.user_id
                    JOIN customers c              ON c.customer_id  = v.customer_id
                    LEFT JOIN items i             ON i.product_id   = l.product_id
                    LEFT JOIN business_lines bl   ON bl.business_line_id = i.business_line_id
                    LEFT JOIN business_units bu   ON bu.business_unit_id = bl.business_unit_id
                    {where_sql}
                    ORDER BY h.movement_id DESC, article_number
                """,
                params      = params,
                chunk       = _CHUNK_LINES,
                dl_filename = "shelf_movement_lines.csv",
                dl_key      = "dl_sm_lines",
            )

    # ---------------------------------------------------------------- Export all
    st.divider()
    if st.button("Export all tables (zip)", type="secondary", key="export_zip"):
        try:
            ts = datetime.now().strftime("%Y-%m-%d_%H%M")

            tables = _get_export_tables()

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                for name, df in tables.items():
                    csv_bytes = df.to_csv(index=False, na_rep="").encode("utf-8-sig")
                    zf.writestr(f"{name}_{ts}.csv", csv_bytes)

            data = buf.getvalue()
            size_mb = len(data) / (1024 * 1024)
            st.success(f"Export ready (~{size_mb:.2f} MB).")
            st.download_button(
                "Download export.zip",
                data=data,
                file_name=f"export_pack_{ts}.zip",
                mime="application/zip",
                key="dl_zip_all",
            )
        except Exception as e:
            st.error("Export failed ❌")
            st.caption(str(e))

    # ----------------------------------------- Full database backup options
    st.divider()
    col_sql, col_zip = st.columns(2)

    def _db_url():
        for k in ("DATABASE_URL", "POSTGRES_URL", "POSTGRES_CONNECTION_STRING"):
            v = os.environ.get(k) or _get_secret(k, "")
            if v:
                return v
        return None

    def _normalize_pg_url(url: str) -> str:
        return url.replace("postgres://", "postgresql://", 1) if url.startswith("postgres://") else url

    def _pg_dump_available() -> bool:
        import shutil
        return shutil.which("pg_dump") is not None

    with col_sql:
        if st.button("Download full DB (.sql via pg_dump)", key="export_pg_dump"):
            try:
                db_url = _db_url()
                if not db_url:
                    raise RuntimeError("DATABASE_URL / POSTGRES_URL not set.")
                if not _pg_dump_available():
                    raise FileNotFoundError("pg_dump not found in PATH.")

                import subprocess
                cmd = [
                    "pg_dump", "--no-owner", "--no-privileges", "--format=plain",
                    _normalize_pg_url(db_url),
                ]
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
                if proc.returncode != 0 or not proc.stdout:
                    err = proc.stderr.decode("utf-8", errors="ignore")
                    raise RuntimeError(f"pg_dump failed.\n{err.strip() or 'No error text.'}")

                ts = datetime.now().strftime("%Y-%m-%d_%H%M")
                st.success("Full SQL dump ready.")
                st.download_button(
                    label="Download database.sql",
                    data=proc.stdout,
                    file_name=f"database_backup_{ts}.sql",
                    mime="application/sql",
                    key="dl_pg_dump_sql",
                )
            except FileNotFoundError:
                st.error("`pg_dump` is not available in this environment.")
                st.info("Use the portable backup (right column) or add `pg_dump` to your image.")
            except Exception as e:
                st.error("Full SQL dump failed ❌")
                st.caption(str(e))

    with col_zip:
        if st.button("Download portable backup (schema+CSVs .zip)", key="export_portable_zip"):
            try:
                ts = datetime.now().strftime("%Y-%m-%d_%H%M")

                tables = _get_export_tables()

                buf = io.BytesIO()
                with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                    schema_bytes = b""
                    try:
                        db_url = _db_url()
                        if not db_url:
                            raise RuntimeError("DATABASE_URL / POSTGRES_URL not set.")
                        if not _pg_dump_available():
                            raise FileNotFoundError("pg_dump not found")

                        import subprocess
                        cmd_schema = [
                            "pg_dump", "--no-owner", "--no-privileges",
                            "--format=plain", "--schema-only",
                            _normalize_pg_url(db_url),
                        ]
                        p = subprocess.run(cmd_schema, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
                        if p.returncode != 0 or not p.stdout:
                            err = p.stderr.decode("utf-8", errors="ignore")
                            raise RuntimeError(f"pg_dump --schema-only failed.\n{err.strip() or 'No error text.'}")
                        schema_bytes = p.stdout
                    except Exception as e:
                        schema_bytes = (
                            b"-- schema.sql not auto-included.\n"
                            b"-- Reason: " + str(e).encode("utf-8", errors="ignore") + b"\n"
                        )

                    zf.writestr("schema.sql", schema_bytes)
                    for name, df in tables.items():
                        csv_bytes = df.to_csv(index=False, na_rep="").encode("utf-8-sig")
                        zf.writestr(f"data/{name}_{ts}.csv", csv_bytes)

                    readme = f"""# Portable Backup

This archive contains:
- `schema.sql` — live PostgreSQL schema (pg_dump when available).
- `data/*.csv` — table data exports.

## Quick restore (psql)

1) Create an empty database.
2) Load the schema:

   psql "$DATABASE_URL" -f schema.sql

3) Load CSVs in dependency order:

   psql "$DATABASE_URL" -c "\\copy users FROM 'data/users_{ts}.csv' WITH (FORMAT csv, HEADER true)"
   psql "$DATABASE_URL" -c "\\copy customers FROM 'data/customers_{ts}.csv' WITH (FORMAT csv, HEADER true)"
   psql "$DATABASE_URL" -c "\\copy objectives FROM 'data/objectives_{ts}.csv' WITH (FORMAT csv, HEADER true)"
   psql "$DATABASE_URL" -c "\\copy business_units FROM 'data/business_units_{ts}.csv' WITH (FORMAT csv, HEADER true)"
   psql "$DATABASE_URL" -c "\\copy business_lines FROM 'data/business_lines_{ts}.csv' WITH (FORMAT csv, HEADER true)"
   psql "$DATABASE_URL" -c "\\copy items FROM 'data/items_{ts}.csv' WITH (FORMAT csv, HEADER true)"
   psql "$DATABASE_URL" -c "\\copy target_audiences FROM 'data/target_audiences_{ts}.csv' WITH (FORMAT csv, HEADER true)"
   psql "$DATABASE_URL" -c "\\copy visits FROM 'data/visits_{ts}.csv' WITH (FORMAT csv, HEADER true)"
   psql "$DATABASE_URL" -c "\\copy home_visits FROM 'data/home_visits_{ts}.csv' WITH (FORMAT csv, HEADER true)"
   psql "$DATABASE_URL" -c "\\copy shelf_movement_headers FROM 'data/shelf_movement_headers_{ts}.csv' WITH (FORMAT csv, HEADER true)"
   psql "$DATABASE_URL" -c "\\copy shelf_movement_lines FROM 'data/shelf_movement_lines_{ts}.csv' WITH (FORMAT csv, HEADER true)"
"""
                    zf.writestr("README_restore.md", readme.encode("utf-8"))

                data = buf.getvalue()
                size_mb = len(data) / (1024 * 1024)
                st.success(f"Portable backup ready (~{size_mb:.2f} MB).")
                st.download_button(
                    "Download portable_backup.zip",
                    data=data,
                    file_name=f"portable_backup_{ts}.zip",
                    mime="application/zip",
                    key="dl_portable_zip",
                )
            except Exception as e:
                st.error("Portable backup failed ❌")
                st.caption(str(e))


# =============================
# Helper — generate a strong temporary password
# =============================
import secrets, string
