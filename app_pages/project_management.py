# pages/project_management.py
import json
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import streamlit as st
from dateutil import tz
from sqlalchemy import text

from auth import resolve_session_user
from config import TIMEZONE
from db import engine
from db_ops import query_df, exec_sql
from utils import _utcnow_iso, _utcnow
from widgets import set_current_page
from ui import section_header


# ---------------- Timezone: Riyadh local time ----------------
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Asia/Riyadh")
except Exception:
    LOCAL_TZ = None  # fallback if zoneinfo not available

def local_now():
    """
    Return a naive datetime representing Riyadh local time (Asia/Riyadh).
    This is what will be saved into the DB for created_at/updated_at/changed_at.
    """
    if LOCAL_TZ is not None:
        return datetime.now(LOCAL_TZ).replace(tzinfo=None)
    return datetime.now()  # fallback, still local server time


# ---------------- Constants + page namespace ----------------
PROJECT_STATUSES = [
    "Not Started",
    "Open",
    "Postponed",
    "Completed",
    "Cancelled",
]

PAGE_NS = "project_mgmt"
st.session_state.setdefault(f"{PAGE_NS}_nonce", 0)

def k(name: str) -> str:
    """Key helper with nonce so fields reset when nonce increments."""
    return f"{PAGE_NS}/{name}_{st.session_state[f'{PAGE_NS}_nonce']}"


# ---------------- DB helpers ----------------
def _fetch_projects_for_management(manager_user: dict) -> pd.DataFrame:
    """
    Projects visible to the current manager/admin.
    Admin: all projects
    Manager: only projects where they are assigned_by_id.
    """
    role = (manager_user.get("role") or "").lower().strip()
    params = {}
    where = "1=1"

    if role == "manager":
        where += " AND p.assigned_by_id = :uid"
        params["uid"] = int(manager_user.get("user_id") or manager_user.get("id"))

    sql = f"""
        WITH visit_counts AS (
            SELECT project_id, COUNT(*)::int AS total_visits
            FROM visits
            WHERE project_id IS NOT NULL
            GROUP BY project_id
        )
        SELECT
            p.project_id,
            p.name,
            (p.project_id::text || '. ' || p.name) AS adj_name,
            p.description,
            p.status,
            p.planned_start_date,
            p.planned_end_date,
            p.actual_end_date,
            u_to.name AS rep_name,
            c.account_name AS customer_name,
            bl.name AS business_line_name,
            po.name AS objective_name,
            COALESCE(vc.total_visits, 0) AS total_visits
        FROM projects p
        JOIN users u_to        ON u_to.user_id = p.assigned_to_id
        JOIN customers c       ON c.customer_id = p.customer_id
        JOIN business_lines bl ON bl.business_line_id = p.business_line_id
        JOIN project_objectives po ON po.project_objective_id = p.project_objective_id
        LEFT JOIN visit_counts vc  ON vc.project_id = p.project_id
        WHERE {where}
        ORDER BY p.project_id DESC
    """
    return query_df(sql, params)


def _fetch_project_row(project_id: int):
    df = query_df(
        """
        SELECT
            p.*,
            u.name AS assigned_by_name
        FROM projects p
        LEFT JOIN users u ON u.user_id = p.assigned_by_id
        WHERE p.project_id = :pid
        """,
        {"pid": project_id},
    )
    if df.empty:
        return None
    return df.iloc[0].to_dict()

def _fetch_project_history(project_id: int) -> pd.DataFrame:
    """
    Minimal change history for a project (most recent first).
    """
    sql = """
        SELECT
            e.event_id,
            e.changed_at,
            u.name AS changed_by_name,
            e.note,
            string_agg(
                d.field_name || ': ' ||
                COALESCE(d.old_value, 'NULL') || ' → ' ||
                COALESCE(d.new_value, 'NULL'),
                E'\n'
                ORDER BY d.detail_id
            ) AS changes_summary
        FROM project_change_events e
        JOIN users u ON u.user_id = e.changed_by_id
        LEFT JOIN project_change_details d ON d.event_id = e.event_id
        WHERE e.project_id = :pid
        GROUP BY e.event_id, e.changed_at, u.name, e.note
        ORDER BY e.changed_at DESC
        LIMIT 50
    """
    return query_df(sql, {"pid": project_id})


