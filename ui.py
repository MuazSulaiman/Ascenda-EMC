# ui.py
import base64
import json
import time
from pathlib import Path
from typing import Optional

import streamlit as st
import streamlit.components.v1 as components
from PIL import Image
from passlib.hash import pbkdf2_sha256

# Constant-time dummy used to prevent email enumeration via timing differences.
DUMMY_HASH = pbkdf2_sha256.hash("dummy")

from auth import (
    resolve_session_user,
    create_session,
    delete_session,
    revoke_session,
    purge_expired_sessions,
    set_url_param,
    get_url_param,
    set_url_session_param,
    get_user_by_email,
    check_login_lockout,
    record_failed_login,
    reset_login_attempts,
)
from config import APP_TITLE, SESSION_TTL_MIN
from utils import _client_ip, _utcnow_iso, _img_b64
from widgets import _reset_location_state_for_page


def get_logo_base64() -> str:
    logo_path = Path(__file__).parent / "static" / "Login_Logo.png"
    try:
        with open(logo_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception:
        return ""


def get_main_logo_base64() -> str:
    logo_path = Path(__file__).parent / "static" / "Main Logo.png"
    try:
        with open(logo_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception:
        return ""


try:
    from streamlit_js_eval import get_user_agent, streamlit_js_eval
except Exception:
    get_user_agent = None
    streamlit_js_eval = None


def capture_client_fingerprints():
    """Populate st.session_state['client_ip'] and ['user_agent'].
    Also reads the session ID from browser localStorage so SID never lives in the URL."""
    # User Agent
    if "user_agent" not in st.session_state:
        ua_val = None
        try:
            if get_user_agent:
                ua = get_user_agent()
                # get_user_agent returns a dict like {"userAgent": "..."} in recent versions
                ua_val = (ua or {}).get("userAgent") if isinstance(ua, dict) else ua
        except Exception:
            ua_val = None
        if ua_val:
            st.session_state["user_agent"] = str(ua_val)

    if "client_ip" not in st.session_state:
        try:
            headers = st.context.headers
            ip = headers.get("X-Forwarded-For", headers.get("X-Real-IP", "unknown"))
            ip = ip.split(",")[0].strip()
        except Exception:
            ip = "unknown"
        st.session_state["client_ip"] = ip

    # Session ID — read from browser localStorage so it is never exposed in the URL.
    # localStorage (not sessionStorage) is used because it is shared across all same-origin
    # contexts regardless of iframe isolation, which avoids a stale-read bug that occurred
    # when components.html() and streamlit_js_eval() ran in separate iframe sandboxes.
    # Uses a sentinel "__none__" to distinguish "JS returned empty" from "JS not yet run".
    # A session-unique key prevents the component from returning a cached value from a
    # prior Streamlit session.
    if "_sid_checked" not in st.session_state:
        if streamlit_js_eval:
            import uuid as _uuid
            if "_sid_read_key" not in st.session_state:
                st.session_state["_sid_read_key"] = f"_get_stored_sid_{_uuid.uuid4().hex}"
            try:
                stored = streamlit_js_eval(
                    js_expressions="localStorage.getItem('_ascenda_sid') ?? '__none__'",
                    key=st.session_state["_sid_read_key"],
                )
            except Exception:
                stored = None
            if stored is not None:          # JS has returned a value
                st.session_state["_sid_checked"] = True
                # Only use the localStorage value when _stored_sid is not already
                # set by a validated URL-based auth.  Never overwrite a live SID
                # with a potentially stale value from localStorage.
                if stored != "__none__" and "_stored_sid" not in st.session_state:
                    st.session_state["_stored_sid"] = stored
        else:
            # Library unavailable — mark checked so we don't spin forever
            st.session_state["_sid_checked"] = True

    # Write-back: persist the current SID to localStorage via streamlit_js_eval.
    # components.html() iframes may run in a different sandbox context and cannot
    # reliably write to the same localStorage that streamlit_js_eval reads from,
    # causing the SID to be missing on page refresh.  Using streamlit_js_eval for
    # both the read (above) and write ensures they share the same origin context.
    if streamlit_js_eval and "_stored_sid" in st.session_state and "_sid_ls_written" not in st.session_state:
        import uuid as _uuid
        if "_sid_write_key" not in st.session_state:
            st.session_state["_sid_write_key"] = f"_write_sid_{_uuid.uuid4().hex}"
        _sid_to_write = st.session_state["_stored_sid"]
        try:
            _write_result = streamlit_js_eval(
                js_expressions=f"localStorage.setItem('_ascenda_sid',{repr(_sid_to_write)});'written'",
                key=st.session_state["_sid_write_key"],
            )
            if _write_result == "written":
                st.session_state["_sid_ls_written"] = True
        except Exception:
            pass


def apply_role_based_layout():
    """
    Make layout 'wider' for manager/admin on larger screens
    but keep default behavior on mobile.
    """
    u = st.session_state.get("user") or resolve_session_user()
    if not u:
        return

    role = (u.get("role") or "").lower().strip()

    # Later you can read this from DB / user settings instead of hard-coding
    prefers_wide = role in ("manager", "admin")

    if not prefers_wide:
        return

    # CSS: only apply on wider viewports (e.g. laptops/desktops)
    st.markdown(
        """
        <style>
        /* Only screens >= 900px width: act like "wide" mode */
        @media (min-width: 900px) {
          .block-container {
            max-width: 1200px;   /* you can increase this if you want */
            padding-left: 2rem;
            padding-right: 2rem;
          }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def login_block():
    app_root = Path(__file__).parent
    logo_light_path = app_root / "static" / "Login_Logo.png"
    logo_dark_path  = app_root / "static" / "Main Logo.png"

    # Override: light blue-tinted page bg for login, strip main block card style
    st.markdown("""
    <style>
    [data-testid="stAppViewContainer"] { background: var(--color-surface-2) !important; }
    [data-testid="stAppViewContainer"] .block-container {
        background: transparent !important;
        box-shadow: none !important;
        border: none !important;
        max-width: 100% !important;
    }
    .login-logo-dark  { display: none; }
    html[data-theme="dark"] .login-logo-light { display: none; }
    html[data-theme="dark"] .login-logo-dark  { display: inline-block; }
    </style>
    """, unsafe_allow_html=True)

    _, col, _ = st.columns([1, 1.6, 1])
    with col:
        light_b64 = _img_b64(logo_light_path) if logo_light_path.exists() else ""
        dark_b64  = _img_b64(logo_dark_path)  if logo_dark_path.exists()  else ""
        if light_b64 or dark_b64:
            _light = (
                f'<img class="login-logo-light" src="data:image/png;base64,{light_b64}" alt="Ascenda" style="width:200px;height:auto;" />'
                if light_b64 else ""
            )
            _dark = (
                f'<img class="login-logo-dark" src="data:image/png;base64,{dark_b64}" alt="Ascenda" style="width:200px;height:auto;" />'
                if dark_b64 else ""
            )
            st.markdown(
                f'<div style="text-align:center;margin-bottom:1.5rem;">{_light}{_dark}</div>',
                unsafe_allow_html=True,
            )


        with st.form("login"):
            email = st.text_input("Email address", placeholder="you@company.com")
            pw = st.text_input("Password", type="password", placeholder="••••••••")
            submitted = st.form_submit_button(
                "Sign in", use_container_width=True, type="primary"
            )

        if submitted:
            em = (email or "").strip().lower()
            if not em or not pw:
                st.error("Please enter both email and password.")
                return

            is_locked, secs = check_login_lockout(em)
            if is_locked:
                mins = (secs + 59) // 60
                st.error(
                    f"Too many failed login attempts. Try again in "
                    f"{mins} minute{'s' if mins != 1 else ''}, or contact your administrator."
                )
                return

            u = get_user_by_email(em)
            if not u:
                pbkdf2_sha256.verify(pw, DUMMY_HASH)  # constant-time dummy to prevent email enumeration
                record_failed_login(em)
                st.error("Invalid email or password.")
                return

            if not bool(u.get("is_active", True)):
                st.error("Your account is inactive. Please contact the administrator.")
                return

            if not pbkdf2_sha256.verify(pw, u["password_hash"]):
                record_failed_login(em)
                st.error("Invalid email or password.")
                return

            reset_login_attempts(em)
            st.session_state.user = u
            sid = create_session(int(u["user_id"]), u.get("role"))
            # Store SID in browser localStorage — NOT in the URL.
            # This means the URL is safe to share; localStorage is reliably readable
            # across all same-origin contexts (including after page navigation).
            import streamlit.components.v1 as _comp
            _comp.html(
                f'<script>localStorage.setItem("_ascenda_sid",{repr(sid)});</script>',
                height=0,
            )
            _prefs = u.get("preferences") or {}
            if isinstance(_prefs, str):
                try:
                    _prefs = json.loads(_prefs)
                except Exception:
                    _prefs = {}
            _theme = _prefs.get("theme", "")
            _comp.html(
                f'<script>try{{if({repr(_theme)})'
                f'localStorage.setItem("_ascenda_theme",{repr(_theme)});'
                f'else localStorage.removeItem("_ascenda_theme");}}catch(e){{}}</script>',
                height=0,
            )
            st.session_state["_stored_sid"] = sid
            st.session_state["_sid_checked"] = True

            st.session_state["_current_page"] = "Dashboard"
            set_url_param("page", "Dashboard")

            st.success(f"Welcome, {u.get('name') or u.get('email')}!")
            st.rerun()


def _clear_page_session_state():
    """Clear all page-scoped session state keys so they don't leak across user sessions."""
    page_prefixes = ("change_request/", "submit_visit/", "check_in/")
    stale = [k for k in st.session_state if any(k.startswith(p) for p in page_prefixes)]
    for k in stale:
        del st.session_state[k]


def _do_logout():
    """Shared logout logic — revoke DB session, wipe local state, clear localStorage SID."""
    _reset_location_state_for_page("submit_visit")
    _clear_page_session_state()

    sid = st.session_state.get("_stored_sid") or st.query_params.get("sid")
    if sid:
        delete_session(sid)

    set_url_session_param(None)
    # Clear the SID from browser localStorage via streamlit_js_eval (reliable same-origin access).
    if streamlit_js_eval:
        try:
            streamlit_js_eval(
                js_expressions="localStorage.removeItem('_ascenda_sid');localStorage.removeItem('_ascenda_theme');'cleared'",
                key="_clear_sid_logout",
            )
        except Exception:
            pass
    st.session_state.pop("_sid_checked", None)
    st.session_state.pop("_sid_ls_written", None)
    st.session_state.pop("_sid_write_key", None)

    st.session_state.user = None
    st.session_state.pop("_current_page", None)
    st.session_state.pop("__current_page", None)
    set_url_param("page", None)
    st.rerun()


def logout_button():
    if st.sidebar.button("Logout"):
        _do_logout()


def sidebar_nav():
    # ── Sidebar CSS ──────────────────────────────────────────────────────────
    st.sidebar.markdown(
        """
        <style>
        section[data-testid="stSidebar"] {
            background: var(--color-surface) !important;
            border-right: 1px solid var(--color-border) !important;
        }
        section[data-testid="stSidebar"] > div:first-child { padding-top: 0.5rem !important; }

        .ascenda-logo-wrap {
            display: flex; justify-content: center; align-items: center;
            padding: 1rem 1rem 1rem !important;
            border-bottom: 1px solid var(--color-border) !important;
            margin-bottom: 0.5rem !important;
        }
        .ascenda-logo-wrap .logo-dark { display: none; }
        html[data-theme="dark"] .ascenda-logo-wrap .logo-light { display: none; }
        html[data-theme="dark"] .ascenda-logo-wrap .logo-dark  { display: block; }

        /* Collapse Streamlit's default element margins inside sidebar */
        section[data-testid="stSidebar"] .stMarkdown {
            margin-bottom: 0 !important;
        }

        /* ── Section labels ─────────────────────────────────────────────────── */
        .nav-section-label {
            display: block; padding: 0.65rem 12px 0.2rem;
            font-size: 0.68rem !important; font-weight: 700 !important;
            color: var(--color-text-subtle) !important; text-transform: uppercase !important;
            letter-spacing: 0.09em !important; user-select: none !important;
        }
        .nav-section-label:first-child { padding-top: 0.1rem !important; }

        /* ── Nav items (HTML anchor links) ──────────────────────────────────── */
        .nav-section-items {
            display: flex; flex-direction: column; gap: 1px; padding: 0 4px;
        }
        a.nav-item {
            display: flex !important; align-items: center !important; gap: 10px !important;
            width: 100% !important; padding: 9px 12px !important; min-height: 44px !important;
            border-radius: 10px !important; cursor: pointer !important;
            font-size: 0.875rem !important; font-weight: 500 !important;
            color: var(--color-text-muted) !important; text-decoration: none !important;
            box-sizing: border-box !important;
            border-left: 3px solid transparent !important;
            transition: background 150ms ease-out, color 150ms ease-out !important;
            line-height: 1.3 !important;
        }
        a.nav-item:hover {
            background: var(--color-surface-2) !important; color: var(--color-text) !important;
            text-decoration: none !important;
        }
        a.nav-item:focus-visible {
            outline: 2px solid var(--color-primary) !important; outline-offset: -2px !important;
        }
        a.nav-item.active {
            background: var(--color-primary-subtle) !important; color: var(--color-primary) !important;
            font-weight: 600 !important; border-left: 3px solid transparent !important;
        }
        a.nav-item svg {
            width: 16px !important; height: 16px !important; flex-shrink: 0 !important;
            stroke: currentColor !important; fill: none !important;
            stroke-width: 1.75 !important; stroke-linecap: round !important;
            stroke-linejoin: round !important;
        }

        /* ── User footer ─────────────────────────────────────────────────────── */
        .sidebar-user-footer {
            border-top: 1px solid var(--color-border); padding: 12px 14px 10px;
            display: flex; align-items: center; gap: 10px; margin-top: 0.75rem;
            cursor: pointer; transition: background 0.15s ease;
            border-radius: 0 0 8px 8px;
        }
        .sidebar-user-footer:hover { background: var(--color-surface-2); }
        .sidebar-user-avatar {
            width: 34px; height: 34px; border-radius: 50%; background: var(--color-primary);
            display: flex; align-items: center; justify-content: center;
            font-size: 0.8rem; font-weight: 700; color: #ffffff !important; flex-shrink: 0;
        }
        .sidebar-user-avatar span, .sidebar-user-avatar * {
            color: #ffffff !important;
        }
        .sidebar-user-name {
            font-size: 0.875rem; font-weight: 600; color: var(--color-text);
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 140px;
        }
        .sidebar-user-meta {
            font-size: 0.72rem; color: var(--color-text-subtle);
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 140px;
        }

        /* ── Sign out button ─────────────────────────────────────────────────── */
        section[data-testid="stSidebar"] .stButton > button {
            background: transparent !important; border: none !important;
            color: var(--status-danger-text) !important; font-size: 0.8rem !important;
            font-weight: 500 !important; padding: 5px 14px !important;
            border-radius: 8px !important; text-align: left !important;
            cursor: pointer !important; width: 100% !important;
            justify-content: flex-start !important;
            transition: background 0.15s ease !important;
        }
        section[data-testid="stSidebar"] .stButton > button:hover {
            background: var(--status-danger-bg) !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # ── Page icons (Heroicons outline, 24×24 viewBox) ─────────────────────────
    _ICONS = {
        "Dashboard":               '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>',
        "Submit Visit":            '<svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="12" y1="18" x2="12" y2="12"/><line x1="9" y1="15" x2="15" y2="15"/></svg>',
        "Check-In":                '<svg viewBox="0 0 24 24"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>',
        "My Visits":               '<svg viewBox="0 0 24 24"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>',
        "My Change Requests":      '<svg viewBox="0 0 24 24"><path d="M7 16V4m0 0L3 8m4-4 4 4M17 8v12m0 0 4-4m-4 4-4-4"/></svg>',
        "Active Projects":         '<svg viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>',
        "Project Creation":        '<svg viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><line x1="12" y1="11" x2="12" y2="17"/><line x1="9" y1="14" x2="15" y2="14"/></svg>',
        "Project Management":      '<svg viewBox="0 0 24 24"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>',
        "Review Target Audiences": '<svg viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
        "Review Other Customers":  '<svg viewBox="0 0 24 24"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/></svg>',
        "Review Change Requests":  '<svg viewBox="0 0 24 24"><path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/><rect x="9" y="3" width="6" height="4" rx="1"/><path d="M9 12l2 2 4-4"/></svg>',
        "Admin: Import Lookups":   '<svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>',
        "Admin: Data Browser":     '<svg viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>',
        "Admin: Users":            '<svg viewBox="0 0 24 24"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>',
        "Admin: Targets":          '<svg viewBox="0 0 24 24"><path d="M12 20V10"/><path d="M18 20V4"/><path d="M6 20v-4"/></svg>',
        "App Settings":            '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    }
    _ICON_DEFAULT = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/></svg>'

    # ── Logo (built into nav block below to avoid Streamlit element gap) ────
    logo_light_b64 = get_logo_base64()
    logo_dark_b64  = get_main_logo_base64()
    if logo_light_b64 or logo_dark_b64:
        _light_img = (
            f'<img class="logo-light" src="data:image/png;base64,{logo_light_b64}" alt="Ascenda" style="width:150px;height:auto;" />'
            if logo_light_b64 else ""
        )
        _dark_img = (
            f'<img class="logo-dark" src="data:image/png;base64,{logo_dark_b64}" alt="Ascenda" style="width:150px;height:auto;" />'
            if logo_dark_b64 else ""
        )
        _logo_html = f'<div class="ascenda-logo-wrap">{_light_img}{_dark_img}</div>'
    else:
        _logo_html = '<div class="ascenda-logo-wrap"><strong style="font-size:1.1rem;color:var(--color-text);">Ascenda</strong></div>'

    # ── User & role ───────────────────────────────────────────────────────────
    user = st.session_state.get("user")
    role = (user.get("role") if user else "").lower().strip()

    # ── Badge counts (cached 60 s) ────────────────────────────────────────────
    uid = (user.get("user_id") or user.get("id")) if user else None
    if uid:
        if time.time() - st.session_state.get("_nav_counts_ts", 0) > 60:
            try:
                from db_ops import query_df as _qdf
                _r = _qdf("SELECT COUNT(*) AS cnt FROM visits WHERE user_id = :u", {"u": int(uid)})
                st.session_state["_nav_mv_count"] = int(_r.iloc[0]["cnt"]) if not _r.empty else 0
                if role == "admin":
                    _cr = _qdf("SELECT COUNT(*) AS cnt FROM request_changes WHERE status = 'IN_REVIEW'")
                    st.session_state["_nav_cr_pending"] = int(_cr.iloc[0]["cnt"]) if not _cr.empty else 0
            except Exception:
                pass
            st.session_state["_nav_counts_ts"] = time.time()

    # ── Build grouped page sections by role ──────────────────────────────────
    main_pages = ["Dashboard"]

    field_pages = ["Submit Visit", "Check-In", "My Visits"]
    if role in ("rep", "maintenance", "sales manager", "biomedical manager", "admin"):
        field_pages.append("My Change Requests")

    project_pages: list = []
    # project pages hidden for all users
    # if role in ("rep", "maintenance"):
    #     project_pages = ["Active Projects"]
    # elif role in ("sales manager", "biomedical manager", "admin"):
    #     project_pages = ["Project Creation", "Project Management", "Active Projects"]

    review_pages: list = []
    if role == "admin":
        review_pages = ["Review Target Audiences", "Review Other Customers", "Review Change Requests"]

    admin_pages: list = []
    if role == "admin":
        admin_pages = ["Admin: Import Lookups", "Admin: Data Browser", "Admin: Users", "Admin: Targets"]

    settings_pages = ["App Settings"]

    sections = [("MAIN", main_pages), ("FIELD ACTIVITY", field_pages)]
    if project_pages:
        sections.append(("PROJECTS", project_pages))
    if review_pages:
        sections.append(("REVIEWS", review_pages))
    if admin_pages:
        sections.append(("ADMINISTRATION", admin_pages))
    sections.append(("PREFERENCES", settings_pages))

    all_pages = [p for _, pgs in sections for p in pgs]

    # ── Resolve current page ──────────────────────────────────────────────────
    prev_page = st.session_state.get("_current_page")
    url_page  = get_url_param("page")

    _on_settings = (
        st.session_state.get("_goto_user_settings", False)
        or url_page == "User Settings"
    )

    if _on_settings:
        current = prev_page if prev_page in all_pages else all_pages[0]
    elif url_page in all_pages:
        current = url_page
    elif prev_page in all_pages:
        current = prev_page
    else:
        current = all_pages[0]

    # ── Submit Visit geo-state cleanup on departure ───────────────────────────
    if prev_page == "Submit Visit" and current != "Submit Visit":
        _reset_location_state_for_page("submit_visit")

    # ── Update session / URL state ────────────────────────────────────────────
    _goto_settings = bool(st.session_state.pop("_goto_user_settings", False)) or _on_settings
    if not _goto_settings:
        st.session_state["_current_page"] = current
        if get_url_param("page") != current:
            set_url_param("page", current)

    # ── Render logo + full nav as one HTML block (no Streamlit element gap) ──
    # The SID is embedded in each href so the new Streamlit session (created by
    # the full-page reload that <a> links trigger) can re-authenticate immediately
    # without relying on localStorage JS round-trips.  resolve_session_user()
    # strips _sid from the URL on the very first render, so it is never visible
    # in the address bar once the page has loaded.
    _nav_sid = st.session_state.get("_stored_sid", "")
    nav_html = _logo_html + '<nav>'
    for sec_label, sec_pages in sections:
        nav_html += f'<span class="nav-section-label">{sec_label}</span>'
        nav_html += '<div class="nav-section-items">'
        for page in sec_pages:
            icon       = _ICONS.get(page, _ICON_DEFAULT)
            is_active  = (page == current) and not _on_settings
            cls        = "nav-item active" if is_active else "nav-item"
            page_param = page.replace(" ", "+")
            href       = f"?page={page_param}&_sid={_nav_sid}" if _nav_sid else f"?page={page_param}"
            nav_html  += f'<a href="{href}" target="_self" class="{cls}">{icon}<span>{page}</span></a>'
        nav_html += '</div>'
    nav_html += '</nav>'
    st.sidebar.markdown(nav_html, unsafe_allow_html=True)

    # ── Collapse sidebar when a nav item is clicked ───────────────────────────
    with st.sidebar:
        components.html(
            """
            <script>
            (function() {
                function attachCollapseHandlers() {
                    var doc = window.parent.document;
                    var navItems = doc.querySelectorAll('a.nav-item');
                    if (!navItems.length) return false;
                    navItems.forEach(function(link) {
                        if (link._collapseAttached) return;
                        link._collapseAttached = true;
                        link.addEventListener('click', function() {
                            var closeBtn = doc.querySelector('[data-testid="stSidebarCollapseButton"]');
                            if (closeBtn) { closeBtn.click(); return; }
                            var toggleBtn = doc.querySelector('[data-testid="collapsedControl"]');
                            if (toggleBtn) toggleBtn.click();
                        });
                    });
                    return true;
                }
                var attempts = 0;
                var timer = setInterval(function() {
                    if (attachCollapseHandlers() || ++attempts > 20) clearInterval(timer);
                }, 100);
            })();
            </script>
            """,
            height=0,
        )

    # ── User profile footer ───────────────────────────────────────────────────
    if user:
        _name     = user.get("name") or user.get("email") or "User"
        _role     = user.get("role") or ""
        _region   = user.get("region") or ""
        _initials = "".join(w[0].upper() for w in (_name or "U").split()[:2])
        _settings_href = f"?page=User+Settings&_sid={_nav_sid}" if _nav_sid else "?page=User+Settings"
        st.sidebar.markdown(
            f'<a href="{_settings_href}" target="_self" style="text-decoration:none;display:block;">'
            f'<div class="sidebar-user-footer">'
            f'<div class="sidebar-user-avatar"><span style="color:#ffffff !important;font-weight:700;">{_initials}</span></div>'
            f'<div style="flex:1;min-width:0;">'
            f'<div class="sidebar-user-name">{_name}</div>'
            f'<div class="sidebar-user-meta">{_role} · {_region}</div>'
            f'</div>'
            f'<svg width="14" height="14" fill="none" stroke="var(--color-border-strong)" stroke-width="2.5" '
            f'viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>'
            f'</div>'
            f'</a>',
            unsafe_allow_html=True,
        )
        if st.sidebar.button("Sign out", key="_nav_logout_btn", use_container_width=True):
            _do_logout()

    if _goto_settings:
        st.session_state["_current_page"] = "User Settings"
        return "User Settings"
    return current


def get_almadar_logo_base64() -> str:
    """
    Load Al Madar logo from /static and return as base64 string.
    Adjust the path if app_v11.py is inside a subfolder.
    """
    logo_path = Path(__file__).parent / "static" / "Almadar-Logo-01.png"
    try:
        with open(logo_path, "rb") as f:
            data = f.read()
        return base64.b64encode(data).decode("utf-8")
    except Exception as e:
        print("Logo load error:", e)
        return ""


def status_badge(label: str, variant: str = "neutral") -> str:
    """Return inline HTML for a soft-color status badge."""
    palettes = {
        "success": ("var(--status-success-bg)", "var(--status-success-text)"),
        "warning": ("var(--status-warning-bg)", "var(--status-warning-text)"),
        "danger":  ("var(--status-danger-bg)",  "var(--status-danger-text)"),
        "info":    ("var(--status-info-bg)",    "var(--status-info-text)"),
        "neutral": ("var(--status-neutral-bg)", "var(--status-neutral-text)"),
        "primary": ("var(--color-primary-subtle)", "var(--color-primary)"),
    }
    bg, fg = palettes.get(variant, palettes["neutral"])
    return (
        f'<span style="display:inline-flex;align-items:center;padding:2px 9px;'
        f'border-radius:6px;font-size:0.75rem;font-weight:600;line-height:1.5;'
        f'background:{bg};color:{fg};">{label}</span>'
    )


def required_legend() -> str:
    """Return HTML snippet for the required-field asterisk legend."""
    return (
        '<div style="margin:.25rem 0 1rem 0;">'
        'Fields marked with <span style="color:#d00000;font-weight:700">*</span> are required.'
        '</div>'
    )


def section_header(title: str, subtitle: str = "") -> None:
    """Render an artifact-style page section header."""
    sub_html = (
        f'<p style="margin:4px 0 0;font-size:0.875rem;color:var(--color-text-muted);">{subtitle}</p>'
        if subtitle else ""
    )
    st.markdown(
        f"""
        <div style="margin-bottom:1.25rem;">
          <h2 style="margin:0;font-size:1.375rem;font-weight:700;
                     color:var(--color-text);letter-spacing:-0.01em;">{title}</h2>
          {sub_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def kpi_card(label: str, value: str, delta: str = "", delta_positive: bool = True) -> str:
    """Return HTML string for a KPI card (pass to st.markdown)."""
    delta_color = "var(--status-success-text)" if delta_positive else "var(--status-danger-text)"
    delta_html = (
        f'<div style="margin-top:4px;font-size:0.75rem;font-weight:600;color:{delta_color};">'
        f'{delta}</div>'
        if delta else ""
    )
    return (
        f'<div style="background:var(--color-surface);border:1px solid var(--color-border);border-radius:14px;'
        f'padding:1rem 1.25rem;box-shadow:var(--shadow-card);">'
        f'<div style="font-size:0.8rem;font-weight:500;color:var(--color-text-muted);'
        f'text-transform:uppercase;letter-spacing:0.04em;">{label}</div>'
        f'<div style="font-size:1.75rem;font-weight:700;color:var(--color-text);'
        f'line-height:1.2;margin-top:4px;">{value}</div>'
        f'{delta_html}</div>'
    )


def compare_row(field: str, original: str, requested: str, changed: bool = False) -> str:
    """Return an HTML <tr> for a compare-grid table."""
    row_bg = "background:var(--status-warning-bg);" if changed else ""
    req_style = "font-weight:600;color:var(--status-warning-text);" if changed else ""
    return (
        f'<tr style="{row_bg}">'
        f'<td style="padding:8px 12px;font-size:0.875rem;font-weight:500;color:var(--color-text-muted);'
        f'white-space:nowrap;border-bottom:1px solid var(--color-border);">{field}</td>'
        f'<td style="padding:8px 12px;font-size:0.875rem;color:var(--color-text);'
        f'border-bottom:1px solid var(--color-border);">{original}</td>'
        f'<td style="padding:8px 12px;font-size:0.875rem;{req_style}'
        f'border-bottom:1px solid var(--color-border);">{requested}</td>'
        f'</tr>'
    )


def stepper(steps: list, current: int) -> None:
    """Render a horizontal step indicator. current is 0-indexed."""
    items = []
    for i, label in enumerate(steps):
        if i < current:
            circle = "background:var(--color-primary);border:2px solid var(--color-primary);color:#fff;"
            text = "color:var(--color-primary);font-weight:600;"
            icon = "&#10003;"
        elif i == current:
            circle = "background:var(--color-surface);border:2px solid var(--color-primary);color:var(--color-primary);"
            text = "color:var(--color-primary);font-weight:700;"
            icon = str(i + 1)
        else:
            circle = "background:var(--color-surface);border:2px solid var(--color-border);color:var(--color-text-subtle);"
            text = "color:var(--color-text-subtle);"
            icon = str(i + 1)

        connector_color = "var(--color-primary)" if i < current else "var(--color-border)"
        connector = (
            f'<div style="flex:1;height:2px;background:{connector_color};'
            f'margin:0 4px;align-self:center;min-width:12px;"></div>'
            if i < len(steps) - 1 else ""
        )
        items.append(
            f'<div style="display:flex;flex-direction:column;align-items:center;min-width:56px;">'
            f'<div style="width:32px;height:32px;border-radius:50%;display:flex;'
            f'align-items:center;justify-content:center;font-size:0.8rem;font-weight:700;{circle}">'
            f'{icon}</div>'
            f'<span style="margin-top:4px;font-size:0.75rem;white-space:nowrap;{text}">{label}</span>'
            f'</div>'
            + connector
        )
    st.markdown(
        f'<div style="display:flex;align-items:flex-start;justify-content:center;'
        f'padding:1rem 0 1.5rem;gap:0;">' + "".join(items) + "</div>",
        unsafe_allow_html=True,
    )


def show_footer():
    placeholder_html = (
        '<div style="height:36px;width:120px;border:1px dashed #ccc;border-radius:4px;'
        'display:flex;align-items:center;justify-content:center;'
        'font-size:0.7rem;color:#aaa;opacity:0.8;">Company Logo</div>'
    )
    st.markdown(
        f"""
        <div style="margin-top:2.5rem;padding-top:1.25rem;
                    border-top:1px solid var(--color-border);
                    display:flex;justify-content:space-between;
                    align-items:center;flex-wrap:wrap;gap:8px;">
          <div>{placeholder_html}</div>
          <div style="text-align:right;font-size:0.8rem;color:var(--color-text-subtle);line-height:1.6;">
            Core System © Cube n' Compass &nbsp;·&nbsp;
            Version 13
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# TOP NAVIGATION BAR
# ─────────────────────────────────────────────────────────────────────────────

def top_nav_bar(page_name: str) -> None:
    """Render the fixed top navigation bar. Call once per page render."""
    st.markdown(
        f"""
        <div class="ascenda-top-nav">
          <div class="ascenda-top-nav-left">
            <svg class="ascenda-top-ham" width="18" height="18" fill="none"
                 stroke="var(--color-text-muted)" stroke-width="2" viewBox="0 0 24 24">
              <line x1="3" y1="12" x2="21" y2="12"/>
              <line x1="3" y1="6"  x2="21" y2="6"/>
              <line x1="3" y1="18" x2="21" y2="18"/>
            </svg>
            <span class="ascenda-top-brand">Ascenda</span>
            <svg width="14" height="14" fill="none" stroke="var(--color-border-strong)" stroke-width="2"
                 viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
            <span class="ascenda-top-page">{page_name}</span>
          </div>
          <div class="ascenda-top-nav-right">
            <button class="ascenda-top-icon-btn" title="Help">
              <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"
                   viewBox="0 0 24 24">
                <circle cx="12" cy="12" r="10"/>
                <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
                <line x1="12" y1="17" x2="12.01" y2="17"/>
              </svg>
            </button>
            <button class="ascenda-top-icon-btn" title="Notifications">
              <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"
                   viewBox="0 0 24 24">
                <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
                <path d="M13.73 21a2 2 0 0 1-3.46 0"/>
              </svg>
            </button>
            <button class="ascenda-top-new-visit">+ New Visit</button>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# VISIT CARD
# ─────────────────────────────────────────────────────────────────────────────

def visit_card(
    visit_id: str,
    date_obj,
    customer: str,
    subtitle: str = "",
    status: str = "—",
    status_variant: str = "neutral",
    href: str = "",
) -> str:
    """Return HTML string for a single visit list card."""
    import pandas as pd
    try:
        dt = pd.to_datetime(date_obj, errors="coerce")
        day   = str(dt.day)            if dt is not None and not pd.isnull(dt) else "—"
        month = dt.strftime("%b").upper() if dt is not None and not pd.isnull(dt) else "—"
        year  = str(dt.year)           if dt is not None and not pd.isnull(dt) else ""
    except Exception:
        day, month, year = "—", "—", ""

    badge = status_badge(status, status_variant)
    card = (
        f'<div class="ascenda-visit-card" style="background:var(--color-surface);border:1px solid var(--color-border);border-radius:12px;'
        f'padding:0.875rem 1rem;margin-bottom:0.5rem;display:flex;align-items:center;gap:0.875rem;'
        f'cursor:pointer;transition:box-shadow 0.15s ease,border-color 0.15s ease;">'
        f'<div style="min-width:44px;width:44px;background:var(--color-surface-2);border-radius:8px;'
        f'text-align:center;padding:0.5rem 0.25rem;flex-shrink:0;">'
        f'<div style="font-size:1.125rem;font-weight:700;color:var(--color-text);line-height:1;">{day}</div>'
        f'<div style="font-size:0.7rem;font-weight:600;color:var(--color-text-subtle);text-transform:uppercase;'
        f'margin-top:2px;letter-spacing:0.04em;">{month}</div>'
        f'<div style="font-size:0.65rem;color:var(--color-border-strong);margin-top:1px;letter-spacing:0.02em;">{year}</div>'
        f'</div>'
        f'<div style="flex:1;min-width:0;">'
        f'<div style="font-size:0.9375rem;font-weight:600;color:var(--color-text);'
        f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{customer}</div>'
        f'<div style="font-size:0.8rem;color:var(--color-text-muted);margin-top:2px;'
        f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{subtitle}</div>'
        f'<div style="font-size:0.75rem;color:var(--color-text-subtle);margin-top:4px;">{visit_id}</div>'
        f'</div>'
        f'<div style="display:flex;align-items:center;gap:6px;flex-shrink:0;">'
        f'{badge}'
        f'<svg width="16" height="16" fill="none" stroke="var(--color-border-strong)" stroke-width="2.5" '
        f'viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>'
        f'</div>'
        f'</div>'
    )
    if href:
        return (
            f'<a href="{href}" target="_self" '
            f'style="text-decoration:none;color:inherit;display:block;">'
            f'{card}</a>'
        )
    return card


# ─────────────────────────────────────────────────────────────────────────────
# KPI CARD V2 (with icon circle)
# ─────────────────────────────────────────────────────────────────────────────

def kpi_card_v2(
    label: str,
    value: str,
    delta: str = "",
    delta_positive: bool = True,
    delta_neutral: bool = False,
    icon_svg: str = "",
    icon_bg: str = "var(--color-primary-subtle)",
) -> str:
    """Return HTML for a KPI card with a right-aligned colored icon circle."""
    delta_color = (
        "var(--color-text-subtle)" if delta_neutral
        else ("var(--status-success-text)" if delta_positive else "var(--status-danger-text)")
    )
    arrow_up = (
        '<svg aria-hidden="true" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.5" '
        'viewBox="0 0 24 24"><polyline points="18 15 12 9 6 15"/></svg>'
    )
    arrow_dn = (
        '<svg aria-hidden="true" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.5" '
        'viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>'
    )
    delta_arrow = "" if delta_neutral else (arrow_up if delta_positive else arrow_dn)
    delta_html = (
        f'<div style="margin-top:5px;font-size:0.8rem;font-weight:500;color:{delta_color};'
        f'display:flex;align-items:center;gap:3px;">'
        f'{delta_arrow}{delta}</div>'
        if delta else ""
    )
    icon_html = (
        f'<div style="width:40px;height:40px;border-radius:50%;background:{icon_bg};'
        f'display:flex;align-items:center;justify-content:center;flex-shrink:0;">'
        f'{icon_svg}</div>'
        if icon_svg else ""
    )
    return (
        f'<div style="background:var(--color-surface);border:1px solid var(--color-border);border-radius:14px;'
        f'padding:1rem 1.25rem;box-shadow:var(--shadow-card);'
        f'display:flex;justify-content:space-between;align-items:flex-start;'
        f'margin-bottom:12px;">'
        f'<div style="flex:1;">'
        f'<div style="font-size:0.8rem;font-weight:500;color:var(--color-text-muted);'
        f'text-transform:uppercase;letter-spacing:0.04em;">{label}</div>'
        f'<div style="font-size:1.75rem;font-weight:700;color:var(--color-text);'
        f'line-height:1.1;margin-top:4px;">{value}</div>'
        f'{delta_html}'
        f'</div>'
        f'{icon_html}'
        f'</div>'
    )


# ─────────────────────────────────────────────────────────────────────────────
# THEMED HTML TABLE (replaces st.dataframe for dark-mode compatibility)
# ─────────────────────────────────────────────────────────────────────────────

def html_table(df, max_rows: int = 500, max_height: int = 400) -> str:
    """Return a fully theme-aware HTML table string from a DataFrame.

    st.dataframe() uses a canvas renderer that ignores CSS variables, so in
    custom dark mode the table background stays white. Use this instead and
    render with st.markdown(..., unsafe_allow_html=True).
    """
    import html as _html

    cols = list(df.columns)

    rows_html = ""
    for i, (_, row) in enumerate(df.head(max_rows).iterrows()):
        bg = "background:var(--color-surface-2);" if i % 2 == 1 else ""
        cells = "".join(
            f'<td style="padding:0.45rem 0.75rem;color:var(--color-text);'
            f'font-size:0.85rem;border-bottom:1px solid var(--color-border);'
            f'white-space:nowrap;max-width:260px;overflow:hidden;text-overflow:ellipsis;">'
            f'{_html.escape(str(val) if val is not None else "—")}</td>'
            for val in row
        )
        rows_html += f'<tr style="{bg}">{cells}</tr>'

    if len(df) > max_rows:
        rows_html += (
            f'<tr><td colspan="{len(cols)}" style="padding:0.5rem 0.75rem;'
            f'color:var(--color-text-subtle);font-size:0.8rem;font-style:italic;">'
            f'… {len(df) - max_rows} more rows not shown</td></tr>'
        )

    sticky_th = (
        'padding:0.5rem 0.75rem;text-align:left;font-weight:600;'
        'color:var(--color-text-muted);white-space:nowrap;font-size:0.8rem;'
        'letter-spacing:0.03em;text-transform:uppercase;'
        'position:sticky;top:0;z-index:1;'
        'background:var(--color-surface-2);'
        'border-bottom:2px solid var(--color-border);'
    )
    header_cells = "".join(
        f'<th style="{sticky_th}">{_html.escape(str(c))}</th>'
        for c in cols
    )
    return (
        '<style>'
        '.ascenda-html-table::-webkit-scrollbar { width: 6px; height: 6px; }'
        '.ascenda-html-table::-webkit-scrollbar-track { background: var(--color-surface-2); border-radius: 0 10px 10px 0; }'
        '.ascenda-html-table::-webkit-scrollbar-thumb { background: var(--color-border-strong); border-radius: 6px; }'
        '.ascenda-html-table::-webkit-scrollbar-thumb:hover { background: var(--color-text-subtle); }'
        '.ascenda-html-table { scrollbar-color: var(--color-border-strong) var(--color-surface-2); scrollbar-width: thin; }'
        '</style>'
        f'<div class="ascenda-html-table" style="border:1px solid var(--color-border);border-radius:10px;'
        f'margin:0.5rem 0;overflow:auto;max-height:{max_height}px;">'
        '<table style="width:100%;border-collapse:collapse;">'
        f'<thead><tr>{header_cells}</tr></thead>'
        f'<tbody>{rows_html}</tbody>'
        '</table></div>'
    )


# ─────────────────────────────────────────────────────────────────────────────
# CIRCULAR FAB
# ─────────────────────────────────────────────────────────────────────────────

def circular_fab() -> None:
    """Render the fixed blue floating action button (bottom-right) — navigates to Submit Visit."""
    _sid = st.session_state.get("_stored_sid", "")
    href = f"?page=Submit+Visit&_sid={_sid}" if _sid else "?page=Submit+Visit"
    st.markdown(
        f"""
        <a href="{href}" target="_self" class="ascenda-fab" aria-label="New Visit">
          <svg aria-hidden="true" width="22" height="22" fill="none" stroke="#fff" stroke-width="2.5"
               stroke-linecap="round" viewBox="0 0 24 24">
            <line x1="12" y1="5" x2="12" y2="19"/>
            <line x1="5" y1="12" x2="19" y2="12"/>
          </svg>
        </a>
        """,
        unsafe_allow_html=True,
    )
