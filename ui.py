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
    """
    Read logo from your Git repo static folder and return it as base64 string.
    Adjust the relative path if your app file is in a subfolder.
    """
    logo_path = Path(__file__).parent / "static" / "Login_Logo.png"
    try:
        with open(logo_path, "rb") as f:
            data = f.read()
        return base64.b64encode(data).decode("utf-8")
    except Exception:
        return ""


try:
    from streamlit_js_eval import get_user_agent, streamlit_js_eval
except Exception:
    get_user_agent = None
    streamlit_js_eval = None


def capture_client_fingerprints():
    """Populate st.session_state['client_ip'] and ['user_agent'] from the browser.
    Also reads the session ID from browser sessionStorage so SID never lives in the URL."""
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

    # Public IP (must be fetched client-side; server-side requests return the server IP)
    if "client_ip" not in st.session_state:
        ip_val = None
        try:
            if streamlit_js_eval:
                ip_val = streamlit_js_eval(
                    js_expressions=(
                        "await fetch('https://api.ipify.org?format=json')"
                        ".then(r=>r.json()).then(j=>j.ip)"
                    ),
                    key="client_ip_fetch"
                )
        except Exception:
            ip_val = None
        if ip_val:
            st.session_state["client_ip"] = str(ip_val)

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
    logo_path = app_root / "static" / "Login_Logo.png"

    # Override: light blue-tinted page bg for login, strip main block card style
    st.markdown("""
    <style>
    [data-testid="stAppViewContainer"] { background: #f6f8fa !important; }
    [data-testid="stAppViewContainer"] .block-container {
        background: transparent !important;
        box-shadow: none !important;
        border: none !important;
        max-width: 100% !important;
    }
    </style>
    """, unsafe_allow_html=True)

    _, col, _ = st.columns([1, 1.6, 1])
    with col:
        if logo_path.exists():
            b64 = _img_b64(logo_path)
            st.markdown(
                f'<div style="text-align:center;margin-bottom:1.5rem;">'
                f'<img src="data:image/png;base64,{b64}" alt="Ascenda"'
                f' style="width:200px;height:auto;" /></div>',
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
                record_failed_login(em)
                st.error("User not found.")
                return

            if not bool(u.get("is_active", True)):
                st.error("Your account is inactive. Please contact the administrator.")
                return

            if not pbkdf2_sha256.verify(pw, u["password_hash"]):
                record_failed_login(em)
                st.error("Invalid password.")
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
    # Clear the SID from browser localStorage so the tab cannot re-auth.
    import streamlit.components.v1 as _comp
    _comp.html('<script>localStorage.removeItem("_ascenda_sid");</script>', height=0)
    st.session_state.pop("_sid_checked", None)

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
            background: #ffffff !important;
            border-right: 1px solid #e4e8ec !important;
        }
        section[data-testid="stSidebar"] > div:first-child { padding-top: 0.5rem !important; }

        .ascenda-logo-wrap {
            display: flex; justify-content: center; align-items: center;
            padding: 1rem 1rem 1rem !important;
            border-bottom: 1px solid #f0f2f5 !important;
            margin-bottom: 0.5rem !important;
        }

        /* Collapse Streamlit's default element margins inside sidebar */
        section[data-testid="stSidebar"] .stMarkdown {
            margin-bottom: 0 !important;
        }

        /* ── Section labels ─────────────────────────────────────────────────── */
        .nav-section-label {
            display: block; padding: 0.65rem 12px 0.2rem;
            font-size: 0.68rem !important; font-weight: 700 !important;
            color: #8b949e !important; text-transform: uppercase !important;
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
            color: #57606a !important; text-decoration: none !important;
            box-sizing: border-box !important;
            border-left: 3px solid transparent !important;
            transition: background 150ms ease-out, color 150ms ease-out !important;
            line-height: 1.3 !important;
        }
        a.nav-item:hover {
            background: #f6f8fa !important; color: #0d1117 !important;
            text-decoration: none !important;
        }
        a.nav-item:focus-visible {
            outline: 2px solid #2563EB !important; outline-offset: -2px !important;
        }
        a.nav-item.active {
            background: #EEF2FF !important; color: #2563EB !important;
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
            border-top: 1px solid #e4e8ec; padding: 12px 14px 10px;
            display: flex; align-items: center; gap: 10px; margin-top: 0.75rem;
            cursor: pointer; transition: background 0.15s ease;
            border-radius: 0 0 8px 8px;
        }
        .sidebar-user-footer:hover { background: #f6f8fa; }
        .sidebar-user-avatar {
            width: 34px; height: 34px; border-radius: 50%; background: #2563EB;
            display: flex; align-items: center; justify-content: center;
            font-size: 0.8rem; font-weight: 700; color: #ffffff !important; flex-shrink: 0;
        }
        .sidebar-user-avatar span, .sidebar-user-avatar * {
            color: #ffffff !important;
        }
        .sidebar-user-name {
            font-size: 0.875rem; font-weight: 600; color: #0d1117;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 140px;
        }
        .sidebar-user-meta {
            font-size: 0.72rem; color: #8b949e;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 140px;
        }

        /* ── Sign out button ─────────────────────────────────────────────────── */
        section[data-testid="stSidebar"] .stButton > button {
            background: transparent !important; border: none !important;
            color: #c83333 !important; font-size: 0.8rem !important;
            font-weight: 500 !important; padding: 5px 14px !important;
            border-radius: 8px !important; text-align: left !important;
            cursor: pointer !important; width: 100% !important;
            justify-content: flex-start !important;
            transition: background 0.15s ease !important;
        }
        section[data-testid="stSidebar"] .stButton > button:hover {
            background: #fdeceb !important;
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
    }
    _ICON_DEFAULT = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/></svg>'

    # ── Logo (built into nav block below to avoid Streamlit element gap) ────
    logo_b64 = get_logo_base64()
    if logo_b64:
        _logo_html = (
            f'<div class="ascenda-logo-wrap">'
            f'<img src="data:image/png;base64,{logo_b64}" alt="Ascenda"'
            f' style="width:150px;height:auto;" /></div>'
        )
    else:
        _logo_html = '<div class="ascenda-logo-wrap"><strong style="font-size:1.1rem;color:#0d1117;">Ascenda</strong></div>'

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
        admin_pages = ["Admin: Import Lookups", "Admin: Data Browser", "Admin: Users"]

    sections = [("MAIN", main_pages), ("FIELD ACTIVITY", field_pages)]
    if project_pages:
        sections.append(("PROJECTS", project_pages))
    if review_pages:
        sections.append(("REVIEWS", review_pages))
    if admin_pages:
        sections.append(("ADMINISTRATION", admin_pages))

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
            f'<svg width="14" height="14" fill="none" stroke="#c9d1d9" stroke-width="2.5" '
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
        "success": ("#e6f6ec", "#0e8a4f"),
        "warning": ("#fdf2e4", "#b5651d"),
        "danger":  ("#fdeceb", "#c83333"),
        "info":    ("#e8f4fd", "#1565c0"),
        "neutral": ("#f0f0f0", "#444444"),
        "primary": ("#eef2ff", "#2667ff"),
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
        f'<p style="margin:4px 0 0;font-size:0.875rem;color:#57606a;">{subtitle}</p>'
        if subtitle else ""
    )
    st.markdown(
        f"""
        <div style="margin-bottom:1.25rem;">
          <h2 style="margin:0;font-size:1.375rem;font-weight:700;
                     color:#0d1117;letter-spacing:-0.01em;">{title}</h2>
          {sub_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def kpi_card(label: str, value: str, delta: str = "", delta_positive: bool = True) -> str:
    """Return HTML string for a KPI card (pass to st.markdown)."""
    delta_color = "#0e8a4f" if delta_positive else "#c83333"
    delta_html = (
        f'<div style="margin-top:4px;font-size:0.75rem;font-weight:600;color:{delta_color};">'
        f'{delta}</div>'
        if delta else ""
    )
    return (
        f'<div style="background:#fff;border:1px solid #e4e8ec;border-radius:14px;'
        f'padding:1rem 1.25rem;box-shadow:0 1px 2px rgba(15,23,42,0.04);">'
        f'<div style="font-size:0.8rem;font-weight:500;color:#57606a;'
        f'text-transform:uppercase;letter-spacing:0.04em;">{label}</div>'
        f'<div style="font-size:1.75rem;font-weight:700;color:#0d1117;'
        f'line-height:1.2;margin-top:4px;">{value}</div>'
        f'{delta_html}</div>'
    )


def compare_row(field: str, original: str, requested: str, changed: bool = False) -> str:
    """Return an HTML <tr> for a compare-grid table."""
    row_bg = "background:#fdf2e4;" if changed else ""
    req_style = "font-weight:600;color:#b5651d;" if changed else ""
    return (
        f'<tr style="{row_bg}">'
        f'<td style="padding:8px 12px;font-size:0.875rem;font-weight:500;color:#57606a;'
        f'white-space:nowrap;border-bottom:1px solid #e4e8ec;">{field}</td>'
        f'<td style="padding:8px 12px;font-size:0.875rem;color:#0d1117;'
        f'border-bottom:1px solid #e4e8ec;">{original}</td>'
        f'<td style="padding:8px 12px;font-size:0.875rem;{req_style}'
        f'border-bottom:1px solid #e4e8ec;">{requested}</td>'
        f'</tr>'
    )


def stepper(steps: list, current: int) -> None:
    """Render a horizontal step indicator. current is 0-indexed."""
    items = []
    for i, label in enumerate(steps):
        if i < current:
            circle = "background:#2667ff;border:2px solid #2667ff;color:#fff;"
            text = "color:#2667ff;font-weight:600;"
            icon = "&#10003;"
        elif i == current:
            circle = "background:#fff;border:2px solid #2667ff;color:#2667ff;"
            text = "color:#2667ff;font-weight:700;"
            icon = str(i + 1)
        else:
            circle = "background:#fff;border:2px solid #e4e8ec;color:#8b949e;"
            text = "color:#8b949e;"
            icon = str(i + 1)

        connector_color = "#2667ff" if i < current else "#e4e8ec"
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
                    border-top:1px solid #e4e8ec;
                    display:flex;justify-content:space-between;
                    align-items:center;flex-wrap:wrap;gap:8px;">
          <div>{placeholder_html}</div>
          <div style="text-align:right;font-size:0.8rem;color:#8b949e;line-height:1.6;">
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
                 stroke="#57606a" stroke-width="2" viewBox="0 0 24 24">
              <line x1="3" y1="12" x2="21" y2="12"/>
              <line x1="3" y1="6"  x2="21" y2="6"/>
              <line x1="3" y1="18" x2="21" y2="18"/>
            </svg>
            <span class="ascenda-top-brand">Ascenda</span>
            <svg width="14" height="14" fill="none" stroke="#c9d1d9" stroke-width="2"
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
        f'<div class="ascenda-visit-card" style="background:#fff;border:1px solid #e4e8ec;border-radius:12px;'
        f'padding:0.875rem 1rem;margin-bottom:0.5rem;display:flex;align-items:center;gap:0.875rem;'
        f'cursor:pointer;transition:box-shadow 0.15s ease,border-color 0.15s ease;">'
        f'<div style="min-width:44px;width:44px;background:#f6f8fa;border-radius:8px;'
        f'text-align:center;padding:0.5rem 0.25rem;flex-shrink:0;">'
        f'<div style="font-size:1.125rem;font-weight:700;color:#0d1117;line-height:1;">{day}</div>'
        f'<div style="font-size:0.7rem;font-weight:600;color:#8b949e;text-transform:uppercase;'
        f'margin-top:2px;letter-spacing:0.04em;">{month}</div>'
        f'<div style="font-size:0.65rem;color:#b0b8c1;margin-top:1px;letter-spacing:0.02em;">{year}</div>'
        f'</div>'
        f'<div style="flex:1;min-width:0;">'
        f'<div style="font-size:0.9375rem;font-weight:600;color:#0d1117;'
        f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{customer}</div>'
        f'<div style="font-size:0.8rem;color:#57606a;margin-top:2px;'
        f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{subtitle}</div>'
        f'<div style="font-size:0.75rem;color:#8b949e;margin-top:4px;">{visit_id}</div>'
        f'</div>'
        f'<div style="display:flex;align-items:center;gap:6px;flex-shrink:0;">'
        f'{badge}'
        f'<svg width="16" height="16" fill="none" stroke="#c9d1d9" stroke-width="2.5" '
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
    icon_bg: str = "#eef2ff",
) -> str:
    """Return HTML for a KPI card with a right-aligned colored icon circle."""
    delta_color = "#8b949e" if delta_neutral else ("#0e8a4f" if delta_positive else "#c83333")
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
        f'<div style="background:#fff;border:1px solid #e4e8ec;border-radius:14px;'
        f'padding:1rem 1.25rem;box-shadow:0 1px 2px rgba(15,23,42,0.04);'
        f'display:flex;justify-content:space-between;align-items:flex-start;'
        f'margin-bottom:12px;">'
        f'<div style="flex:1;">'
        f'<div style="font-size:0.8rem;font-weight:500;color:#57606a;'
        f'text-transform:uppercase;letter-spacing:0.04em;">{label}</div>'
        f'<div style="font-size:1.75rem;font-weight:700;color:#0d1117;'
        f'line-height:1.1;margin-top:4px;">{value}</div>'
        f'{delta_html}'
        f'</div>'
        f'{icon_html}'
        f'</div>'
    )


# ─────────────────────────────────────────────────────────────────────────────
# CIRCULAR FAB
# ─────────────────────────────────────────────────────────────────────────────

def circular_fab() -> None:
    """Render the fixed blue floating action button (bottom-right)."""
    st.markdown(
        """
        <div class="ascenda-fab" role="button" aria-label="New Visit" tabindex="0">
          <svg aria-hidden="true" width="22" height="22" fill="none" stroke="#fff" stroke-width="2.5"
               viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
        </div>
        """,
        unsafe_allow_html=True,
    )