def _update_project_with_history(
    project_id: int,
    new_values: dict,
    changed_by_id: int,
    change_note: str,
):
    """
    Compare existing row with new_values for:
      - name
      - description
      - planned_start_date
      - planned_end_date
      - status
      - actual_end_date

    If anything changed:
      1. Insert into project_change_events (with local Riyadh time)
      2. Insert per-field rows into project_change_details
      3. Update projects (with updated_at in local Riyadh time)
    """
    cur = _fetch_project_row(project_id)
    if not cur:
        raise ValueError("Project not found")

    fields_to_track = [
        "name",
        "description",
        "planned_start_date",
        "planned_end_date",
        "status",
        "actual_end_date",
    ]

    def _norm(v):
        if v is None:
            return None
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return str(v)

    changes = []
    for fld in fields_to_track:
        old_val = cur.get(fld)
        new_val = new_values.get(fld)
        old_val_str = _norm(old_val)
        new_val_str = _norm(new_val)

        if old_val_str != new_val_str:
            changes.append(
                {
                    "field_name": fld,
                    "old_value": old_val_str,
                    "new_value": new_val_str,
                }
            )

    if not changes:
        # Nothing changed
        return

    if not change_note or not change_note.strip():
        raise ValueError("Change note is required when modifying a project.")

    now_local = local_now()

    with engine.begin() as conn:
        # 1) Insert event in local (Riyadh) time
        res = conn.execute(
            text(
                """
                INSERT INTO project_change_events (
                    project_id,
                    changed_by_id,
                    changed_at,
                    note
                )
                VALUES (:pid, :uid, :ts, :note)
                RETURNING event_id
                """
            ),
            {
                "pid": project_id,
                "uid": changed_by_id,
                "ts": now_local,
                "note": change_note.strip(),
            },
        )
        event_id = res.scalar_one()

        # 2) Insert details
        for ch in changes:
            conn.execute(
                text(
                    """
                    INSERT INTO project_change_details (
                        event_id,
                        field_name,
                        old_value,
                        new_value
                    )
                    VALUES (:eid, :field_name, :old_value, :new_value)
                    """
                ),
                {
                    "eid": event_id,
                    "field_name": ch["field_name"],
                    "old_value": ch["old_value"],
                    "new_value": ch["new_value"],
                },
            )

        # 3) Update project row (with updated_at in local Riyadh time)
        conn.execute(
            text(
                """
                UPDATE projects
                SET
                    name               = :name,
                    description        = :description,
                    planned_start_date = :planned_start_date,
                    planned_end_date   = :planned_end_date,
                    actual_end_date    = :actual_end_date,
                    status             = :status,
                    updated_at         = :updated_at
                WHERE project_id = :pid
                """
            ),
            {
                "pid": project_id,
                "name": new_values["name"],
                "description": new_values.get("description"),
                "planned_start_date": new_values["planned_start_date"],
                "planned_end_date": new_values["planned_end_date"],
                "actual_end_date": new_values.get("actual_end_date"),
                "status": new_values["status"],
                "updated_at": now_local,
            },
        )


