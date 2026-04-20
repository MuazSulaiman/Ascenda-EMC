# pages/admin_data.py
import io
import pandas as pd
import streamlit as st
from sqlalchemy import text

from auth import resolve_session_user
from db_ops import query_df
from widgets import set_current_page
from ui import section_header

def page_admin_data():
    section_header("Admin — Data Browser", "Browse and export all app data tables")
    set_current_page("admin_data")
    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9, tab10 = st.tabs([
        "Visits", "Users", "Customers", "Target Audiences",
        "Business Units", "Business Lines",  # ← added Business Lines right after Business Units
        "Items", "Objectives", "Home Visits", "Shelf Movement",
    ])

    # ---------- Visits ----------
    with tab1:
        df = query_df(
            """
            SELECT
                v.visit_id,
                v.submitted_at_local,
                u.name AS rep,
                c.account_name AS customer,
                ta.name AS audience,
                -- product fields (may be NULL if Shelf Movement)
                i.article_number,
                i.description,
                -- BU/BL resolved from the visit's business_line_id
                bu.name AS business_unit,
                bl.name AS business_line,
                o.name AS objective,
                v.evaluation,
                v.latitude,
                v.longitude,
                v.accuracy_m,
                v.notes,
                hv.patient_name,
                hv.patient_phone,
                hv.serial_no,
                COALESCE((
                  SELECT COUNT(*)
                  FROM shelf_movement_lines l
                  JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                  WHERE h.visit_id = v.visit_id
                ),0) AS shelf_lines_count,
                COALESCE((
                  SELECT SUM(l.qty_checked)
                  FROM shelf_movement_lines l
                  JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                  WHERE h.visit_id = v.visit_id
                ),0) AS shelf_total_qty
            FROM visits v
            JOIN users u            ON v.user_id = u.user_id
            JOIN customers c        ON v.customer_id = c.customer_id
            LEFT JOIN target_audiences ta ON v.audience_id = ta.audience_id
            LEFT JOIN items i       ON v.product_id = i.product_id
            LEFT JOIN business_lines bl  ON bl.business_line_id = v.business_line_id
            LEFT JOIN business_units bu  ON bu.business_unit_id = bl.business_unit_id
            JOIN objectives o       ON v.objective_id = o.objective_id
            LEFT JOIN home_visits hv ON hv.visit_id = v.visit_id
            ORDER BY v.visit_id DESC
            """
        )
        st.markdown(f"**Total: {len(df):,}**")
        st.dataframe(df, width="stretch", hide_index=True)
        if not df.empty:
            st.download_button(
                "Download CSV",
                df.to_csv(index=False).encode("utf-8-sig"),
                "visits.csv",
                "text/csv",
                key="dl_visits",
            )

    # ---------- Users ----------
    with tab2:
        df = query_df("SELECT user_id, email, name, region, role, is_active FROM users ORDER BY user_id DESC")
        st.markdown(f"**Total: {len(df):,}**")
        st.dataframe(df, width="stretch", hide_index=True)
        if not df.empty:
            st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                               "users.csv", "text/csv", key="dl_users")

    # ---------- Customers ----------
    with tab3:
        df = query_df("SELECT * FROM customers ORDER BY account_name")
        st.markdown(f"**Total: {len(df):,}**")
        st.dataframe(df, width="stretch", hide_index=True)
        if not df.empty:
            st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                               "customers.csv", "text/csv", key="dl_customers")

    # ---------- Target Audiences ----------
    with tab4:
        df = query_df("""
            SELECT
                ta.audience_id,
                ta.customer_id,
                c.account_name,
                ta.name,
                ta.department,
                ta.position,
                ta.is_active
            FROM target_audiences ta
            LEFT JOIN customers c
                ON c.customer_id = ta.customer_id
            ORDER BY ta.audience_id DESC
        """)
        st.markdown(f"**Total: {len(df):,}**")
        st.dataframe(df, width="stretch", hide_index=True)
        if not df.empty:
            st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                               "target_audiences.csv", "text/csv", key="dl_audiences")

    # ---------- Business Units ----------
    with tab5:
        df = query_df("SELECT business_unit_id, name, is_active FROM business_units ORDER BY name")
        st.markdown(f"**Total: {len(df):,}**")
        st.dataframe(df, width="stretch", hide_index=True)
        if not df.empty:
            st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                               "business_units.csv", "text/csv", key="dl_business_units")

    # ---------- Business Lines (NEW) ----------
    with tab6:
        df = query_df(
            """
            SELECT
                bl.business_line_id,
                bu.name AS business_unit,
                bl.name AS business_line,
                bl.category,
                bl.supplier,
                bl.product_group,
                bl.is_active
            FROM business_lines bl
            JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
            ORDER BY bu.name, bl.name
            """
        )
        st.markdown(f"**Total: {len(df):,}**")
        st.dataframe(df, width="stretch", hide_index=True)
        if not df.empty:
            st.download_button(
                "Download CSV",
                df.to_csv(index=False).encode("utf-8-sig"),
                "business_lines.csv",
                "text/csv",
                key="dl_business_lines"
            )

    # ---------- Items ----------
    with tab7:
        df = query_df(
            """
            SELECT
                i.product_id,
                i.article_number,
                i.description,
                i.is_active,
                bl.name AS business_line,
                bu.name AS business_unit
            FROM items i
            JOIN business_lines bl ON bl.business_line_id = i.business_line_id
            JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
            ORDER BY COALESCE(i.article_number, i.product_id)
           """
        )
        st.markdown(f"**Total: {len(df):,}**")
        st.dataframe(df, width="stretch", hide_index=True)
        if not df.empty:
            st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                               "items.csv", "text/csv", key="dl_items")

    # ---------- Objectives ----------
    with tab8:
        df = query_df("SELECT * FROM objectives ORDER BY objective_id")
        st.markdown(f"**Total: {len(df):,}**")
        st.dataframe(df, width="stretch", hide_index=True)
        if not df.empty:
            st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                               "objectives.csv", "text/csv", key="dl_objectives")

    # ---------- Home Visits ----------
    with tab9:
        df = query_df(
            """
            SELECT hv.home_visit_id,
                   v.visit_id,
                   v.submitted_at_local,
                   u.name AS rep,
                   c.account_name AS customer,
                   hv.patient_name,
                   hv.patient_phone,
                   hv.serial_no,
                   v.latitude, v.longitude, v.accuracy_m,
                   o.name AS objective
            FROM home_visits hv
            JOIN visits v           ON v.visit_id = hv.visit_id
            JOIN users  u           ON u.user_id  = v.user_id
            JOIN customers c        ON c.customer_id = v.customer_id
            JOIN objectives o       ON o.objective_id = v.objective_id
            ORDER BY hv.home_visit_id DESC
            """
        )
        st.markdown(f"**Total Home Visits: {len(df):,}**")
        st.dataframe(df, width="stretch", hide_index=True)
        if not df.empty:
            st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                               "home_visits.csv", "text/csv", key="dl_home_visits")

    # ---------- Shelf Movement ----------
    with tab10:
        sub1, sub2 = st.tabs(["Headers (per visit)", "Lines (per product)"])

        # Headers with aggregates
        with sub1:
            df = query_df(
                """
                SELECT
                    h.movement_id,
                    v.visit_id,
                    v.submitted_at_local,
                    u.name AS rep,
                    c.account_name AS customer,
                    bu.name AS business_unit,
                    bl.name AS business_line,
                    o.name AS objective,
                    COALESCE((
                      SELECT COUNT(*)
                      FROM shelf_movement_lines l WHERE l.movement_id = h.movement_id
                    ),0) AS lines_count,
                    COALESCE((
                      SELECT SUM(l.qty_checked)
                      FROM shelf_movement_lines l WHERE l.movement_id = h.movement_id
                    ),0) AS total_qty
                FROM shelf_movement_headers h
                JOIN visits v       ON v.visit_id = h.visit_id
                JOIN users u        ON u.user_id  = v.user_id
                JOIN customers c    ON c.customer_id = v.customer_id
                JOIN business_lines bl ON bl.business_line_id = v.business_line_id
                JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
                JOIN objectives o   ON o.objective_id = v.objective_id
                ORDER BY h.movement_id DESC
                """
            )
            st.markdown(f"**Total Movements: {len(df):,}**")
            st.dataframe(df, width="stretch", hide_index=True)
            if not df.empty:
                st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                                   "shelf_movement_headers.csv", "text/csv", key="dl_sm_headers")

        # Lines detail
        with sub2:
            df = query_df(
                """
                SELECT
                    h.movement_id,
                    v.visit_id,
                    v.submitted_at_local,
                    u.name AS rep,
                    c.account_name AS customer,
                    i.product_id,
                    COALESCE(i.article_number, i.product_id) AS article_number,
                    i.description,
                    bu.name AS business_unit,
                    bl.name AS business_line,
                    l.qty_checked
                FROM shelf_movement_lines l
                JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                JOIN visits v                 ON v.visit_id = h.visit_id
                JOIN users u                  ON u.user_id  = v.user_id
                JOIN customers c              ON c.customer_id = v.customer_id
                LEFT JOIN items i             ON i.product_id = l.product_id
                LEFT JOIN business_lines bl   ON bl.business_line_id = i.business_line_id
                LEFT JOIN business_units bu   ON bu.business_unit_id = bl.business_unit_id
                ORDER BY h.movement_id DESC, article_number
                """
            )
            st.markdown(f"**Total Lines: {len(df):,}**")
            st.dataframe(df, width="stretch", hide_index=True)
            if not df.empty:
                st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                                   "shelf_movement_lines.csv", "text/csv", key="dl_sm_lines")

    # ---------- Export all ----------
    st.divider()
    if st.button("Export all tables (zip)", type="secondary", key="export_zip"):
        try:
            # Safer timestamp for filenames (no ":" which breaks on Windows)
            ts = datetime.now().strftime("%Y-%m-%d_%H%M")

            # Build dataframes
            tables = {
                "visits": query_df("""
                    SELECT
                        v.*,
                        c.account_name AS customer_name,
                        i.article_number,
                        i.description,
                        bl.name AS business_line,
                        bu.name AS business_unit,
                        o.name AS objective_name,
                        hv.patient_name,
                        hv.patient_phone,
                        hv.serial_no,
                        COALESCE((
                        SELECT COUNT(*)
                        FROM shelf_movement_lines l
                        JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                        WHERE h.visit_id = v.visit_id
                        ),0) AS shelf_lines_count,
                        COALESCE((
                        SELECT SUM(l.qty_checked)
                        FROM shelf_movement_lines l
                        JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                        WHERE h.visit_id = v.visit_id
                        ),0) AS shelf_total_qty
                    FROM visits v
                    JOIN customers c        ON v.customer_id = c.customer_id
                    LEFT JOIN items i        ON v.product_id = i.product_id
                    LEFT JOIN business_lines bl   ON bl.business_line_id = v.business_line_id
                    LEFT JOIN business_units bu   ON bu.business_unit_id = bl.business_unit_id
                    JOIN objectives o        ON v.objective_id = o.objective_id
                    LEFT JOIN home_visits hv ON hv.visit_id = v.visit_id
                    ORDER BY v.visit_id DESC
                """),
                "users": query_df("SELECT * FROM users ORDER BY user_id DESC"),
                "customers": query_df("SELECT * FROM customers ORDER BY account_name"),
                "target_audiences": query_df("SELECT * FROM target_audiences ORDER BY audience_id DESC"),
                "business_units": query_df("SELECT * FROM business_units ORDER BY business_unit_id"),
                "business_lines": query_df("""
                    SELECT bl.*, bu.name AS business_unit
                    FROM business_lines bl
                    JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
                    ORDER BY bu.name, bl.name
                """),
                "items": query_df("""
                    SELECT
                        i.product_id,
                        i.article_number,
                        i.description,
                        i.is_active,
                        bl.name AS business_line,
                        bu.name AS business_unit
                    FROM items i
                    JOIN business_lines bl ON bl.business_line_id = i.business_line_id
                    JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
                    ORDER BY COALESCE(i.article_number, i.product_id)
                """),
                "objectives": query_df("SELECT * FROM objectives ORDER BY objective_id"),
                "home_visits": query_df("""
                    SELECT hv.*, v.submitted_at_local, u.name AS rep, c.account_name AS customer
                    FROM home_visits hv
                    JOIN visits v ON v.visit_id = hv.visit_id
                    JOIN users u  ON u.user_id  = v.user_id
                    JOIN customers c ON c.customer_id = v.customer_id
                    ORDER BY hv.home_visit_id DESC
                """),
                "shelf_movement_headers": query_df("SELECT * FROM shelf_movement_headers ORDER BY movement_id DESC"),
                "shelf_movement_lines": query_df("SELECT * FROM shelf_movement_lines ORDER BY line_id DESC"),
            }

            # Write a zip in-memory
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                for name, df in tables.items():
                    # Make sure we don't emit "nan" strings in Excel
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

    # ---------- Full database backup options (auto schema from pg_dump) ----------
    st.divider()
    col_sql, col_zip = st.columns(2)

    def _db_url():
        # Prefer env; fall back to secrets (you already have _get_secret, reuse if you like)
        for k in ("DATABASE_URL", "POSTGRES_URL", "POSTGRES_CONNECTION_STRING"):
            v = os.environ.get(k) or _get_secret(k, "")
            if v:
                return v
        return None

    def _normalize_pg_url(url: str) -> str:
        # Some pg tools prefer 'postgresql://' over 'postgres://'
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

                # Plain SQL, no owner/privs for easy import
                import subprocess
                cmd = [
                    "pg_dump",
                    "--no-owner",
                    "--no-privileges",
                    "--format=plain",
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
                    data=proc.stdout,                      # bytes
                    file_name=f"database_backup_{ts}.sql",
                    mime="application/sql",
                    key="dl_pg_dump_sql",
                )
            except FileNotFoundError as e:
                st.error("`pg_dump` is not available in this environment.")
                st.info("Use the portable backup (right column) or add `pg_dump` to your image.")
            except Exception as e:
                st.error("Full SQL dump failed ❌")
                st.caption(str(e))

    with col_zip:
        if st.button("Download portable backup (schema+CSVs .zip)", key="export_portable_zip"):
            try:
                ts = datetime.now().strftime("%Y-%m-%d_%H%M")

                # 1) Prepare dataframes (same queries you already use)
                tables = {
                    "visits": query_df("""
                        SELECT
                            v.*,
                            c.account_name AS customer_name,
                            i.article_number,
                            i.description,
                            bl.name AS business_line,
                            bu.name AS business_unit,
                            o.name AS objective_name,
                            hv.patient_name,
                            hv.patient_phone,
                            hv.serial_no,
                            COALESCE((
                            SELECT COUNT(*)
                            FROM shelf_movement_lines l
                            JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                            WHERE h.visit_id = v.visit_id
                            ),0) AS shelf_lines_count,
                            COALESCE((
                            SELECT SUM(l.qty_checked)
                            FROM shelf_movement_lines l
                            JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                            WHERE h.visit_id = v.visit_id
                            ),0) AS shelf_total_qty
                        FROM visits v
                        JOIN customers c        ON v.customer_id = c.customer_id
                        LEFT JOIN items i        ON v.product_id = i.product_id
                        LEFT JOIN business_lines bl   ON bl.business_line_id = v.business_line_id
                        LEFT JOIN business_units bu   ON bu.business_unit_id = bl.business_unit_id
                        JOIN objectives o        ON v.objective_id = o.objective_id
                        LEFT JOIN home_visits hv ON hv.visit_id = v.visit_id
                        ORDER BY v.visit_id DESC
                    """),
                    "users": query_df("SELECT * FROM users ORDER BY user_id DESC"),
                    "customers": query_df("SELECT * FROM customers ORDER BY account_name"),
                    "target_audiences": query_df("SELECT * FROM target_audiences ORDER BY audience_id DESC"),
                    "business_units": query_df("SELECT * FROM business_units ORDER BY business_unit_id"),
                    "business_lines": query_df("""
                        SELECT bl.*, bu.name AS business_unit
                        FROM business_lines bl
                        JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
                        ORDER BY bu.name, bl.name
                    """),
                    "items": query_df("""
                        SELECT
                            i.product_id,
                            i.article_number,
                            i.description,
                            i.is_active,
                            bl.name AS business_line,
                            bu.name AS business_unit
                        FROM items i
                        JOIN business_lines bl ON bl.business_line_id = i.business_line_id
                        JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
                        ORDER BY COALESCE(i.article_number, i.product_id)
                    """),
                    "objectives": query_df("SELECT * FROM objectives ORDER BY objective_id"),
                    "home_visits": query_df("""
                        SELECT hv.*, v.submitted_at_local, u.name AS rep, c.account_name AS customer
                        FROM home_visits hv
                        JOIN visits v ON v.visit_id = hv.visit_id
                        JOIN users u  ON u.user_id  = v.user_id
                        JOIN customers c ON c.customer_id = v.customer_id
                        ORDER BY hv.home_visit_id DESC
                    """),
                    "shelf_movement_headers": query_df("SELECT * FROM shelf_movement_headers ORDER BY movement_id DESC"),
                    "shelf_movement_lines": query_df("SELECT * FROM shelf_movement_lines ORDER BY line_id DESC"),
                }

                # 2) Build ZIP in-memory: schema.sql (from live DB) + CSVs + README
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:

                    # 2a) Try to get **live schema** via pg_dump --schema-only
                    schema_bytes = b""
                    try:
                        db_url = _db_url()
                        if not db_url:
                            raise RuntimeError("DATABASE_URL / POSTGRES_URL not set.")

                        if not _pg_dump_available():
                            raise FileNotFoundError("pg_dump not found")

                        import subprocess
                        cmd_schema = [
                            "pg_dump",
                            "--no-owner",
                            "--no-privileges",
                            "--format=plain",
                            "--schema-only",
                            _normalize_pg_url(db_url),
                        ]
                        p = subprocess.run(cmd_schema, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
                        if p.returncode != 0 or not p.stdout:
                            err = p.stderr.decode("utf-8", errors="ignore")
                            raise RuntimeError(f"pg_dump --schema-only failed.\n{err.strip() or 'No error text.'}")
                        schema_bytes = p.stdout
                    except Exception as e:
                        # Fall back to placeholder schema + guidance
                        schema_bytes = (
                            b"-- schema.sql not auto-included.\n"
                            b"-- Reason: " + str(e).encode("utf-8", errors="ignore") + b"\n"
                            b"-- Tip: Run a full SQL dump from a machine with pg_dump installed, or\n"
                            b"--       enable pg_dump in your deployment image.\n"
                        )

                    zf.writestr("schema.sql", schema_bytes)

                    # 2b) Add CSVs
                    for name, df in tables.items():
                        csv_bytes = df.to_csv(index=False, na_rep="").encode("utf-8-sig")
                        zf.writestr(f"data/{name}_{ts}.csv", csv_bytes)

                    # 2c) README with restore steps
                    readme = f"""# Portable Backup

    This archive contains:
    - `schema.sql` — your **live** PostgreSQL schema (dumped via pg_dump when available).
    - `data/*.csv` — table data exports.

    ## Quick restore (psql)

    1) Create an empty database.
    2) Load the schema:

    psql "$DATABASE_URL" -f schema.sql

    3) Load CSVs (example):

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

    > If you get FK errors, import in dependency order (units → lines → items → customers → target_audiences → visits → home_visits → shelf_*).
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

