# Admin Dashboard Design

**Date:** 2026-04-23  
**Status:** Approved

## Overview

The existing `page_dashboard()` in `app_pages/dashboard.py` is extended to be role-aware. When the logged-in user is an admin, it renders the admin command-center view instead of the rep view. All other roles see the existing rep dashboard unchanged.

## Architecture

### Entry point change — `app_pages/dashboard.py`

`page_dashboard()` checks role immediately after resolving the user. If `role == "admin"`, it calls `_render_admin_dashboard()` and returns early. No other files are modified.

```
page_dashboard()
  ├── if role == "admin" → _render_admin_dashboard() → return
  └── else → existing rep dashboard (unchanged)
```

### New helper — `_render_admin_dashboard()`

Private function in `app_pages/dashboard.py`. Three visual zones:

1. **Header** — "Command Center" title + current date, same `section_header()` style as rep dashboard
2. **Field Activity** — period filter + KPI cards
3. **Pending Reviews** — summary badges + unified action list

## Field Activity Zone

**Period filter** — horizontal radio: "This week" / "This month" / "All time" (key: `dash_admin_period`). Uses the same `date_trunc` SQL pattern as the rep dashboard.

**Three KPI cards** using `kpi_card_v2`:

| Card | Query |
|------|-------|
| Total Visits | `SELECT COUNT(*) FROM visits WHERE {period_filter}` |
| Unique Customers | `SELECT COUNT(DISTINCT customer_id) FROM visits WHERE {period_filter}` |
| Active Reps | `SELECT COUNT(DISTINCT user_id) FROM visits WHERE {period_filter}` |

## Pending Reviews Zone

### Summary badges

Three `status_badge()` counts shown inline at the top of the section:

| Badge | Query |
|-------|-------|
| Change Requests | `SELECT COUNT(*) FROM request_changes WHERE status = 'IN_REVIEW'` |
| Target Audiences | `SELECT COUNT(*) FROM visits WHERE audience_id IS NULL AND customer_id <> 807 AND other_audience_name IS NOT NULL AND trim(other_audience_name) <> ''` |
| Other Customers | `SELECT COUNT(*) FROM visits WHERE customer_id = 807` |

If all three counts are zero, show a single `st.success("No pending reviews — all clear.")` and skip the action list.

### Unified action list

A single list of all pending items across all three types, sorted **oldest first** (submitted/requested date ascending), so the admin triages in FIFO order.

Built from a UNION of three queries:

```sql
-- Change requests
SELECT 'Change Request'   AS type,
       rc.request_id      AS item_id,
       'Visit #' || rc.visit_id AS identifier,
       u.name             AS rep_name,
       rc.request_date    AS submitted_at,
       'Review Change Requests' AS target_page
FROM request_changes rc
JOIN users u ON u.user_id = rc.requested_by
WHERE rc.status = 'IN_REVIEW'

UNION ALL

-- Target audience "Other" visits
SELECT 'Target Audience'  AS type,
       v.visit_id         AS item_id,
       'Visit #' || v.visit_id AS identifier,
       u.name             AS rep_name,
       v.submitted_at_local AS submitted_at,
       'Review Target Audiences' AS target_page
FROM visits v
JOIN users u ON u.user_id = v.user_id
WHERE v.audience_id IS NULL
  AND v.customer_id <> 807
  AND v.other_audience_name IS NOT NULL
  AND trim(v.other_audience_name) <> ''

UNION ALL

-- Other customer visits
SELECT 'Other Customer'   AS type,
       v.visit_id         AS item_id,
       'Visit #' || v.visit_id AS identifier,
       u.name             AS rep_name,
       v.submitted_at_local AS submitted_at,
       'Review Other Customers' AS target_page
FROM visits v
JOIN users u ON u.user_id = v.user_id
WHERE v.customer_id = 807

ORDER BY submitted_at ASC
```

Each row renders as a card with:
- **Type label** — pill badge (color-coded: orange = Change Request, blue = Target Audience, purple = Other Customer)
- **Identifier** — e.g. "Visit #42"
- **Rep name**
- **Date** — formatted as "DD Mon YYYY"
- **"Review →" button** — calls `set_current_page(target_page)` + `st.rerun()` to navigate to the relevant review page

The list is not paginated — pending items are expected to be small in number (action items, not history). If volume grows, pagination can be added later.

## Data Flow

```
_render_admin_dashboard()
  ├── _admin_kpi_counts(period_filter) → 3 ints
  ├── _admin_pending_counts() → 3 ints
  └── _admin_pending_items() → DataFrame (UNION query)
        └── rendered as action-list cards
```

All queries use the existing `query_df()` helper from `db_ops`. No new DB helpers needed.

## Error Handling

- All KPI queries wrapped in try/except returning 0 on failure (same as rep dashboard `_safe_count` pattern).
- Pending items query wrapped in try/except; on failure show `st.warning("Could not load pending items.")`.

## Testing

- Log in as admin → Dashboard shows "Command Center" header, not rep greeting.
- Log in as rep → Dashboard unchanged.
- Period filter changes all three KPI cards.
- All counts zero → "all clear" message, no action list.
- "Review →" button on each item type navigates to the correct review page.
