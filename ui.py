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
    [data-testid="stAppViewContainer"] { background: #f0f4ff !important; }
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

        st.markdown("""
        <div style="background:#fff;border:1px solid #e4e8ec;border-radius:14px;
                    padding:2rem 2.25rem 1.25rem;
                    box-shadow:0 4px 12px rgba(15,23,42,0.08);">
          <h2 style="margin:0 0 1.5rem;font-size:1.375rem;font-weight:700;
                     color:#0d1117;text-align:center;letter-spacing:-0.01em;">
            Sign in to Ascenda
          </h2>
        </div>
        """, unsafe_allow_html=True)

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

        st.session_state["_current_page"] = "Submit Visit"
        set_url_param("page", "Submit Visit")

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
    # -----------------------------
    # Sidebar CSS (tight layout + no circles + highlight selected)
    # -----------------------------
    st.sidebar.markdown(
        """
        <style>
        /* ── Sidebar shell ── */
        section[data-testid="stSidebar"] {
            background: #ffffff !important;
            border-right: 1px solid #e4e8ec !important;
        }
        section[data-testid="stSidebar"] > div:first-child {
            padding-top: 0.5rem !important;
        }

        /* ── Logo area ── */
        .ascenda-logo-wrap {
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 0.75rem 1rem 0.5rem !important;
        }

        /* ── Sidebar title spacing ── */
        section[data-testid="stSidebar"] h1,
        section[data-testid="stSidebar"] h2,
        section[data-testid="stSidebar"] h3 {
            margin-top: 0.2rem !important;
            margin-bottom: 0.5rem !important;
            padding: 0 !important;
            font-size: 0.75rem !important;
            font-weight: 600 !important;
            color: #8b949e !important;
            text-transform: uppercase !important;
            letter-spacing: 0.06em !important;
        }

        /* ── Remove radio circle ── */
        div[role="radiogroup"] > label > div:first-child {
            display: none !important;
        }

        /* ── Nav item base ── */
        div[role="radiogroup"] > label {
            display: flex !important;
            align-items: center !important;
            width: 100% !important;
            padding: 7px 14px !important;
            margin: 1px 0 !important;
            border-radius: 8px !important;
            cursor: pointer !important;
            font-size: 0.9rem !important;
            font-weight: 500 !important;
            color: #57606a !important;
            box-sizing: border-box !important;
            border-left: 3px solid transparent !important;
            transition: background 0.15s ease, color 0.15s ease !important;
        }

        /* ── Hover ── */
        div[role="radiogroup"] > label:hover {
            background: #f6f8fa !important;
            color: #0d1117 !important;
        }

        /* ── Active / selected ── */
        div[role="radiogroup"] > label:has(input:checked) {
            background: #eef2ff !important;
            border-left: 3px solid #2667ff !important;
            color: #2667ff !important;
            font-weight: 600 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # -----------------------------
    # Logo from static (base64)
    # -----------------------------
    logo_b64 = get_logo_base64()
    if logo_b64:
        st.sidebar.markdown(
            f"""
            <div class="ascenda-logo-wrap">
                <img src="data:image/png;base64,{logo_b64}"
                     alt="Ascenda"
                     style="width:150px; height:auto;" />
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.sidebar.markdown("### Ascenda")

    # -----------------------------
    # Navigation title (tighter)
    # -----------------------------
    st.sidebar.markdown(" ")
    st.sidebar.markdown("### Navigation")

    user = st.session_state.get("user")
    role = (user.get("role") if user else "").lower().strip()

    # Base pages
    pages = ["Submit Visit", "Check-In", "My Submissions"]

    if role == "rep":
        pages += ["Projects View", "User Settings"]

    if role == "maintenance":
        pages += ["Projects View", "User Settings"]

    if role == "sales manager":
        pages += ["Project Creation", "Project Management", "Projects View", "User Settings"]

    if role == "biomedical manager":
        pages += ["Project Creation", "Project Management", "Projects View", "User Settings"]

    if role == "admin":
        pages += [
            "Project Creation",
            "Project Management",
            "Projects View",
            "Admin: Import Lookups",
            "Admin: Data Browser",
            "Admin: Users",
            "Review Target Audiences",
            "Review Other Customers",
            "Visit Change Requests",
            "User Settings",
        ]

    # -----------------------------
    # Resolve current page
    # -----------------------------
    url_page = get_url_param("page")
    sess_page = st.session_state.get("_current_page")

    if url_page in pages:
        current = url_page
    elif sess_page in pages:
        current = sess_page
    else:
        current = pages[0]

    idx = pages.index(current)

    # -----------------------------
    # Navigation radio (styled as menu)
    # -----------------------------
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
    )

    # Sync state <-> URL
    if st.session_state.get("_current_page") != choice:
        st.session_state["_current_page"] = choice
    if get_url_param("page") != choice:
        set_url_param("page", choice)

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


def section_header(title: str, subtitle: str = "") -> None:
    """Render an artifact-style page section header."""
    sub_html = (
        f'<p style="margin:4px 0 0;font-size:0.9rem;color:#57606a;">{subtitle}</p>'
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
            f'<span style="margin-top:4px;font-size:0.7rem;white-space:nowrap;{text}">{label}</span>'
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
          <div style="text-align:right;font-size:0.78rem;color:#8b949e;line-height:1.6;">
            © 2025 Al Madar Medical Co. &nbsp;·&nbsp;
            Core System © Muaz Sulaiman &nbsp;·&nbsp;
            Version 13
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
