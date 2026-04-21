# app_pages/admin_change_requests.py
import pandas as pd
import streamlit as st
from sqlalchemy import text

from auth import resolve_session_user
from db import engine
from db_ops import query_df, exec_sql
from ui import section_header, status_badge, compare_row
from widgets import set_current_page

PAGE_NS = "admin_change_req"

ALLOWED_VISIT_FIELDS = {
    "visits.customer_id", "visits.audience_id", "visits.business_line_id",
    "visits.product_id", "visits.objective_id", "visits.notes",
    "visits.evaluation", "visits.project_id", "visits.other_customer_name",
    "visits.other_audience_title", "visits.other_audience_name",
    "visits.other_audience_department", "visits.other_audience_position",
    "visits.other_audience_phone", "visits.other_audience_email",
}


def _fmt_field(field: str) -> str:
    """Strip 'visits.' prefix for display."""
    return field.split(".", 1)[-1] if "." in field else field


def _apply_changes(request_id: int, visit_id: int, admin_uid: int):
    """
    Apply all request_change_details for request_id to the visits table.
    Returns (success: bool, error_msg: str | None).
    """
    detail_rows = query_df(
        "SELECT field, new_value FROM request_change_details WHERE request_id = :rid",
        {"rid": request_id},
    )

    if detail_rows.empty:
        return False, "No change details found for this request."

    # Whitelist check before opening transaction
    # Fields are stored as "visits.columnname" — must match ALLOWED_VISIT_FIELDS exactly.
    for _, r in detail_rows.iterrows():
        if r["field"] not in ALLOWED_VISIT_FIELDS:
            return False, f"Field not in allowed list: {r['field']}"

    try:
        with engine.begin() as conn:
            for _, r in detail_rows.iterrows():
                col = r["field"].split(".", 1)[-1]
                conn.execute(
                    text(f"UPDATE visits SET {col} = :val WHERE visit_id = :vid"),
                    {"val": r["new_value"], "vid": visit_id},
                )
            conn.execute(
                text(
                    """
                    UPDATE request_changes
                    SET status = 'APPROVED', applied_at = NOW(), resolve_date = NOW(), changed_by = :admin_uid
                    WHERE request_id = :rid
                    """
                ),
                {"admin_uid": admin_uid, "rid": request_id},
            )
        return True, None
    except Exception as e:
        # Record error in a separate connection (main transaction rolled back)
        try:
            exec_sql(
                "UPDATE request_changes SET apply_error = :err WHERE request_id = :rid",
                {"err": str(e), "rid": request_id},
            )
        except Exception:
            pass
        return False, str(e)


def _load_pending() -> pd.DataFrame:
    return query_df(
        """
        SELECT
          rc.request_id,
          rc.visit_id,
          u.name            AS rep_name,
          rc.request_date,
          rc.request_note,
          COUNT(rcd.detail_id) AS fields_changed
        FROM request_changes rc
        JOIN users u ON u.user_id = rc.requested_by
        LEFT JOIN request_change_details rcd ON rcd.request_id = rc.request_id
        WHERE rc.status = 'IN_REVIEW'
        GROUP BY rc.request_id, rc.visit_id, u.name, rc.request_date, rc.request_note
        ORDER BY rc.request_date ASC
        """
    )


def _load_visit_context(visit_id: int) -> dict:
    df = query_df(
        """
        SELECT
          v.visit_id,
          c.account_name AS customer_name,
          v.submitted_at_local,
          u.name         AS rep_name,
          bl.name        AS business_line
        FROM visits v
        JOIN customers c    ON c.customer_id    = v.customer_id
        JOIN users u        ON u.user_id        = v.user_id
        LEFT JOIN business_lines bl ON bl.business_line_id = v.business_line_id
        WHERE v.visit_id = :vid
        """,
        {"vid": visit_id},
    )
    return df.iloc[0].to_dict() if not df.empty else {}


def _load_diff(request_id: int) -> pd.DataFrame:
    return query_df(
        "SELECT field, old_value, new_value FROM request_change_details WHERE request_id = :rid ORDER BY field",
        {"rid": request_id},
    )


def _render_diff_table(diff_df: pd.DataFrame):
    rows_html = "".join(
        compare_row(
            _fmt_field(str(r["field"])),
            str(r["old_value"] if pd.notna(r["old_value"]) else "—"),
            str(r["new_value"] if pd.notna(r["new_value"]) else "—"),
            changed=True,
        )
        for _, r in diff_df.iterrows()
    )
    st.markdown(
        f"""
        <table style="width:100%;border-collapse:collapse;border:1px solid #e4e8ec;
                      border-radius:10px;overflow:hidden;font-size:0.875rem;">
          <thead>
            <tr style="background:#f6f8fa;">
              <th style="padding:10px 12px;text-align:left;font-weight:600;color:#57606a;
                         border-bottom:1px solid #e4e8ec;width:30%;">Field</th>
              <th style="padding:10px 12px;text-align:left;font-weight:600;color:#57606a;
                         border-bottom:1px solid #e4e8ec;">Original</th>
              <th style="padding:10px 12px;text-align:left;font-weight:600;color:#57606a;
                         border-bottom:1px solid #e4e8ec;">Requested</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
        """,
        unsafe_allow_html=True,
    )


