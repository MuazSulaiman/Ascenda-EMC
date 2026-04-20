# theme.py — inject design tokens and fonts once per app load
import streamlit.components.v1 as components


def inject_theme():
    components.html("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Inter+Tight:wght@400;500;600;700&family=Fira+Code:wght@400;500&display=swap');
</style>
<script>
(function() {
  var s = document.createElement('style');
  s.textContent = [
    ':root {',
    '  --color-primary: #2667ff;',
    '  --color-primary-hover: #1a50d4;',
    '  --color-primary-subtle: #eef2ff;',
    '  --color-bg: #fafbfc;',
    '  --color-surface: #ffffff;',
    '  --color-surface-2: #f6f8fa;',
    '  --color-border: #e4e8ec;',
    '  --color-border-strong: #c9d1d9;',
    '  --color-text: #0d1117;',
    '  --color-text-muted: #57606a;',
    '  --color-text-subtle: #8b949e;',
    '  --status-success-bg: #e6f6ec; --status-success-text: #0e8a4f;',
    '  --status-warning-bg: #fdf2e4; --status-warning-text: #b5651d;',
    '  --status-danger-bg: #fdeceb;  --status-danger-text: #c83333;',
    '  --status-info-bg: #e8f4fd;    --status-info-text: #1565c0;',
    '  --status-neutral-bg: #f0f0f0; --status-neutral-text: #444444;',
    '  --font-body: "Inter", system-ui, sans-serif;',
    '  --font-display: "Inter Tight", "Inter", sans-serif;',
    '  --font-mono: "Fira Code", ui-monospace, monospace;',
    '  --radius-sm: 6px;',
    '  --radius-md: 10px;',
    '  --radius-lg: 14px;',
    '  --shadow-card: 0 1px 2px rgba(15,23,42,0.04), 0 0 0 1px #e4e8ec;',
    '  --shadow-elevated: 0 4px 12px rgba(15,23,42,0.08);',
    '}',
    '* { font-family: var(--font-body) !important; box-sizing: border-box; }',
    'h1, h2, h3, [data-testid="stHeading"] {',
    '  font-family: var(--font-display) !important;',
    '  color: var(--color-text) !important;',
    '}',
  ].join('\n');
  document.head.appendChild(s);
})();
</script>
""", height=0)
