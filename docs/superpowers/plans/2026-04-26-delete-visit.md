# Delete Visit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Delete Visit" action to the Force Adjust tab so admins can soft-delete visits with an audit trail, and reps can see their deleted visits with the deletion reason in My Visits.

**Architecture:** Visits are soft-deleted (`is_deleted = TRUE`) while child records (`home_visits`, `shelf_movement_headers`/`lines`) are hard-deleted. A `request_changes` row with `change_source = 'DELETE'` records the deletion for the history tab. A `_run_migrations()` function in `db.py` runs idempotent ALTER statements at app startup.

**Tech Stack:** Python, Streamlit, SQLAlchemy, PostgreSQL (psycopg3 dialect via `postgresql+psycopg://`)

---

## File Map

| File | Change |
|---|---|
| `db.py` | Add `_run_migrations()` called at module load |
| `init_db_v11.py` | Update `SCHEMA_SQL` for fresh installs |
| `app_pages/admin_change_requests.py` | Add `_delete_visit()`, delete UI in force tab, DELETE rendering in history |
| `app_pages/change_request.py` | Filter `_load_user_visits()` |
| `app_pages/dashboard.py` | Filter all visit count/list queries |
| `app_pages/admin_data.py` | Filter visit queries in data browser and export |
| `app_pages/my_submissions.py` | Include deleted visits with deletion note; add Deleted filter tab |

---

## Task 1: DB Migration — `db.py` + `init_db_v11.py`

**Files:**
- Modify: `db.py`
- Modify: `init_db_v11.py`

- [ ] **Step 1: Add `_run_migrations()` to `db.py`**

Open `db.py`. After the line `engine = create_engine(...)`, add the import and the function, then call it:

```python
# db.py  — add at the bottom, after the engine = line

from sqlalchemy import text

def _run_migrations() -> None:
    """Idempotent schema migrations. Safe to re-run on every startup."""
    # ALTER TYPE ADD VALUE cannot run inside a normal transaction on PG < 12.
    # Use AUTOCOMMIT isolation to avoid that restriction.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(
            "ALTER TYPE public.asc_change_source ADD VALUE IF NOT EXISTS 'DELETE'"
        ))

    with engine.begin() as conn:
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name   = 'visits'
                      AND column_name  = 'is_deleted'
                ) THEN
                    ALTER TABLE visits
                        ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
                        ADD COLUMN deleted_at TIMESTAMPTZ,
                        ADD COLUMN deleted_by INTEGER REFERENCES users(user_id);
                END IF;
            END $$;
        """))


_run_migrations()
```

- [ ] **Step 2: Update `init_db_v11.py` for fresh installs**

In `init_db_v11.py`, find the `asc_change_source` enum block:
```python
        CREATE TYPE public.asc_change_source AS ENUM ('REQUEST', 'FORCE');
```
Change it to:
```python
        CREATE TYPE public.asc_change_source AS ENUM ('REQUEST', 'FORCE', 'DELETE');
```

Then find the `CREATE TABLE IF NOT EXISTS visits (` block and add three columns before the closing `);`:
```sql
    is_deleted  BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at  TIMESTAMPTZ,
    deleted_by  INTEGER REFERENCES users(user_id),
```
Place them after the `other_customer_name TEXT` line.

- [ ] **Step 3: Verify migration ran**

Restart the Streamlit app (so `db.py` re-imports and `_run_migrations()` runs), then connect to the database and run:

```sql
-- Check enum value exists
SELECT enumlabel FROM pg_enum
JOIN pg_type ON pg_type.oid = pg_enum.enumtypid
WHERE pg_type.typname = 'asc_change_source';
-- Expected: rows for REQUEST, FORCE, DELETE

-- Check columns exist
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = 'visits'
  AND column_name IN ('is_deleted', 'deleted_at', 'deleted_by');
-- Expected: 3 rows
```

- [ ] **Step 4: Commit**

```bash
git add db.py init_db_v11.py
git commit -m "feat: add DB migration for visit soft-delete and DELETE change_source"
```

---

## Task 2: `_delete_visit()` Function

**Files:**
- Modify: `app_pages/admin_change_requests.py`

- [ ] **Step 1: Add `_delete_visit()` after `_apply_force_adjustment()`**

In `admin_change_requests.py`, after the closing `return True, None` of `_apply_force_adjustment` (currently around line 318), insert:

