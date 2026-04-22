# All Requests Visit-Grouped History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the flat table in the "All Requests" tab with visit-grouped accordions that show the full change history for each visit.

**Architecture:** A single function `_render_history_tab()` in `admin_change_requests.py` is replaced entirely. A new enriched SQL query fetches all requests with visit + customer context in one shot. Python groups rows by `visit_id`, builds a status summary per visit, then renders one `st.expander` per visit containing a chronological request timeline.

**Tech Stack:** Python, Streamlit (`st.expander`, `st.markdown`), pandas, existing `_render_diff_table()` and `status_badge()` helpers.

---

### Task 1: Replace the SQL query in `_render_history_tab()`

**Files:**
- Modify: `app_pages/admin_change_requests.py` — `_render_history_tab()` function (currently lines 285–376)

- [ ] **Step 1: Open the file and locate `_render_history_tab()`**

The function starts at line 285. The entire function body will be replaced in this task and Task 2.

- [ ] **Step 2: Replace the function with the new enriched query**

Replace the entire `_render_history_tab()` function with:

```python
def _render_history_tab():
    all_df = query_df(
        """
        SELECT
          rc.request_id,
          rc.visit_id,
          rep.name             AS rep_name,
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
        """
    )

    if all_df.empty:
        st.info("No change requests found.")
        return

    # ── Status filter ─────────────────────────────────────────────────────────
    status_opts = ["All"] + sorted(all_df["status"].unique().tolist())
    status_filter = st.selectbox("Filter by status:", status_opts, key=f"{PAGE_NS}_hist_filter")

    if status_filter != "All":
        # Keep only visits that have at least one request with this status
        visit_ids_with_status = all_df[all_df["status"] == status_filter]["visit_id"].unique()
        all_df = all_df[all_df["visit_id"].isin(visit_ids_with_status)]

    if all_df.empty:
        st.info(f"No requests with status: {status_filter}")
        return

    # ── Group by visit ────────────────────────────────────────────────────────
    # Sort so most-recently-active visit comes first; within each visit,
    # requests are oldest→newest for the timeline.
    all_df["request_date"] = pd.to_datetime(all_df["request_date"], errors="coerce")
    all_df["visit_date"]   = pd.to_datetime(all_df["visit_date"],   errors="coerce")

    latest_per_visit = (
        all_df.groupby("visit_id")["request_date"].max().rename("latest_req_date")
    )
    all_df = all_df.join(latest_per_visit, on="visit_id")
    all_df = all_df.sort_values(
        ["latest_req_date", "visit_id", "request_date"],
        ascending=[False, False, True],
    )

    _render_visit_groups(all_df)
```

- [ ] **Step 3: Commit**

```bash
git add app_pages/admin_change_requests.py
git commit -m "refactor: replace flat history query with enriched visit+customer query"
```

---

### Task 2: Add `_render_visit_groups()` and `_visit_status_summary()`

**Files:**
- Modify: `app_pages/admin_change_requests.py` — add two new functions above `_render_history_tab()`

- [ ] **Step 1: Add `_visit_status_summary()` above `_render_history_tab()`**

This helper takes all rows for one visit and returns a plain-text summary string like `"2 Approved · 1 In Review"`.

```python
_STATUS_LABEL = {
    "APPROVED":  "Approved",
    "REJECTED":  "Rejected",
    "IN_REVIEW": "In Review",
    "WITHDRAWN": "Withdrawn",
}

def _visit_status_summary(visit_rows: pd.DataFrame) -> str:
    counts = visit_rows["status"].value_counts()
    parts = []
    for status in ["IN_REVIEW", "APPROVED", "REJECTED", "WITHDRAWN"]:
        n = counts.get(status, 0)
        if n:
            label = _STATUS_LABEL.get(status, status)
            parts.append(f"{n} {label}")
    return " · ".join(parts) if parts else ""
```

- [ ] **Step 2: Add `_render_visit_groups()` below `_visit_status_summary()`**

```python
def _render_visit_groups(df: pd.DataFrame) -> None:
    _BADGE_VARIANT = {
        "APPROVED":  ("success", "#0e8a4f"),
        "REJECTED":  ("danger",  "#c83333"),
        "IN_REVIEW": ("warning", "#b5651d"),
        "WITHDRAWN": ("neutral", "#444444"),
    }

    for visit_id, group in df.groupby("visit_id", sort=False):
        first = group.iloc[0]
        customer  = str(first.get("customer_name") or "—")
        rep       = str(first.get("rep_name") or "—")
        visit_dt  = first.get("visit_date")
        visit_date_str = (
            pd.to_datetime(visit_dt).strftime("%d %b %Y")
            if pd.notna(visit_dt) else "—"
        )
        summary = _visit_status_summary(group)

        expander_label = (
            f"V-{visit_id}  ·  {customer}  ·  {rep}  ·  {visit_date_str}"
            + (f"        {summary}" if summary else "")
        )

        with st.expander(expander_label):
            _render_request_timeline(group)
```

- [ ] **Step 3: Add `_render_request_timeline()` below `_render_visit_groups()`**

