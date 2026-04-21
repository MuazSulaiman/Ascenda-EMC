# app_v11.py — Ascenda Sales Daily Feedback (orchestrator)
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image

from config import APP_TITLE
from theme import inject_theme
from ui import (
    apply_role_based_layout,
    capture_client_fingerprints,
    circular_fab,
    login_block,
    show_footer,
    sidebar_nav,
)
from auth import _ensure_sessions_table_exists, purge_expired_sessions, resolve_session_user
from app_pages.submit_visit import page_submit_visit
from app_pages.check_in import page_check_in
from app_pages.my_submissions import page_my_submissions
from app_pages.dashboard import page_dashboard
from app_pages.user_settings import page_user_settings
from app_pages.create_project import page_create_project
from app_pages.project_view import page_project_view
from app_pages.project_management import page_project_management
from app_pages.admin_import import page_admin_import
from app_pages.admin_data import page_admin_data
from app_pages.admin_users import page_admin_users
from app_pages.review_audiences import page_review_target_audiences
from app_pages.review_customers import page_review_other_customers
from app_pages.change_request import page_change_request
from app_pages.admin_change_requests import page_admin_change_requests

st.set_page_config(
    page_title=APP_TITLE,
    page_icon=Image.open("static/ascenda_180.png"),
    layout="centered",
)

inject_theme()

components.html("""
<script>
(function() {
  const head = document.head;
  function add(tag, attrs) {
    const el = document.createElement(tag);
    Object.entries(attrs).forEach(([k,v]) => el.setAttribute(k, v));
    head.appendChild(el);
  }
  add('link', { rel: 'apple-touch-icon', href: '/static/ascenda_180.png' });
  add('meta', { name: 'apple-mobile-web-app-capable', content: 'yes' });
  add('meta', { name: 'apple-mobile-web-app-title', content: 'Ascenda' });
  add('link', { rel: 'manifest', href: '/static/manifest.webmanifest' });
  add('meta', { name: 'theme-color', content: '#2667ff' });
  add('link', { rel: 'icon', type: 'image/png', sizes: '192x192', href: '/static/ascenda_192.png' });
})();
</script>
""", height=0)

