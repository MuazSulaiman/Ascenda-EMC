# Ascenda UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adapt the Ascenda React artifact's design language (Geist fonts, #2667ff primary, white sidebar, frosted topbar, card system, badge system, stepper forms) into the existing Streamlit app via CSS injection and HTML components — without altering any Python business logic.

**Architecture:** All visual changes go through (a) a new `theme.py` CSS-injection module loaded once in `app_v11.py`, (b) targeted CSS overrides per page injected at the top of each page function, and (c) HTML helper functions in `ui.py` for badges, KPI cards, and section headers. Python logic in `app_pages/` is untouched.

**Tech Stack:** Streamlit CSS injection (`st.markdown(unsafe_allow_html=True)`), `streamlit.components.v1.html` for Google Fonts, Python f-strings for HTML card/badge helpers, existing PIL/base64 logo pipeline.

---

## Phase 2 — Artifact Design Language (Reference Tokens)

> These are extracted directly from the provided React artifact. All downstream tasks must use these exact values.

### Color Tokens

| Token | Value | Usage |
|---|---|---|
| `--color-primary` | `#2667ff` | Buttons, links, active nav, focus rings |
| `--color-primary-hover` | `#1a50d4` | Button hover, link hover |
| `--color-primary-subtle` | `#eef2ff` | Badge bg, chip bg, selected row bg |
| `--color-bg` | `#fafbfc` | Page background |
| `--color-surface` | `#ffffff` | Cards, sidebar, modals |
| `--color-surface-2` | `#f6f8fa` | Input bg, table row alt, disabled fields |
| `--color-border` | `#e4e8ec` | Card borders, dividers, input borders |
| `--color-border-strong` | `#c9d1d9` | Focused input border |
| `--color-text` | `#0d1117` | Body text |
| `--color-text-muted` | `#57606a` | Labels, captions, placeholders |
| `--color-text-subtle` | `#8b949e` | Disabled text, secondary captions |
| `--status-success-bg` | `#e6f6ec` | Success badge background |
| `--status-success-text` | `#0e8a4f` | Success badge text |
| `--status-warning-bg` | `#fdf2e4` | Warning badge background |
| `--status-warning-text` | `#b5651d` | Warning badge text |
| `--status-danger-bg` | `#fdeceb` | Danger/error badge background |
| `--status-danger-text` | `#c83333` | Danger badge text |
| `--status-info-bg` | `#e8f4fd` | Info badge background |
| `--status-info-text` | `#1565c0` | Info badge text |
| `--status-neutral-bg` | `#f0f0f0` | Neutral/draft badge background |
| `--status-neutral-text` | `#444444` | Neutral badge text |

### Typography Tokens

| Token | Value | Usage |
|---|---|---|
| `--font-body` | `'Geist', 'Inter', system-ui, sans-serif` | All body text, UI labels |
| `--font-display` | `'Inter Tight', 'Inter', sans-serif` | Page titles (h1, h2), card headings |
| `--font-mono` | `'Geist Mono', 'Fira Code', monospace` | IDs, codes, table numbers, timestamps |
| `--text-xs` | `0.75rem / 1.125rem` | Captions, badges |
| `--text-sm` | `0.875rem / 1.375rem` | Secondary labels, helper text |
| `--text-base` | `1rem / 1.625rem` | Body, form labels |
| `--text-lg` | `1.125rem / 1.75rem` | Card titles |
| `--text-xl` | `1.375rem / 2rem` | Section headings (h3) |
| `--text-2xl` | `1.75rem / 2.25rem` | Page headings (h2) |
| `--text-3xl` | `2.25rem / 2.75rem` | Hero / KPI values |

### Spacing & Shape Tokens

| Token | Value | Usage |
|---|---|---|
| `--radius-sm` | `6px` | Badges, chips, tags |
| `--radius-md` | `10px` | Buttons, inputs |
| `--radius-lg` | `14px` | Cards, panels |
| `--radius-xl` | `20px` | Modals, large panels |
| `--shadow-card` | `0 1px 2px rgba(15,23,42,0.04), 0 0 0 1px #e4e8ec` | Cards |
| `--shadow-elevated` | `0 4px 12px rgba(15,23,42,0.08)` | Dropdowns, tooltips |
| `--sidebar-width` | `240px` | Sidebar |
| `--topbar-height` | `64px` | Top navigation bar |