```python
def _render_request_timeline(group: pd.DataFrame) -> None:
    _BADGE_VARIANT = {
        "APPROVED":  "success",
        "REJECTED":  "danger",
        "IN_REVIEW": "warning",
        "WITHDRAWN": "neutral",
    }

    rows = list(group.iterrows())
    for i, (_, row) in enumerate(rows):
        request_id  = int(row["request_id"])
        status_val  = str(row["status"])
        req_date    = row["request_date"]
        req_date_str = (
            pd.to_datetime(req_date).strftime("%d %b %Y, %H:%M")
            if pd.notna(req_date) else "—"
        )

        # ── Request header ────────────────────────────────────────────────────
        badge = status_badge(status_val, _BADGE_VARIANT.get(status_val, "neutral"))
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:10px;'
            f'margin-bottom:6px;">'
            f'<span style="font-size:0.875rem;font-weight:600;color:#0d1117;">'
            f'Request #{request_id}</span>'
            f'<span style="font-size:0.8rem;color:#8b949e;">{req_date_str}</span>'
            f'{badge}'
            f'</div>',
            unsafe_allow_html=True,
        )

        # ── Rep note ──────────────────────────────────────────────────────────
        req_note = str(row.get("request_note") or "").strip()
        if req_note:
            st.markdown(
                f'<p style="font-size:0.85rem;color:#57606a;'
                f'font-style:italic;margin:0 0 8px 0;">"{req_note}"</p>',
                unsafe_allow_html=True,
            )

        # ── Diff table ────────────────────────────────────────────────────────
        diff_df = _load_diff(request_id)
        if not diff_df.empty:
            _render_diff_table(diff_df)
        else:
            st.caption("No field details recorded.")

        # ── Resolution line ───────────────────────────────────────────────────
        if status_val == "APPROVED":
            applied_str = (
                pd.to_datetime(row.get("applied_at")).strftime("%d %b %Y, %H:%M")
                if pd.notna(row.get("applied_at")) else "—"
            )
            resolver = str(row.get("resolved_by") or "—")
            st.success(f"Approved by {resolver} on {applied_str}")

        elif status_val == "REJECTED":
            reject_note = str(row.get("reject_note") or "").strip()
            if reject_note:
                st.error(f"Rejected: {reject_note}")
            else:
                st.error("Rejected.")

        elif status_val == "IN_REVIEW":
            st.info("Pending review")

        elif status_val == "WITHDRAWN":
            resolve_str = (
                pd.to_datetime(row.get("resolve_date")).strftime("%d %b %Y")
                if pd.notna(row.get("resolve_date")) else "—"
            )
            st.caption(f"Withdrawn on {resolve_str}")

        if pd.notna(row.get("apply_error")) and str(row.get("apply_error")).strip():
            st.warning(f"Apply error: {row['apply_error']}")

        # ── Divider between requests (not after last) ─────────────────────────
        if i < len(rows) - 1:
            st.markdown(
                '<hr style="border:none;border-top:1px solid #e4e8ec;margin:12px 0;">',
                unsafe_allow_html=True,
            )
```

- [ ] **Step 4: Verify the file has no syntax errors**

```bash
cd "C:\Users\muazs\OneDrive\Desktop\Streamlit11DD Claude"
python -c "import ast; ast.parse(open('app_pages/admin_change_requests.py').read()); print('OK')"
```

Expected output: `OK`

- [ ] **Step 5: Commit**

```bash
git add app_pages/admin_change_requests.py
git commit -m "feat: visit-grouped accordion history in All Requests tab"
```

---

### Task 3: Remove `_load_visit_context()` call from history tab (cleanup)

**Files:**
- Modify: `app_pages/admin_change_requests.py`

The old `_render_history_tab()` called `_load_visit_context()`. The new version doesn't — the context comes from the enriched query. `_load_visit_context()` itself is still used by Tab 1 (Review Pending), so **do not delete it**.

- [ ] **Step 1: Confirm `_load_visit_context()` is no longer referenced in `_render_history_tab()` or `_render_visit_groups()` or `_render_request_timeline()`**

```bash
cd "C:\Users\muazs\OneDrive\Desktop\Streamlit11DD Claude"
grep -n "_load_visit_context" app_pages/admin_change_requests.py
```

Expected: only one reference remains, inside `page_admin_change_requests()` (Tab 1 block, around line 220). If any reference appears inside `_render_history_tab`, `_render_visit_groups`, or `_render_request_timeline`, remove it.

- [ ] **Step 2: Confirm Tab 1 still works by checking its reference is intact**

```bash
grep -n "_load_visit_context\|_load_pending\|_apply_changes" app_pages/admin_change_requests.py
```

Expected: all three appear inside `page_admin_change_requests()` / Tab 1 block only.

- [ ] **Step 3: Commit cleanup**

```bash
git add app_pages/admin_change_requests.py
git commit -m "chore: confirm _load_visit_context unused in history tab after refactor"
```

---

### Task 4: Manual smoke test

No automated tests are appropriate here (pure Streamlit UI rendering). Run the app and verify the following.

- [ ] **Step 1: Start the app**

```bash
cd "C:\Users\muazs\OneDrive\Desktop\Streamlit11DD Claude"
streamlit run app_v11.py
```

- [ ] **Step 2: Navigate to Review Change Requests → All Requests tab**

Verify:
- The flat table is gone
- Visit group accordions appear, each labeled `V-{id} · {customer} · {rep} · {date}    {summary}`
- Status summary in the header (e.g. `1 Approved · 1 In Review`) is correct

- [ ] **Step 3: Expand a visit with multiple requests**

Verify:
- Requests appear oldest → newest
- Each request shows: header (id, date, badge), rep note if present, diff table, resolution line
- Dividers appear between requests but not after the last one

- [ ] **Step 4: Test the status filter**

Select `APPROVED` — verify only visits that have at least one approved request are shown (visits with only IN_REVIEW requests disappear).

Select `All` — verify all visits return.

- [ ] **Step 5: Verify Tab 1 (Review Pending) is unaffected**

Click the Review Pending tab — confirm pending requests still load, diff table renders, approve/reject buttons work.

- [ ] **Step 6: Commit final**

```bash
git add app_pages/admin_change_requests.py
git commit -m "chore: smoke test passed — visit-grouped history complete"
```