```python
def _delete_visit(visit_id: int, admin_uid: int, note: str):
    """
    Soft-delete a visit. All five steps run in one transaction.
    Returns (success: bool, error_msg: str | None).
    """
    try:
        with engine.begin() as conn:
            # 1. Guard: visit must exist and not already be deleted
            row = conn.execute(
                text(
                    "SELECT visit_id FROM visits "
                    "WHERE visit_id = :vid AND COALESCE(is_deleted, FALSE) IS FALSE"
                ),
                {"vid": visit_id},
            ).fetchone()
            if not row:
                return False, "Visit not found or already deleted."

            # 2. Auto-reject any open change requests for this visit
            conn.execute(
                text(
                    """
                    UPDATE request_changes
                    SET status       = 'REJECTED',
                        reject_note  = 'Visit was deleted by admin',
                        resolve_date = NOW(),
                        changed_by   = :admin_uid
                    WHERE visit_id = :vid AND status = 'IN_REVIEW'
                    """
                ),
                {"admin_uid": admin_uid, "vid": visit_id},
            )

            # 3. Hard-delete child records
            # shelf_movement_lines cascade automatically from shelf_movement_headers
            conn.execute(text("DELETE FROM home_visits WHERE visit_id = :vid"), {"vid": visit_id})
            conn.execute(text("DELETE FROM shelf_movement_headers WHERE visit_id = :vid"), {"vid": visit_id})

            # 4. Insert deletion audit record in request_changes
            conn.execute(
                text(
                    """
                    INSERT INTO request_changes
                      (visit_id, change_source, requested_by, request_note, status,
                       request_date, applied_at, changed_by, resolve_date)
                    VALUES
                      (:vid, 'DELETE', :admin_uid, :note, 'APPROVED',
                       NOW(), NOW(), :admin_uid, NOW())
                    """
                ),
                {"vid": visit_id, "admin_uid": admin_uid, "note": note},
            )

            # 5. Soft-delete the visit row
            conn.execute(
                text(
                    """
                    UPDATE visits
                    SET is_deleted = TRUE,
                        deleted_at = NOW(),
                        deleted_by = :admin_uid
                    WHERE visit_id = :vid
                    """
                ),
                {"admin_uid": admin_uid, "vid": visit_id},
            )

        return True, None
    except Exception as e:
        try:
            # surface the error without swallowing it
            pass
        except Exception:
            pass
        return False, str(e)
```

- [ ] **Step 2: Manually verify logic (no pytest in this project)**

Connect to the DB. Pick a test visit ID (one you can afford to delete in dev). Run:
```sql
-- Before: confirm visit exists
SELECT visit_id, is_deleted FROM visits WHERE visit_id = <test_id>;

-- After calling _delete_visit from a Python shell or Streamlit action:
SELECT visit_id, is_deleted, deleted_at FROM visits WHERE visit_id = <test_id>;
-- Expected: is_deleted = true, deleted_at set

SELECT * FROM request_changes WHERE visit_id = <test_id> AND change_source = 'DELETE';
-- Expected: one row, status = APPROVED

SELECT * FROM home_visits WHERE visit_id = <test_id>;
-- Expected: 0 rows

SELECT * FROM shelf_movement_headers WHERE visit_id = <test_id>;
-- Expected: 0 rows
```

- [ ] **Step 3: Commit**

```bash
git add app_pages/admin_change_requests.py
git commit -m "feat: add _delete_visit() transactional soft-delete function"
```

---

## Task 3: Delete UI in Force Adjust Tab

**Files:**
- Modify: `app_pages/admin_change_requests.py` — `_render_force_tab()`

- [ ] **Step 1: Inject red-button CSS at the top of `_render_force_tab`**

In `_render_force_tab`, immediately after `NS = _FA_NS`, add:

```python
    st.markdown(
        "<style>"
        "div[class*='st-key-admin_change_req_fa_del_btn'] button {"
        "  background-color:#dc2626!important;"
        "  color:#fff!important;"
        "  border-color:#dc2626!important;"
        "}"
        "</style>",
        unsafe_allow_html=True,
    )
```

- [ ] **Step 2: Add delete success banner just above the visit search**

Still near the top of `_render_force_tab`, after the CSS injection and the existing `success_key` success-pop check, add:

```python
    del_success_key = f"{NS}_del_success"
    if st.session_state.get(del_success_key):
        st.success(st.session_state.pop(del_success_key))
```

- [ ] **Step 3: Add the Delete Visit expander after the visit summary `st.info(...)` block**

In `_render_force_tab`, find the visit summary block that ends with `st.markdown("---")` (the divider before the edit form — currently around line 440). Insert the expander AFTER that `st.markdown("---")` line and BEFORE the `st.markdown("#### Customer")` line:

```python
    # ── Delete Visit ──────────────────────────────────────────────────────────
    with st.expander("🗑️ Delete Visit", expanded=False):
        st.warning(
            "⚠️ **This action permanently hides the visit** and deletes any "
            "associated home visit and shelf movement records. It cannot be undone."
        )
        del_reason = st.text_area(
            "Deletion reason (required) *",
            key=f"{NS}_del_reason",
            placeholder="Explain why this visit is being deleted.",
        )
        del_confirm = st.checkbox(
            "I confirm I want to delete this visit",
            key=f"{NS}_del_confirm",
        )
        del_enabled = bool((del_reason or "").strip()) and del_confirm

        if st.button(
            "🗑️ Delete Visit",
            key=f"{NS}_del_btn",
            type="secondary",
            disabled=not del_enabled,
        ):
            ok, err = _delete_visit(visit_id, admin_uid, (del_reason or "").strip())
            if ok:
                st.session_state[del_success_key] = f"Visit V-{visit_id} has been deleted."
                # Clear visit selection state
                for k in list(st.session_state.keys()):
                    if k.startswith(f"{NS}_") and k not in (del_success_key, f"{NS}_search"):
                        del st.session_state[k]
                st.rerun()
            else:
                st.error(f"Delete failed: {err}")
```

- [ ] **Step 4: Browser test the delete UI**

In a running Streamlit app:
1. Go to Admin Change Requests → ⚡ Force Adjust tab
2. Search for and select any visit
3. Confirm the "🗑️ Delete Visit" expander appears below the visit summary, collapsed by default
4. Expand it — verify the warning, text area, checkbox, and red button are visible
5. Try clicking the button without filling in the reason or checking the box — confirm it stays disabled
6. Fill in the reason, check the box, click the button
7. Confirm success message appears, visit selection is cleared, and the visit no longer appears in the search results

- [ ] **Step 5: Commit**

```bash
git add app_pages/admin_change_requests.py
git commit -m "feat: add Delete Visit expander UI to Force Adjust tab"
```

---

## Task 4: Render DELETE Entries in History Tab

**Files:**
- Modify: `app_pages/admin_change_requests.py` — `_render_request_timeline()`

- [ ] **Step 1: Detect DELETE source and add its badge**

In `_render_request_timeline`, find the block that sets `is_force` and `source_badge`. It currently reads:

```python
        is_force = str(row.get("change_source", "")).upper() == "FORCE"
        badge = status_badge(status_val, _BADGE_VARIANT.get(status_val, "neutral"))
        source_badge = (
            '<span style="...">⚡ Force</span>'
            if is_force else
            '<span style="...">👤 Rep</span>'
        )
```

Replace the `is_force` line and `source_badge` assignment with:

```python
        change_source = str(row.get("change_source", "")).upper()
        is_force  = change_source == "FORCE"
        is_delete = change_source == "DELETE"
        badge = status_badge(status_val, _BADGE_VARIANT.get(status_val, "neutral"))
        if is_delete:
            source_badge = (
                '<span style="font-size:0.75rem;font-weight:600;color:#991b1b;'
                'background:#fee2e2;border:1px solid #fca5a5;border-radius:4px;'
                'padding:1px 7px;">🗑️ Deleted</span>'
            )
        elif is_force:
            source_badge = (
                '<span style="font-size:0.75rem;font-weight:600;color:#b45309;'
                'background:#fef3c7;border:1px solid #fcd34d;border-radius:4px;'
                'padding:1px 7px;">⚡ Force</span>'
            )
        else:
            source_badge = (
                '<span style="font-size:0.75rem;font-weight:600;color:#1d4ed8;'
                'background:#eff6ff;border:1px solid #bfdbfe;border-radius:4px;'
                'padding:1px 7px;">👤 Rep</span>'
            )
```

- [ ] **Step 2: Skip diff table for DELETE entries, show admin note instead**

Still in `_render_request_timeline`, find the diff table block:

```python
        # ── Diff table ────────────────────────────────────────────────────────
        diff_df = _load_diff(request_id)
        if not diff_df.empty:
            is_force = str(row.get("change_source", "")).upper() == "FORCE"
            _render_diff_table(
                diff_df,
                before_label="Before" if is_force else "Original",
                after_label="After" if is_force else "Requested",
            )
        else:
            st.caption("No field details recorded.")
```

Replace with:

```python
        # ── Diff table (skip for DELETE — no field changes) ───────────────────
        if not is_delete:
            diff_df = _load_diff(request_id)
            if not diff_df.empty:
                _render_diff_table(
                    diff_df,
                    before_label="Before" if is_force else "Original",
                    after_label="After" if is_force else "Requested",
                )
            else:
                st.caption("No field details recorded.")
```