### Component Patterns (from artifact)

1. **Sidebar** — White background, `--color-border` right border, 240px wide. Group labels (MAIN, PROJECTS, REVIEW, ADMIN) in `--color-text-subtle` 10px uppercase tracking. Active item: `--color-primary-subtle` bg + `--color-primary` left border 3px + `--color-primary` text.
2. **Topbar** — `backdrop-filter: blur(12px)`, `background: rgba(250,251,252,0.85)`, 64px height, sticky. Left: breadcrumb. Right: user avatar chip + logout.
3. **Cards** — `--color-surface` bg, `--shadow-card`, `--radius-lg`, `16px` padding.
4. **Stepper** — Horizontal steps with circle indicator (filled=done, outlined=active, grey=future), connector line. Used on Submit Visit (5 steps).
5. **Badges/Status chips** — `--radius-sm`, `4px 10px` padding, soft color pairs from status tokens above, `font-weight: 600`, `font-size: 0.75rem`.
6. **Filter chips** — Outline button style, toggleable, `--radius-sm`, `--color-border` default border, `--color-primary` when active.
7. **KPI cards** — Icon (32×32 `--color-primary-subtle` bg), value (`--text-3xl`, `font-family: --font-mono`), label (`--text-sm`, muted), optional delta badge.
8. **Compare grid** — Two-column layout for Change Requests: "Original" vs "Requested" with subtle diff highlighting on changed fields.
9. **Activity feed** — Vertical timeline with dot + connector, timestamp right-aligned in mono, description left.
10. **Section header** — `--font-display` h2 with optional subtitle in `--color-text-muted`, `margin-bottom: 20px`.

---

## Phase 3 — Adapted Design System (Streamlit Implementation)

### Constraints

- Streamlit renders its own HTML; we override via CSS selectors targeting `data-testid` attributes
- Fonts must be injected via `components.html` (Google Fonts import) or inline `<style>` in `st.markdown`
- No React components — replicate patterns with Python f-string HTML helpers
- CSS injection runs on every rerun — keep it in a cached function
- Sidebar CSS uses `section[data-testid="stSidebar"]` selectors (already partially done in `ui.py`)

### Key Selector Mapping (Streamlit → Artifact)

| Streamlit element | CSS target | Artifact equivalent |
|---|---|---|
| Page background | `[data-testid="stAppViewContainer"]` | `--color-bg` |
| Sidebar | `section[data-testid="stSidebar"]` | White sidebar + border |
| Main content | `.block-container` | Card/content area |
| `st.button` (primary) | `[data-testid="stButton"] > button[kind="primary"]` | Primary button |
| `st.button` (secondary) | `[data-testid="stButton"] > button[kind="secondary"]` | Secondary button |
| `st.text_input` | `[data-testid="stTextInput"] input` | Input field |
| `st.selectbox` | `[data-testid="stSelectbox"]` | Select/dropdown |
| `st.form` | `[data-testid="stForm"]` | Card (form container) |
| `st.success` | `[data-testid="stAlert"][kind="success"]` | Success badge/alert |
| `st.error` | `[data-testid="stAlert"][kind="error"]` | Error alert |
| `st.warning` | `[data-testid="stAlert"][kind="warning"]` | Warning alert |
| `st.metric` | `[data-testid="stMetric"]` | KPI card |
| Radio nav | `div[role="radiogroup"] > label` | Sidebar nav items |
| `st.dataframe` | `[data-testid="stDataFrame"]` | Data table |
| Expander | `[data-testid="stExpander"]` | Collapsible section |

---

## Phase 4 — Prioritized Implementation Tasks

### Task 1: Font & CSS Variable Foundation (`theme.py`)

**Files:**
- Create: `theme.py`
- Modify: `app_v11.py` (import and call `inject_theme()`)

