# theme.py — inject design tokens and theme switcher once per app load
import streamlit.components.v1 as components


def inject_theme():
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
    'h1,h2,h3,[data-testid="stHeading"] { font-family: var(--font-display) !important; color: var(--color-text) !important; }',

    'html[data-theme="dark"] [data-testid="stAppViewContainer"] { background: var(--color-bg) !important; }',
    'html[data-theme="dark"] .block-container { background: var(--color-bg) !important; }',
    'html[data-theme="dark"] section[data-testid="stSidebar"] { background: var(--color-surface) !important; border-right: 1px solid var(--color-border) !important; }',
    'html[data-theme="dark"] p { color: var(--color-text) !important; }',
    'html[data-theme="dark"] label { color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-testid="stMarkdown"] { color: var(--color-text) !important; }',
    'html[data-theme="dark"] h1, html[data-theme="dark"] h2, html[data-theme="dark"] h3 { color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-baseweb="input"] input { background: var(--color-surface-2) !important; color: var(--color-text) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-baseweb="input"] { background: var(--color-surface-2) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-baseweb="base-input"] { background: var(--color-surface-2) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-baseweb="textarea"] { background: var(--color-surface-2) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-baseweb="textarea"] textarea { background: var(--color-surface-2) !important; color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-baseweb="select"] > div { background: var(--color-surface-2) !important; border-color: var(--color-border) !important; color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-baseweb="select"] span { color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-baseweb="menu"] { background: var(--color-surface) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-baseweb="menu"] li { color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-baseweb="menu"] li:hover { background: var(--color-surface-2) !important; }',
    'html[data-theme="dark"] [data-testid="stForm"] { background: var(--color-surface) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] hr, html[data-theme="dark"] [data-testid="stDivider"] { border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-testid="stExpander"] { background: var(--color-surface) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-testid="stExpander"] summary { color: var(--color-text) !important; }',
    'html[data-theme="dark"] .stButton > button:not([data-testid="baseButton-primary"]) { background: var(--color-surface-2) !important; color: var(--color-text) !important; border-color: var(--color-border) !important; }',
    'html[data-theme="dark"] [data-testid="stDataFrame"] { background: var(--color-surface) !important; color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-testid="stTable"] { background: var(--color-surface) !important; color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-testid="stMetricValue"] { color: var(--color-text) !important; }',
    'html[data-theme="dark"] [data-testid="stMetricLabel"] { color: var(--color-text-muted) !important; }',
  ].join('\\n');
  doc.head.appendChild(s);

  /* ── 2. Apply theme from localStorage or OS preference ── */
  function applyTheme(val) {
    doc.documentElement.setAttribute('data-theme', val || 'light');
  }
  var stored = null;
  try { stored = localStorage.getItem('_ascenda_theme'); } catch (e) {}
  if (stored === 'dark' || stored === 'light') {
    applyTheme(stored);
  } else {
    applyTheme(window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  }

  /* ── 3. Listen for OS preference changes (only when no manual override) ── */
  try {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function (e) {
      try {
        if (!localStorage.getItem('_ascenda_theme')) {
          applyTheme(e.matches ? 'dark' : 'light');
        }
      } catch (ex) {}
    });
  } catch (e) {}
})();
</script>
""", height=0)