- [ ] **Step 3: Show "Deleted by" resolution line for DELETE entries**

Find the resolution block that starts with:
```python
        if status_val == "APPROVED":
            applied_str = ...
            resolver = str(row.get("resolved_by") or "—")
            st.success(f"Approved by {resolver} on {applied_str}")
```

Replace **only** the `st.success(...)` call with:

```python
            if is_delete:
                st.error(f"Deleted by {resolver} on {applied_str}")
            else:
                st.success(f"Approved by {resolver} on {applied_str}")
```

- [ ] **Step 4: Browser test history rendering**

After deleting a visit (Task 3), go to the 📋 All Requests tab:
1. Find the visit group — the expander label should include the visit's customer/rep/date
2. Open the expander — confirm the `🗑️ Deleted` red badge is shown
3. Confirm no diff table appears, just the admin note in italics
4. Confirm the resolution line reads "Deleted by [admin name] on [date]" in red

- [ ] **Step 5: Commit**

```bash
git add app_pages/admin_change_requests.py
git commit -m "feat: render DELETE audit entries in All Requests history tab"
```

---

## Task 5: Filter Deleted Visits from Admin Change Requests Queries

**Files:**
- Modify: `app_pages/admin_change_requests.py` — `_fa_load_all_visits()`, `_load_pending()`

- [ ] **Step 1: Filter `_fa_load_all_visits()`**

Find `_fa_load_all_visits()`. Add `WHERE COALESCE(v.is_deleted, FALSE) IS FALSE` before `ORDER BY`:

```python
def _fa_load_all_visits() -> pd.DataFrame:
    return query_df(
        """
        SELECT v.visit_id, v.submitted_at_local, c.account_name, u.name AS rep_name
        FROM visits v
        JOIN customers c ON c.customer_id = v.customer_id
        JOIN users u ON u.user_id = v.user_id
        WHERE COALESCE(v.is_deleted, FALSE) IS FALSE
        ORDER BY v.visit_id DESC
        LIMIT 1000
        """
    )
```

- [ ] **Step 2: Filter `_load_pending()`**