- [ ] **Step 1: Create `theme.py` with font injection and CSS variables**

```python
# theme.py
import streamlit as st
import streamlit.components.v1 as components

def inject_theme():
    """Inject Geist fonts + CSS custom properties once per app load."""
    components.html("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;600;700&display=swap');
</style>
<script>
(function() {
  const s = document.createElement('style');
  s.textContent = `
    :root {
      --color-primary: #2667ff;
      --color-primary-hover: #1a50d4;
      --color-primary-subtle: #eef2ff;
      --color-bg: #fafbfc;
      --color-surface: #ffffff;
      --color-surface-2: #f6f8fa;
      --color-border: #e4e8ec;
      --color-border-strong: #c9d1d9;
      --color-text: #0d1117;
      --color-text-muted: #57606a;
      --color-text-subtle: #8b949e;
      --status-success-bg: #e6f6ec; --status-success-text: #0e8a4f;
      --status-warning-bg: #fdf2e4; --status-warning-text: #b5651d;
      --status-danger-bg: #fdeceb;  --status-danger-text: #c83333;
      --status-info-bg: #e8f4fd;    --status-info-text: #1565c0;
      --status-neutral-bg: #f0f0f0; --status-neutral-text: #444444;
      --font-body: 'Geist', 'Inter', system-ui, sans-serif;
      --font-display: 'Inter Tight', 'Inter', sans-serif;
      --font-mono: 'Geist Mono', 'Fira Code', ui-monospace, monospace;
      --radius-sm: 6px;
      --radius-md: 10px;
      --radius-lg: 14px;
      --shadow-card: 0 1px 2px rgba(15,23,42,0.04), 0 0 0 1px #e4e8ec;
      --shadow-elevated: 0 4px 12px rgba(15,23,42,0.08);
    }
    * { font-family: var(--font-body) !important; box-sizing: border-box; }
    h1, h2, h3, [data-testid="stHeading"] {
      font-family: var(--font-display) !important;
      color: var(--color-text) !important;
    }
  `;
  document.head.appendChild(s);
})();
</script>
""", height=0)
```

- [ ] **Step 2: Wire into `app_v11.py` — call after `st.set_page_config`**

In `app_v11.py`, add to imports:
```python
from theme import inject_theme
```
Add call immediately after `st.set_page_config(...)` block, before `components.html(...)`:
```python
inject_theme()
```

- [ ] **Step 3: Verify syntax**
```bash
py -3.9 -m py_compile theme.py app_v11.py && echo OK
```
Expected: `OK`

- [ ] **Step 4: Commit**
```bash
git add theme.py app_v11.py
git commit -m "feat: inject Geist/Inter Tight fonts and CSS design tokens"
```

---

### Task 2: Page Background & App Layout

**Files:**
- Modify: `app_v11.py` (replace existing `<style>` st.markdown block)

The current CSS block only sets padding. Replace with the full layout foundation.

- [ ] **Step 1: Replace the existing `st.markdown("""<style>...""")` block in `app_v11.py`**

