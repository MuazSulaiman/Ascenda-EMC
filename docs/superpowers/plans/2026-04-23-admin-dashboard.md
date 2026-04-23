# Admin Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `page_dashboard()` so admins see a command-center view (field activity KPIs + pending review action list) instead of the rep dashboard.

**Architecture:** Single role branch at the top of `page_dashboard()` — if `role == "admin"`, call `_render_admin_dashboard()` and return early. All admin logic lives in private helpers in the same file. No new files, no new routes.

**Tech Stack:** Streamlit, SQLAlchemy (`query_df` from `db_ops`), existing UI helpers (`kpi_card_v2`, `section_header`, `status_badge` from `ui`), pandas.

---

## File Map

| File | Change |
|------|--------|
| `app_pages/dashboard.py` | Add role branch in `page_dashboard()` + new private helpers |

---

### Task 1: Add role branch and update imports

**Files:**
- Modify: `app_pages/dashboard.py` (lines 1–10, 38–44)

- [ ] **Step 1: Update the import line for `ui` helpers**

In `app_pages/dashboard.py`, line 8, change:

```python
from ui import kpi_card_v2, section_header
```

to:

```python
from ui import kpi_card_v2, section_header, status_badge
```

- [ ] **Step 2: Add `import pandas as pd` after the existing imports**

After `from utils import _local_now` (line 10), add:

```python
import pandas as pd
```

So the top of the file reads:

```python
# pages/dashboard.py — Ascenda Dashboard
import streamlit as st
from datetime import datetime

import pandas as pd

from auth import resolve_session_user
from config import TIMEZONE
from db_ops import query_df
from ui import kpi_card_v2, section_header, status_badge
from widgets import set_current_page
from utils import _local_now
```

- [ ] **Step 3: Add role branch at the top of `page_dashboard()`**

Replace the existing `page_dashboard()` function body opening (lines 38–57, up to and including `section_header(...)`) with:

```python
def page_dashboard():
    set_current_page("dashboard")

    u = st.session_state.get("user") or resolve_session_user()
    if not u:
        st.warning("Please sign in.")
        return

    uid  = int(u.get("user_id") or u.get("id"))
    name = u.get("name") or u.get("email") or "there"
    first_name = name.split()[0] if name else "there"
    role = (u.get("role") or "").lower().strip()

    if role == "admin":
        _render_admin_dashboard(uid, first_name)
        return

    # ── Greeting header ───────────────────────────────────────────────────────
    try:
        now_local = _local_now()
        date_str  = f"{now_local.strftime('%A, %B')} {now_local.day}"
    except Exception:
        date_str = datetime.now().strftime("%A, %B %d")

    section_header(f"Welcome back, {first_name}", f"Here's what's happening in the field, {date_str}.")
```

Everything after `section_header(...)` in `page_dashboard()` stays exactly as-is.

- [ ] **Step 4: Add the stub for `_render_admin_dashboard` at the bottom of the file**

Append after the last line of `page_dashboard()`:

```python


def _render_admin_dashboard(uid: int, first_name: str) -> None:
    """Admin command-center dashboard. Called from page_dashboard() when role==admin."""
    pass
```

- [ ] **Step 5: Verify the app still loads**

Run: `streamlit run app_v11.py`

Log in as a non-admin rep → should see the rep dashboard unchanged.
Log in as an admin → should see a blank page (stub). No errors in terminal.

- [ ] **Step 6: Commit**

```bash
git add app_pages/dashboard.py
git commit -m "feat: add admin role branch in page_dashboard, stub _render_admin_dashboard"
```

---

### Task 2: Field activity zone (header + period filter + KPI cards)

**Files:**
- Modify: `app_pages/dashboard.py` — fill in `_render_admin_dashboard()`

**Icons needed** (add as module-level constants after the existing `_ICON_ALERT` block):

```python
_ICON_USERS = (
    '<svg width="18" height="18" fill="none" stroke="#2667ff" stroke-width="2" '
    'viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>'
    '<circle cx="9" cy="7" r="4"/>'
    '<path d="M23 21v-2a4 4 0 0 0-3-3.87"/>'
    '<path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>'
)
_ICON_BUILDING = (
    '<svg width="18" height="18" fill="none" stroke="#0e8a4f" stroke-width="2" '
    'viewBox="0 0 24 24"><rect x="2" y="7" width="20" height="14" rx="2"/>'
    '<path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/></svg>'
)
```

- [ ] **Step 1: Add `_ICON_USERS` and `_ICON_BUILDING` after `_ICON_ALERT`**

After the `_ICON_ALERT = (...)` block (line ~35), add the two icon constants shown above.

- [ ] **Step 2: Replace the `pass` stub with the field activity implementation**