def page_admin_change_requests():
    set_current_page(PAGE_NS)

    u = st.session_state.get("user") or resolve_session_user()
    if not u:
        st.warning("Please sign in to continue.")
        return

    role = (u.get("role") or "").lower().strip()
    if role != "admin":
        st.warning("You do not have access to this page.")
        return

    uid_raw = u.get("user_id") or u.get("id")
    if not uid_raw:
        st.error("Session user ID could not be resolved.")
        return
    admin_uid = int(uid_raw)

    section_header("Review Change Requests", "Approve or reject visit change requests submitted by reps")

    tab_review, tab_history = st.tabs(["🔍 Review Pending", "📋 All Requests"])

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 1 — Review Pending
    # ──────────────────────────────────────────────────────────────────────────
    with tab_review:
        success_key = f"{PAGE_NS}_review_success"
        if st.session_state.get(success_key):
            st.success(st.session_state.pop(success_key))

        pending_df = _load_pending()
        count = len(pending_df)

        if count == 0:
            st.info("No pending change requests.")
        else:
            st.markdown(
                status_badge(f"{count} request{'s' if count != 1 else ''} pending review", "warning"),
                unsafe_allow_html=True,
            )
            st.markdown("")

            def _label(row) -> str:
                date_str = pd.to_datetime(row["request_date"]).strftime("%b %d") if pd.notna(row["request_date"]) else "?"
                n = int(row["fields_changed"])
                return f"Request #{int(row['request_id'])} — Visit #{int(row['visit_id'])} — {row['rep_name']} — {date_str} ({n} field{'s' if n != 1 else ''})"

            options = {_label(row): row for _, row in pending_df.iterrows()}
            chosen_label = st.selectbox(
                "Select a request to review:",
                list(options.keys()),
                key=f"{PAGE_NS}_sel",
            )
            sel = options[chosen_label]
            request_id = int(sel["request_id"])
            visit_id   = int(sel["visit_id"])

            ctx = _load_visit_context(visit_id)
            if ctx:
                date_str = pd.to_datetime(ctx.get("submitted_at_local")).strftime("%d/%m/%Y") if pd.notna(ctx.get("submitted_at_local")) else "—"
                st.info(
                    f"**Visit #{visit_id}**  \n"
                    f"Customer: {ctx.get('customer_name', '—')}  \n"
                    f"Rep: {ctx.get('rep_name', '—')}  \n"
                    f"Date: {date_str}  \n"
                    f"Business Line: {ctx.get('business_line', '—')}"
                )

            if pd.notna(sel.get("request_note")) and sel.get("request_note"):
                st.info(f"**Rep note:** \"{sel['request_note']}\"")

            diff_df = _load_diff(request_id)
            if not diff_df.empty:
                _render_diff_table(diff_df)
            else:
                st.warning("No field details found for this request.")

            st.markdown("---")
            col_approve, col_reject = st.columns(2)

            with col_approve:
                st.markdown("**Approve**")
                if st.button("✅ Approve & Apply Changes", type="primary", key=f"{PAGE_NS}_approve_{request_id}"):
                    ok, err = _apply_changes(request_id, visit_id, admin_uid)
                    if ok:
                        st.session_state[success_key] = f"Request #{request_id} approved — changes applied to Visit #{visit_id}."
                        st.rerun()
                    else:
                        st.error(f"Apply failed: {err}")

            with col_reject:
                st.markdown("**Reject**")
                reject_note = st.text_area(
                    "Rejection reason (required)",
                    key=f"{PAGE_NS}_reject_note_{request_id}",
                    placeholder="Explain why the request is rejected.",
                )
                if st.button("❌ Reject Request", type="secondary", key=f"{PAGE_NS}_reject_{request_id}"):
                    if not reject_note or not reject_note.strip():
                        st.error("Rejection reason is required.")
                    else:
                        exec_sql(
                            """
                            UPDATE request_changes
                            SET status = 'REJECTED',
                                reject_note = :note,
                                resolve_date = NOW(),
                                changed_by = :admin_uid
                            WHERE request_id = :rid
                            """,
                            {"note": reject_note.strip(), "admin_uid": admin_uid, "rid": request_id},
                        )
                        st.session_state[success_key] = f"Request #{request_id} rejected."
                        st.rerun()

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 2 — All Requests (History)
    # ──────────────────────────────────────────────────────────────────────────
    with tab_history:
        _render_history_tab()