Find and replace the block starting at line ~55:
```python
st.markdown("""
<style>
/* ── Layout foundation ── */
[data-testid="stAppViewContainer"] {
    background: #fafbfc !important;
}
[data-testid="stAppViewContainer"] .block-container {
    padding-top: 1.5rem !important;
    padding-bottom: 2rem !important;
    max-width: 860px;
}

/* ── Hide heading anchor links ── */
[data-testid="stHeading"] a,
.stMarkdown h1 a, .stMarkdown h2 a, .stMarkdown h3 a,
.stMarkdown h4 a, .stMarkdown h5 a, .stMarkdown h6 a {
    display: none !important;
    visibility: hidden !important;
    pointer-events: none !important;
}

/* ── Typography scale ── */
.stMarkdown h1 { font-size: 1.75rem !important; font-weight: 700 !important;
    letter-spacing: -0.02em; margin-bottom: 0.5rem !important; }
.stMarkdown h2 { font-size: 1.375rem !important; font-weight: 600 !important;
    letter-spacing: -0.01em; }
.stMarkdown h3 { font-size: 1.125rem !important; font-weight: 600 !important; }
p, li { font-size: 1rem !important; line-height: 1.625 !important; }

/* ── Main content card-like feel ── */
section[data-testid="stMain"] > div > div > [data-testid="stVerticalBlock"] {
    background: #ffffff;
    border-radius: 14px;
    box-shadow: 0 1px 2px rgba(15,23,42,0.04), 0 0 0 1px #e4e8ec;
    padding: 1.5rem 1.75rem !important;
    margin-top: 0.5rem;
}

/* ── Primary buttons ── */
[data-testid="stButton"] > button[kind="primary"],
[data-testid="stFormSubmitButton"] > button {
    background: #2667ff !important;
    border: none !important;
    border-radius: 10px !important;
    font-weight: 600 !important;
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
    background: #ffffff !important;
    border: 1px solid #e4e8ec !important;
    border-radius: 10px !important;
    color: #0d1117 !important;
    font-weight: 500 !important;
    transition: background 0.15s ease, border-color 0.15s ease !important;
}
[data-testid="stButton"] > button[kind="secondary"]:hover {
    background: #f6f8fa !important;
    border-color: #c9d1d9 !important;
}

/* ── Inputs and selects ── */
[data-testid="stTextInput"] input,
[data-testid="stTextArea"] textarea,
[data-testid="stSelectbox"] div[data-baseweb="select"] {
    background: #f6f8fa !important;
    border: 1px solid #e4e8ec !important;
    border-radius: 10px !important;
    color: #0d1117 !important;
    font-size: 0.9375rem !important;
    transition: border-color 0.15s ease, box-shadow 0.15s ease !important;
}
[data-testid="stTextInput"] input:focus,
[data-testid="stTextArea"] textarea:focus {
    background: #ffffff !important;
    border-color: #2667ff !important;
    box-shadow: 0 0 0 3px rgba(38,103,255,0.12) !important;
    outline: none !important;
}

/* ── Form containers ── */
[data-testid="stForm"] {
    border: 1px solid #e4e8ec !important;
    border-radius: 14px !important;
    padding: 1.25rem 1.5rem !important;
    background: #ffffff !important;
}

/* ── Alert styling ── */
[data-testid="stAlert"][kind="success"] {
    background: #e6f6ec !important; border-color: #0e8a4f !important;
    border-radius: 10px !important; color: #0e8a4f !important;
}
[data-testid="stAlert"][kind="error"] {
    background: #fdeceb !important; border-color: #c83333 !important;
    border-radius: 10px !important; color: #c83333 !important;
}
[data-testid="stAlert"][kind="warning"] {
    background: #fdf2e4 !important; border-color: #b5651d !important;
    border-radius: 10px !important; color: #b5651d !important;
}
[data-testid="stAlert"][kind="info"] {
    background: #e8f4fd !important; border-color: #1565c0 !important;
    border-radius: 10px !important;
}

/* ── Metric / KPI cards ── */
[data-testid="stMetric"] {
    background: #ffffff !important;
    border: 1px solid #e4e8ec !important;
    border-radius: 14px !important;
    padding: 1rem 1.25rem !important;
    box-shadow: 0 1px 2px rgba(15,23,42,0.04) !important;
}
[data-testid="stMetricValue"] {
    font-family: 'Geist Mono', 'Fira Code', monospace !important;
    font-size: 1.75rem !important; font-weight: 700 !important;
    color: #0d1117 !important;
}
[data-testid="stMetricLabel"] {
    font-size: 0.8125rem !important; color: #57606a !important;
    font-weight: 500 !important; text-transform: uppercase !important;
    letter-spacing: 0.04em !important;
}

/* ── Dataframe / table ── */
[data-testid="stDataFrame"] {
    border: 1px solid #e4e8ec !important;
    border-radius: 10px !important;
    overflow: hidden !important;
}

/* ── Expander ── */
[data-testid="stExpander"] {
    border: 1px solid #e4e8ec !important;
    border-radius: 10px !important;
    background: #ffffff !important;
}
</style>
""", unsafe_allow_html=True)
```

