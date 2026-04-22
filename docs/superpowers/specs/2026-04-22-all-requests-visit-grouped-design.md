# All Requests Tab — Visit-Grouped History Design

**Date:** 2026-04-22
**Scope:** `app_pages/admin_change_requests.py` — `_render_history_tab()` only

---

## Problem

The current "All Requests" tab shows a flat table of all change requests sorted by date. There is no way to see the history of changes for a specific visit — requests for the same visit are scattered across the table.

## Goal

Group all change requests by visit so that admins can see the full change history for each visit in one place, without navigating away from the tab.

---

## Design

### Layout

The tab renders a vertical list of **visit group accordions**. Each accordion represents one visit that has at least one change request.

**Visit groups are sorted** by their most recent request date descending — the most actively-changed visits appear first.

**Status filter** (selectbox at top) filters which visit groups are shown. A visit group is included if it has at least one request matching the selected status. "All" shows every visit group.

---

### Visit Group Header (collapsed state)

Each accordion header shows, on one line:

```
V-{visit_id}  ·  {customer_name}  ·  {rep_name}  ·  {visit_date}        [2 Approved · 1 Rejected]
```

- Visit ID, customer, rep, and date give the admin enough context to identify the visit without opening it
- The mini status summary (e.g. `2 Approved · 1 Rejected`) shows the outcome history at a glance
- Status summary uses colored text matching badge colors (green for Approved, red for Rejected, amber for In Review)

---

### Expanded Accordion — Request Timeline

Requests inside the accordion are ordered **oldest → newest** (chronological, so you read the history top to bottom).

Each request renders as a compact block:

```
── Request #42  ·  15 Apr 2026  ·  [APPROVED badge]  ──────────────────
Rep note: "Wrong business line selected"

  Field            Original        Requested
  business_line    Cardiology      Oncology

  ✅ Approved by Admin Name on 16 Apr 2026
```

```
── Request #67  ·  20 Apr 2026  ·  [IN_REVIEW badge]  ─────────────────
Rep note: "Customer changed"

  Field            Original        Requested
  customer_id      Al Madar        XYZ Clinic

  ⏳ Pending review
```

Each request block contains:
- Request number, submission date, status badge (inline header)
- Rep note (if present)
- Diff table (Field / Original / Requested) — same `_render_diff_table()` used today
- Resolution line: approval info (who, when) OR rejection reason OR "Pending review"

Dividers (horizontal rules) separate requests within a visit group.

---

## Data / Query Changes

One new query replaces the current flat query in `_render_history_tab()`:

```sql
SELECT
  rc.request_id,
  rc.visit_id,
  rep.name            AS rep_name,
  rc.request_date,
  rc.status,
  COUNT(rcd.detail_id) AS fields_changed,
  resolver.name        AS resolved_by,
  rc.resolve_date,
  rc.applied_at,
  rc.apply_error,
  rc.request_note,
  rc.reject_note,
  c.account_name       AS customer_name,
  v.submitted_at_local AS visit_date
FROM request_changes rc
JOIN users rep ON rep.user_id = rc.requested_by
LEFT JOIN users resolver ON resolver.user_id = rc.changed_by
LEFT JOIN request_change_details rcd ON rcd.request_id = rc.request_id
JOIN visits v ON v.visit_id = rc.visit_id
JOIN customers c ON c.customer_id = v.customer_id
GROUP BY rc.request_id, rc.visit_id, rep.name, rc.request_date, rc.status,
         rc.applied_at, rc.apply_error, rc.request_note, rc.reject_note,
         rc.resolve_date, resolver.name, c.account_name, v.submitted_at_local
ORDER BY rc.request_date DESC
```

This is then grouped in Python by `visit_id` before rendering. No schema changes required.

---

## Code Changes

- `_render_history_tab()` in `admin_change_requests.py` is replaced entirely
- `_load_visit_context()` is no longer needed inside the history tab (data comes from the new query)
- All other functions (`_apply_changes`, `_load_pending`, `_load_diff`, `_render_diff_table`) are unchanged
- Tab 1 (Review Pending) is unchanged

---

## What Is Not Changing

- Tab 1 (Review Pending) — no changes
- The diff table HTML (`_render_diff_table`) — reused as-is
- Database schema — no migrations needed
- The rep-facing change_request.py page — untouched