def _render_history_tab():
    all_df = query_df(
        """
        SELECT
          rc.request_id,
          rc.visit_id,
          rep.name            AS rep_name,
          rc.request_date,
          rc.status,
          COUNT(rcd.detail_id) AS fields_changed,
          resolver.name       AS resolved_by,
          rc.resolve_date,
          rc.applied_at,
          rc.apply_error,
          rc.request_note,
          rc.reject_note
        FROM request_changes rc
        JOIN users rep ON rep.user_id = rc.requested_by
        LEFT JOIN users resolver ON resolver.user_id = rc.changed_by
        LEFT JOIN request_change_details rcd ON rcd.request_id = rc.request_id
        GROUP BY rc.request_id, rc.visit_id, rep.name, rc.request_date, rc.status,
                 rc.applied_at, rc.apply_error, rc.request_note, rc.reject_note,
                 rc.resolve_date, resolver.name
        ORDER BY rc.request_date DESC
        """
    )

    if all_df.empty:
        st.info("No change requests found.")
        return

    status_opts = ["All"] + sorted(all_df["status"].unique().tolist())
    status_filter = st.selectbox("Filter by status:", status_opts, key=f"{PAGE_NS}_hist_filter")
    if status_filter != "All":
        all_df = all_df[all_df["status"] == status_filter]

    if all_df.empty:
        st.info(f"No requests with status: {status_filter}")
        return

    display_df = all_df[["request_id", "visit_id", "rep_name", "request_date",
                          "fields_changed", "status", "resolved_by", "resolve_date"]].copy()
    display_df["request_date"] = pd.to_datetime(display_df["request_date"], errors="coerce").dt.strftime("%d/%m/%Y %H:%M")
    display_df["resolve_date"] = pd.to_datetime(display_df["resolve_date"], errors="coerce").dt.strftime("%d/%m/%Y %H:%M")
    display_df = display_df.rename(columns={
        "request_id": "Req #", "visit_id": "Visit #", "rep_name": "Rep",
        "request_date": "Submitted", "fields_changed": "# Fields",
        "status": "Status", "resolved_by": "Resolved By", "resolve_date": "Resolved Date",
    })
    st.dataframe(display_df, hide_index=True, use_container_width=True)

    req_options = {
        f"Request #{int(r['request_id'])} — Visit #{int(r['visit_id'])} — {r['rep_name']} ({r['status']})": r
        for _, r in all_df.iterrows()
    }
    chosen = st.selectbox("Select a request to view details:", list(req_options.keys()), key=f"{PAGE_NS}_hist_sel")
    sel = req_options[chosen]
    request_id = int(sel["request_id"])
    visit_id = int(sel["visit_id"])

    ctx = _load_visit_context(visit_id)
    if ctx:
        date_str = pd.to_datetime(ctx.get("submitted_at_local")).strftime("%d/%m/%Y") if pd.notna(ctx.get("submitted_at_local")) else "—"
        st.info(
            f"**Visit #{visit_id}**  \n"
            f"Customer: {ctx.get('customer_name', '—')}  \n"
            f"Rep: {ctx.get('rep_name', '—')}  \n"
            f"Date: {date_str}  \n"
            f"Business Line: {ctx.get('business_line', '—')}"
        )

    if pd.notna(sel.get("request_note")) and sel.get("request_note"):
        st.markdown(f"**Rep note:** \"{sel['request_note']}\"")

    diff_df = _load_diff(request_id)
    if not diff_df.empty:
        _render_diff_table(diff_df)

    status_val = str(sel["status"])
    if status_val == "REJECTED" and pd.notna(sel.get("reject_note")) and sel.get("reject_note"):
        st.error(f"**Rejection reason:** {sel['reject_note']}")

    if status_val == "APPROVED":
        applied_str = pd.to_datetime(sel.get("applied_at")).strftime("%d/%m/%Y %H:%M") if pd.notna(sel.get("applied_at")) else "—"
        st.success(f"Approved and applied at {applied_str} by {sel.get('resolved_by') or '—'}")

    if pd.notna(sel.get("apply_error")) and sel.get("apply_error"):
        st.warning(f"Apply error (request remains IN_REVIEW): {sel['apply_error']}")

    req_date_str = pd.to_datetime(sel["request_date"]).strftime("%d/%m/%Y %H:%M") if pd.notna(sel["request_date"]) else "?"
    resolve_str = pd.to_datetime(sel["resolve_date"]).strftime("%d/%m/%Y %H:%M") if pd.notna(sel.get("resolve_date")) else "Pending"
    st.caption(f"Timeline: Requested {req_date_str} → Resolved {resolve_str} ({status_val})")