- [ ] **Step 2: Verify syntax**
```bash
py -3.9 -m py_compile app_v11.py && echo OK
```

- [ ] **Step 3: Commit**
```bash
git add app_v11.py
git commit -m "feat: apply artifact-matched layout, button, input, card, and alert CSS"
```

---

### Task 3: Sidebar Redesign

**Files:**
- Modify: `ui.py` — replace `sidebar_nav()` CSS block and nav HTML

The existing sidebar is partially styled. This task replaces it with the artifact's white sidebar + group labels pattern.

- [ ] **Step 1: Replace the `st.sidebar.markdown(...)` CSS block inside `sidebar_nav()` in `ui.py`**

Replace everything from `st.sidebar.markdown("""` down to the closing `""", unsafe_allow_html=True,)` with:

```python
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
            display: flex; justify-content: center; align-items: center;
            padding: 0.75rem 1rem 0.5rem !important;
        }

        /* ── Group label (injected as disabled radio options labeled with "---") ── */
        /* handled via custom HTML injection below nav */

        /* ── Nav items: remove radio circle ── */
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
            transition: background 0.15s ease, color 0.15s ease !important;
            border-left: 3px solid transparent !important;
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
```

- [ ] **Step 2: Verify syntax**
```bash
py -3.9 -m py_compile ui.py && echo OK
```

- [ ] **Step 3: Commit**
```bash
git add ui.py
git commit -m "feat: redesign sidebar to artifact white-panel style with active state highlight"
```

---

### Task 4: HTML Helper Functions (`ui.py`)

**Files:**
- Modify: `ui.py` — add helper functions for reusable HTML components

These functions are called by page modules to render artifact-style components without duplicating HTML.

- [ ] **Step 1: Add helper functions at the bottom of `ui.py` (before the final blank lines)**

```python
def status_badge(label: str, variant: str = "neutral") -> str:
    """Return inline HTML for an artifact-style status badge.
    variant: success | warning | danger | info | neutral | primary
    """
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
    """Return HTML string for an artifact-style KPI card (use inside st.markdown)."""
    delta_color = "#0e8a4f" if delta_positive else "#c83333"
    delta_html = (
        f'<span style="margin-top:4px;font-size:0.75rem;font-weight:600;color:{delta_color};">'
        f'{delta}</span>'
        if delta else ""
    )
    return (
        f'<div style="background:#fff;border:1px solid #e4e8ec;border-radius:14px;'
        f'padding:1rem 1.25rem;box-shadow:0 1px 2px rgba(15,23,42,0.04);">'
        f'<div style="font-size:0.8125rem;font-weight:500;color:#57606a;'
        f'text-transform:uppercase;letter-spacing:0.04em;">{label}</div>'
        f'<div style="font-family:\'Geist Mono\',monospace;font-size:1.75rem;'
        f'font-weight:700;color:#0d1117;line-height:1.2;margin-top:4px;">{value}</div>'
        f'{delta_html}</div>'
    )


def compare_row(field: str, original: str, requested: str, changed: bool = False) -> str:
    """Return HTML for a single compare-grid row (used in change_request page)."""
    highlight = "background:#fdf2e4;" if changed else ""
    return (
        f'<tr style="{highlight}">'
        f'<td style="padding:8px 12px;font-size:0.875rem;font-weight:500;'
        f'color:#57606a;white-space:nowrap;border-bottom:1px solid #e4e8ec;">{field}</td>'
        f'<td style="padding:8px 12px;font-size:0.875rem;color:#0d1117;'
        f'border-bottom:1px solid #e4e8ec;">{original}</td>'
        f'<td style="padding:8px 12px;font-size:0.875rem;color:#0d1117;'
        f'border-bottom:1px solid #e4e8ec;{"font-weight:600;color:#b5651d;" if changed else ""}">'
        f'{requested}</td>'
        f'</tr>'
    )
```

