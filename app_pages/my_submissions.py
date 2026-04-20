# pages/my_submissions.py
import pandas as pd
import streamlit as st
from sqlalchemy import text

from auth import resolve_session_user
from config import TIMEZONE
from db_ops import query_df
from utils import _local_now
from widgets import set_current_page
from ui import section_header


def page_my_submissions():
    section_header("My Submissions", "View your submitted visit records")
    set_current_page("my_submissions")
    u = st.session_state.user
    uid = int(u.get("user_id")) if u.get("user_id") is not None else int(u["id"])

    # --- Defensive fallbacks ---
    display_name = u.get("name") or u.get("email") or f"User #{u.get('user_id', '?')}"
    display_region = u.get("region") or "—"
    display_role = u.get("role") or "—"

    # --- Display info ---
    st.caption(f"Logged in as **{display_name}** · Region: **{display_region}** · Role: **{display_role}**")

    sql = """
        SELECT v.visit_id,
               v.submitted_at_local,
               to_char(v.submitted_at_local, 'Day') AS day_name,
               c.account_name AS customer,
               c.account_id AS account_id,
               ta.name AS audience,
               v.latitude, v.longitude, v.accuracy_m,
               i.article_number, i.description,
               bu.name AS business_unit,
               bl.name AS business_line,
               bl.category AS category,
               o.name AS objective,
               v.evaluation,
               v.notes,
               hv.patient_name, hv.patient_phone, hv.serial_no,
               -- Shelf movement aggregates
               COALESCE((
                 SELECT COUNT(*)
                 FROM shelf_movement_lines l
                 JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                 WHERE h.visit_id = v.visit_id
               ), 0) AS shelf_lines_count,
               COALESCE((
                 SELECT SUM(l.qty_checked)
                 FROM shelf_movement_lines l
                 JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                 WHERE h.visit_id = v.visit_id
               ), 0) AS shelf_total_qty
        FROM visits v
        JOIN customers c              ON v.customer_id = c.customer_id
        LEFT JOIN target_audiences ta ON v.audience_id = ta.audience_id
        LEFT JOIN items i             ON v.product_id = i.product_id
        LEFT JOIN business_lines bl        ON bl.business_line_id = v.business_line_id
        LEFT JOIN business_units bu        ON bu.business_unit_id = bl.business_unit_id
        JOIN objectives o             ON v.objective_id = o.objective_id
        LEFT JOIN home_visits hv      ON hv.visit_id = v.visit_id
        WHERE v.user_id = :uid
        ORDER BY v.visit_id DESC
    """

    df = query_df(sql, {"uid": uid})

    if df.empty:
        st.info("No submissions yet.")
    else:
        # ---- Format date ----
        if "submitted_at_local" in df.columns:
            df["submitted_at_local"] = (
                pd.to_datetime(df["submitted_at_local"], errors="coerce")
                .dt.strftime("%d/%m/%Y %H:%M")
            )

        # --- Create Google Maps URL column ---
        df["location_url"] = df.apply(
            lambda r: f"https://www.google.com/maps/search/{r['latitude']},{r['longitude']}?sa=X&ved=1t:242&ictx=111"
            if r["latitude"] and r["longitude"] else "",
            axis=1
        )

        # --- Reorder so Location is before latitude ---
        cols = df.columns.tolist()
        if "location_url" in cols and "latitude" in cols:
            cols.insert(cols.index("latitude"), cols.pop(cols.index("location_url")))
        df = df[cols]

        # Remove lat/long/accuracy from final output
        cols_to_remove = ["latitude", "longitude", "accuracy_m"]
        df_display = df.drop(columns=[c for c in cols_to_remove if c in df.columns])


        st.markdown(f"**Total: {len(df):,}**")
        st.dataframe(
            df_display,
            width='stretch',
            hide_index=True,
            column_config={
                "location_url": st.column_config.LinkColumn(
                    "Location",
                    help="Open location in Google Maps",
                    display_text="Location"
                )
            }
        )

        st.download_button(
            "Download CSV",
            df.to_csv(index=False).encode("utf-8-sig"),
            "my_submissions.csv",
            "text/csv"
        )