st.markdown("""
<style>
/* ── Visit card hover state ── */
.ascenda-visit-card:hover {
    border-color: #c9d1d9 !important;
    box-shadow: 0 2px 8px rgba(15,23,42,0.06) !important;
}

/* ── Page background ── */
[data-testid="stAppViewContainer"] { background: #fafbfc !important; }
[data-testid="stAppViewContainer"] .block-container {
    padding-top: 1.5rem !important;
    padding-bottom: 2rem !important;
    background: #ffffff;
    border-radius: 14px;
    box-shadow: 0 1px 2px rgba(15,23,42,0.04), 0 0 0 1px #e4e8ec;
    padding-left: 1.75rem !important;
    padding-right: 1.75rem !important;
}

/* ── Floating action button ── */
.ascenda-fab {
    position: fixed; bottom: 20px; right: 20px;
    width: 52px; height: 52px; border-radius: 50%;
    background: #2667ff; display: flex; align-items: center; justify-content: center;
    box-shadow: 0 4px 16px rgba(38,103,255,0.35);
    cursor: pointer; z-index: 999; transition: background 0.15s ease, box-shadow 0.15s ease;
}
.ascenda-fab:hover { background: #1a50d4; box-shadow: 0 6px 20px rgba(38,103,255,0.45); }

/* ── Radio groups as pills in main content (filter tabs, mode selectors) ── */
[data-testid="stAppViewContainer"] div[role="radiogroup"] {
    display: flex !important; flex-wrap: wrap !important; gap: 4px !important;
}
[data-testid="stAppViewContainer"] div[role="radiogroup"] > label {
    border: 1px solid #e4e8ec !important; border-radius: 20px !important;
    padding: 4px 14px !important; font-size: 0.82rem !important;
    font-weight: 500 !important; background: transparent !important;
    color: #57606a !important; border-left: 1px solid #e4e8ec !important;
    min-width: 0 !important; margin: 0 !important; cursor: pointer !important;
    transition: background 0.15s ease, color 0.15s ease !important;
}
[data-testid="stAppViewContainer"] div[role="radiogroup"] > label > div:first-child {
    display: none !important;
}
[data-testid="stAppViewContainer"] div[role="radiogroup"] > label:has(input:checked) {
    background: #eef2ff !important; border-color: #2667ff !important;
    color: #2667ff !important; font-weight: 600 !important;
}
[data-testid="stAppViewContainer"] div[role="radiogroup"] > label:hover:not(:has(input:checked)) {
    background: #f6f8fa !important; color: #0d1117 !important;
}

/* ── Hide heading anchor links ── */
[data-testid="stHeading"] a,
.stMarkdown h1 a, .stMarkdown h2 a, .stMarkdown h3 a,
.stMarkdown h4 a, .stMarkdown h5 a, .stMarkdown h6 a {
    display: none !important; visibility: hidden !important; pointer-events: none !important;
}

/* ── Typography ── */
.stMarkdown h1 { font-size: 1.75rem !important; font-weight: 700 !important;
    letter-spacing: -0.02em; color: #0d1117 !important; }
.stMarkdown h2 { font-size: 1.375rem !important; font-weight: 600 !important;
    letter-spacing: -0.01em; color: #0d1117 !important; }
.stMarkdown h3 { font-size: 1.125rem !important; font-weight: 600 !important;
    color: #0d1117 !important; }

/* ── Primary buttons ── */
[data-testid="stButton"] > button[kind="primary"],
[data-testid="stFormSubmitButton"] > button {
    background: #2667ff !important; border: none !important;
    border-radius: 10px !important; font-weight: 600 !important;
    padding: 0.5rem 1.25rem !important;
    transition: background 0.15s ease, box-shadow 0.15s ease !important;
}
[data-testid="stButton"] > button[kind="primary"]:hover,
[data-testid="stFormSubmitButton"] > button:hover {
    background: #1a50d4 !important;
    box-shadow: 0 2px 8px rgba(38,103,255,0.25) !important;
}

/* ── Secondary buttons ── */
[data-testid="stButton"] > button[kind="secondary"] {
    background: #ffffff !important; border: 1px solid #e4e8ec !important;
    border-radius: 10px !important; color: #0d1117 !important;
    font-weight: 500 !important;
    transition: background 0.15s ease, border-color 0.15s ease !important;
}
[data-testid="stButton"] > button[kind="secondary"]:hover {
    background: #f6f8fa !important; border-color: #c9d1d9 !important;
}

/* ── Inputs ── */
[data-testid="stTextInput"] input,
[data-testid="stTextArea"] textarea,
[data-testid="stNumberInput"] input {
    background: #f6f8fa !important; border: 1px solid #e4e8ec !important;
    border-radius: 10px !important; color: #0d1117 !important;
    font-size: 0.9375rem !important;
    transition: border-color 0.15s ease, box-shadow 0.15s ease !important;
}
[data-testid="stTextInput"] input:focus,
[data-testid="stTextArea"] textarea:focus {
    background: #ffffff !important; border-color: #2667ff !important;
    box-shadow: 0 0 0 3px rgba(38,103,255,0.12) !important; outline: none !important;
}

/* ── Select / multiselect ── */
[data-testid="stSelectbox"] > div > div,
[data-testid="stMultiSelect"] > div > div {
    background: #f6f8fa !important; border: 1px solid #e4e8ec !important;
    border-radius: 10px !important;
}

/* ── Forms ── */
[data-testid="stForm"] {
    border: 1px solid #e4e8ec !important; border-radius: 14px !important;
    padding: 1.25rem 1.5rem !important; background: #ffffff !important;
}

/* ── Alerts ── */
[data-testid="stAlert"][data-baseweb="notification"][kind="success"],
div[data-testid="stAlert"].stSuccess {
    background: #e6f6ec !important; border-color: #0e8a4f !important;
    border-radius: 10px !important; color: #0e8a4f !important;
}
[data-testid="stAlert"][data-baseweb="notification"][kind="error"],
div[data-testid="stAlert"].stError {
    background: #fdeceb !important; border-color: #c83333 !important;
    border-radius: 10px !important; color: #c83333 !important;
}
[data-testid="stAlert"][data-baseweb="notification"][kind="warning"],
div[data-testid="stAlert"].stWarning {
    background: #fdf2e4 !important; border-color: #b5651d !important;
    border-radius: 10px !important; color: #b5651d !important;
}
[data-testid="stAlert"][data-baseweb="notification"][kind="info"],
div[data-testid="stAlert"].stInfo {
    background: #e8f4fd !important; border-color: #1565c0 !important;
    border-radius: 10px !important;
}

/* ── Metric / KPI ── */
[data-testid="stMetric"] {
    background: #ffffff !important; border: 1px solid #e4e8ec !important;
    border-radius: 14px !important; padding: 1rem 1.25rem !important;
    box-shadow: 0 1px 2px rgba(15,23,42,0.04) !important;
}
[data-testid="stMetricValue"] {
    font-size: 1.75rem !important; font-weight: 700 !important; color: #0d1117 !important;
}
[data-testid="stMetricLabel"] {
    font-size: 0.8rem !important; color: #57606a !important;
    font-weight: 500 !important; text-transform: uppercase !important;
    letter-spacing: 0.04em !important;
}

/* ── Dataframe ── */
[data-testid="stDataFrame"] {
    border: 1px solid #e4e8ec !important; border-radius: 10px !important;
    overflow: hidden !important;
}

/* ── Expander ── */
[data-testid="stExpander"] {
    border: 1px solid #e4e8ec !important; border-radius: 10px !important;
    background: #ffffff !important;
}
</style>
""", unsafe_allow_html=True)

capture_client_fingerprints()

# Session bootstrap (runs once per cold start)
_ensure_sessions_table_exists()
if "user" not in st.session_state:
    purge_expired_sessions()
    st.session_state.user = resolve_session_user()

user = st.session_state.get("user")

if not user:
    login_block()
    show_footer()
else:
    apply_role_based_layout()
    page = sidebar_nav()

    PAGE_MAP = {
        "Dashboard":                page_dashboard,
        "Submit Visit":             page_submit_visit,
        "Check-In":                 page_check_in,
        "My Visits":                page_my_submissions,
        "User Settings":            page_user_settings,
        "Project Creation":         page_create_project,
        "Active Projects":          page_project_view,
        "Project Management":       page_project_management,
        "Admin: Import Lookups":    page_admin_import,
        "Admin: Data Browser":      page_admin_data,
        "Admin: Users":             page_admin_users,
        "Review Target Audiences":  page_review_target_audiences,
        "Review Other Customers":   page_review_other_customers,
        "Visit Change Requests":    page_change_request,
        "Review Change Requests":   page_admin_change_requests,
    }

    fn = PAGE_MAP.get(page)
    if fn:
        fn()
    else:
        st.warning(f"Unknown page: {page}")

    circular_fab()
    show_footer()
