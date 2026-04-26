# Delete Visit Feature — Design Spec
**Date:** 2026-04-26
**Status:** Approved

## Overview

Add a "Delete Visit" action to the Force Adjust tab in the Admin Change Requests page. When an admin deletes a visit, its related child records are hard-deleted, the visit row is soft-deleted (hidden everywhere except the All Requests history), and the deletion is recorded as a `request_changes` entry with `change_source = 'DELETE'` so it appears permanently in the history tab.

---

## 1. Database Changes

### New columns on `visits`

```sql
is_deleted  BOOLEAN     NOT NULL DEFAULT FALSE
deleted_at  TIMESTAMPTZ
deleted_by  INTEGER REFERENCES users(user_id)
```

### Enum extension

```sql
ALTER TYPE public.asc_change_source ADD VALUE IF NOT EXISTS 'DELETE';
```

### Migration strategy

A `_run_migrations()` function is added to `db.py` and called at app startup. It uses idempotent guards (`DO $$ IF NOT EXISTS` / `ALTER TYPE ... ADD VALUE IF NOT EXISTS`) so re-runs on an existing DB are safe. `init_db_v11.py` SCHEMA_SQL is also updated for fresh installs.

---

## 2. Deletion Logic

### Function: `_delete_visit(visit_id, admin_uid, note) → (success: bool, error: str | None)`

All five steps execute inside a single SQLAlchemy transaction. If any step raises, the transaction rolls back and nothing is committed.

1. **Guard check** — Verify the visit exists and `is_deleted IS FALSE`. Return error if not found or already deleted.
2. **Auto-reject open requests** — `UPDATE request_changes SET status = 'REJECTED', reject_note = 'Visit was deleted by admin', resolve_date = NOW(), changed_by = :admin_uid WHERE visit_id = :vid AND status = 'IN_REVIEW'`
3. **Hard-delete child records** — `DELETE FROM home_visits WHERE visit_id = :vid` and `DELETE FROM shelf_movement_headers WHERE visit_id = :vid` (shelf_movement_lines cascade automatically).
4. **Insert deletion audit record** — Insert into `request_changes` with `change_source = 'DELETE'`, `status = 'APPROVED'`, `requested_by = admin_uid`, `changed_by = admin_uid`, `request_note = note`, `applied_at = NOW()`, `resolve_date = NOW()`.
5. **Soft-delete the visit** — `UPDATE visits SET is_deleted = TRUE, deleted_at = NOW(), deleted_by = :admin_uid WHERE visit_id = :vid`

---

## 3. UI — Force Adjust Tab

After the visit summary `st.info(...)` block and before the edit form, add:

```
st.expander("🗑️ Delete Visit", expanded=False)
```

Inside the expander:
- `st.warning(...)` explaining this permanently hides the visit and deletes home visit / shelf movement data
- `st.text_area("Deletion reason (required) *")` — admin must fill this
- `st.checkbox("I confirm I want to delete this visit")` — must be checked
- `st.button("🗑️ Delete Visit", type="secondary", disabled=...)` — enabled only when reason is filled and checkbox is checked; styled red via inline CSS (Streamlit `type="primary"` is brand-blue, not red)
- On success: clear the visit selection session state keys, set a success message key, `st.rerun()`

The expander is collapsed by default so it does not intrude on the normal force-adjust workflow.

---

## 4. History Tab — Rendering Deletions

In `_render_request_timeline`, add a branch for `change_source == 'DELETE'`:

- **Source badge:** red `"🗑️ Deleted"` badge (matching the existing amber Force badge style)
- **No diff table** — no field changes exist for a deletion record
- **Admin note** displayed as the reason (same italics style as rep notes)
- **Resolution line:** `st.error(f"Deleted by {resolver} on {applied_str}")`

The existing visit-grouped expander label in `_render_visit_groups` will naturally show the deletion alongside any prior requests for the same visit.

---

## 5. Query Filtering

Add `WHERE COALESCE(v.is_deleted, FALSE) IS FALSE` to every query that lists visits for normal use. The history tab is the **only exception** — deleted visits must remain visible there with their deletion record.

Files to update:

| File | Function/Query | Change |
|---|---|---|
| `admin_change_requests.py` | `_fa_load_all_visits()` | Add is_deleted filter |
| `admin_change_requests.py` | `_load_pending()` | Add is_deleted filter on visits join |
| `admin_change_requests.py` | `_render_history_tab()` | No filter — keep deleted visits visible |
| `change_request.py` | `_load_user_visits()` | Add is_deleted filter |
| `app_pages/dashboard.py` | Any visit list/count queries | Add is_deleted filter |
| `app_pages/admin_data.py` | Any visit list/count queries | Add is_deleted filter |
| `app_pages/my_submissions.py` | Any visit list/count queries | Add is_deleted filter |
| `app_pages/submit_visit.py` | Any visit queries | Add is_deleted filter |
| `app_pages/check_in.py` | Any visit queries | Add is_deleted filter |

---

## 6. Rep Visibility of Deleted Visits

Reps cannot see the Admin Change Requests page, so they never see the `🗑️ Deleted` audit entry there. However, a rep whose visit is deleted should not see it silently disappear — they need to know it was deleted and why.

**`my_submissions.py`** is the rep-facing page that lists their visits. Changes:

- Query includes `is_deleted = TRUE` visits (do **not** filter them out here)
- Deleted visits render with a red `"🗑️ Deleted"` badge alongside the normal visit info
- Below the badge, show the admin's deletion note: sourced by joining `request_changes WHERE change_source = 'DELETE'` for that visit_id, pulling `request_note`
- Deleted visits are shown read-only — no "Request Change" button

**Query filtering table update** (from Section 5):

| File | Function/Query | Change |
|---|---|---|
| `app_pages/my_submissions.py` | Visit list query | Include deleted visits; join request_changes for deletion note |

All other rep-facing pages (dashboard counts, change request visit picker) still exclude deleted visits via the `is_deleted` filter.

---

## 7. Out of Scope

- **Recovery / undelete** — not required; soft-delete is for audit purposes only
- **Bulk deletion** — single visit at a time only