Replace `_render_admin_dashboard`:

```python
def _render_admin_dashboard(uid: int, first_name: str) -> None:
    """Admin command-center dashboard. Called from page_dashboard() when role==admin."""

    # ── Header ────────────────────────────────────────────────────────────────
    try:
        now_local = _local_now()
        date_str  = f"{now_local.strftime('%A, %B')} {now_local.day}"
    except Exception:
        date_str = datetime.now().strftime("%A, %B %d")

    section_header("Command Center", f"Field activity & pending reviews — {date_str}.")

    # ── Period filter ─────────────────────────────────────────────────────────
    period = st.radio(
        "",
        ["This week", "This month", "All time"],
        horizontal=True,
        key="dash_admin_period",
        label_visibility="collapsed",
    )

    period_filter = {
        "This week":  "AND v.submitted_at_local >= date_trunc('week',  NOW() AT TIME ZONE 'Asia/Riyadh')",
        "This month": "AND v.submitted_at_local >= date_trunc('month', NOW() AT TIME ZONE 'Asia/Riyadh')",
        "All time":   "",
    }.get(period, "")

    def _safe_count(sql: str, params: dict = None) -> int:
        try:
            r = query_df(sql, params or {})
            return int(r.iloc[0, 0]) if not r.empty else 0
        except Exception:
            return 0

    # ── Field Activity KPIs ───────────────────────────────────────────────────
    st.markdown("#### Field Activity")

    total_visits = _safe_count(
        f"SELECT COUNT(*) FROM visits v WHERE 1=1 {period_filter}"
    )
    unique_customers = _safe_count(
        f"SELECT COUNT(DISTINCT v.customer_id) FROM visits v WHERE 1=1 {period_filter}"
    )
    active_reps = _safe_count(
        f"SELECT COUNT(DISTINCT v.user_id) FROM visits v WHERE 1=1 {period_filter}"
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown(
            kpi_card_v2(
                label=f"Total Visits ({period})",
                value=str(total_visits),
                delta="All reps combined",
                delta_positive=True,
                icon_svg=_ICON_LOCATION,
                icon_bg="#eef2ff",
            ),
            unsafe_allow_html=True,
        )
    with col2:
        st.markdown(
            kpi_card_v2(
                label="Unique Customers",
                value=str(unique_customers),
                delta=f"In {period.lower()}",
                delta_positive=True,
                icon_svg=_ICON_BUILDING,
                icon_bg="#e6f6ec",
            ),
            unsafe_allow_html=True,
        )
    with col3:
        st.markdown(
            kpi_card_v2(
                label="Active Reps",
                value=str(active_reps),
                delta=f"Submitted ≥1 visit",
                delta_positive=True,
                icon_svg=_ICON_USERS,
                icon_bg="#eef2ff",
            ),
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # ── Pending Reviews ───────────────────────────────────────────────────────
    _render_admin_pending_reviews()
```

Note: `_render_admin_pending_reviews()` will be implemented in Task 3. Add a stub for it now:

```python
def _render_admin_pending_reviews() -> None:
    pass
```

- [ ] **Step 3: Verify field activity renders correctly**

Run: `streamlit run app_v11.py`

Log in as admin → should see:
- "Command Center" header with today's date
- Period filter radio (This week / This month / All time)
- Three KPI cards in a row: Total Visits, Unique Customers, Active Reps
- Counts update when period filter changes

- [ ] **Step 4: Commit**

```bash
git add app_pages/dashboard.py
git commit -m "feat: admin dashboard field activity zone with period filter and KPI cards"
```

---

### Task 3: Pending reviews zone (badges + unified action list)

**Files:**
- Modify: `app_pages/dashboard.py` — replace `_render_admin_pending_reviews()` stub

- [ ] **Step 1: Replace the `_render_admin_pending_reviews` stub with the full implementation**

