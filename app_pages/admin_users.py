# pages/admin_users.py
import streamlit as st
from passlib.hash import pbkdf2_sha256
from sqlalchemy import text

from auth import resolve_session_user
from db_ops import query_df, exec_sql
from utils import _gen_tmp_pw
from widgets import set_current_page
from ui import section_header, status_badge as _status_badge

def page_admin_users():
    section_header("Admin — Users", "Create, manage, and deactivate user accounts")
    set_current_page("admin_users")
    st.markdown(
        '<h3 style="font-size:1.125rem;font-weight:600;color:#0d1117;margin:1.25rem 0 0.5rem;">Add a user</h3>',
        unsafe_allow_html=True,
    )

    # --- Temp Password Generator (outside the form) ---
    st.session_state.setdefault("create_user_tmp_pw", "")

    gcol1, gcol2 = st.columns([1, 4])
    if gcol1.button("🔄 Generate Temporary Password"):
        st.session_state["create_user_tmp_pw"] = _gen_tmp_pw()
    if st.session_state["create_user_tmp_pw"]:
        st.caption(f"Generated: `{st.session_state['create_user_tmp_pw']}` (you can edit before saving)")

    # --- User Creation Form ---
    with st.form("add_user", clear_on_submit=True):
        col1, col2 = st.columns(2)

        with col1:
            email = st.text_input("Email *")
            name = st.text_input("Name *")
            region = st.selectbox("Region", ["", "C/R", "W/R", "E/R"], index=0)

        with col2:
            bu_df = query_df("SELECT business_unit_id, name FROM business_units WHERE is_active IS TRUE ORDER BY name")
            bu_names = bu_df["name"].tolist()
            bu_sel = st.selectbox("Business Unit (optional)", [""] + bu_names, index=0)
            role = st.selectbox("Role", ["","rep", "admin","sales manager","biomedical manager","maintenance"], index=0)
            pw = st.text_input("Temporary Password *", type="password",
                               value=st.session_state["create_user_tmp_pw"])

        add_btn = st.form_submit_button("Create User", type="primary")

    if add_btn:
        if not (email and name and pw):
            st.error("Email, Name, and Password are required.")
        else:
            try:
                bu_id = None
                if bu_sel:
                    bu_id = int(bu_df.loc[bu_df["name"] == bu_sel, "business_unit_id"].iloc[0])

                # Insert (PostgreSQL named parameters). Use proper booleans.
                exec_sql(
                    """
                    INSERT INTO users(email, password_hash, name, region, business_unit_id, role, is_active)
                    VALUES (:email, :pwd, :name, :region, :buid, :role, TRUE)
                    """,
                    {
                        "email": email.strip().lower(),
                        "pwd": pbkdf2_sha256.hash(pw),
                        "name": name.strip(),
                        "region": (region.strip() if region else None),
                        "buid": bu_id,
                        "role": role,
                    },
                )
                st.success("✅ User added successfully")
                st.session_state["create_user_tmp_pw"] = ""
            except Exception as e:
                st.error("Could not add user (email might already exist).")
                st.caption(str(e))

    # ---- All users (with BU) ----
    st.markdown(
        '<h3 style="font-size:1.125rem;font-weight:600;color:#0d1117;margin:1.25rem 0 0.5rem;">All Users</h3>',
        unsafe_allow_html=True,
    )
    df = query_df("""
        SELECT u.user_id,
               u.email,
               u.name,
               u.region,
               u.role,
               u.is_active,
               bu.name AS business_unit
        FROM users u
        LEFT JOIN business_units bu ON bu.business_unit_id = u.business_unit_id
        ORDER BY u.user_id DESC
    """)
    st.markdown(f"**Total: {len(df):,}**")
    st.dataframe(df, width="stretch", hide_index=True)
    if not df.empty:
        st.download_button(
            "Download CSV",
            df.to_csv(index=False).encode("utf-8-sig"),
            "users.csv",
            "text/csv",
            key="dl_users2"
        )

    st.divider()
    st.subheader("Manage Users")

    mdf = query_df("""
        SELECT u.user_id,
               u.email,
               u.name,
               u.region,
               u.role,
               u.is_active,
               u.business_unit_id,
               bu.name AS business_unit
        FROM users u
        LEFT JOIN business_units bu ON bu.business_unit_id = u.business_unit_id
        ORDER BY u.name, u.user_id
    """)

    if mdf.empty:
        st.info("No users to manage yet.")
        return

    def _fmt_user(r):
        status = "active" if bool(r.is_active) else "inactive"
        bu = f" · BU: {r.business_unit}" if pd.notna(r.business_unit) and str(r.business_unit).strip() else ""
        return f"{r.name or r.email} <{r.email}> ({r.role}) — {status}{bu}"

    labels = [_fmt_user(r) for r in mdf.itertuples(index=False)]
    sel = st.selectbox("Select user", [""] + labels, index=0, key="mg_user_sel")

    if not sel:
        st.info("Select a user above to manage.")
        return

    row = mdf.iloc[labels.index(sel)]
    uid = int(row["user_id"])
    is_active = bool(row["is_active"])
    status_text = "Active" if is_active else "Inactive"
    badge_variant = "success" if is_active else "neutral"
    st.markdown(
        f"Selected: **{row['name'] or row['email']}** &nbsp; {_status_badge(status_text, badge_variant)}",
        unsafe_allow_html=True,
    )

    colA, colB, colC = st.columns([1, 1, 2])

    # Activate / Deactivate
    with colA:
        label = "Deactivate" if is_active else "Activate"
        if st.button(label, key=f"mg_user_toggle_{uid}"):
            current = st.session_state.get("user")
            current_uid = int(current["user_id"]) if current and "user_id" in current else None
            if label == "Deactivate" and current_uid == uid:
                st.error("You can't deactivate your own account while logged in.")
            else:
                try:
                    exec_sql(
                        "UPDATE users SET is_active = :active WHERE user_id = :uid",
                        {"active": (not is_active), "uid": uid},  # send True/False
                    )
                    st.success("Updated ✅")
                except Exception as e:
                    st.error("Could not update user status.")
                    st.caption(str(e))

    # Show current Role / BU
    with colB:
        bu_display = row["business_unit"] or "—"
        st.markdown(f"**Role:** {row['role']}  \n**Business Unit:** {bu_display}")

    # Quick Edit (Region / BU / Role)
    with colC:
        edit_box = st.popover("Edit") if hasattr(st, "popover") else st.expander("Edit", expanded=False)
        with edit_box:
            bu_df2 = query_df("SELECT business_unit_id, name FROM business_units WHERE is_active IS TRUE ORDER BY name")
            bu_labels = [""] + bu_df2["name"].tolist()

            current_bu_name = row["business_unit"] or ""
            bu_idx = bu_labels.index(current_bu_name) if current_bu_name in bu_labels else 0

            with st.form(f"mg_user_edit_{uid}"):
                new_region = st.selectbox(
                    "Region",
                    ["", "C/R", "W/R", "E/R"],
                    index=(["", "C/R", "W/R", "E/R"].index(row["region"]) if row["region"] in ["", "C/R", "W/R", "E/R"] else 0)
                )
                new_bu_label = st.selectbox("Business Unit (optional)", bu_labels, index=bu_idx)
                
                role_opts = ["", "rep", "admin", "sales manager","biomedical manager", "maintenance"]

                current_role = (row.get("role") or "").strip().lower()
                role_idx = role_opts.index(current_role) if current_role in role_opts else 0

                new_role = st.selectbox("Role", role_opts, index=role_idx)
                
                save = st.form_submit_button("Save changes")

            if save:
                try:
                    new_bu_id = None
                    if new_bu_label:
                        new_bu_id = int(bu_df2.loc[bu_df2["name"] == new_bu_label, "business_unit_id"].iloc[0])
                    exec_sql(
                        "UPDATE users SET region = :region, business_unit_id = :buid, role = :role WHERE user_id = :uid",
                        {
                            "region": (new_region.strip() if new_region else None),
                            "buid": new_bu_id,
                            "role": new_role,
                            "uid": uid,
                        },
                    )
                    st.success("Saved ✅")
                except Exception as e:
                    st.error("Could not save changes.")
                    st.caption(str(e))

    st.divider()

    # --- Admin: Reset password for selected user (no forced change) ---
    st.subheader("Reset Password")

    # Flash message area (rendered directly under the button group)
    flash_key = f"flash_reset_{uid}"
    if st.session_state.get(flash_key):
        st.success(st.session_state[flash_key])

    # Keys for the input + buffer
    tmp_input_key = f"tmp_pw_input_{uid}"
    buf_key = f"tmp_pw_buf_{uid}"
    st.session_state.setdefault(buf_key, "")

    # Handle 'Generate' BEFORE rendering the text_input,
    gen_col, _ = st.columns([1, 6])
    gen_clicked = gen_col.button("Generate", key=f"gen_tmp_pw_{uid}")
    if gen_clicked:
        gen_pw = _gen_tmp_pw()
        st.session_state[buf_key] = gen_pw
        st.session_state[tmp_input_key] = gen_pw

    # Now render the input (uses session_state if present)
    tmp_pw = st.text_input(
        "Temporary Password *",
        key=tmp_input_key,
        type="password",
        help="Share this with the user. They can change it later from User Settings."
    )

    # Action buttons row
    b1, _ = st.columns([2, 5])
    if b1.button("Set Temporary Password", type="primary", key=f"set_tmp_pw_{uid}"):
        final_tmp_pw = (st.session_state.get(tmp_input_key) or "").strip()
        if not final_tmp_pw:
            st.error("Please enter or generate a temporary password.")
        else:
            try:
                new_hash = pbkdf2_sha256.hash(final_tmp_pw)
                exec_sql(
                    "UPDATE users SET password_hash = :pwd WHERE user_id = :uid",
                    {"pwd": new_hash, "uid": uid},
                )
                st.session_state[flash_key] = (
                    f"Temporary password set ✅ (user not forced to change). Temp password: `{final_tmp_pw}`"
                )
                st.success(st.session_state[flash_key])
            except Exception as e:
                st.error("Could not reset the password.")
                st.caption(str(e))

# =============================
# Review / Cleanup Target Audiences Page
# =============================            
