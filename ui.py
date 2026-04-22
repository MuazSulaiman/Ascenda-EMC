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
    """Populate st.session_state['client_ip'] and ['user_agent'] from the browser."""
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

            u = get_user_by_email(em)
            if not u:
                st.error("User not found.")
                return

            if not bool(u.get("is_active", True)):
                st.error("Your account is inactive. Please contact the administrator.")
                return

            if not pbkdf2_sha256.verify(pw, u["password_hash"]):
                st.error("Invalid password.")
                return

            st.session_state.user = u
            sid = create_session(int(u["user_id"]), u.get("role"))
            set_url_session_param(sid)

            st.session_state["_current_page"] = "Dashboard"
            set_url_param("page", "Dashboard")

            st.success(f"Welcome, {u.get('name') or u.get('email')}!")
            st.rerun()


def logout_button():
    if st.sidebar.button("Logout"):
        # Clear submit page geo state when logging out
        _reset_location_state_for_page("submit_visit")

        sid = st.query_params.get("sid")
        if sid:
            delete_session(sid)
        set_url_session_param(None)
        st.session_state.user = None
        st.session_state.pop("_current_page", None)
        st.session_state.pop("__current_page", None)  # ensure page tracker is cleared
        set_url_param("page", None)
        st.rerun()


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
            padding: 0.75rem 1rem 0.5rem !important;
        }

        section[data-testid="stSidebar"] h1,
        section[data-testid="stSidebar"] h2,
        section[data-testid="stSidebar"] h3 {
            margin-top: 1rem !important; margin-bottom: 0.2rem !important;
            padding: 0 4px !important; font-size: 0.72rem !important;
            font-weight: 700 !important; color: #8b949e !important;
            text-transform: uppercase !important; letter-spacing: 0.07em !important;
        }

        /* Remove radio circle in sidebar only */
        section[data-testid="stSidebar"] div[role="radiogroup"] > label > div:first-child {
            display: none !important;
        }

        section[data-testid="stSidebar"] div[role="radiogroup"] > label {
            display: flex !important; align-items: center !important;
            width: 100% !important; padding: 7px 14px !important; margin: 1px 0 !important;
            border-radius: 10px !important; cursor: pointer !important;
            font-size: 0.9rem !important; font-weight: 500 !important;
            color: #57606a !important; box-sizing: border-box !important;
            border-left: 3px solid transparent !important;
            transition: background 0.15s ease, color 0.15s ease !important;
        }
        section[data-testid="stSidebar"] div[role="radiogroup"] > label:hover {
            background: #f6f8fa !important; color: #0d1117 !important;
        }
        section[data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked) {
            background: #eef2ff !important; border-left: 3px solid #2667ff !important;
            color: #2667ff !important; font-weight: 600 !important;
        }

        .sidebar-user-footer {
            border-top: 1px solid #e4e8ec; padding: 12px 14px 10px;
            display: flex; align-items: center; gap: 10px; margin-top: 0.75rem;
            cursor: pointer; transition: background 0.15s ease;
            border-radius: 0 0 8px 8px;
        }
        .sidebar-user-footer:hover { background: #f6f8fa; }
        .sidebar-user-avatar {
            width: 34px; height: 34px; border-radius: 50%; background: #2667ff;
            display: flex; align-items: center; justify-content: center;
            font-size: 0.8rem; font-weight: 700; color: #fff; flex-shrink: 0;
        }
        .sidebar-user-name {
            font-size: 0.875rem; font-weight: 600; color: #0d1117;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 140px;
        }
        .sidebar-user-meta {
            font-size: 0.72rem; color: #8b949e;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 140px;
        }
        /* Sign out button */
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

    # ── Logo ─────────────────────────────────────────────────────────────────
    logo_b64 = get_logo_base64()
    if logo_b64:
        st.sidebar.markdown(
            f'<div class="ascenda-logo-wrap">'
            f'<img src="data:image/png;base64,{logo_b64}" alt="Ascenda"'
            f' style="width:150px;height:auto;" /></div>',
            unsafe_allow_html=True,
        )
    else:
        st.sidebar.markdown("### Ascenda")


    # ── User & role ───────────────────────────────────────────────────────────
    user = st.session_state.get("user")
    role = (user.get("role") if user else "").lower().strip()

    # ── Badge counts (cached 60 s) ────────────────────────────────────────────
    mv_count = 0
    cr_pending = 0
    uid = (user.get("user_id") or user.get("id")) if user else None
    if uid:
        if time.time() - st.session_state.get("_nav_counts_ts", 0) > 60:
            try:
                from db_ops import query_df as _qdf
                _r = _qdf("SELECT COUNT(*) AS cnt FROM visits WHERE user_id = :u", {"u": int(uid)})
                st.session_state["_nav_mv_count"] = int(_r.iloc[0]["cnt"]) if not _r.empty else 0
                if (user.get("role") or "").lower().strip() == "admin":
                    _cr = _qdf("SELECT COUNT(*) AS cnt FROM request_changes WHERE status = 'IN_REVIEW'")
                    st.session_state["_nav_cr_pending"] = int(_cr.iloc[0]["cnt"]) if not _cr.empty else 0
            except Exception:
                pass
            st.session_state["_nav_counts_ts"] = time.time()
        mv_count = st.session_state.get("_nav_mv_count", 0)
        cr_pending = st.session_state.get("_nav_cr_pending", 0)

    # ── Build page list ───────────────────────────────────────────────────────
    st.sidebar.markdown("### MAIN")
    pages = ["Dashboard", "Submit Visit", "Check-In", "My Visits"]

    if role == "rep":
        pages += ["Active Projects", "Visit Change Requests"]
    if role == "maintenance":
        pages += ["Active Projects"]
    if role in ("sales manager", "biomedical manager"):
        pages += ["Project Creation", "Project Management", "Active Projects", "Visit Change Requests"]
    if role == "admin":
        pages += [
            "Project Creation",
            "Project Management",
            "Active Projects",
            "Admin: Import Lookups",
            "Admin: Data Browser",
            "Admin: Users",
            "Review Target Audiences",
            "Review Other Customers",
            "Visit Change Requests",
            "Review Change Requests",
        ]

    # ── Resolve current page ──────────────────────────────────────────────────
    url_page = get_url_param("page")
    sess_page = st.session_state.get("_current_page")

    _on_settings = (
        st.session_state.get("_goto_user_settings", False)
        or url_page == "User Settings"
    )

    if _on_settings:
        idx = None
        current = sess_page if sess_page in pages else pages[0]
    elif url_page in pages:
        current = url_page
        idx = pages.index(current)
    elif sess_page in pages:
        current = sess_page
        idx = pages.index(current)
    else:
        current = pages[0]
        idx = 0

    # ── Format function: append live count to My Visits label ─────────────────
    def _fmt(page: str) -> str:
        return page

    # ── Navigation radio ──────────────────────────────────────────────────────
    def _on_change():
        old = st.session_state.get("_current_page")
        chosen = st.session_state["_nav_choice"]
        st.session_state["_current_page"] = chosen
        set_url_param("page", chosen)
        if old == "Submit Visit" and chosen != "Submit Visit":
            _reset_location_state_for_page("submit_visit")

    choice = st.sidebar.radio(
        "",
        pages,
        index=idx,
        key="_nav_choice",
        on_change=_on_change,
        label_visibility="collapsed",
        format_func=_fmt,
    )

    # ── Navigation bypass: User Settings (not in radio list) ─────────────────
    _goto_settings = bool(st.session_state.pop("_goto_user_settings", False)) or _on_settings

    if not _goto_settings:
        if st.session_state.get("_current_page") != choice:
            st.session_state["_current_page"] = choice
        if get_url_param("page") != choice:
            set_url_param("page", choice)

    # ── User profile footer ───────────────────────────────────────────────────
    if user:
        _name = user.get("name") or user.get("email") or "User"
        _role = user.get("role") or ""
        _region = user.get("region") or ""
        _initials = "".join(w[0].upper() for w in (_name or "U").split()[:2])
        _sid = st.query_params.get("sid", "")
        _settings_href = f"?page=User+Settings&sid={_sid}" if _sid else "?page=User+Settings"
        st.sidebar.markdown(
            f'<a href="{_settings_href}" target="_self" style="text-decoration:none;display:block;">'
            f'<div class="sidebar-user-footer">'
            f'<div class="sidebar-user-avatar">{_initials}</div>'
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
            _reset_location_state_for_page("submit_visit")
            sid = st.query_params.get("sid")
            if sid:
                delete_session(sid)
            set_url_session_param(None)
            st.session_state.user = None
            st.session_state.pop("_current_page", None)
            st.session_state.pop("__current_page", None)
            set_url_param("page", None)
            st.rerun()

    if _goto_settings:
        st.session_state["_current_page"] = "User Settings"
        return "User Settings"
    return choice


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
    logo_b64 = get_almadar_logo_base64()
    logo_html = (
        f'<img src="data:image/png;base64,{logo_b64}" style="height:36px;opacity:0.7;" />'
        if logo_b64 else '<strong style="color:#57606a;">Al Madar Medical Co.</strong>'
    )
    st.markdown(
        f"""
        <div style="margin-top:2.5rem;padding-top:1.25rem;
                    border-top:1px solid #e4e8ec;
                    display:flex;justify-content:space-between;
                    align-items:center;flex-wrap:wrap;gap:8px;">
          <div>{logo_html}</div>
          <div style="text-align:right;font-size:0.8rem;color:#8b949e;line-height:1.6;">
            © 2025 Al Madar Medical Co. &nbsp;·&nbsp;
            Core System © Muaz Sulaiman &nbsp;·&nbsp;
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
    icon_svg: str = "",
    icon_bg: str = "#eef2ff",
) -> str:
    """Return HTML for a KPI card with a right-aligned colored icon circle."""
    delta_color = "#0e8a4f" if delta_positive else "#c83333"
    arrow_up = (
        '<svg aria-hidden="true" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.5" '
        'viewBox="0 0 24 24"><polyline points="18 15 12 9 6 15"/></svg>'
    )
    arrow_dn = (
        '<svg aria-hidden="true" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.5" '
        'viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>'
    )
    delta_html = (
        f'<div style="margin-top:5px;font-size:0.8rem;font-weight:500;color:{delta_color};'
        f'display:flex;align-items:center;gap:3px;">'
        f'{"" + arrow_up if delta_positive else "" + arrow_dn}{delta}</div>'
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
