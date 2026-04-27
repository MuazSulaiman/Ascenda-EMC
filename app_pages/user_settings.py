# pages/user_settings.py
import re

import streamlit as st
import streamlit.components.v1 as components
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
    st.markdown(
        '<h3 style="font-size:1.125rem;font-weight:600;color:var(--color-text);margin:0 0 0.5rem;">My Profile</h3>',
        unsafe_allow_html=True,
    )
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
    st.markdown(
        '<h3 style="font-size:1.125rem;font-weight:600;color:var(--color-text);margin:0 0 0.5rem;">Change Password</h3>',
        unsafe_allow_html=True,
    )
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

    st.divider()

    # ── Install app section ──────────────────────────────────────────────────
    st.markdown(
        '<h3 style="font-size:1.125rem;font-weight:600;color:var(--color-text);margin:0 0 0.25rem;">Install App</h3>',
        unsafe_allow_html=True,
    )
    st.caption("Add Ascenda to your home screen for quick, full-screen access.")

    components.html("""
<style>
  #install-wrap { font-family: sans-serif; padding: 4px 0 8px; }
  #install-btn {
    display: inline-flex; align-items: center; gap: 8px;
    background: #2667ff; color: #fff; border: none; border-radius: 8px;
    padding: 10px 20px; font-size: 15px; font-weight: 600; cursor: pointer;
    transition: background 0.15s;
  }
  #install-btn:hover { background: #1a4fd6; }
  #install-btn:disabled { background: #94a3b8; cursor: default; }
  #ios-guide {
    display: none; margin-top: 12px;
    background: #f0f6ff; border: 1px solid #bfdbfe;
    border-radius: 10px; padding: 14px 16px; font-size: 14px; line-height: 1.7;
    color: #1e3a5f;
  }
  #ios-guide ol { margin: 6px 0 0 18px; padding: 0; }
  #already-msg {
    display: none; color: #16a34a; font-size: 14px; font-weight: 500;
    margin-top: 4px;
  }
</style>
<div id="install-wrap">
  <button id="install-btn">
    <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
      <path stroke-linecap="round" stroke-linejoin="round"
        d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3"/>
    </svg>
    Add to Home Screen
  </button>
  <div id="ios-guide">
    <strong>On iPhone / iPad:</strong>
    <ol>
      <li>Tap the <strong>Share</strong> button at the bottom of Safari (the box with an arrow pointing up)</li>
      <li>Scroll down and tap <strong>"Add to Home Screen"</strong></li>
      <li>Tap <strong>"Add"</strong> — done!</li>
    </ol>
  </div>
  <div id="already-msg">✓ Ascenda is already installed on this device.</div>
</div>

<script>
(function() {
  const btn = document.getElementById('install-btn');
  const iosGuide = document.getElementById('ios-guide');
  const alreadyMsg = document.getElementById('already-msg');

  function isIOS() {
    return /iphone|ipad|ipod/i.test(navigator.userAgent) && !window.MSStream;
  }
  function isInstalled() {
    return window.matchMedia('(display-mode: standalone)').matches ||
           window.navigator.standalone === true;
  }

  function resize() {
    const h = document.getElementById('install-wrap').scrollHeight + 16;
    window.parent.document.querySelectorAll('iframe').forEach(function(f) {
      try { if (f.contentWindow === window) f.style.height = h + 'px'; } catch(e) {}
    });
  }

  if (isInstalled()) {
    btn.style.display = 'none';
    alreadyMsg.style.display = 'block';
    resize();
  } else if (isIOS()) {
    btn.innerHTML = '<svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M8.684 13.342C8.886 12.938 9 12.482 9 12c0-.482-.114-.938-.316-1.342m0 2.684a3 3 0 110-2.684m0 2.684l6.632 3.316m-6.632-6l6.632-3.316m0 0a3 3 0 105.367-2.684 3 3 0 00-5.367 2.684zm0 9.316a3 3 0 105.368 2.684 3 3 0 00-5.368-2.684z"/></svg> How to install on iPhone';
    btn.onclick = function() {
      const open = iosGuide.style.display === 'block';
      iosGuide.style.display = open ? 'none' : 'block';
      resize();
    };
    resize();
  } else {
    // Android / desktop — use deferred prompt
    function tryBind() {
      const prompt = window.parent.__ascendaDeferredPrompt;
      if (prompt) {
        btn.disabled = false;
        btn.onclick = function() {
          prompt.prompt();
          prompt.userChoice.then(function(r) {
            if (r.outcome === 'accepted') {
              btn.textContent = '✓ Installing…';
              btn.disabled = true;
              window.parent.__ascendaDeferredPrompt = null;
            }
          });
        };
      } else {
        btn.disabled = true;
        btn.title = 'Open in Chrome and browse the app for a moment, then try again.';
        btn.innerHTML = btn.innerHTML.replace('Add to Home Screen', 'Install not available yet');
        // Retry a few times in case the event fires after page load
        setTimeout(tryBind, 2000);
      }
    }
    tryBind();
  }
})();
</script>
""", height=56, scrolling=False)