Find `_load_pending()`. Add a JOIN on `visits` and the `is_deleted` filter. Also add `v.visit_id` to the GROUP BY (it's already there via `rc.visit_id`, but we need the JOIN). The full replacement:

```python
def _load_pending() -> pd.DataFrame:
    return query_df(
        """
        SELECT
          rc.request_id,
          rc.visit_id,
          u.name            AS rep_name,
          rc.request_date,
          rc.request_note,
          COUNT(rcd.detail_id) AS fields_changed
        FROM request_changes rc
        JOIN users u ON u.user_id = rc.requested_by
        JOIN visits v ON v.visit_id = rc.visit_id
        LEFT JOIN request_change_details rcd ON rcd.request_id = rc.request_id
        WHERE rc.status = 'IN_REVIEW'
          AND COALESCE(v.is_deleted, FALSE) IS FALSE
        GROUP BY rc.request_id, rc.visit_id, u.name, rc.request_date, rc.request_note
        ORDER BY rc.request_date ASC
        """
    )
```

- [ ] **Step 3: Verify in browser**

After deleting a visit that had an IN_REVIEW request:
- Force Adjust tab: confirm the deleted visit no longer appears in the search dropdown
- Review Pending tab: confirm any request for the deleted visit is no longer shown

- [ ] **Step 4: Commit**

```bash
git add app_pages/admin_change_requests.py
git commit -m "fix: filter soft-deleted visits from Force Adjust and Review Pending queries"
```

---

## Task 6: Filter Deleted Visits from Rep Change Request Page

**Files:**
- Modify: `app_pages/change_request.py` — `_load_user_visits()`

- [ ] **Step 1: Add `is_deleted` filter**

Find `_load_user_visits()`. The `WHERE` clause currently reads `WHERE v.user_id = :uid`. Change it to:

```python
def _load_user_visits(user_id: int) -> list[dict]:
    df = query_df(
        """
        SELECT v.visit_id, v.submitted_at_local, c.account_name
        FROM visits v
        JOIN customers c ON c.customer_id = v.customer_id
        WHERE v.user_id = :uid
          AND COALESCE(v.is_deleted, FALSE) IS FALSE
        ORDER BY v.submitted_at_utc DESC
        LIMIT 300
        """,
        {"uid": int(user_id)},
    )
```

- [ ] **Step 2: Verify in browser**

Log in as a rep. Go to My Change Requests → New Request tab. Confirm that a previously deleted visit no longer appears in the visit picker dropdown.

- [ ] **Step 3: Commit**

```bash
git add app_pages/change_request.py
git commit -m "fix: hide soft-deleted visits from rep change request visit picker"
```

---

## Task 7: Filter Deleted Visits from Dashboard

**Files:**
- Modify: `app_pages/dashboard.py`

- [ ] **Step 1: Filter rep dashboard KPI queries**

In `page_dashboard()`, find the three `_safe_count` calls and the `eval_df` query. Each queries `FROM visits v WHERE v.user_id = :uid {period_filter}`. Add `AND COALESCE(v.is_deleted, FALSE) IS FALSE` to each:

```python
    period_total = _safe_count(
        f"SELECT COUNT(*) FROM visits v "
        f"WHERE v.user_id = :uid AND COALESCE(v.is_deleted, FALSE) IS FALSE {period_filter}",
        {"uid": uid},
    )

    customers_visited = _safe_count(
        f"SELECT COUNT(DISTINCT v.customer_id) FROM visits v "
        f"WHERE v.user_id = :uid AND COALESCE(v.is_deleted, FALSE) IS FALSE {period_filter}",
        {"uid": uid},
    )

    eval_df = query_df(
        f"SELECT evaluation, COUNT(*) AS cnt FROM visits v "
        f"WHERE v.user_id = :uid AND COALESCE(v.is_deleted, FALSE) IS FALSE {period_filter} "
        f"GROUP BY evaluation",
        {"uid": uid},
    ) if period_total > 0 else None
```

- [ ] **Step 2: Filter admin dashboard KPI queries**

In `_render_admin_dashboard()`, find the three `_safe_count` calls:

```python
    total_visits = _safe_count(
        f"SELECT COUNT(*) FROM visits v "
        f"WHERE COALESCE(v.is_deleted, FALSE) IS FALSE {period_filter}"
    )
    unique_customers = _safe_count(
        f"SELECT COUNT(DISTINCT v.customer_id) FROM visits v "
        f"WHERE COALESCE(v.is_deleted, FALSE) IS FALSE {period_filter}"
    )
    active_reps = _safe_count(
        f"SELECT COUNT(DISTINCT v.user_id) FROM visits v "
        f"WHERE COALESCE(v.is_deleted, FALSE) IS FALSE {period_filter}"
    )
```

- [ ] **Step 3: Filter admin pending reviews queries**

In `_render_admin_pending_reviews()`, find the `ta_count` and `oc_count` queries (they query `FROM visits`). Add the filter to each:

```python
    ta_count = _safe_count(
        """
        SELECT COUNT(*) FROM visits
        WHERE audience_id IS NULL
          AND customer_id <> 807
          AND other_audience_name IS NOT NULL
          AND trim(other_audience_name) <> ''
          AND COALESCE(is_deleted, FALSE) IS FALSE
        """
    )
    oc_count = _safe_count(
        "SELECT COUNT(*) FROM visits "
        "WHERE customer_id = 807 AND COALESCE(is_deleted, FALSE) IS FALSE"
    )
```

Also in the big `items_df` UNION query, add `AND COALESCE(v.is_deleted, FALSE) IS FALSE` to both the `Target Audience` and `Other Customer` sub-selects (the `Change Request` sub-select joins `request_changes`, not `visits` directly — leave it as-is since deleted visit requests are auto-rejected).

Find the two `FROM visits v` blocks in the UNION and add to each:
```sql
AND COALESCE(v.is_deleted, FALSE) IS FALSE
```

- [ ] **Step 4: Verify**

After deleting a test visit: confirm the admin and rep dashboard KPI counts decrease by 1, and the deleted visit no longer appears in the pending reviews list.

- [ ] **Step 5: Commit**

```bash
git add app_pages/dashboard.py
git commit -m "fix: exclude soft-deleted visits from dashboard KPI counts and pending reviews"
```

---

## Task 8: Filter Deleted Visits from Admin Data Browser

**Files:**
- Modify: `app_pages/admin_data.py`

- [ ] **Step 1: Filter the count query in the Visits tab**

In `page_admin_data()`, find the `count_sql` inside the `_transactional_table(...)` call for the Visits tab. The current `count_sql` ends with `{where_sql}`. Add the filter:

```python
            count_sql   = f"""
                SELECT COUNT(*) AS n
                FROM visits v
                JOIN users u     ON v.user_id     = u.user_id
                JOIN customers c ON v.customer_id = c.customer_id
                WHERE COALESCE(v.is_deleted, FALSE) IS FALSE
                {('AND ' + where_sql[6:]) if where_sql.startswith('WHERE') else where_sql}
            """,
```

Wait — `where_sql` is built by `_date_rep_cust_where` which returns either `""` or `"WHERE ..."`. So the correct pattern is:

```python
            count_sql   = f"""
                SELECT COUNT(*) AS n
                FROM visits v
                JOIN users u     ON v.user_id     = u.user_id
                JOIN customers c ON v.customer_id = c.customer_id
                WHERE COALESCE(v.is_deleted, FALSE) IS FALSE
                {"AND " + " AND ".join(where_sql.lstrip("WHERE").lstrip().split(" AND ")) if where_sql else ""}
            """,
```

Actually the cleanest approach: change `_date_rep_cust_where` to always return an `AND`-style clause rather than a `WHERE`-style clause — but that touches shared code. Instead, build the final WHERE manually:

Replace the entire `_transactional_table(key_prefix="v", ...)` call's `count_sql` and `data_sql` parameters. The current `count_sql` is:
```python
count_sql = f"""
    SELECT COUNT(*) AS n
    FROM visits v
    JOIN users u     ON v.user_id     = u.user_id
    JOIN customers c ON v.customer_id = c.customer_id
    {where_sql}
""",
```
Change to:
```python
count_sql = f"""
    SELECT COUNT(*) AS n
    FROM visits v
    JOIN users u     ON v.user_id     = u.user_id
    JOIN customers c ON v.customer_id = c.customer_id
    WHERE COALESCE(v.is_deleted, FALSE) IS FALSE
    {"AND " + where_sql[len("WHERE "):] if where_sql.startswith("WHERE ") else ""}
""",
```

Apply the same pattern to `data_sql` (the long SELECT query) — the `{where_sql}` near the bottom becomes:
```python
WHERE COALESCE(v.is_deleted, FALSE) IS FALSE
{"AND " + where_sql[len("WHERE "):] if where_sql.startswith("WHERE ") else ""}
```

- [ ] **Step 2: Filter the bulk export query in `_get_export_tables()`**

Find the `"visits": query_df("""...""")` entry in `_get_export_tables()`. Add `WHERE COALESCE(v.is_deleted, FALSE) IS FALSE` before `ORDER BY v.visit_id DESC`:

```python
        "visits": query_df("""
            SELECT v.*, c.account_name AS customer_name,
                   ...
            FROM visits v
            JOIN customers c        ON v.customer_id = c.customer_id
            ...
            WHERE COALESCE(v.is_deleted, FALSE) IS FALSE
            ORDER BY v.visit_id DESC
        """),
```

- [ ] **Step 3: Verify**

Go to Admin Data → Visits tab. Confirm deleted visits no longer appear in the count or data rows. Download the CSV export and confirm deleted visits are absent.

- [ ] **Step 4: Commit**

```bash
git add app_pages/admin_data.py
git commit -m "fix: exclude soft-deleted visits from admin data browser and export"
```

---

## Task 9: Rep Visibility of Deleted Visits in My Visits

**Files:**
- Modify: `app_pages/my_submissions.py`

- [ ] **Step 1: Update the main list query to include deleted visits and their deletion note**

In `page_my_submissions()`, find the `sql = """..."""` main query. Add two things:
1. `v.is_deleted` to the SELECT
2. A `LEFT JOIN` on `request_changes` for the deletion note
3. Remove the implicit `is_deleted IS FALSE` assumption (no filter — include all visits)

Replace the existing `sql` with:

```python
    sql = """
        SELECT
            v.visit_id,
            v.submitted_at_local,
            v.is_deleted,
            c.account_name   AS customer,
            c.account_id     AS account_id,
            bu.name          AS business_unit,
            bl.name          AS business_line,
            i.description    AS product,
            o.name           AS objective,
            v.evaluation,
            v.latitude, v.longitude, v.accuracy_m,
            v.notes,
            ta.name          AS audience,
            ta.department    AS audience_department,
            ta.position      AS audience_position,
            hv.patient_name, hv.patient_phone, hv.serial_no,
            COALESCE((
                SELECT COUNT(*)
                FROM shelf_movement_lines l
                JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                WHERE h.visit_id = v.visit_id
            ), 0) AS shelf_lines_count,
            COALESCE((
                SELECT SUM(l.qty_checked)
                FROM shelf_movement_lines l
                JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                WHERE h.visit_id = v.visit_id
            ), 0) AS shelf_total_qty,
            del_rc.request_note AS deletion_note
        FROM visits v
        JOIN customers c              ON v.customer_id = c.customer_id
        LEFT JOIN target_audiences ta ON v.audience_id = ta.audience_id
        LEFT JOIN items i             ON v.product_id = i.product_id
        LEFT JOIN business_lines bl   ON bl.business_line_id = v.business_line_id
        LEFT JOIN business_units bu   ON bu.business_unit_id = bl.business_unit_id
        JOIN objectives o             ON v.objective_id = o.objective_id
        LEFT JOIN home_visits hv      ON hv.visit_id = v.visit_id
        LEFT JOIN request_changes del_rc
               ON del_rc.visit_id = v.visit_id
              AND del_rc.change_source = 'DELETE'
              AND del_rc.status = 'APPROVED'
        WHERE v.user_id = :uid
        ORDER BY v.visit_id DESC
    """
```

- [ ] **Step 2: Add a "Deleted" filter tab**

After the existing `df["evaluation"] = df["evaluation"].fillna("").str.strip()` line, add a boolean mask for deleted rows:

```python
    df["is_deleted"] = df["is_deleted"].fillna(False).astype(bool)
```

Then update the `cnt_*` counts and `filter_labels` to add a Deleted count and exclude deleted visits from the evaluation counts:

```python
    # Separate deleted from active
    df_active  = df[~df["is_deleted"]]
    df_deleted = df[df["is_deleted"]]

    cnt_total   = len(df_active)
    cnt_pos     = (df_active["evaluation"] == "Positive").sum()
    cnt_neg     = (df_active["evaluation"] == "Negative").sum()
    cnt_neutral = (df_active["evaluation"] == "Neutral").sum()
    cnt_unrated = ((df_active["evaluation"] == "") | df_active["evaluation"].isna()).sum()
    cnt_deleted = len(df_deleted)

    filter_labels = [
        f"All  {cnt_total}",
        f"Positive  {cnt_pos}",
        f"Negative  {cnt_neg}",
        f"Neutral  {cnt_neutral}",
        f"Unrated  {cnt_unrated}",
        f"Deleted  {cnt_deleted}",
    ]
```

- [ ] **Step 3: Update filter application to handle the Deleted tab**

Find the `chosen = active_filter.split(...)` line and the `if chosen == "All":` block. Add the Deleted case:

```python
    chosen = active_filter.split("  ")[0].strip() if "  " in active_filter else active_filter.strip()
    if chosen == "All":
        visible = df_active
    elif chosen == "Unrated":
        visible = df_active[df_active["evaluation"].isin(["", None]) | df_active["evaluation"].isna()]
    elif chosen == "Deleted":
        visible = df_deleted
    else:
        visible = df_active[df_active["evaluation"] == chosen]
```

- [ ] **Step 4: Render deleted visit cards differently**

In the card-building loop, find `for _, row in page_df.iterrows():`. Replace the entire loop body with:

```python
    cards_html = ""
    for _, row in page_df.iterrows():
        raw_id = int(row["visit_id"])
        vid    = f"V-{raw_id}"
        href   = f"?page=My+Visits&visit_id={raw_id}"

        if row.get("is_deleted"):
            # Deleted visit — red card with deletion note
            deletion_note = str(row.get("deletion_note") or "No reason provided.")
            try:
                dt = pd.to_datetime(row.get("submitted_at_local"), errors="coerce")
                date_str = dt.strftime("%d %b %Y") if dt and not pd.isnull(dt) else "—"
            except Exception:
                date_str = "—"
            import html as _html
            cards_html += (
                '<div style="background:#fff5f5;border:1px solid #fca5a5;border-radius:12px;'
                'padding:1rem 1.25rem;margin-bottom:0.75rem;">'
                '<div style="display:flex;justify-content:space-between;align-items:center;'
                'margin-bottom:0.5rem;">'
                f'<span style="font-weight:600;font-size:0.95rem;color:#991b1b;">{_html.escape(vid)}</span>'
                '<span style="font-size:0.75rem;font-weight:600;color:#991b1b;background:#fee2e2;'
                'border:1px solid #fca5a5;border-radius:4px;padding:1px 7px;">🗑️ Deleted</span>'
                '</div>'
                f'<div style="font-size:0.85rem;color:#57606a;margin-bottom:0.25rem;">'
                f'{_html.escape(str(row.get("customer") or "—"))} · {date_str}</div>'
                f'<div style="font-size:0.8rem;color:#991b1b;font-style:italic;">'
                f'Reason: {_html.escape(deletion_note)}</div>'
                '</div>'
            )
        else:
            eval_val = row.get("evaluation") or ""
            variant  = _EVAL_VARIANT.get(eval_val, "neutral")
            label    = _EVAL_LABEL.get(eval_val, "Unrated")

            audience_name = row.get("audience") or ""
            audience_dept = row.get("audience_department") or ""
            audience_pos  = row.get("audience_position") or ""
            subtitle_parts = [p for p in [audience_name, audience_dept, audience_pos] if p]
            subtitle = " · ".join(subtitle_parts)

            cards_html += visit_card(
                visit_id=vid,
                date_obj=row.get("submitted_at_local"),
                customer=row.get("customer") or "—",
                subtitle=subtitle,
                status=label,
                status_variant=variant,
                href=href,
            )
```

- [ ] **Step 5: Update `_show_visit_detail` to show deleted banner**

In `_show_visit_detail`, find the `sql = """..."""` detail query. Add `v.is_deleted` and the deletion note JOIN to the SELECT:

```python
    sql = """
        SELECT
            v.visit_id,
            v.submitted_at_local,
            v.is_deleted,
            c.account_name        AS customer,
            c.account_id          AS account_id,
            c.region              AS customer_region,
            c.city                AS customer_city,
            c.sector              AS customer_sector,
            v.other_customer_name,
            bu.name               AS business_unit,
            bl.name               AS business_line,
            i.product_id          AS product_id,
            i.description         AS product,
            o.name                AS objective,
            v.evaluation,
            v.latitude, v.longitude, v.accuracy_m,
            v.notes,
            ta.name               AS audience,
            ta.department         AS audience_department,
            ta.position           AS audience_position,
            hv.patient_name, hv.patient_phone, hv.serial_no,
            COALESCE((
                SELECT COUNT(*)
                FROM shelf_movement_lines l
                JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                WHERE h.visit_id = v.visit_id
            ), 0) AS shelf_lines_count,
            del_rc.request_note AS deletion_note
        FROM visits v
        JOIN customers c              ON v.customer_id = c.customer_id
        LEFT JOIN target_audiences ta ON v.audience_id = ta.audience_id
        LEFT JOIN items i             ON v.product_id = i.product_id
        LEFT JOIN business_lines bl   ON bl.business_line_id = v.business_line_id
        LEFT JOIN business_units bu   ON bu.business_unit_id = bl.business_unit_id
        JOIN objectives o             ON v.objective_id = o.objective_id
        LEFT JOIN home_visits hv      ON hv.visit_id = v.visit_id
        LEFT JOIN request_changes del_rc
               ON del_rc.visit_id = v.visit_id
              AND del_rc.change_source = 'DELETE'
              AND del_rc.status = 'APPROVED'
        WHERE v.visit_id = :vid AND v.user_id = :uid
    """
```

Then after the `section_header(...)` call, add the deleted banner:

```python
    if row.get("is_deleted"):
        deletion_note = str(row.get("deletion_note") or "No reason provided.")
        st.error(f"🗑️ **This visit has been deleted.** Reason: {deletion_note}")
```

- [ ] **Step 6: Browser test rep visibility**

Log in as a rep whose visit was deleted:
1. Go to My Visits — confirm the deleted visit appears in the "Deleted N" filter tab
2. Confirm the active filter tabs (All, Positive, etc.) exclude deleted visits from their counts
3. The deleted visit card should show a red background, "🗑️ Deleted" badge, and the admin's deletion note
4. Click through to the detail view — confirm "🗑️ This visit has been deleted. Reason: ..." banner appears at the top

- [ ] **Step 7: Commit**

```bash
git add app_pages/my_submissions.py
git commit -m "feat: show deleted visits to reps in My Visits with deletion reason"
```

---

## Self-Review Checklist

- [x] Spec §1 DB changes: covered in Task 1
- [x] Spec §2 deletion logic: covered in Task 2
- [x] Spec §3 Force Adjust UI: covered in Task 3
- [x] Spec §4 history rendering: covered in Task 4
- [x] Spec §5 query filtering (all files): covered in Tasks 5–8
- [x] Spec §6 rep visibility + deletion note: covered in Task 9
- [x] No TBDs or placeholder steps
- [x] `_delete_visit` signature in Task 3 matches definition in Task 2: `_delete_visit(visit_id, admin_uid, note)`
- [x] `del_success_key` and `f"{NS}_del_*"` key names consistent across Task 3 steps
- [x] `is_delete` variable introduced in Task 4 Step 1 is used in Steps 2 and 3