# ---------------- Main Page: Project Management (Panel Style) ----------------
def page_project_management():
    import pandas as pd
    from sqlalchemy import text

    section_header("Project Management", "Track and update project status and history")
    set_current_page("project_management")

    # --- Auth & role check ---
    u = st.session_state.get("user") or resolve_session_user()
    if not u:
        st.warning("Please sign in to continue.")
        st.stop()

    role = (u.get("role") or "").lower().strip()
    if role not in ("manager", "admin"):
        st.warning("You do not have access to this page.")
        st.stop()

    manager_id = int(u.get("user_id") or u.get("id"))

    # --- Defensive fallbacks ---
    display_name = u.get("name") or u.get("email") or f"User #{u.get('user_id', '?')}"
    display_region = u.get("region") or "—"
    display_role = u.get("role") or "—"

    # -- Formatter --
    def fmt_date(val):
        if val is None or val == "" or pd.isna(val):
            return "—"
        try:
            return pd.to_datetime(val).strftime("%d/%m/%Y")
        except:
            return str(val)

    # --- Display info ---
    st.caption(
        f"Logged in as **{display_name}** · Region: **{display_region}** · Role: **{display_role}**"
    )

    # --- Load projects for this manager/admin ---
    df = _fetch_projects_for_management(u)
    if df.empty:
        st.info("No projects found.")
        return

    # ===================== Panel 1 — Select Project =====================
    st.markdown("### 1️⃣ Select Project")

    with st.expander("Filter projects", expanded=False):
        col_f1, col_f2, col_f3 = st.columns(3)

        with col_f1:
            status_filter = st.multiselect(
                "Status", PROJECT_STATUSES, default=[], key="pm_status_filter"
            )

        with col_f2:
            rep_filter = st.multiselect(
                "Assigned To",
                sorted(df["rep_name"].dropna().unique().tolist()),
                default=[],
                key="pm_rep_filter",
            )

        with col_f3:
            search_text = st.text_input(
                "Search",
                "",
                key="pm_search_text",
                placeholder="Project or customer name…",
            )

        fdf = df.copy()
        if status_filter:
            fdf = fdf[fdf["status"].isin(status_filter)]
        if rep_filter:
            fdf = fdf[fdf["rep_name"].isin(rep_filter)]
        if search_text.strip():
            s = search_text.lower().strip()
            fdf = fdf[
                fdf["name"].str.lower().str.contains(s)
                | fdf["customer_name"].str.lower().str.contains(s)
            ]

    if fdf.empty:
        st.info("No projects match your filters.")
        return

    proj_labels = []
    id_list = []
    for r in fdf.itertuples(index=False):
        lbl = f"{r.adj_name} — {r.customer_name}"
        proj_labels.append(lbl)
        id_list.append(int(r.project_id))

    options = [""] + proj_labels
    label_to_id = dict(zip(proj_labels, id_list))

    sel_label = st.selectbox(
        "Project", options, index=0, key=k("project_sel")
    )

    if not sel_label:
        st.info("Select a project above to continue.")
        return

    selected_pid = label_to_id.get(sel_label)
    if not selected_pid:
        st.error("Could not resolve selected project.")
        return

    cur = _fetch_project_row(selected_pid)
    if not cur:
        st.error("Project not found.")
        return

    row_match = fdf.loc[fdf["project_id"] == selected_pid]
    row = row_match.iloc[0] if not row_match.empty else None

    # ===================== Panel 2 — Project Summary =====================
    st.markdown("---")
    st.markdown("### 2️⃣ Project Summary")

    ps = fmt_date(cur.get("planned_start_date"))
    pe = fmt_date(cur.get("planned_end_date"))
    ae = fmt_date(cur.get("actual_end_date"))

    c1, c2 = st.columns(2)

    with c1:
        st.markdown(
            f"""
            **Project Name:** {cur['name']}
            **Assigned By:** {cur.get('assigned_by_name') or '—'}
            **Assigned To:** {row.rep_name if row is not None else '—'}
            **Customer:** {row.customer_name if row is not None else '—'}
            **Business Line:** {row.business_line_name if row is not None else '—'}
            """
        )

    with c2:
        st.markdown(
            f"""
            **Status:** `{cur.get('status')}`
            **Planned:** {ps} → {pe}
            **Actual End:** {ae}
            **Total Visits:** {int(row.total_visits) if row is not None else 0}
            """
        )

    # View-only mode for completed/cancelled
    is_view_only = (cur.get("status") in ("Completed", "Cancelled")) and role != "admin"

    if is_view_only:
        st.markdown("---")
        st.info(f"This project is **{cur.get('status')}** and view-only.")
        return

    # ===================== Panel 3 — Edit Details & Status =====================
    st.markdown("---")
    st.markdown("### 3️⃣ Edit Details & Status")

    name_key = f"pm_name_{selected_pid}"
    desc_key = f"pm_desc_{selected_pid}"
    status_key = f"pm_status_{selected_pid}"
    aed_key = f"pm_aed_{selected_pid}"
    aed_dis_key = f"pm_aed_dis_{selected_pid}"
    note_key = f"pm_note_{selected_pid}"

    col1, col2 = st.columns(2)

    with col1:
        new_name = st.text_input(
            "Project Name *",
            value=cur["name"],
            key=name_key,
        )

        new_desc = st.text_area(
            "Description",
            value=cur.get("description") or "",
            key=desc_key,
        )

        # Planned dates removed from editing (shown only above)

    with col2:
        status_index = (
            PROJECT_STATUSES.index(cur.get("status"))
            if cur.get("status") in PROJECT_STATUSES
            else 0
        )

        new_status = st.selectbox(
            "Status *",
            PROJECT_STATUSES,
            index=status_index,
            key=status_key,
        )

        default_aed = (
            cur.get("actual_end_date")
            if cur.get("actual_end_date")
            else local_now().date()
        )

        if new_status == "Completed":
            new_aed = st.date_input(
                "Actual End Date *",
                value=default_aed,
                key=aed_key,
            )
        else:
            new_aed = None
            st.date_input(
                "Actual End Date",
                value=default_aed,
                key=aed_dis_key,
                disabled=True,
            )

    st.markdown("### 4️⃣ Change Note")
    change_note = st.text_area(
        "Change Note *",
        placeholder="Why are you changing this project?",
        key=note_key,
    )

    # ===================== Save =====================
    if st.button("💾 Save Changes", type="primary", key=f"pm_save_btn_{selected_pid}"):

        errs = []

        if not new_name.strip():
            errs.append("Please enter a **Project Name**.")

        if new_status == "Completed" and not new_aed:
            errs.append("Completed project requires an **Actual End Date**.")

        today = local_now().date()
        if new_status == "Completed" and new_aed and new_aed > today:
            errs.append("Actual End Date cannot be in the future.")

        if not change_note.strip():
            errs.append("Change Note is required.")

        if errs:
            for e in errs:
                st.error(e)
            return

        new_vals = {
            "name": new_name.strip(),
            "description": new_desc.strip() or None,
            "status": new_status,
            "actual_end_date": new_aed if new_status == "Completed" else None,
        }

        try:
            _update_project_with_history(selected_pid, new_vals, manager_id, change_note)
            st.success("Project updated successfully.")
            st.rerun()

        except Exception as e:
            st.error("Could not update project.")
            st.caption(str(e))

    # ===================== Panel 5 — History =====================
    st.markdown("---")
    with st.expander("📜 View Change History", expanded=False):
        hist_df = _fetch_project_history(selected_pid)
        if hist_df.empty:
            st.info("No history yet.")
        else:
            if "changed_at" in hist_df.columns:
                hist_df["changed_at"] = pd.to_datetime(
                    hist_df["changed_at"], errors="coerce"
                ).dt.strftime("%d/%m/%Y %H:%M")

            st.dataframe(
                hist_df.rename(
                    columns={
                        "changed_at": "When",
                        "changed_by_name": "By",
                        "note": "Note",
                        "changes_summary": "Changes",
                    }
                ),
                width='stretch',
                hide_index=True,
            )
