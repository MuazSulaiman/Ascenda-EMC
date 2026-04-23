# pages/review_audiences.py
import json
import pandas as pd
import streamlit as st
from sqlalchemy import text

from auth import resolve_session_user
from config import TIMEZONE
from db_ops import query_df, exec_sql
from utils import _utcnow_iso
from widgets import set_current_page
from ui import section_header, status_badge

def page_review_target_audiences():
    """
    Admin / manager page to review visits that used 'Other' Target Audience.
    - Shows all visits where audience_id IS NULL but other_audience_* is filled.
    - Suggests closest matches from existing target_audiences using generic similarity.
    - Allows linking visit to an existing TA or creating a new TA from the 'Other' fields.
    """
    import pandas as pd
    import re
    from difflib import SequenceMatcher
    from datetime import datetime

    PAGE_NS = "review_ta"
    set_current_page(PAGE_NS)
    
    TITLE_OPTIONS = ["", "Dr.", "Mr.", "Ms.", "Mrs.", "Prof.", "Eng.", "Other"]

    success_key = f"{PAGE_NS}_last_success"
    if success_key not in st.session_state:
        st.session_state[success_key] = ""

    # ------------- Auth -------------
    u = st.session_state.get("user") or resolve_session_user()
    if not u:
        st.warning("Please sign in to continue.")
        st.stop()

    role = (u.get("role") or "").lower().strip()
    if role not in ("admin", "manager"):
        st.warning("You do not have access to this page.")
        st.stop()

    uid = int(u.get("user_id") or u.get("id"))
    display_name   = u.get("name") or u.get("email") or f"User #{uid}"
    display_region = u.get("region") or "—"
    display_role   = u.get("role") or "—"

    section_header("Review Target Audiences", "Manage visits that used an 'Other' target audience")

    # Show any success message from last rerun
    last_msg = st.session_state.get(success_key) or ""
    if last_msg:
        st.success(last_msg)
        st.session_state[success_key] = ""

    # ------------- Similarity helpers (generic) -------------
    SIM_THRESHOLD = 0.78  # left here in case you want to use it later

    def normalize_name(s: str) -> str:
        if not s:
            return ""
        s = str(s).strip().lower()

        titles = ["dr.", "dr", "mr.", "mr", "mrs.", "mrs", "ms.", "ms", "prof.", "prof"]
        for t in titles:
            if s.startswith(t + " "):
                s = s[len(t) + 1:]

        s = "".join(ch if (ch.isalpha() or ch.isspace()) else " " for ch in s)
        s = " ".join(s.split())
        return s

    def string_similarity(a: str, b: str) -> float:
        a_norm, b_norm = normalize_name(a), normalize_name(b)
        if not a_norm or not b_norm:
            return 0.0

        s1 = SequenceMatcher(None, a_norm, b_norm).ratio()

        def only_cons(s: str) -> str:
            return "".join(ch for ch in s if ch not in "aeiou ")

        cons_a = only_cons(a_norm)
        cons_b = only_cons(b_norm)
        s2 = SequenceMatcher(None, cons_a, cons_b).ratio() if (cons_a and cons_b) else 0.0

        return 0.6 * s1 + 0.4 * s2

    def audience_similarity(other_row: pd.Series, ta_row: pd.Series | dict) -> float:
        name_other = (other_row.get("other_audience_name") or "").strip()
        name_ta    = (ta_row.get("name") or "").strip()
        name_score = string_similarity(name_other, name_ta)

        dept_other = (other_row.get("other_audience_department") or "").strip().lower()
        dept_ta    = (ta_row.get("department") or "").strip().lower()
        pos_other  = (other_row.get("other_audience_position") or "").strip().lower()
        pos_ta     = (ta_row.get("position") or "").strip().lower()

        dept_score = 1.0 if dept_other and dept_other == dept_ta else 0.0
        pos_score  = 1.0 if pos_other and pos_other == pos_ta else 0.0

        return 0.7 * name_score + 0.15 * dept_score + 0.15 * pos_score

    def format_ta_label(row) -> str:
        if isinstance(row, pd.Series):
            #title = (row.get("title") or "").strip()
            name  = (row.get("name") or "").strip()
            dept  = (row.get("department") or "").strip()
            pos   = (row.get("position") or "").strip()
        else:
            #title = (getattr(row, "title", "") or "").strip()
            name  = (getattr(row, "name", "") or "").strip()
            dept  = (getattr(row, "department", "") or "").strip()
            pos   = (getattr(row, "position", "") or "").strip()

        parts = []
        if name:
            #full_name = f"{title} {name}".strip() if title else name
            full_name = f"{name}".strip()
            parts.append(full_name)
        if dept:
            parts.append(dept)
        if pos:
            parts.append(pos)
        return " || ".join(parts) if parts else "(unnamed)"

    # ------------- Load unresolved "Other" visits -------------
    unresolved_df = query_df(
        """
        SELECT
            v.visit_id,
            v.customer_id,
            c.account_name               AS customer_name,
            v.submitted_at_local,
            v.other_audience_title,
            v.other_audience_name,
            v.other_audience_department,
            v.other_audience_position,
            v.other_audience_phone,
            v.other_audience_email,
            v.notes                      AS visit_notes,
            v.user_id,
            u.name                       AS rep_name,
            u.email                      AS rep_email,
            bu.name                      AS business_unit_name
        FROM visits v
        JOIN customers c 
            ON c.customer_id = v.customer_id
        JOIN users u     
            ON u.user_id = v.user_id
        LEFT JOIN business_lines bl
            ON bl.business_line_id = v.business_line_id
        LEFT JOIN business_units bu
            ON bu.business_unit_id = bl.business_unit_id
        WHERE v.audience_id IS NULL
        AND v.customer_id <> 807               -- ✅ exclude "Other Customer" placeholder
        AND v.other_audience_name IS NOT NULL
        AND trim(v.other_audience_name) <> ''
        ORDER BY v.submitted_at_local DESC, v.visit_id DESC
        """
    )

    if unresolved_df.empty:
        st.success("✅ No visits pending review for 'Other' Target Audience.")
        return

    unresolved_df["submitted_at_local"] = pd.to_datetime(
        unresolved_df["submitted_at_local"], errors="coerce"
    ).dt.strftime("%d/%m/%Y %H:%M")

    st.markdown("### 1️⃣ Visits with 'Other' Target Audience")
    st.caption("These visits have no audience_id but contain Other Target Audience details.")

    unresolved_display = unresolved_df.rename(
        columns={
            "customer_name": "Customer",
            "submitted_at_local": "Visit Date/Time",
            "other_audience_title": "Other TA Title",
            "other_audience_name": "Other TA Name",
            "other_audience_department": "Other TA Dept",
            "other_audience_position": "Other TA Position",
            "other_audience_phone": "Other TA Phone",
            "other_audience_email": "Other TA Email",
            "visit_notes": "Notes",
            "rep_name": "Submitted By",
            "rep_email": "Email",
            "business_unit_name": "Business Unit",
        }
    )

    st.dataframe(
        unresolved_display[
            [
                "visit_id",
                "Customer",
                "Visit Date/Time",
                "Other TA Title",
                "Other TA Name",
                "Other TA Dept",
                "Other TA Position",
                "Other TA Phone",
                "Other TA Email",
                "Notes",
                "Submitted By",
                "Email",
                "Business Unit",
            ]
        ],
        width="stretch",
        hide_index=True,
    )

    # ------------- Pick a visit to review -------------
    st.markdown("---")
    st.markdown("### 2️⃣ Review & Resolve One Visit")

    visit_labels = []
    visit_id_map = {}
    for _, row in unresolved_df.iterrows():
        label = (
            f"{int(row['visit_id'])} — {row['customer_name']} — "
            f"{row['other_audience_name']} ({row['submitted_at_local']})"
        )
        visit_labels.append(label)
        visit_id_map[label] = int(row["visit_id"])

    visit_select_options = [""] + visit_labels
    preselect_id = st.session_state.pop("_admin_preselect_id", None)
    if preselect_id is not None:
        for lbl, vid in visit_id_map.items():
            if vid == preselect_id:
                st.session_state[f"{PAGE_NS}_visit_sel"] = lbl
                break

    selected_visit_label = st.selectbox(
        "Select a visit to review",
        options=visit_select_options,
        index=0,
        key=f"{PAGE_NS}_visit_sel",
    )

    if not selected_visit_label:
        st.info("Please select a visit from the list above to start reviewing.")
        return

    selected_visit_id = visit_id_map[selected_visit_label]

    visit_row = unresolved_df.loc[unresolved_df["visit_id"] == selected_visit_id].iloc[0]

    st.info(
        f"**Visit #{selected_visit_id}** — Customer: **{visit_row['customer_name']}**  · \n"
        f"Title: **{visit_row['other_audience_title'] or '—'}**  · "
        f"Name: **{visit_row['other_audience_name']}**  · "
        f"Dept: **{visit_row['other_audience_department'] or '—'}**  · "
        f"Position: **{visit_row['other_audience_position'] or '—'}**  \n"
        f"Phone: **{visit_row['other_audience_phone'] or '—'}**  · "
        f"Email: **{visit_row['other_audience_email'] or '—'}**  \n"
        f"Submitted by: **{visit_row['rep_name']}** ({visit_row['rep_email']})  · "
        f"Business Unit: **{visit_row.get('business_unit_name') or '-'}**"
    )

    # ------------- Load all existing TAs for that customer -------------
    ta_df = query_df(
        """
        WITH last_visits AS (
            SELECT DISTINCT ON (v.audience_id)
                v.audience_id,
                v.submitted_at_local AS last_visited_date,
                v.user_id            AS last_visited_user_id
            FROM visits v
            ORDER BY v.audience_id, v.submitted_at_local DESC, v.visit_id DESC
        )
        SELECT
            ta.audience_id,
            ta.title,
            ta.name,
            ta.department,
            ta.position,
            ta.mobile,
            ta.email,
            lv.last_visited_date,
            u.name AS last_visited_by
        FROM target_audiences ta
        LEFT JOIN last_visits lv
            ON lv.audience_id = ta.audience_id
        LEFT JOIN users u
            ON u.user_id = lv.last_visited_user_id
        WHERE ta.customer_id = :cid
        AND COALESCE(ta.is_active, TRUE) IS TRUE
        ORDER BY ta.name
        """,
        {"cid": int(visit_row["customer_id"])},
    )

    if ta_df.empty:
        st.warning("This customer has no existing Target Audiences. You can only create a new one.")
        existing_options = []
    else:
        ta_df["similarity"] = ta_df.apply(
            lambda r: audience_similarity(
                visit_row,
                {
                    "name": r["name"],
                    "department": r["department"],
                    "position": r["position"],
                },
            ),
            axis=1,
        )
        ta_df = ta_df.sort_values(by="similarity", ascending=False)

        st.markdown("#### Suggested Matches")
        st.caption("Sorted by similarity (generic name similarity + department + position).")

        ta_display = ta_df.copy()
        ta_display["Label"] = ta_display.apply(format_ta_label, axis=1)
        ta_display["Similarity"] = ta_display["similarity"].map(lambda x: f"{x:.2f}")

        st.dataframe(
            ta_display[
                ["audience_id", "Label", "Similarity", "department", "position", "mobile", "email", "last_visited_date", "last_visited_by"]
            ].rename(
                columns={
                    "audience_id": "ID",
                    "department": "Dept",
                    "position": "Position",
                    "mobile": "Mobile",
                    "email": "Email",
                    "last_visited_date": "Last Visited At",
                    "last_visited_by": "Visited By",
                }
            ),
            width='stretch',
            hide_index=True,
        )

        existing_options = [
            f"{int(r.audience_id)} — {format_ta_label(r)}"
            for r in ta_df.itertuples(index=False)
        ]

    # ------------- Global dept/position lists for dropdowns -------------
    dept_choices_base: list[str] = []
    pos_choices_base:  list[str] = []

    dept_df = query_df(
        """
        SELECT DISTINCT department
        FROM target_audiences
        WHERE department IS NOT NULL
          AND trim(department) <> ''
        ORDER BY department
        """
    )
    if not dept_df.empty:
        dept_choices_base = dept_df["department"].astype(str).str.strip().tolist()

    pos_df = query_df(
        """
        SELECT DISTINCT position
        FROM target_audiences
        WHERE position IS NOT NULL
          AND trim(position) <> ''
        ORDER BY position
        """
    )
    if not pos_df.empty:
        pos_choices_base = pos_df["position"].astype(str).str.strip().tolist()

    # ------------- Actions: Link existing / Create new -------------
    col_link, col_new = st.columns(2)

    # --- Link to existing TA ---
    with col_link:
        st.markdown("#### 🔗 Link to Existing Target Audience")

        existing_sel = st.selectbox(
            "Existing Target Audience",
            options=[""] + existing_options,
            index=0,
            key=f"{PAGE_NS}_existing_ta_sel",
            help="Pick an existing TA for this customer, then click 'Link to Selected'.",
        )

        if st.button("✅ Link to Selected", key=f"{PAGE_NS}_link_btn"):
            if not existing_sel:
                st.error("Please select an existing Target Audience first.")
            else:
                sel_id_str = existing_sel.split("—", 1)[0].strip()
                try:
                    audience_id = int(sel_id_str)
                except ValueError:
                    st.error("Could not parse selected Target Audience ID.")
                    st.stop()

                try:
                    with engine.begin() as conn:
                        conn.execute(
                            text(
                                """
                                UPDATE visits
                                SET audience_id = :aid
                                WHERE visit_id = :vid
                                """
                            ),
                            {"aid": audience_id, "vid": selected_visit_id},
                        )
                    st.session_state[success_key] = (
                        f"Linked visit #{selected_visit_id} to existing Target Audience ID {audience_id} ✅"
                    )
                    st.rerun()
                except Exception as e:
                    st.error("Failed to link visit to existing Target Audience.")
                    st.caption(str(e))

    # --- Create new TA ---
    with col_new:
        st.markdown("#### 🆕 Create New Target Audience")

        st.caption(
            "This will create a new Target Audience using the details below "
            "(you can adjust them first) and link this visit to it. "
            "The original 'Other' fields in the visit will remain stored."
        )

        name_key        = f"{PAGE_NS}_new_ta_name_{selected_visit_id}"
        title_sel_key   = f"{PAGE_NS}_new_ta_title_sel_{selected_visit_id}"
        mobile_key      = f"{PAGE_NS}_new_ta_mobile_{selected_visit_id}"
        email_key       = f"{PAGE_NS}_new_ta_email_{selected_visit_id}"
        dept_sel_key    = f"{PAGE_NS}_new_ta_dept_sel_{selected_visit_id}"
        dept_custom_key = f"{PAGE_NS}_new_ta_dept_custom_{selected_visit_id}"
        pos_sel_key     = f"{PAGE_NS}_new_ta_pos_sel_{selected_visit_id}"
        pos_custom_key  = f"{PAGE_NS}_new_ta_pos_custom_{selected_visit_id}"
        confirm_key     = f"{PAGE_NS}_confirm_new_{selected_visit_id}"

        # Prefill from visit (title/name/dept/pos/phone/email)
        raw_title  = (visit_row.get("other_audience_title") or "").strip()
        raw_name   = (visit_row.get("other_audience_name") or "")
        raw_dept   = (visit_row.get("other_audience_department") or "").strip()
        raw_pos    = (visit_row.get("other_audience_position") or "").strip()
        raw_mobile = (visit_row.get("other_audience_phone") or "").strip()
        raw_email  = (visit_row.get("other_audience_email") or "").strip()

        # Title (optional) — TITLE_OPTIONS defined globally
        if raw_title and raw_title in TITLE_OPTIONS:
            title_index = TITLE_OPTIONS.index(raw_title)
        else:
            title_index = 0

        selected_title = st.selectbox(
            "Title (optional)",
            TITLE_OPTIONS,
            index=title_index,
            key=title_sel_key,
        )

        # Name (required)
        new_name = st.text_input(
            "Target Audience Name *",
            value=raw_name.upper(),  # show as ALL CAPS
            key=name_key,
        )

        # Department
        dept_opts = [""] + dept_choices_base + ["Other"]
        if raw_dept and raw_dept in dept_choices_base:
            dept_index = 1 + dept_choices_base.index(raw_dept)
        elif raw_dept:
            dept_index = len(dept_opts) - 1
        else:
            dept_index = 0

        selected_dept = st.selectbox(
            "Department *",
            dept_opts,
            index=dept_index,
            key=dept_sel_key,
        )

        dept_custom = None
        if selected_dept == "Other":
            dept_custom = st.text_input(
                "Custom Department *",
                value=raw_dept,
                key=dept_custom_key,
            )

        # Position
        pos_opts = [""] + pos_choices_base + ["Other"]
        if raw_pos and raw_pos in pos_choices_base:
            pos_index = 1 + pos_choices_base.index(raw_pos)
        elif raw_pos:
            pos_index = len(pos_opts) - 1
        else:
            pos_index = 0

        selected_pos = st.selectbox(
            "Position *",
            pos_opts,
            index=pos_index,
            key=pos_sel_key,
        )

        pos_custom = None
        if selected_pos == "Other":
            pos_custom = st.text_input(
                "Custom Position *",
                value=raw_pos,
                key=pos_custom_key,
            )

        # Mobile (optional)
        new_mobile = st.text_input(
            "Mobile # (optional)",
            value=raw_mobile,
            key=mobile_key,
            help="Optional – KSA mobile like 05XXXXXXXX.",
        )

        # Email (optional)
        new_email = st.text_input(
            "Email (optional)",
            value=raw_email,
            key=email_key,
            help="Optional – must be a valid email address.",
        )

        confirm_new = st.checkbox(
            "I confirm this is a **new** Target Audience (not already in the list).",
            key=confirm_key,
        )

        if st.button("➕ Create New & Link", key=f"{PAGE_NS}_create_btn"):
            if not confirm_new:
                st.error("Please confirm that this is a new Target Audience.")
                return

            name = (new_name or "").strip().upper()

            if not selected_dept:
                st.error("Please select a **Department** or choose **Other** and type a value.")
                return
            if selected_dept == "Other":
                dept_to_save = (dept_custom or "").strip()
                if not dept_to_save:
                    st.error("Please enter a **Custom Department**.")
                    return
            else:
                dept_to_save = selected_dept

            if not selected_pos:
                st.error("Please select a **Position** or choose **Other** and type a value.")
                return
            if selected_pos == "Other":
                pos_to_save = (pos_custom or "").strip()
                if not pos_to_save:
                    st.error("Please enter a **Custom Position**.")
                    return
            else:
                pos_to_save = selected_pos

            if not name:
                st.error("Cannot create a new Target Audience without a name.")
                return

            # Optional mobile validation (KSA)
            mobile_to_save = (new_mobile or "").strip()
            if mobile_to_save:
                if not re.fullmatch(r"(?:\+966|00966|0)?5\d{8}", mobile_to_save):
                    st.error(
                        "**Mobile #** looks invalid (expected KSA mobile like 05XXXXXXXX)."
                    )
                    return

            # Optional email validation
            email_to_save = (new_email or "").strip()
            if email_to_save:
                if not re.fullmatch(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_to_save):
                    st.error("**Email** looks invalid.")
                    return

            title_to_save = selected_title or None

            try:
                with engine.begin() as conn:
                    # Insert new TA with title + mobile + email
                    res = conn.execute(
                        text(
                            """
                            INSERT INTO target_audiences
                                (customer_id, title, name, department, position, mobile, email, is_active)
                            VALUES
                                (:cid, :title, :name, :dept, :pos, :mobile, :email, TRUE)
                            RETURNING audience_id
                            """
                        ),
                        {
                            "cid":    int(visit_row["customer_id"]),
                            "title":  title_to_save,
                            "name":   name,
                            "dept":   dept_to_save,
                            "pos":    pos_to_save,
                            "mobile": mobile_to_save or None,
                            "email":  email_to_save or None,
                        },
                    )
                    new_aid = res.scalar_one()

                    # Link visit to this new TA
                    conn.execute(
                        text(
                            """
                            UPDATE visits
                            SET audience_id = :aid
                            WHERE visit_id  = :vid
                            """
                        ),
                        {"aid": new_aid, "vid": selected_visit_id},
                    )

                st.session_state[success_key] = (
                    f"Created new Target Audience (ID {new_aid}) and linked visit #{selected_visit_id} ✅"
                )
                st.rerun()

            except Exception as e:
                st.error("Failed to create new Target Audience and link visit.")
                st.caption(str(e))

# =============================
# Review / Cleanup Other Customers Page
# =============================    
