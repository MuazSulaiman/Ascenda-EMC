# pages/user_settings.py
import re

import streamlit as st
from passlib.hash import pbkdf2_sha256
from sqlalchemy import text

from auth import resolve_session_user
from db import engine
from db_ops import query_df, exec_sql
from widgets import set_current_page
from ui import section_header


def page_user_settings():
    section_header("User Settings", "Manage your profile and account password")
    set_current_page("user_settings")

    u = st.session_state.user
    uid = int(u["user_id"] if "user_id" in u else u["id"])

    # Load fresh user row (and BU name for display)
    me = query_df("""
        SELECT u.user_id, u.email, u.name, u.region, u.role, u.is_active,
               bu.name AS business_unit, u.password_hash
        FROM users u
        LEFT JOIN business_units bu ON bu.business_unit_id = u.business_unit_id
        WHERE u.user_id = :uid
    """, {"uid": uid})
    if me.empty:
        st.error("Could not load your profile.")
        return

    row = me.iloc[0]

    # Read-only profile block
    st.subheader("My Profile")
    c1, c2 = st.columns(2)
    with c1:
        st.text_input("Name", value=row.get("name") or "", disabled=True)
        st.text_input("Email", value=row.get("email") or "", disabled=True)
        st.text_input("Region", value=row.get("region") or "", disabled=True)
    with c2:
        st.text_input("Role", value=row.get("role") or "", disabled=True)
        st.text_input("Business Unit", value=row.get("business_unit") or "", disabled=True)
        st.text_input("Status", value=("Active" if bool(row.get("is_active", True)) else "Inactive"), disabled=True)

    st.divider()

    # Change password form
    st.subheader("Change Password")
    with st.form("change_pw_form", clear_on_submit=True):
        old_pw = st.text_input("Current Password *", type="password")
        new_pw = st.text_input("New Password *", type="password", help="Min 8 chars, include a letter and a number.")
        new_pw2 = st.text_input("Confirm New Password *", type="password")
        submit = st.form_submit_button("Update Password", type="primary")

    # Validation + update (in field order)
    if submit:
        # 1) Old password present?
        if not old_pw:
            st.error("Please enter your current password.")
            st.stop()

        # 2) Verify old password
        if not pbkdf2_sha256.verify(old_pw, row["password_hash"]):
            st.error("Current password is incorrect.")
            st.stop()

        # 3) New password present?
        if not new_pw:
            st.error("Please enter a new password.")
            st.stop()

        # 4) Confirm present?
        if not new_pw2:
            st.error("Please confirm your new password.")
            st.stop()

        # 5) Match?
        if new_pw != new_pw2:
            st.error("New password and confirmation do not match.")
            st.stop()

        # 6) Strength checks
        if len(new_pw) < 8:
            st.error("New password must be at least 8 characters long.")
            st.stop()
        if not re.search(r"[A-Za-z]", new_pw) or not re.search(r"\d", new_pw):
            st.error("New password must include at least one letter and one number.")
            st.stop()

        # 7) Prevent reusing the same password
        if pbkdf2_sha256.verify(new_pw, row["password_hash"]):
            st.error("New password must be different from the current password.")
            st.stop()

        # 8) Save (PostgreSQL: use named params with exec_sql)
        try:
            new_hash = pbkdf2_sha256.hash(new_pw)
            exec_sql(
                "UPDATE users SET password_hash = :ph WHERE user_id = :uid",
                {"ph": new_hash, "uid": uid}
            )
            st.success("Password updated ✅")
        except Exception as e:
            st.error("Could not update password.")
            st.caption(str(e))