- [ ] **Step 2: Verify syntax**
```bash
py -3.9 -m py_compile ui.py && echo OK
```

- [ ] **Step 3: Commit**
```bash
git add ui.py
git commit -m "feat: add status_badge, section_header, kpi_card, compare_row HTML helpers"
```

---

### Task 5: Login Page Redesign

**Files:**
- Modify: `ui.py` — `login_block()` function

The current login is plain Streamlit widgets. Replace with an artifact-style centered card.

- [ ] **Step 1: Replace `login_block()` body in `ui.py`**

Replace the entire `login_block()` function body (keep the `def login_block():` line):

```python
def login_block():
    app_root = Path(__file__).parent
    logo_path = app_root / "static" / "Login_Logo.png"

    # ── Page background override for login ──
    st.markdown("""
    <style>
    [data-testid="stAppViewContainer"] { background: #f0f4ff !important; }
    section[data-testid="stMain"] > div > div > [data-testid="stVerticalBlock"] {
        background: transparent !important;
        box-shadow: none !important;
        border: none !important;
        padding: 0 !important;
    }
    </style>
    """, unsafe_allow_html=True)

    # ── Centering spacer ──
    _, col, _ = st.columns([1, 2, 1])
    with col:
        # Logo
        if logo_path.exists():
            b64 = _img_b64(logo_path)
            st.markdown(
                f"""<div style="text-align:center;margin-bottom:1.5rem;">
                  <img src="data:image/png;base64,{b64}" alt="Ascenda"
                       style="width:200px;height:auto;" />
                </div>""",
                unsafe_allow_html=True,
            )

        # Card wrapper
        st.markdown("""
        <div style="background:#fff;border:1px solid #e4e8ec;border-radius:14px;
                    padding:2rem 2.25rem;box-shadow:0 4px 12px rgba(15,23,42,0.08);">
          <h2 style="margin:0 0 1.5rem;font-size:1.375rem;font-weight:700;
                     color:#0d1117;text-align:center;">Sign in to Ascenda</h2>
        </div>
        """, unsafe_allow_html=True)

        with st.form("login"):
            email = st.text_input("Email address", placeholder="you@company.com")
            pw = st.text_input("Password", type="password", placeholder="••••••••")
            submitted = st.form_submit_button("Sign in", use_container_width=True, type="primary")

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
```

- [ ] **Step 2: Verify syntax**
```bash
py -3.9 -m py_compile ui.py && echo OK
```

- [ ] **Step 3: Commit**
```bash
git add ui.py
git commit -m "feat: redesign login page with centered card and sign-in button"
```

---

### Task 6: Status Badges in Review Pages

**Files:**
- Modify: `app_pages/review_audiences.py`
- Modify: `app_pages/review_customers.py`
- Modify: `app_pages/change_request.py`

Use the `status_badge()` helper instead of raw text for status column rendering.

- [ ] **Step 1: Add import to each of the three files**

At the top of each file, add (alongside other ui imports):
```python
from ui import status_badge, section_header
```

- [ ] **Step 2: Replace status text in `review_audiences.py`**

Find any pattern like `df["status"]` used in an `st.dataframe` or loop. For dataframe column rendering, add a helper that maps status values to badge HTML:
```python
STATUS_BADGE_MAP = {
    "active":   ("Active",   "success"),
    "inactive": ("Inactive", "neutral"),
    "pending":  ("Pending",  "warning"),
    "rejected": ("Rejected", "danger"),
    "approved": ("Approved", "success"),
    "draft":    ("Draft",    "neutral"),
}

def _render_status_badge(val: str) -> str:
    label, variant = STATUS_BADGE_MAP.get(str(val).lower(), (val, "neutral"))
    return status_badge(label, variant)
```

Apply this as a `.apply()` on the status column before passing to `st.dataframe()`, or use `st.markdown()` in loop-based renders.

- [ ] **Step 3: Add `section_header()` calls to replace plain `st.subheader()` / `st.title()` calls in all three files**

Replace any top-level `st.title("...")` or `st.subheader("...")` with:
```python
section_header("Review Target Audiences", "Manage and audit audience records")
```
Adapt subtitle text per page.