```python
def _render_admin_pending_reviews() -> None:
    """Render the pending reviews section: summary badges + unified action list."""

    st.markdown("#### Pending Reviews")

    # ── Summary counts ────────────────────────────────────────────────────────
    def _safe_count(sql: str) -> int:
        try:
            r = query_df(sql)
            return int(r.iloc[0, 0]) if not r.empty else 0
        except Exception:
            return 0

    cr_count = _safe_count(
        "SELECT COUNT(*) FROM request_changes WHERE status = 'IN_REVIEW'"
    )
    ta_count = _safe_count(
        """
        SELECT COUNT(*) FROM visits
        WHERE audience_id IS NULL
          AND customer_id <> 807
          AND other_audience_name IS NOT NULL
          AND trim(other_audience_name) <> ''
        """
    )
    oc_count = _safe_count(
        "SELECT COUNT(*) FROM visits WHERE customer_id = 807"
    )

    total_pending = cr_count + ta_count + oc_count

    badges_html = (
        f'<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:1rem;">'
        f'{status_badge(f"Change Requests: {cr_count}", "warning")}'
        f'{status_badge(f"Target Audiences: {ta_count}", "info")}'
        f'{status_badge(f"Other Customers: {oc_count}", "primary")}'
        f'</div>'
    )
    st.markdown(badges_html, unsafe_allow_html=True)

    if total_pending == 0:
        st.success("No pending reviews — all clear.")
        return

    # ── Unified action list ───────────────────────────────────────────────────
    try:
        items_df = query_df(
            """
            SELECT 'Change Request' AS type,
                   rc.request_id   AS item_id,
                   'Visit #' || rc.visit_id AS identifier,
                   u.name          AS rep_name,
                   rc.request_date AS submitted_at,
                   'Review Change Requests' AS target_page
            FROM request_changes rc
            JOIN users u ON u.user_id = rc.requested_by
            WHERE rc.status = 'IN_REVIEW'

            UNION ALL

            SELECT 'Target Audience'      AS type,
                   v.visit_id             AS item_id,
                   'Visit #' || v.visit_id AS identifier,
                   u.name                 AS rep_name,
                   v.submitted_at_local   AS submitted_at,
                   'Review Target Audiences' AS target_page
            FROM visits v
            JOIN users u ON u.user_id = v.user_id
            WHERE v.audience_id IS NULL
              AND v.customer_id <> 807
              AND v.other_audience_name IS NOT NULL
              AND trim(v.other_audience_name) <> ''

            UNION ALL

            SELECT 'Other Customer'        AS type,
                   v.visit_id              AS item_id,
                   'Visit #' || v.visit_id  AS identifier,
                   u.name                  AS rep_name,
                   v.submitted_at_local    AS submitted_at,
                   'Review Other Customers' AS target_page
            FROM visits v
            JOIN users u ON u.user_id = v.user_id
            WHERE v.customer_id = 807

            ORDER BY submitted_at ASC
            """
        )
    except Exception as e:
        st.warning(f"Could not load pending items: {e}")
        return

    if items_df.empty:
        st.success("No pending reviews — all clear.")
        return

    items_df["submitted_at"] = pd.to_datetime(items_df["submitted_at"], errors="coerce")

    _TYPE_VARIANT = {
        "Change Request":  "warning",
        "Target Audience": "info",
        "Other Customer":  "primary",
    }

    for _, row in items_df.iterrows():
        date_str = (
            row["submitted_at"].strftime("%d %b %Y")
            if pd.notna(row["submitted_at"]) else "—"
        )
        variant = _TYPE_VARIANT.get(str(row["type"]), "neutral")
        badge   = status_badge(str(row["type"]), variant)
        target  = str(row["target_page"])

        col_info, col_btn = st.columns([5, 1])
        with col_info:
            st.markdown(
                f'<div style="display:flex;align-items:center;gap:10px;'
                f'padding:10px 0;border-bottom:1px solid #e4e8ec;">'
                f'{badge}'
                f'<span style="font-weight:600;font-size:0.9rem;color:#0d1117;">'
                f'{row["identifier"]}</span>'
                f'<span style="font-size:0.85rem;color:#57606a;">'
                f'{row["rep_name"]}</span>'
                f'<span style="font-size:0.8rem;color:#8b949e;">{date_str}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
        with col_btn:
            if st.button(
                "Review →",
                key=f"admin_review_{row['type']}_{int(row['item_id'])}",
                use_container_width=True,
            ):
                st.session_state["_current_page"] = target
                st.rerun()
```

- [ ] **Step 2: Verify pending reviews section renders correctly**

Run: `streamlit run app_v11.py`

Log in as admin → scroll past KPI cards → should see:
- Three colored summary badges showing live counts
- If counts are all zero: green "all clear" message
- If counts > 0: action list with one row per pending item, oldest first
- Each row has: type badge, "Visit #N", rep name, date, "Review →" button
- Clicking "Review →" on a Change Request row → navigates to "Review Change Requests" page
- Clicking "Review →" on a Target Audience row → navigates to "Review Target Audiences" page
- Clicking "Review →" on an Other Customer row → navigates to "Review Other Customers" page

- [ ] **Step 3: Verify rep dashboard is unchanged**

Log in as a rep → Dashboard page shows rep greeting, rep KPI cards, no admin content.

- [ ] **Step 4: Commit**

```bash
git add app_pages/dashboard.py
git commit -m "feat: admin dashboard pending reviews zone with summary badges and action list"
```
