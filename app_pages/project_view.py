# pages/project_view.py
import json
import pandas as pd
import streamlit as st
from sqlalchemy import text

from auth import resolve_session_user
from config import TIMEZONE
from db_ops import query_df
from utils import _local_now, _utcnow
from widgets import set_current_page
from ui import section_header


def page_project_view():
    import pandas as pd
    from datetime import datetime

    PAGE_NS = "projects_basic"
    set_current_page(PAGE_NS)

    def k(name: str) -> str:
        return f"{PAGE_NS}/{name}"

    # --- Resolve logged-in user ---
    u = st.session_state.get("user") or resolve_session_user()
    if not u:
        st.warning("Please sign in to continue.")
        st.stop()

    uid  = int(u.get("user_id") or u.get("id"))
    role = (u.get("role") or "").lower().strip()
    display_name   = u.get("name") or u.get("email") or f"User #{uid}"
    display_role   = u.get("role") or "—"
    display_region = u.get("region") or "—"

    section_header("Projects", f"Logged in as {display_name} · {display_region} · {display_role}")

    # --- Simple view state: list or detail ---
    st.session_state.setdefault(k("mode"), "list")
    st.session_state.setdefault(k("selected_project_id"), None)

    mode = st.session_state[k("mode")]

    # =========================================
    # 1) Base projects query (role-scoped)
    # =========================================
    where_clauses = ["1=1"]
    params: dict[str, object] = {}

    if role == "rep":
        where_clauses.append("p.assigned_to_id = :uid")
        params["uid"] = uid
    elif role == "manager":
        where_clauses.append("p.assigned_by_id = :uid")
        params["uid"] = uid
    elif role == "admin":
        # see all projects
        pass
    else:
        # fallback: only projects where user is involved
        where_clauses.append("(p.assigned_to_id = :uid OR p.assigned_by_id = :uid)")
        params["uid"] = uid

    projects_df = query_df(
        f"""
        WITH visit_agg AS (
            SELECT
                project_id,
                COUNT(*)                AS total_visits,
                MAX(submitted_at_local) AS last_visit_at
            FROM visits
            WHERE project_id IS NOT NULL
            GROUP BY project_id
        )
        SELECT
            p.project_id,
            p.name                AS project_name,
            p.description,
            p.status,
            p.planned_start_date,
            p.planned_end_date,
            p.actual_end_date,
            p.assigned_to_id,
            p.assigned_by_id,
            c.customer_id,
            c.account_name        AS customer_name,
            c.region,
            c.city,
            c.sector,
            bl.business_line_id,
            bl.name               AS business_line,
            bl.category,
            bu.business_unit_id,
            bu.name               AS business_unit,
            i.product_id,
            i.article_number,
            i.description         AS product_description,
            COALESCE(va.total_visits, 0)     AS total_visits,
            va.last_visit_at,
            uto.name              AS assigned_to_name,
            uba.name              AS assigned_by_name
        FROM projects p
        JOIN customers          c  ON c.customer_id           = p.customer_id
        JOIN business_lines     bl ON bl.business_line_id     = p.business_line_id
        LEFT JOIN business_units bu ON bu.business_unit_id    = bl.business_unit_id
        LEFT JOIN items          i  ON i.product_id           = p.product_id
        LEFT JOIN visit_agg      va ON va.project_id          = p.project_id
        LEFT JOIN users          uto ON uto.user_id           = p.assigned_to_id
        LEFT JOIN users          uba ON uba.user_id           = p.assigned_by_id
        WHERE {" AND ".join(where_clauses)}
        ORDER BY p.planned_end_date NULLS LAST, p.project_id DESC
        """,
        params,
    )

    if projects_df.empty:
        st.info("No projects assigned.")
        return

    # Normalize datetime/date columns for display
    for col in ["planned_start_date", "planned_end_date", "actual_end_date", "last_visit_at"]:
        if col in projects_df.columns:
            projects_df[col] = pd.to_datetime(projects_df[col], errors="coerce")

    # Small helper for date formatting (DD/MM/YYYY)
    def _fmt_date(val):
        if pd.isna(val):
            return "—"
        try:
            return pd.to_datetime(val).strftime("%d/%m/%Y")
        except Exception:
            return str(val)

    # =====================================================
    # MODE: LIST — show simple table + choose project
    # =====================================================
    if mode == "list":
        st.subheader("Projects List")

        # Very light search (optional)
        search_text = st.text_input(
            "Search (optional)",
            placeholder="Search by project or customer name...",
            key=k("search"),
        ).strip()

        df_list = projects_df.copy()

        if search_text:
            mask = (
                df_list["project_name"].str.contains(search_text, case=False, na=False)
                | df_list["customer_name"].str.contains(search_text, case=False, na=False)
            )
            df_list = df_list[mask]

        if df_list.empty:
            st.warning("No projects match your search.")
            return

        # Build a clean table for display
        table = df_list[[
            "project_id",
            "project_name",
            "customer_name",
            "status",
            "planned_end_date",
            "total_visits",
            "last_visit_at",
        ]].copy()

        table.rename(
            columns={
                "project_id": "ID",
                "project_name": "Project",
                "customer_name": "Customer",
                "status": "Status",
                "planned_end_date": "Planned End",
                "total_visits": "Visits",
                "last_visit_at": "Last Visit",
            },
            inplace=True,
        )

        table["Planned End"] = table["Planned End"].apply(_fmt_date)
        table["Last Visit"]  = table["Last Visit"].apply(_fmt_date)

        st.dataframe(
            table,
            width='stretch',
            hide_index=True,
        )

        # --- Simple select + button to view visits ---
        st.markdown("### View Project Visits")

        proj_options = [
            f"{int(row.project_id)} — {row.project_name} ({row.customer_name})"
            for row in df_list.itertuples(index=False)
        ]
        proj_id_map = {
            label: int(row.project_id)
            for label, row in zip(proj_options, df_list.itertuples(index=False))
        }

        default_idx = 0
        current_selected = st.session_state.get(k("selected_project_id"))
        if current_selected is not None:
            for i, row in enumerate(df_list.itertuples(index=False)):
                if int(row.project_id) == int(current_selected):
                    default_idx = i
                    break

        selected_label = st.selectbox(
            "Select a project:",
            options=proj_options,
            index=default_idx,
            key=k("project_select"),
        )
        selected_project_id = proj_id_map[selected_label]

        if st.button("🔍 View Visits for Selected Project", key=k("view_btn")):
            st.session_state[k("selected_project_id")] = int(selected_project_id)
            st.session_state[k("mode")] = "detail"
            st.rerun()

        return  # end of list mode

    # =====================================================
    # MODE: DETAIL — show single project + visits
    # =====================================================
    st.subheader("Project Details & Visits")

    pid = st.session_state.get(k("selected_project_id"))
    if pid is None:
        st.info("No project selected. Go back to the projects list.")
        if st.button("⬅ Back to Projects List", key=k("back_no_pid")):
            st.session_state[k("mode")] = "list"
            st.rerun()
        return

    # Back button
    if st.button("⬅ Back to Projects List", key=k("back_btn")):
        st.session_state[k("mode")] = "list"
        st.rerun()

    # Find the project row
    proj_rows = projects_df[projects_df["project_id"] == pid]
    if proj_rows.empty:
        st.error("Selected project not found.")
        return

    proj = proj_rows.iloc[0]

    # ---- Project summary ----
    st.markdown(f"### {proj['project_name']}")
    if isinstance(proj.get("description"), str) and proj["description"].strip():
        st.write(proj["description"])

    st.caption(
        f"Customer: **{proj['customer_name']}** · "
        f"Region: {proj.get('region', '—')} · "
        f"City: {proj.get('city', '—')} · "
        f"Sector: {proj.get('sector', '—')}"
    )
    st.caption(
        f"BU: {proj.get('business_unit', '—')} · "
        f"Business Line: {proj.get('business_line', '—')} · "
        f"Category: {proj.get('category', '—')}"
    )
    if isinstance(proj.get("article_number"), str) and proj["article_number"].strip():
        st.caption(
            f"Product: {proj['article_number']} — "
            f"{proj.get('product_description', '')}"
        )

    col1, col2, col3 = st.columns(3)
    col1.metric("Status", proj["status"] or "—")
    col2.metric("Planned End", _fmt_date(proj["planned_end_date"]))
    col3.metric("Total Visits", int(proj.get("total_visits", 0) or 0))

    # ---- Visit history ----
    st.markdown("---")
    st.markdown("#### Visit History")

    visits_df = query_df(
        """
        SELECT
            v.visit_id,
            v.submitted_at_local,
            u.name      AS rep_name,
            c.account_name AS customer_name,
            o.name      AS objective,
            v.evaluation,
            v.notes,
            v.latitude,
            v.longitude,
            v.accuracy_m
        FROM visits v
        LEFT JOIN users      u ON u.user_id      = v.user_id
        LEFT JOIN customers  c ON c.customer_id  = v.customer_id
        LEFT JOIN objectives o ON o.objective_id = v.objective_id
        WHERE v.project_id = :pid
        ORDER BY v.submitted_at_local DESC
        """,
        {"pid": int(pid)},
    )

    if visits_df.empty:
        st.info("No visits linked to this project yet.")
        return

    # Submitted date/time: DD/MM/YYYY HH:MM
    visits_df["submitted_at_local"] = pd.to_datetime(
        visits_df["submitted_at_local"], errors="coerce"
    ).dt.strftime("%d/%m/%Y %H:%M")

    # Create Google Maps URL column (like My Submissions)
    visits_df["location_url"] = visits_df.apply(
        lambda r: (
            f"https://www.google.com/maps/search/{r['latitude']},{r['longitude']}?sa=X&ved=1t:242&ictx=111"
            if pd.notna(r["latitude"]) and pd.notna(r["longitude"])
            else ""
        ),
        axis=1,
    )

    # Remove raw lat/long/accuracy from the display
    cols_to_remove = ["latitude", "longitude", "accuracy_m"]
    visits_clean = visits_df.drop(
        columns=[c for c in cols_to_remove if c in visits_df.columns]
    )

    # Prepare display table with renamed columns
    visits_display = visits_clean.rename(
        columns={
            "visit_id": "Visit ID",
            "submitted_at_local": "Date/Time",
            "rep_name": "Rep",
            "customer_name": "Customer",
            "objective": "Objective",
            "evaluation": "Evaluation",
            "notes": "Notes",
            "location_url": "Location",
        }
    )

    visits_display = visits_display[
        ["Visit ID", "Date/Time", "Rep", "Customer", "Objective", "Evaluation", "Notes", "Location"]
    ]

    st.dataframe(
        visits_display,
        width='stretch',
        hide_index=True,
        column_config={
            "Location": st.column_config.LinkColumn(
                "Location",
                help="Open location in Google Maps",
                display_text="Location",
            )
        },
    )