- [ ] **Step 4: Verify syntax for all three files**
```bash
py -3.9 -m py_compile app_pages/review_audiences.py app_pages/review_customers.py app_pages/change_request.py && echo OK
```

- [ ] **Step 5: Commit**
```bash
git add app_pages/review_audiences.py app_pages/review_customers.py app_pages/change_request.py
git commit -m "feat: use status_badge helper and section_header in review/change-request pages"
```

---

### Task 7: Submit Visit Stepper UI

**Files:**
- Modify: `app_pages/submit_visit.py`

The artifact shows Submit Visit as a 5-step stepper. This task adds a visual stepper header above the existing step logic (which already uses `st.session_state` step tracking — we just add the visual indicator).

- [ ] **Step 1: Create stepper HTML helper in `ui.py`**

Add after the existing helpers:
```python
def stepper(steps: list[str], current: int) -> None:
    """Render a horizontal stepper bar. current is 0-indexed."""
    items = []
    for i, label in enumerate(steps):
        if i < current:
            circle_style = ("background:#2667ff;border:2px solid #2667ff;"
                           "color:#fff;")
            text_style = "color:#2667ff;font-weight:600;"
            icon = "✓"
        elif i == current:
            circle_style = ("background:#fff;border:2px solid #2667ff;"
                           "color:#2667ff;")
            text_style = "color:#2667ff;font-weight:700;"
            icon = str(i + 1)
        else:
            circle_style = ("background:#fff;border:2px solid #e4e8ec;"
                           "color:#8b949e;")
            text_style = "color:#8b949e;"
            icon = str(i + 1)

        connector = (
            f'<div style="flex:1;height:2px;background:'
            f'{"#2667ff" if i < current else "#e4e8ec"};margin:0 4px;'
            f'align-self:center;"></div>'
            if i < len(steps) - 1 else ""
        )
        items.append(
            f'<div style="display:flex;flex-direction:column;align-items:center;'
            f'min-width:60px;">'
            f'<div style="width:32px;height:32px;border-radius:50%;display:flex;'
            f'align-items:center;justify-content:center;font-size:0.8rem;'
            f'font-weight:700;{circle_style}">{icon}</div>'
            f'<span style="margin-top:4px;font-size:0.7rem;white-space:nowrap;{text_style}">'
            f'{label}</span></div>'
            + connector
        )
    st.markdown(
        f'<div style="display:flex;align-items:flex-start;justify-content:center;'
        f'padding:1rem 0 1.5rem;gap:0;">' + "".join(items) + '</div>',
        unsafe_allow_html=True,
    )
```

- [ ] **Step 2: Add `from ui import stepper` to `app_pages/submit_visit.py` imports**

- [ ] **Step 3: Find where `page_submit_visit` renders step headings and insert `stepper()` call**

At the point where the current step is determined (look for `step = st.session_state.get("submit_step", 1)` or equivalent), insert:

```python
SUBMIT_STEPS = ["Customer", "Visit Details", "Products", "Outcomes", "Review"]
stepper(SUBMIT_STEPS, step - 1)  # step is 1-indexed, stepper is 0-indexed
```

Adjust step variable name and list length to match the actual number of steps in the file.

- [ ] **Step 4: Verify syntax**
```bash
py -3.9 -m py_compile ui.py app_pages/submit_visit.py && echo OK
```

- [ ] **Step 5: Commit**
```bash
git add ui.py app_pages/submit_visit.py
git commit -m "feat: add stepper component and visual step indicator to Submit Visit"
```

---

### Task 8: Change Request Compare Grid

**Files:**
- Modify: `app_pages/change_request.py`

The artifact shows Change Requests with a side-by-side Original vs Requested compare grid. This task adds that HTML table for each change request detail view.

- [ ] **Step 1: Add `from ui import compare_row, section_header, status_badge` to `change_request.py` imports**

- [ ] **Step 2: Find the section in `page_change_request` that displays request fields (likely in an expander or detail block)**

