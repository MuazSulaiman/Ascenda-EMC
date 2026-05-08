# theme.py — inject design tokens and theme switcher once per app load
import streamlit as st
import streamlit.components.v1 as components


def inject_theme():
    # ── Immediate CSS (server-side, no iframe delay) ──────────────────────────
    # Applies dark background instantly on page load if OS prefers dark,
    # and adds a transition so any residual flash is a smooth fade.
    # The JS block below then refines with the user's saved preference.
    st.markdown("""
<style>
/* Hide the page until data-theme is set by JS — eliminates white flash.
   The animation is a hard 1s fallback in case the script is slow/blocked. */
@keyframes _ascenda_reveal { to { opacity: 1; } }

[data-testid="stAppViewContainer"] {
    opacity: 0;
    animation: _ascenda_reveal 0s 1.5s forwards;
}
html[data-theme] [data-testid="stAppViewContainer"] {
    opacity: 1;
    animation: none;
    transition: opacity 0.12s ease;
}
</style>
""", unsafe_allow_html=True)

    components.html("""
<script>
(function () {
  var doc = document;
  try { if (window.parent !== window) doc = window.parent.document; } catch (e) {}

  /* ── 1. CSS tokens (light + dark) + native Streamlit dark overrides ── */
  /* ── Load fonts via <link> (avoids @import-in-textContent browser drop) ── */
  if (!doc.getElementById('_ascenda_fonts')) {
    var lnk = doc.createElement('link');
    lnk.id = '_ascenda_fonts';
    lnk.rel = 'stylesheet';
    lnk.href = 'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Inter+Tight:wght@400;500;600;700&family=Fira+Code:wght@400;500&display=swap';
    doc.head.appendChild(lnk);
  }
  if (!doc.getElementById('_ascenda_material_symbols')) {
    var ms = doc.createElement('link');
    ms.id = '_ascenda_material_symbols';
    ms.rel = 'stylesheet';
    ms.href = 'https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200';
    doc.head.appendChild(ms);
  }

  var existing = doc.getElementById('_ascenda_tokens');
  if (existing) existing.remove();
  var s = doc.createElement('style');
  s.id = '_ascenda_tokens';
  s.textContent = [

    ':root {',
    '  --color-primary:        #2667ff;',
    '  --color-primary-hover:  #1a50d4;',
    '  --color-primary-subtle: #eef2ff;',
    '  --color-bg:             #fafbfc;',
    '  --color-surface:        #ffffff;',
    '  --color-surface-2:      #f6f8fa;',
    '  --color-border:         #e4e8ec;',
    '  --color-border-strong:  #c9d1d9;',
    '  --color-text:           #0d1117;',
    '  --color-text-muted:     #57606a;',
    '  --color-text-subtle:    #8b949e;',
    '  --status-success-bg:    #e6f6ec; --status-success-text: #0e8a4f;',
    '  --status-warning-bg:    #fdf2e4; --status-warning-text: #b5651d;',
    '  --status-danger-bg:     #fdeceb; --status-danger-text:  #c83333;',
    '  --status-info-bg:       #e8f4fd; --status-info-text:    #1565c0;',
    '  --status-neutral-bg:    #f0f0f0; --status-neutral-text: #444444;',
    '  --font-body:    "Inter", system-ui, sans-serif;',
    '  --font-display: "Inter Tight", "Inter", sans-serif;',
    '  --font-mono:    "Fira Code", ui-monospace, monospace;',
    '  --radius-sm: 6px; --radius-md: 10px; --radius-lg: 14px;',
    '  --shadow-card:     0 1px 2px rgba(15,23,42,0.04), 0 0 0 1px #e4e8ec;',
    '  --shadow-elevated: 0 4px 12px rgba(15,23,42,0.08);',
    '}',

    'html[data-theme="dark"] {',
    '  --color-primary:        #4d8ef0;',
    '  --color-primary-hover:  #3b7de8;',
    '  --color-primary-subtle: #1e2a4a;',
    '  --color-bg:             #0d1117;',
    '  --color-surface:        #161b22;',
    '  --color-surface-2:      #21262d;',
    '  --color-border:         #30363d;',
    '  --color-border-strong:  #484f58;',
    '  --color-text:           #e6edf3;',
    '  --color-text-muted:     #8b949e;',
    '  --color-text-subtle:    #6e7681;',
    '  --status-success-bg:    #0d2f1e; --status-success-text: #3fb950;',
    '  --status-warning-bg:    #2d1f0a; --status-warning-text: #d29922;',
    '  --status-danger-bg:     #2d0c0c; --status-danger-text:  #f85149;',
    '  --status-info-bg:       #0d1b2e; --status-info-text:    #79c0ff;',
    '  --status-neutral-bg:    #21262d; --status-neutral-text: #8b949e;',
    '  --shadow-card:     0 1px 2px rgba(0,0,0,0.3), 0 0 0 1px #30363d;',
    '  --shadow-elevated: 0 4px 12px rgba(0,0,0,0.4);',
    '}',

    '* { font-family: var(--font-body) !important; box-sizing: border-box; }',
    '[data-testid="stIconMaterial"] { font-family: "Material Symbols Rounded" !important; }',
    'h1,h2,h3,[data-testid="stHeading"] { font-family: var(--font-display) !important; color: var(--color-text) !important; }',

    'html[data-theme="dark"] [data-testid="stAppViewContainer"] { background: var(--color-bg) !important; }',
    'html[data-theme="dark"] .block-container { background: var(--color-bg) !important; }',
    'html[data-theme="dark"] section[data-testid="stSidebar"] { background: var(--color-surface) !important; border-right: 1px solid var(--color-border) !important; }',
    'html[data-theme="dark"] p { color: var(--color-text) !important; }',
    'html[data-theme="dark"] label { color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-testid="stMarkdown"] { color: var(--color-text) !important; }',
    'html[data-theme="dark"] h1, html[data-theme="dark"] h2, html[data-theme="dark"] h3 { color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-baseweb="input"] input { background: var(--color-surface-2) !important; color: var(--color-text) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-baseweb="input"] input::placeholder { color: var(--color-text-subtle) !important; opacity: 1 !important; }',
    'html[data-theme="dark"] [data-baseweb="input"] { background: var(--color-surface-2) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-baseweb="base-input"] { background: var(--color-surface-2) !important; border-color: var(--color-border) !important; color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-baseweb="base-input"] input { color: var(--color-text) !important; background: transparent !important; }',
    'html[data-theme="dark"] [data-baseweb="base-input"] input::placeholder { color: var(--color-text-subtle) !important; opacity: 1 !important; }',

    /* date inputs */
    'html[data-theme="dark"] [data-testid="stDateInput"] input { background: var(--color-surface-2) !important; color: var(--color-text) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-testid="stDateInput"] input::placeholder { color: var(--color-text-subtle) !important; opacity: 1 !important; }',
    'html[data-theme="dark"] [data-testid="stDateInput"] > div > div { background: var(--color-surface-2) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-testid="stDateInput"] label { color: var(--color-text) !important; }',

    /* date picker calendar popover */
    'html[data-theme="dark"] [data-baseweb="calendar"] { background: var(--color-surface) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-baseweb="calendar"] * { color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-baseweb="calendar"] [aria-selected="true"] > div { background: var(--color-primary) !important; }',
    'html[data-theme="dark"] [data-baseweb="calendar"] button:hover > div { background: var(--color-surface-2) !important; }',
    /* textarea */
    'html[data-theme="dark"] [data-baseweb="textarea"] { background: var(--color-surface-2) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-baseweb="textarea"] textarea { background: var(--color-surface-2) !important; color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-baseweb="textarea"] textarea::placeholder { color: var(--color-text-subtle) !important; opacity: 1 !important; }',
    'html[data-theme="dark"] [data-testid="stTextArea"] textarea { background: var(--color-surface-2) !important; color: var(--color-text) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-testid="stTextArea"] textarea::placeholder { color: var(--color-text-subtle) !important; opacity: 1 !important; }',
    'html[data-theme="dark"] [data-testid="stTextArea"] label { color: var(--color-text) !important; }',

    /* disabled / locked inputs — override Streamlit opacity fade */
    'html[data-theme="dark"] input:disabled { color: var(--color-text-muted) !important; -webkit-text-fill-color: var(--color-text-muted) !important; opacity: 1 !important; background: var(--color-surface) !important; }',
    'html[data-theme="dark"] [data-baseweb="input"]:has(input:disabled) { background: var(--color-surface) !important; border-color: var(--color-border) !important; opacity: 1 !important; }',
    'html[data-theme="dark"] [data-baseweb="base-input"]:has(input:disabled) { background: var(--color-surface) !important; opacity: 1 !important; }',
    'html[data-theme="dark"] [data-testid="stTextInput"]:has(input:disabled) label { color: var(--color-text-muted) !important; }',
    /* select trigger box */
    'html[data-theme="dark"] [data-baseweb="select"] > div { background: var(--color-surface-2) !important; border-color: var(--color-border) !important; color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-baseweb="select"] span { color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-baseweb="select"] svg { fill: var(--color-text-muted) !important; }',

    /* dropdown popover container (Streamlit renders this in a body-level portal) */
    'html[data-theme="dark"] [data-baseweb="popover"] { background: var(--color-surface) !important; border: 1px solid var(--color-border) !important; box-shadow: 0 8px 24px rgba(0,0,0,0.4) !important; }',
    'html[data-theme="dark"] [data-baseweb="popover"] * { background: var(--color-surface) !important; color: var(--color-text) !important; border-color: var(--color-border) !important; }',

    /* menu list and items */
    'html[data-theme="dark"] [data-baseweb="menu"] { background: var(--color-surface) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-baseweb="menu"] li { color: var(--color-text) !important; background: var(--color-surface) !important; }',
    'html[data-theme="dark"] [data-baseweb="menu"] li:hover { background: var(--color-primary-subtle) !important; color: var(--color-primary) !important; }',
    'html[data-theme="dark"] [data-baseweb="menu"] li[aria-selected="true"] { background: var(--color-primary-subtle) !important; color: var(--color-primary) !important; font-weight: 600 !important; }',

    /* listbox (role-based selectors for robustness) */
    'html[data-theme="dark"] ul[role="listbox"] { background: var(--color-surface) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] li[role="option"] { color: var(--color-text) !important; background: var(--color-surface) !important; }',
    'html[data-theme="dark"] li[role="option"]:hover { background: var(--color-primary-subtle) !important; color: var(--color-primary) !important; }',
    'html[data-theme="dark"] li[role="option"][aria-selected="true"] { background: var(--color-primary-subtle) !important; color: var(--color-primary) !important; font-weight: 600 !important; }',
    /* file uploader drag-and-drop zone */
    'html[data-theme="dark"] [data-testid="stFileUploader"] section { background: var(--color-surface-2) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-testid="stFileUploaderDropzone"] { background: var(--color-surface-2) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-testid="stFileUploaderDropzone"]:hover { background: var(--color-primary-subtle) !important; border-color: var(--color-primary) !important; }',
    'html[data-theme="dark"] [data-testid="stFileUploaderDropzone"] * { color: var(--color-text-muted) !important; }',
    'html[data-theme="dark"] [data-testid="stFileUploaderDropzone"] small { color: var(--color-text-subtle) !important; }',
    'html[data-theme="dark"] [data-testid="stFileUploaderDropzone"] svg { color: var(--color-text-muted) !important; opacity: 0.7 !important; }',

    'html[data-theme="dark"] [data-testid="stForm"] { background: var(--color-surface) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] hr, html[data-theme="dark"] [data-testid="stDivider"] { border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-testid="stExpander"] { background: var(--color-surface) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-testid="stExpander"] summary { color: var(--color-text) !important; background: var(--color-surface) !important; }',
    'html[data-theme="dark"] [data-testid="stExpander"] summary * { color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-testid="stExpander"] summary svg { stroke: var(--color-text) !important; fill: none !important; }',
    'html[data-theme="dark"] [data-testid="stExpander"] summary:hover { background: var(--color-surface-2) !important; }',
    'html[data-theme="dark"] .stButton > button:not([data-testid="baseButton-primary"]) { background: var(--color-surface-2) !important; color: var(--color-text) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-testid="stDataFrame"] { background: var(--color-surface) !important; color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-testid="stTable"] { background: var(--color-surface) !important; color: var(--color-text) !important; }',

    /* tabs — outer wrapper + content panel */
    'html[data-theme="dark"] [data-testid="stTabs"] { background: transparent !important; }',
    'html[data-theme="dark"] [data-testid="stTabsContent"] { background: var(--color-bg) !important; border-color: var(--color-border) !important; }',
    /* tab strip */
    'html[data-theme="dark"] [data-testid="stTabs"] [role="tablist"] { background: var(--color-bg) !important; border-bottom: 1px solid var(--color-border) !important; }',
    /* inactive tab */
    'html[data-theme="dark"] [data-testid="stTabs"] button[role="tab"] { color: var(--color-text-muted) !important; background: transparent !important; border-bottom: 2px solid transparent !important; }',
    /* active tab */
    'html[data-theme="dark"] [data-testid="stTabs"] button[role="tab"][aria-selected="true"] { color: var(--color-primary) !important; background: transparent !important; border-bottom: 2px solid var(--color-primary) !important; }',
    /* hover */
    'html[data-theme="dark"] [data-testid="stTabs"] button[role="tab"]:hover:not([aria-selected="true"]) { color: var(--color-text) !important; background: transparent !important; }',
    /* tab text / spans inside buttons */
    'html[data-theme="dark"] [data-testid="stTabs"] button[role="tab"] p, html[data-theme="dark"] [data-testid="stTabs"] button[role="tab"] span { color: inherit !important; }',

    /* multiselect selected tags */
    'html[data-theme="dark"] [data-baseweb="tag"] { background: var(--color-primary-subtle) !important; border-color: var(--color-primary) !important; }',
    'html[data-theme="dark"] [data-baseweb="tag"] span { color: var(--color-primary) !important; }',
    'html[data-theme="dark"] [data-baseweb="tag"] [role="presentation"] { color: var(--color-primary) !important; fill: var(--color-primary) !important; }',
    'html[data-theme="dark"] [data-testid="stMetricValue"] { color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-testid="stMetricLabel"] { color: var(--color-text-muted) !important; }',

    /* block-container border/shadow */
    'html[data-theme="dark"] [data-testid="stAppViewContainer"] .block-container { box-shadow: 0 1px 2px rgba(0,0,0,0.3), 0 0 0 1px var(--color-border) !important; }',

    /* radio pill filters */
    'html[data-theme="dark"] [data-testid="stAppViewContainer"] div[role="radiogroup"] > label { color: var(--color-text-muted) !important; border-color: var(--color-border) !important; background: transparent !important; }',
    'html[data-theme="dark"] [data-testid="stAppViewContainer"] div[role="radiogroup"] > label:has(input:checked) { background: var(--color-primary-subtle) !important; border-color: var(--color-primary) !important; color: var(--color-primary) !important; }',
    'html[data-theme="dark"] [data-testid="stAppViewContainer"] div[role="radiogroup"] > label:hover:not(:has(input:checked)) { background: var(--color-surface-2) !important; color: var(--color-text) !important; }',

    /* visit card hover */
    'html[data-theme="dark"] .ascenda-visit-card:hover { border-color: var(--color-border-strong) !important; box-shadow: 0 2px 8px rgba(0,0,0,0.25) !important; }',

    /* primary buttons */
    'html[data-theme="dark"] [data-testid="stBaseButton-primary"], html[data-theme="dark"] [data-testid="stButton"] > button[kind="primary"] { background: var(--color-primary) !important; color: #ffffff !important; border-color: var(--color-primary) !important; }',
    'html[data-theme="dark"] [data-testid="stBaseButton-primary"]:hover, html[data-theme="dark"] [data-testid="stButton"] > button[kind="primary"]:hover { background: var(--color-primary-hover) !important; border-color: var(--color-primary-hover) !important; }',

    /* secondary buttons */
    'html[data-theme="dark"] [data-testid="stButton"] > button[kind="secondary"], html[data-theme="dark"] [data-testid="stBaseButton-secondary"] { background: var(--color-surface-2) !important; border-color: var(--color-border) !important; color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-testid="stButton"] > button[kind="secondary"]:hover, html[data-theme="dark"] [data-testid="stBaseButton-secondary"]:hover { background: var(--color-surface) !important; border-color: var(--color-border-strong) !important; }',

    /* sign-out button — restore danger colours overridden by the general dark button rule */
    'html[data-theme="dark"] section[data-testid="stSidebar"] .stButton > button { background: transparent !important; color: var(--status-danger-text) !important; border-color: transparent !important; }',
    'html[data-theme="dark"] section[data-testid="stSidebar"] .stButton > button p, html[data-theme="dark"] section[data-testid="stSidebar"] .stButton > button span { color: var(--status-danger-text) !important; }',
    'html[data-theme="dark"] section[data-testid="stSidebar"] .stButton > button:hover { background: var(--status-danger-bg) !important; }',

    /* select/multiselect */
    'html[data-theme="dark"] [data-testid="stSelectbox"] > div > div, html[data-theme="dark"] [data-testid="stMultiSelect"] > div > div { background: var(--color-surface-2) !important; border-color: var(--color-border) !important; color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-testid="stMultiSelect"] [data-baseweb="select"] div, html[data-theme="dark"] [data-testid="stSelectbox"] [data-baseweb="select"] div { color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-testid="stMultiSelect"] input { color: var(--color-text) !important; background: transparent !important; -webkit-text-fill-color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-testid="stMultiSelect"] input::placeholder { color: var(--color-text-subtle) !important; opacity: 1 !important; }',
    'html[data-theme="dark"] [data-testid="stSelectbox"] input { color: var(--color-text) !important; -webkit-text-fill-color: var(--color-text) !important; }',

    /* toggle component — wrapper + label text */
    'html[data-theme="dark"] [data-testid="stToggle"] p { color: var(--color-text) !important; }',

    /* ── Toggle track: OFF state ─────────────────────────────────────────── */
    /* Covers every structural variant Streamlit may render (span or div,    */
    /* sibling-of-input or first-child-of-label).                            */

    /* dark mode OFF: visible grey pill */
    'html[data-theme="dark"] [data-testid="stToggle"] label > span:first-of-type { background: #484f58 !important; border-color: #484f58 !important; }',
    'html[data-theme="dark"] [data-testid="stToggle"] label > div:first-of-type { background: #484f58 !important; border-color: #484f58 !important; }',
    'html[data-theme="dark"] [data-testid="stToggle"] input[type="checkbox"] + span { background: #484f58 !important; border-color: #484f58 !important; }',
    'html[data-theme="dark"] [data-testid="stToggle"] input[type="checkbox"] + div { background: #484f58 !important; border-color: #484f58 !important; }',

    /* dark mode ON: primary blue — must come after the OFF rules */
    'html[data-theme="dark"] [data-testid="stToggle"] label:has(input:checked) > span:first-of-type { background: var(--color-primary) !important; border-color: var(--color-primary) !important; }',
    'html[data-theme="dark"] [data-testid="stToggle"] label:has(input:checked) > div:first-of-type { background: var(--color-primary) !important; border-color: var(--color-primary) !important; }',
    'html[data-theme="dark"] [data-testid="stToggle"] input[type="checkbox"]:checked + span { background: var(--color-primary) !important; border-color: var(--color-primary) !important; }',
    'html[data-theme="dark"] [data-testid="stToggle"] input[type="checkbox"]:checked + div { background: var(--color-primary) !important; border-color: var(--color-primary) !important; }',

    /* dark mode thumb — keep white in both states */
    'html[data-theme="dark"] [data-testid="stToggle"] label > span > span { background: #ffffff !important; }',
    'html[data-theme="dark"] [data-testid="stToggle"] label > div > div:first-child { background: #ffffff !important; }',
    'html[data-theme="dark"] [data-testid="stToggle"] input + span > span { background: #ffffff !important; }',
    'html[data-theme="dark"] [data-testid="stToggle"] input + div > div:first-child { background: #ffffff !important; }',

    /* light mode OFF — add visible border so grey pill reads on white background */
    '[data-testid="stToggle"] label > span:first-of-type { outline: 2px solid var(--color-border-strong) !important; outline-offset: -1px !important; }',
    '[data-testid="stToggle"] label > div:first-of-type { outline: 2px solid var(--color-border-strong) !important; outline-offset: -1px !important; }',
    '[data-testid="stToggle"] input[type="checkbox"] + span { outline: 2px solid var(--color-border-strong) !important; outline-offset: -1px !important; }',
    '[data-testid="stToggle"] input[type="checkbox"] + div { outline: 2px solid var(--color-border-strong) !important; outline-offset: -1px !important; }',

    /* container borders */
    'html[data-theme="dark"] [data-testid="stVerticalBlockBorderWrapper"] > div { border: 1px solid var(--color-border) !important; border-radius: var(--radius-md) !important; background: var(--color-surface) !important; }',

    /* sidebar collapse/expand arrow button */
    'html[data-theme="dark"] [data-testid="collapsedControl"] { background: var(--color-surface) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-testid="collapsedControl"] svg, html[data-theme="dark"] [data-testid="collapsedControl"] svg * { stroke: #e6edf3 !important; fill: #e6edf3 !important; color: #e6edf3 !important; }',

    /* sidebar nav — all SVG icons and arrows, light text in dark mode */
    'html[data-theme="dark"] section[data-testid="stSidebar"] svg { stroke: #e6edf3 !important; fill: #e6edf3 !important; color: #e6edf3 !important; }',
    'html[data-theme="dark"] section[data-testid="stSidebar"] svg path { stroke: #e6edf3 !important; fill: #e6edf3 !important; }',
    'html[data-theme="dark"] section[data-testid="stSidebar"] svg polyline { stroke: #e6edf3 !important; }',
    'html[data-theme="dark"] section[data-testid="stSidebar"] svg line { stroke: #e6edf3 !important; }',
  ].join('\\n');
  doc.head.appendChild(s);

  /* ── 2. Apply theme from localStorage or OS preference ── */
  function applyTheme(val) {
    doc.documentElement.setAttribute('data-theme', val || 'light');
  }
  var stored = null;
  try { stored = localStorage.getItem('_ascenda_theme'); } catch (e) {}
  /* Always default to light — OS preference is intentionally ignored.
     Dark mode is only activated when the user explicitly enables it in App Settings. */
  applyTheme(stored === 'dark' ? 'dark' : 'light');

  /* ── 4. Toggle track patcher ─────────────────────────────────────────────
     Streamlit emotion CSS uses generated class names with high specificity
     that CSS attribute selectors cannot beat. We locate the track as the
     immediate next sibling of the checkbox input and force colors inline.   */
  function patchToggles() {
    var isDark = doc.documentElement.getAttribute('data-theme') === 'dark';
    doc.querySelectorAll('[data-testid="stToggle"]').forEach(function (toggle) {
      var input = toggle.querySelector('input[type="checkbox"]');
      if (!input) return;
      var checked = input.checked;
      /* track = next element sibling of the checkbox */
      var track = input.nextElementSibling;
      /* fallback: first span/div in the label that is not the input */
      if (!track) {
        var label = toggle.querySelector('label');
        if (label) {
          var kids = label.children;
          for (var i = 0; i < kids.length; i++) {
            if (kids[i].tagName !== 'INPUT') { track = kids[i]; break; }
          }
        }
      }
      if (!track) return;
      if (isDark) {
        var col = checked ? '#4d8ef0' : '#6b7280';
        track.style.setProperty('background-color', col, 'important');
        track.style.setProperty('border-color', col, 'important');
      } else {
        track.style.removeProperty('background-color');
        track.style.removeProperty('border-color');
        if (!checked) {
          track.style.setProperty('box-shadow', 'inset 0 0 0 2px #c9d1d9', 'important');
        } else {
          track.style.removeProperty('box-shadow');
        }
      }
    });
  }

  /* ── 5. Required-field asterisk colouring ───────────────────────────────
     Streamlit renders label text as plain text inside <p>, so CSS alone
     cannot target the trailing *. This walker wraps it in a red <span>.   */
  function patchRequiredLabels() {
    doc.querySelectorAll('[data-testid="stWidgetLabel"] p').forEach(function (p) {
      if (p.dataset.reqPatched) return;
      var raw = p.textContent || '';
      if (!raw.trimEnd().endsWith('*')) return;
      var before = raw.trimEnd().slice(0, -1);
      p.textContent = '';
      p.appendChild(doc.createTextNode(before));
      var star = doc.createElement('span');
      star.style.cssText = 'color:#d00000;font-weight:700;';
      star.textContent = '*';
      p.appendChild(star);
      p.dataset.reqPatched = '1';
    });
  }

  /* run after Streamlit renders, and on any DOM mutation */
  setTimeout(patchToggles, 300);
  setTimeout(patchToggles, 900);
  setTimeout(patchRequiredLabels, 400);
  setTimeout(patchRequiredLabels, 1000);
  try {
    new MutationObserver(function () { patchToggles(); patchRequiredLabels(); }).observe(doc.body, {
      childList: true, subtree: true, attributes: true, attributeFilter: ['checked', 'data-theme']
    });
  } catch (e) {}
})();
</script>
""", height=0)