Wrap the field display in compare_row HTML:

```python
def _render_compare_grid(original: dict, requested: dict, fields: list[tuple]) -> None:
    """Render the artifact-style compare grid.
    fields: list of (field_key, display_label) tuples
    """
    rows = ""
    for key, label in fields:
        orig_val = str(original.get(key, "—") or "—")
        req_val  = str(requested.get(key, "—") or "—")
        changed  = orig_val != req_val
        rows += compare_row(label, orig_val, req_val, changed)

    st.markdown(
        f"""
        <table style="width:100%;border-collapse:collapse;border:1px solid #e4e8ec;
                      border-radius:10px;overflow:hidden;font-size:0.875rem;">
          <thead>
            <tr style="background:#f6f8fa;">
              <th style="padding:10px 12px;text-align:left;font-weight:600;
                         color:#57606a;border-bottom:1px solid #e4e8ec;width:30%;">Field</th>
              <th style="padding:10px 12px;text-align:left;font-weight:600;
                         color:#57606a;border-bottom:1px solid #e4e8ec;">Original</th>
              <th style="padding:10px 12px;text-align:left;font-weight:600;
                         color:#57606a;border-bottom:1px solid #e4e8ec;">Requested</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        """,
        unsafe_allow_html=True,
    )
```

- [ ] **Step 3: Replace inline field display with `_render_compare_grid()` call**

Identify the fields displayed per request (customer name, visit date, visit type, notes, etc.) and pass as the `fields` list.

- [ ] **Step 4: Verify syntax**
```bash
py -3.9 -m py_compile app_pages/change_request.py && echo OK
```

- [ ] **Step 5: Commit**
```bash
git add app_pages/change_request.py
git commit -m "feat: add artifact-style compare grid to change request detail view"
```

---

### Task 9: Footer Refinement

**Files:**
- Modify: `ui.py` — `show_footer()` function

The current footer is centered text with logos. Align it with the artifact's minimal, border-separated footer style.

- [ ] **Step 1: Replace `show_footer()` body in `ui.py`**

```python
def show_footer():
    logo_b64 = get_almadar_logo_base64()
    logo_html = (
        f'<img src="data:image/png;base64,{logo_b64}" '
        f'style="height:36px;opacity:0.7;" />'
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
```

- [ ] **Step 2: Verify syntax**
```bash
py -3.9 -m py_compile ui.py && echo OK
```

- [ ] **Step 3: Commit**
```bash
git add ui.py
git commit -m "feat: refine footer to horizontal layout matching artifact style"
```

---

## Verification (all tasks)

After completing all tasks, run a full syntax check:
```bash
py -3.9 -m py_compile theme.py app_v11.py ui.py
py -3.9 -m py_compile app_pages/submit_visit.py app_pages/review_audiences.py
py -3.9 -m py_compile app_pages/review_customers.py app_pages/change_request.py
```

Then deploy to Render and manually test:
- [ ] Login page — card centered, fonts loaded, button blue
- [ ] Sidebar — white, active item highlighted blue
- [ ] Submit Visit — stepper visible above form
- [ ] Review pages — section headers visible, status badges colored
- [ ] Change Request — compare grid renders for a changed field
- [ ] Footer — horizontal layout, logo left, text right
- [ ] Mobile (375px) — no horizontal overflow, touch targets ≥ 44px

---

## Priority Order

| Priority | Task | Visual Impact | Effort |
|---|---|---|---|
| 1 | Task 1 (fonts + tokens) | High — foundation | Low |
| 2 | Task 2 (layout + buttons) | High — every page | Medium |
| 3 | Task 3 (sidebar) | High — always visible | Low |
| 4 | Task 5 (login) | High — first impression | Low |
| 5 | Task 4 (helpers) | Medium — enabler | Low |
| 6 | Task 6 (badges) | Medium — review pages | Medium |
| 7 | Task 7 (stepper) | Medium — Submit Visit | Medium |
| 8 | Task 8 (compare grid) | Medium — change requests | Medium |
| 9 | Task 9 (footer) | Low — bottom of page | Low |
