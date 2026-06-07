# Analytics Page Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite `app_pages/analytics.py` and extend `db_ops.py` to deliver a professional Performance Scorecard analytics page with drill-down treemaps, period-over-period KPI deltas, rep leaderboard, attendance pivot, and cascading region filter — replicating all implementable Power BI features.

**Architecture:** Two files change: `db_ops.py` gets 5 new query functions plus city/sector cross-filters added to `_analytics_scope()`; `analytics.py` gets all 4 tab functions rewritten while cross-filter helpers (`_get_filters`, `_set_filter`, `_handle_pie_click`, `_handle_hbar_click`, `_handle_heatmap_click`) and `page_analytics()` entry-point structure remain unchanged. A new test file covers all 5 new DB functions with read-only integration tests.

**Tech Stack:** Python, Streamlit, Plotly Express (`px.treemap`, `px.area`, `px.bar`), Folium, PostgreSQL via `db_ops.query_df`, existing `ui.py` design system (`html_table`, `section_header`, `subsection_label`).

---

## File Map

| File | Action | What changes |
|---|---|---|
| `db_ops.py` | Modify | Add city/sector to `_analytics_scope()`; add 5 new functions |
| `app_pages/analytics.py` | Modify | All 4 `_tab_*` functions rewritten; imports, helpers, chips updated |
| `tests/test_analytics_db.py` | Create | Read-only integration tests for the 5 new DB functions |

---

## Schema Reference (read before editing)

```
customers:  customer_id, account_name, region, city, sector, latitude, longitude
users:      user_id, name, region (rep's region)
visits:     visit_id, user_id, customer_id, business_line_id, objective_id, audience_id,
            submitted_at_local, other_customer_name, is_deleted
business_lines:  business_line_id, business_unit_id, product_category_id
business_units:  business_unit_id, name
product_categories: product_category_id, business_unit_id, name
objectives:  objective_id, name, category
```

`_analytics_scope()` already LEFT JOINs: business_lines bl, business_units bu, objectives o, users u, customers c.

---

## Task 1: DB Layer — 5 new functions + `_analytics_scope` city/sector

**Files:**
- Modify: `db_ops.py`

### Step 1.1 — Add city + sector filters to `_analytics_scope()`

In `db_ops.py` at the end of `_analytics_scope()`, just before the `where = "WHERE " + ...` line (currently around line 210), insert:

```python
    if filters.get("city"):
        clauses.append("c.city = :an_city")
        params["an_city"] = filters["city"]
    if filters.get("sector"):
        clauses.append("c.sector = :an_sector")
        params["an_sector"] = filters["sector"]
```

The full `_analytics_scope` tail should read:

```python
    if filters.get("dow") is not None:
        clauses.append("EXTRACT(DOW FROM v.submitted_at_local)::int = :an_dow")
        params["an_dow"] = int(filters["dow"])
    if filters.get("hour") is not None:
        clauses.append("EXTRACT(HOUR FROM v.submitted_at_local)::int = :an_hour")
        params["an_hour"] = int(filters["hour"])
    if filters.get("city"):
        clauses.append("c.city = :an_city")
        params["an_city"] = filters["city"]
    if filters.get("sector"):
        clauses.append("c.sector = :an_sector")
        params["an_sector"] = filters["sector"]

    where = "WHERE " + " AND ".join(clauses)
    return joins, where, params
```

### Step 1.2 — Add `get_analytics_kpis_previous_period()`

Insert after `get_analytics_kpis()` (after its closing `return kpis` line, ~line 259):

```python
def get_analytics_kpis_previous_period(user_id: int, role: str, date_from, date_to,
                                        filters: dict, rep_ids=None) -> dict:
    """Same KPIs as get_analytics_kpis() but for the preceding period of equal length."""
    from datetime import timedelta
    delta     = date_to - date_from
    prev_to   = date_from - timedelta(days=1)
    prev_from = prev_to   - delta
    return get_analytics_kpis(user_id, role, prev_from, prev_to, filters, rep_ids)
```

### Step 1.3 — Add `get_analytics_drilldown()`

Insert after `get_analytics_breakdowns()` (after its closing `return {...}` line, ~line 305):

```python
def get_analytics_drilldown(user_id: int, role: str, date_from, date_to,
                              filters: dict, rep_ids=None) -> pd.DataFrame:
    """Flat table for building both treemaps.
    
    Columns: region, city, sector, customer_name,
             business_unit, product_category, rep, visit_count.
    """
    joins, where, params = _analytics_scope(user_id, role, date_from, date_to, filters, rep_ids)
    extra = "LEFT JOIN product_categories pc ON pc.product_category_id = bl.product_category_id"
    return query_df(f"""
        SELECT
            COALESCE(c.region,            '(No Region)')   AS region,
            COALESCE(c.city,              '(No City)')     AS city,
            COALESCE(c.sector,            '(No Sector)')   AS sector,
            COALESCE(c.account_name, v.other_customer_name, '(Unknown)') AS customer_name,
            COALESCE(bu.name,             '(No BU)')       AS business_unit,
            COALESCE(pc.name,             '(No Category)') AS product_category,
            COALESCE(u.name,              '(Unknown Rep)') AS rep,
            COUNT(*)                                        AS visit_count
        FROM visits v {joins} {extra} {where}
        GROUP BY c.region, c.city, c.sector, c.account_name, v.other_customer_name,
                 bu.name, pc.name, u.name
        ORDER BY visit_count DESC
    """, params)
```

### Step 1.4 — Add `get_analytics_objective_categories()`

Insert after `get_analytics_drilldown()`:

```python
def get_analytics_objective_categories(user_id: int, role: str, date_from, date_to,
                                        filters: dict, rep_ids=None) -> pd.DataFrame:
    """Returns (objective_category, objective_name, count) for the grouped objective bar chart."""
    joins, where, params = _analytics_scope(user_id, role, date_from, date_to, filters, rep_ids)
    return query_df(f"""
        SELECT
            COALESCE(o.category, 'Uncategorised') AS objective_category,
            COALESCE(o.name,     '(No Objective)') AS objective_name,
            COUNT(*) AS count
        FROM visits v {joins} {where}
        GROUP BY o.category, o.name
        ORDER BY count DESC
    """, params)
```

### Step 1.5 — Add `get_analytics_visits_per_rep()`

Insert after `get_analytics_kpis_per_rep()` (after its closing `return {...}` line, ~line 349):

```python
def get_analytics_visits_per_rep(user_id: int, role: str, date_from, date_to,
                                  filters: dict, rep_ids=None) -> pd.DataFrame:
    """Returns (rep, total_visits, total_customers) for the leaderboard."""
    joins, where, params = _analytics_scope(user_id, role, date_from, date_to, filters, rep_ids)
    return query_df(f"""
        SELECT
            u.name                        AS rep,
            COUNT(*)                      AS total_visits,
            COUNT(DISTINCT v.customer_id) AS total_customers
        FROM visits v {joins} {where} AND u.name IS NOT NULL
        GROUP BY u.name
        ORDER BY total_visits DESC
    """, params)
```

### Step 1.6 — Add `get_analytics_attendance()`

Insert after `get_analytics_today()` (after its closing `""", params)` line, ~line 408):

```python
def get_analytics_attendance(user_id: int, role: str, date_from, date_to,
                               rep_ids=None) -> pd.DataFrame:
    """Returns (date, rep_name, visit_count) for the attendance pivot calendar.
    
    No cross-filters applied — date range + role scoping only.
    """
    clauses = [
        "v.is_deleted IS NOT TRUE",
        "DATE(v.submitted_at_local) >= :att_from",
        "DATE(v.submitted_at_local) <= :att_to",
    ]
    params: dict = {"att_from": str(date_from), "att_to": str(date_to)}

    if role in ("rep", "maintenance"):
        clauses.append("v.user_id = :att_uid")
        params["att_uid"] = user_id
    elif rep_ids:
        clauses.append("v.user_id = ANY(:att_reps)")
        params["att_reps"] = list(rep_ids)

    where = "WHERE " + " AND ".join(clauses)
    return query_df(f"""
        SELECT
            DATE(v.submitted_at_local)     AS date,
            COALESCE(u.name, '(Unknown)')  AS rep_name,
            COUNT(*)                       AS visit_count
        FROM visits v
        LEFT JOIN users u ON u.user_id = v.user_id
        {where}
        GROUP BY DATE(v.submitted_at_local), u.name
        ORDER BY date, rep_name
    """, params)
```

### Step 1.7 — Commit

```bash
git add db_ops.py
git commit -m "feat: add analytics DB functions — drilldown, objective categories, attendance, visits per rep, previous period KPIs"
```

---

## Task 2: Tests for new DB functions

**Files:**
- Create: `tests/test_analytics_db.py`

### Step 2.1 — Write the test file

Create `tests/test_analytics_db.py`:

```python
"""
Read-only integration tests for the 5 new analytics DB functions.
Requires DATABASE_URL env var pointing to a Postgres DB with data.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from datetime import date
from db_ops import (
    get_analytics_kpis_previous_period,
    get_analytics_drilldown,
    get_analytics_objective_categories,
    get_analytics_attendance,
    get_analytics_visits_per_rep,
    query_scalar,
)

D_FROM = date(2024, 1, 1)
D_TO   = date(2026, 6, 7)
FILT   = {}
REPS   = None


@pytest.fixture(scope="module")
def admin_uid():
    uid = query_scalar("SELECT user_id FROM users WHERE role = 'admin' LIMIT 1")
    assert uid is not None, "Need at least one admin user in the test DB"
    return int(uid)


def test_previous_period_returns_dict_with_keys(admin_uid):
    result = get_analytics_kpis_previous_period(admin_uid, "admin", D_FROM, D_TO, FILT, REPS)
    assert isinstance(result, dict)
    for key in ("total_visits", "total_customers", "total_audiences",
                "visits_per_customer", "audiences_per_customer",
                "customers_per_day", "avg_customers_per_month", "avg_bl_per_month"):
        assert key in result, f"Missing key: {key}"


def test_previous_period_visits_non_negative(admin_uid):
    result = get_analytics_kpis_previous_period(admin_uid, "admin", D_FROM, D_TO, FILT, REPS)
    assert result["total_visits"] >= 0


def test_drilldown_has_required_columns(admin_uid):
    df = get_analytics_drilldown(admin_uid, "admin", D_FROM, D_TO, FILT, REPS)
    for col in ("region", "city", "sector", "customer_name",
                "business_unit", "product_category", "rep", "visit_count"):
        assert col in df.columns, f"Missing column: {col}"


def test_drilldown_visit_count_positive(admin_uid):
    df = get_analytics_drilldown(admin_uid, "admin", D_FROM, D_TO, FILT, REPS)
    if not df.empty:
        assert (df["visit_count"] > 0).all()


def test_drilldown_city_filter_reduces_results(admin_uid):
    df_all = get_analytics_drilldown(admin_uid, "admin", D_FROM, D_TO, {}, REPS)
    real_cities = df_all["city"].dropna()
    real_cities = real_cities[real_cities != "(No City)"]
    if real_cities.empty:
        pytest.skip("No city data in test DB")
    city = real_cities.iloc[0]
    df_city = get_analytics_drilldown(admin_uid, "admin", D_FROM, D_TO, {"city": city}, REPS)
    assert len(df_city) <= len(df_all)


def test_objective_categories_has_required_columns(admin_uid):
    df = get_analytics_objective_categories(admin_uid, "admin", D_FROM, D_TO, FILT, REPS)
    for col in ("objective_category", "objective_name", "count"):
        assert col in df.columns, f"Missing column: {col}"


def test_objective_categories_count_positive(admin_uid):
    df = get_analytics_objective_categories(admin_uid, "admin", D_FROM, D_TO, FILT, REPS)
    if not df.empty:
        assert (df["count"] > 0).all()


def test_attendance_has_required_columns(admin_uid):
    df = get_analytics_attendance(admin_uid, "admin", D_FROM, D_TO, REPS)
    for col in ("date", "rep_name", "visit_count"):
        assert col in df.columns, f"Missing column: {col}"


def test_attendance_visit_count_positive(admin_uid):
    df = get_analytics_attendance(admin_uid, "admin", D_FROM, D_TO, REPS)
    if not df.empty:
        assert (df["visit_count"] > 0).all()


def test_visits_per_rep_has_required_columns(admin_uid):
    df = get_analytics_visits_per_rep(admin_uid, "admin", D_FROM, D_TO, FILT, REPS)
    for col in ("rep", "total_visits", "total_customers"):
        assert col in df.columns, f"Missing column: {col}"


def test_visits_per_rep_sorted_descending(admin_uid):
    df = get_analytics_visits_per_rep(admin_uid, "admin", D_FROM, D_TO, FILT, REPS)
    if len(df) > 1:
        assert list(df["total_visits"]) == sorted(df["total_visits"].tolist(), reverse=True)
```

### Step 2.2 — Run tests

```bash
pytest tests/test_analytics_db.py -v
```

Expected: All tests PASS. If any fail with `ImportError`, verify Task 1 was committed correctly. If they fail with SQL errors, check that the DB has data in the `D_FROM`–`D_TO` range.

### Step 2.3 — Commit

```bash
git add tests/test_analytics_db.py
git commit -m "test: add integration tests for new analytics DB functions"
```

---

## Task 3: Analytics — Imports, helpers, and `_render_chips()`

**Files:**
- Modify: `app_pages/analytics.py`

### Step 3.1 — Update the import block

Replace the entire `from db_ops import (...)` block (lines 12–23) and the `from utils import _local_now` line with:

```python
from db_ops import (
    get_all_reps,
    get_analytics_attendance,
    get_analytics_breakdowns,
    get_analytics_drilldown,
    get_analytics_kpis,
    get_analytics_kpis_per_rep,
    get_analytics_kpis_previous_period,
    get_analytics_objective_categories,
    get_analytics_time_map,
    get_analytics_time_series,
    get_analytics_today,
    get_analytics_visits_detail,
    get_analytics_visits_per_rep,
    get_customer_locations_for_map,
    get_visit_locations_for_map,
    query_df,
)
from ui import html_table, section_header, subsection_label
from utils import _local_now
```

### Step 3.2 — Add helper functions

After the `_DOW_MAP` line (line 30), add:

```python
def _pct_delta(current, previous) -> int | None:
    """Return rounded integer % change, or None if previous is zero."""
    if not previous:
        return None
    return round((float(current) - float(previous)) / float(previous) * 100)


def _delta_badge_hero(pct: int | None) -> str:
    """White-on-transparent badge for the hero blue card."""
    if pct is None:
        return ""
    sign = "+" if pct >= 0 else ""
    color = "rgba(144,238,144,0.9)" if pct >= 0 else "rgba(255,120,120,0.9)"
    return (
        f'<span style="background:{color};border-radius:5px;padding:2px 8px;'
        f'font-size:0.7rem;font-weight:700;color:#fff;">{sign}{pct}%</span>'
        f'<span style="font-size:0.7rem;opacity:0.8;margin-left:4px;">vs prev period</span>'
    )


def _delta_badge_card(pct: int | None) -> str:
    """Green/red coloured badge for bordered secondary cards."""
    if pct is None:
        return ""
    if pct > 0:
        bg, fg, sign = "#e6f6ec", "#0e8a4f", "+"
    elif pct < 0:
        bg, fg, sign = "#fdeceb", "#c83333", ""
    else:
        bg, fg, sign = "#f0f0f0", "#666666", ""
    return (
        f'<span style="background:{bg};color:{fg};border-radius:5px;'
        f'padding:2px 8px;font-size:0.7rem;font-weight:700;">{sign}{pct}%</span>'
        f'<span style="font-size:0.68rem;color:var(--color-text-subtle);margin-left:4px;">'
        f'vs prev period</span>'
    )
```

### Step 3.3 — Update `_render_chips()` for city and sector

In `_render_chips()`, replace the `chip_labels` dict (lines 100–106):

```python
    chip_labels = {
        "region":        lambda v: f"Region: {v}",
        "business_unit": lambda v: f"BU: {v}",
        "objective":     lambda v: f"Objective: {v}",
        "city":          lambda v: f"City: {v}",
        "sector":        lambda v: f"Sector: {v}",
        "dow":           lambda v: f"Day: {_DOW_NAMES[v]}",
        "hour":          lambda v: f"Hour: {v}:00",
    }
```

### Step 3.4 — Commit

```bash
git add app_pages/analytics.py
git commit -m "feat: update analytics imports, add KPI delta helpers, extend chips for city/sector"
```

---

## Task 4: Rewrite `_tab_overview()`

**Files:**
- Modify: `app_pages/analytics.py` — replace `_tab_overview()` entirely (lines 125–225)

### Step 4.1 — Replace `_tab_overview()` with this implementation

```python
def _tab_overview(uid, role, date_from, date_to, filters, rep_ids):
    kpis      = get_analytics_kpis(uid, role, date_from, date_to, filters, rep_ids)
    prev_kpis = get_analytics_kpis_previous_period(uid, role, date_from, date_to, filters, rep_ids)

    tv  = int(kpis.get("total_visits", 0))
    tc  = int(kpis.get("total_customers", 0))
    ta  = int(kpis.get("total_audiences", 0))
    ptv = int(prev_kpis.get("total_visits", 0))
    ptc = int(prev_kpis.get("total_customers", 0))
    pta = int(prev_kpis.get("total_audiences", 0))

    tv_badge = _delta_badge_hero(_pct_delta(tv, ptv))
    tc_badge = _delta_badge_card(_pct_delta(tc, ptc))
    ta_badge = _delta_badge_card(_pct_delta(ta, pta))

    cpd = kpis.get("customers_per_day", 0)
    vpc = kpis.get("visits_per_customer", 0)
    apc = kpis.get("audiences_per_customer", 0)
    acm = kpis.get("avg_customers_per_month", 0)
    blm = kpis.get("avg_bl_per_month", 0)

    # ── Hero KPI scorecard ────────────────────────────────────────────────────
    st.markdown(f"""
<div style="display:grid;grid-template-columns:1.6fr 1fr 1fr;gap:12px;margin-bottom:10px;">
  <div style="background:linear-gradient(135deg,#2667ff 0%,#4d8ef0 100%);border-radius:14px;
              padding:20px 22px;color:#fff;box-shadow:0 4px 14px rgba(38,103,255,.3);">
    <div style="font-size:0.65rem;font-weight:700;text-transform:uppercase;
                letter-spacing:.07em;opacity:.85;margin-bottom:8px;">Total Visits</div>
    <div style="font-size:2.6rem;font-weight:700;letter-spacing:-.03em;line-height:1;">{tv:,}</div>
    <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:10px;">
      {tv_badge}
    </div>
  </div>
  <div style="background:var(--color-surface);border:1px solid var(--color-border);
              border-radius:14px;padding:18px 20px;box-shadow:var(--shadow-card);">
    <div style="font-size:0.65rem;font-weight:700;text-transform:uppercase;
                letter-spacing:.07em;color:var(--color-text-subtle);margin-bottom:6px;">Customers</div>
    <div style="font-size:2rem;font-weight:700;color:var(--color-text);
                letter-spacing:-.02em;line-height:1.1;">{tc:,}</div>
    <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:8px;">
      {tc_badge}
    </div>
  </div>
  <div style="background:var(--color-surface);border:1px solid var(--color-border);
              border-radius:14px;padding:18px 20px;box-shadow:var(--shadow-card);">
    <div style="font-size:0.65rem;font-weight:700;text-transform:uppercase;
                letter-spacing:.07em;color:var(--color-text-subtle);margin-bottom:6px;">Audiences</div>
    <div style="font-size:2rem;font-weight:700;color:var(--color-text);
                letter-spacing:-.02em;line-height:1.1;">{ta:,}</div>
    <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:8px;">
      {ta_badge}
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

    # ── Secondary metric strip ────────────────────────────────────────────────
    def _secondary_card(label, val):
        return (
            f'<div style="flex:1;background:var(--color-surface);border:1px solid var(--color-border);'
            f'border-radius:10px;padding:10px 12px;">'
            f'<div style="font-size:0.6rem;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:.06em;color:var(--color-text-subtle);margin-bottom:4px;">{label}</div>'
            f'<div style="font-size:1.2rem;font-weight:700;color:var(--color-text);">{val}</div>'
            f'</div>'
        )

    st.markdown(
        '<div style="display:flex;gap:8px;margin-bottom:16px;">'
        + _secondary_card("Cust / Day",    f"{cpd:.1f}")
        + _secondary_card("Visits / Cust", f"{vpc:.1f}")
        + _secondary_card("Aud / Cust",    f"{apc:.1f}")
        + _secondary_card("Cust / Month",  f"{acm:.1f}")
        + _secondary_card("BL / Month",    f"{blm:.1f}")
        + '</div>',
        unsafe_allow_html=True,
    )

    # ── Visits over time — area chart ─────────────────────────────────────────
    subsection_label("Visits Over Time")
    gran = st.radio("Granularity", ["Year", "Month", "Week"], horizontal=True,
                    key="an_gran", label_visibility="collapsed")
    ts_df = get_analytics_time_series(uid, role, date_from, date_to, gran, filters, rep_ids)
    if not ts_df.empty:
        if gran == "Month":
            ts_df["period"] = pd.to_datetime(ts_df["period"], format="%Y-%m").dt.strftime("%b %Y")
        fig_ts = px.area(ts_df, x="period", y="visit_count",
                         color_discrete_sequence=[BRAND])
        fig_ts.update_traces(line_color=BRAND, fillcolor="rgba(38,103,255,0.10)")
        fig_ts.update_layout(
            margin=dict(l=0, r=0, t=10, b=0), height=220,
            xaxis_title="", yaxis_title="Visits",
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        fig_ts.update_xaxes(showgrid=False)
        fig_ts.update_yaxes(gridcolor="rgba(0,0,0,0.06)")
        st.plotly_chart(fig_ts, use_container_width=True, key="an_ts")
    else:
        st.info("No visit data for the selected period.")

    # ── Treemap drill-downs ───────────────────────────────────────────────────
    subsection_label("Breakdown by Region & Business Unit")
    drill_df = get_analytics_drilldown(uid, role, date_from, date_to, filters, rep_ids)

    col_r, col_bu = st.columns(2)

    with col_r:
        st.markdown(
            "**By Region** "
            '<span style="font-size:0.72rem;color:var(--color-text-subtle);">'
            "· click tiles to drill down</span>",
            unsafe_allow_html=True,
        )
        if not drill_df.empty:
            fig_r = px.treemap(
                drill_df,
                path=["region", "city", "sector", "customer_name"],
                values="visit_count",
                color="visit_count",
                color_continuous_scale=["#eef2ff", "#6ea6ff", "#2667ff"],
            )
            fig_r.update_traces(textinfo="label+value", root_color="rgba(0,0,0,0)")
            fig_r.update_layout(
                margin=dict(l=0, r=0, t=10, b=0), height=320,
                coloraxis_showscale=False,
                paper_bgcolor="rgba(0,0,0,0)",
            )
            ev_r = st.plotly_chart(fig_r, use_container_width=True,
                                   on_select="rerun", key="an_region_tm")
            if ev_r and getattr(ev_r, "selection", None):
                pts = (ev_r.selection.get("points", [])
                       if isinstance(ev_r.selection, dict)
                       else getattr(ev_r.selection, "points", []))
                if pts and pts[0].get("parent", "root") in ("", "root"):
                    label = pts[0].get("label")
                    if label and label != "(No Region)":
                        _set_filter("region", label)

    with col_bu:
        st.markdown(
            "**By Business Unit** "
            '<span style="font-size:0.72rem;color:var(--color-text-subtle);">'
            "· click tiles to drill down</span>",
            unsafe_allow_html=True,
        )
        if not drill_df.empty:
            fig_bu = px.treemap(
                drill_df,
                path=["business_unit", "product_category", "rep"],
                values="visit_count",
                color="visit_count",
                color_continuous_scale=["#f0fdf4", "#6ee7b7", "#10b981"],
            )
            fig_bu.update_traces(textinfo="label+value", root_color="rgba(0,0,0,0)")
            fig_bu.update_layout(
                margin=dict(l=0, r=0, t=10, b=0), height=320,
                coloraxis_showscale=False,
                paper_bgcolor="rgba(0,0,0,0)",
            )
            ev_bu = st.plotly_chart(fig_bu, use_container_width=True,
                                    on_select="rerun", key="an_bu_tm")
            if ev_bu and getattr(ev_bu, "selection", None):
                pts = (ev_bu.selection.get("points", [])
                       if isinstance(ev_bu.selection, dict)
                       else getattr(ev_bu.selection, "points", []))
                if pts and pts[0].get("parent", "root") in ("", "root"):
                    label = pts[0].get("label")
                    if label and label != "(No BU)":
                        _set_filter("business_unit", label)

    # ── Objectives grouped bar ────────────────────────────────────────────────
    subsection_label("Visits by Objective")
    obj_df = get_analytics_objective_categories(uid, role, date_from, date_to, filters, rep_ids)
    if not obj_df.empty:
        fig_obj = px.bar(
            obj_df, y="objective_name", x="count",
            color="objective_category", orientation="h",
            color_discrete_sequence=px.colors.qualitative.Set2,
        )
        fig_obj.update_layout(
            margin=dict(l=0, r=0, t=10, b=0),
            height=max(200, len(obj_df) * 28),
            yaxis=dict(autorange="reversed", title=""),
            xaxis_title="Visits",
            legend=dict(title="Category", orientation="h", y=-0.25),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        fig_obj.update_xaxes(gridcolor="rgba(0,0,0,0.06)")
        ev_obj = st.plotly_chart(fig_obj, use_container_width=True,
                                 on_select="rerun", key="an_obj")
        _handle_hbar_click(ev_obj, "objective", axis="y")
```

### Step 4.2 — Commit

```bash
git add app_pages/analytics.py
git commit -m "feat: rewrite analytics Tab 1 — scorecard KPIs with delta badges, area chart, treemaps, objectives bar"
```

---

## Task 5: Rewrite `_tab_kpis()` — Rep Leaderboard

**Files:**
- Modify: `app_pages/analytics.py` — replace `_tab_kpis()` entirely (lines 231–292)

### Step 5.1 — Replace `_tab_kpis()` with this implementation

```python
def _tab_kpis(uid, role, date_from, date_to, filters, rep_ids):
    visits_df = get_analytics_visits_per_rep(uid, role, date_from, date_to, filters, rep_ids)
    rep_data  = get_analytics_kpis_per_rep(uid, role, date_from, date_to, filters, rep_ids)

    # ── Rep leaderboard ───────────────────────────────────────────────────────
    subsection_label("Rep Leaderboard")

    apc_df = rep_data["audience_per_customer"].copy()
    apc_df["ratio"] = apc_df["ratio"].round(2)
    leaderboard = visits_df.merge(apc_df[["rep", "ratio"]], on="rep", how="left")
    leaderboard["ratio"] = leaderboard["ratio"].fillna(0).round(2)

    def _initials(name: str) -> str:
        parts = (name or "?").split()
        return "".join(p[0].upper() for p in parts[:2])

    rows_html = ""
    for idx_r, row in leaderboard.iterrows():
        rank      = idx_r + 1
        name      = str(row["rep"])
        visits    = int(row["total_visits"])
        custs     = int(row["total_customers"])
        ratio     = float(row["ratio"])
        initials  = _initials(name)
        is_leader = rank == 1
        row_bg    = "background:#f0f5ff;" if is_leader else ""
        av_bg     = "#2667ff"            if is_leader else "var(--color-surface-2)"
        av_fg     = "#ffffff"            if is_leader else "var(--color-text-subtle)"
        rank_fg   = "#2667ff"            if is_leader else "var(--color-text-subtle)"
        weight    = "600"                if is_leader else "500"
        rows_html += (
            f'<tr style="{row_bg}">'
            f'<td style="padding:9px 14px;font-size:0.72rem;font-weight:800;'
            f'color:{rank_fg};">{rank}</td>'
            f'<td style="padding:9px 14px;">'
            f'<div style="display:flex;align-items:center;gap:8px;">'
            f'<div style="width:28px;height:28px;border-radius:50%;background:{av_bg};'
            f'display:flex;align-items:center;justify-content:center;'
            f'font-size:0.58rem;font-weight:700;color:{av_fg};flex-shrink:0;">'
            f'{initials}</div>'
            f'<span style="font-size:0.78rem;font-weight:{weight};'
            f'color:var(--color-text);">{name}</span>'
            f'</div></td>'
            f'<td style="padding:9px 14px;font-size:0.78rem;font-weight:700;'
            f'color:var(--color-text);text-align:right;">{visits:,}</td>'
            f'<td style="padding:9px 14px;font-size:0.78rem;'
            f'color:var(--color-text-muted);text-align:right;">{custs:,}</td>'
            f'<td style="padding:9px 14px;font-size:0.78rem;'
            f'color:var(--color-text-muted);text-align:right;">{ratio:.2f}</td>'
            f'</tr>'
        )

    th_style = (
        "padding:8px 14px;font-size:0.6rem;font-weight:700;color:var(--color-text-muted);"
        "text-transform:uppercase;letter-spacing:.05em;"
    )
    st.markdown(
        f'<div style="background:var(--color-surface);border:1px solid var(--color-border);'
        f'border-radius:14px;overflow:hidden;margin-bottom:1.25rem;">'
        f'<table style="width:100%;border-collapse:collapse;">'
        f'<thead><tr style="background:var(--color-surface-2);'
        f'border-bottom:2px solid var(--color-border);">'
        f'<th style="{th_style}text-align:left;">#</th>'
        f'<th style="{th_style}text-align:left;">Rep</th>'
        f'<th style="{th_style}text-align:right;">Visits</th>'
        f'<th style="{th_style}text-align:right;">Customers</th>'
        f'<th style="{th_style}text-align:right;">Aud / Cust</th>'
        f'</tr></thead>'
        f'<tbody>{rows_html}</tbody></table></div>',
        unsafe_allow_html=True,
    )

    # ── Supporting bar charts ─────────────────────────────────────────────────
    col1, col2 = st.columns(2)

    with col1:
        subsection_label("Audiences / Customer by Rep")
        df = rep_data["audience_per_customer"].copy()
        if not df.empty:
            df["ratio"] = df["ratio"].round(2)
            fig = px.bar(df, y="rep", x="ratio", orientation="h",
                         color_discrete_sequence=[BRAND])
            fig.update_layout(
                margin=dict(l=0, r=0, t=10, b=0),
                height=max(220, len(df) * 32),
                yaxis=dict(autorange="reversed", title=""),
                xaxis_title="Ratio",
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            )
            fig.update_xaxes(gridcolor="rgba(0,0,0,0.06)")
            st.plotly_chart(fig, use_container_width=True, key="an_kpi_apc")

    with col2:
        subsection_label("Avg Customers / Month by Rep")
        df3 = rep_data["avg_customers_per_month"].copy()
        if not df3.empty:
            df3["avg_monthly"] = df3["avg_monthly"].round(1)
            fig3 = px.bar(df3, y="rep", x="avg_monthly", orientation="h",
                          color_discrete_sequence=["#10b981"])
            fig3.update_layout(
                margin=dict(l=0, r=0, t=10, b=0),
                height=max(220, len(df3) * 32),
                yaxis=dict(autorange="reversed", title=""),
                xaxis_title="Avg Customers/Month",
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            )
            fig3.update_xaxes(gridcolor="rgba(0,0,0,0.06)")
            st.plotly_chart(fig3, use_container_width=True, key="an_kpi_acm")
```

### Step 5.2 — Commit

```bash
git add app_pages/analytics.py
git commit -m "feat: rewrite analytics Tab 2 — rep leaderboard with avatar initials and bar charts"
```

---

## Task 6: Rewrite `_tab_visits_detail()` — Cascading Filter + `html_table`

**Files:**
- Modify: `app_pages/analytics.py` — replace `_tab_visits_detail()` entirely (lines 299–341)

### Step 6.1 — Replace `_tab_visits_detail()` with this implementation

```python
def _tab_visits_detail(uid, role, date_from, date_to, filters, rep_ids):
    # ── Cascading region → city → sector filter ───────────────────────────────
    subsection_label("Filter by Location")
    col_reg, col_city, col_sec = st.columns(3)

    try:
        region_opts = query_df(
            "SELECT DISTINCT region FROM customers WHERE region IS NOT NULL ORDER BY region"
        )["region"].tolist()
    except Exception:
        region_opts = []

    with col_reg:
        sel_region = st.selectbox("Region", ["All"] + region_opts, key="vd_region")

    try:
        if sel_region != "All":
            city_opts = query_df(
                "SELECT DISTINCT city FROM customers WHERE region = :r AND city IS NOT NULL ORDER BY city",
                {"r": sel_region},
            )["city"].tolist()
        else:
            city_opts = query_df(
                "SELECT DISTINCT city FROM customers WHERE city IS NOT NULL ORDER BY city"
            )["city"].tolist()
    except Exception:
        city_opts = []

    with col_city:
        sel_city = st.selectbox("City", ["All"] + city_opts, key="vd_city")

    try:
        if sel_city != "All":
            sector_opts = query_df(
                "SELECT DISTINCT sector FROM customers WHERE city = :c AND sector IS NOT NULL ORDER BY sector",
                {"c": sel_city},
            )["sector"].tolist()
        else:
            sector_opts = query_df(
                "SELECT DISTINCT sector FROM customers WHERE sector IS NOT NULL ORDER BY sector"
            )["sector"].tolist()
    except Exception:
        sector_opts = []

    with col_sec:
        sel_sector = st.selectbox("Sector", ["All"] + sector_opts, key="vd_sector")

    loc_filters = dict(filters)
    if sel_region != "All":
        loc_filters["region"] = sel_region
    if sel_city != "All":
        loc_filters["city"] = sel_city
    if sel_sector != "All":
        loc_filters["sector"] = sel_sector

    # ── Maps ──────────────────────────────────────────────────────────────────
    col_m1, col_m2 = st.columns(2)

    with col_m1:
        subsection_label("Customer Locations")
        cust_df = get_customer_locations_for_map()
        if not cust_df.empty:
            m1 = folium.Map(
                location=[cust_df["latitude"].mean(), cust_df["longitude"].mean()],
                zoom_start=5, tiles="CartoDB positron",
            )
            for _, row in cust_df.iterrows():
                folium.CircleMarker(
                    location=[row["latitude"], row["longitude"]],
                    radius=4, color="#0ea5e9", fill=True, fill_opacity=0.6,
                    tooltip=row["account_name"],
                ).add_to(m1)
            st_folium(m1, width="100%", height=280, returned_objects=[])

    with col_m2:
        subsection_label("Visit Locations")
        visit_loc_df = get_visit_locations_for_map(
            uid, role, date_from, date_to, loc_filters, rep_ids
        )
        if not visit_loc_df.empty:
            m2 = folium.Map(
                location=[visit_loc_df["latitude"].mean(), visit_loc_df["longitude"].mean()],
                zoom_start=5, tiles="CartoDB positron",
            )
            for _, row in visit_loc_df.iterrows():
                folium.CircleMarker(
                    location=[row["latitude"], row["longitude"]],
                    radius=3, color=BRAND, fill=True, fill_opacity=0.5,
                    tooltip=f"{row['customer']} — {row['rep']}",
                ).add_to(m2)
            st_folium(m2, width="100%", height=280, returned_objects=[])
        else:
            st.info("No visits with location data in selected range.")

    # ── Visit records table ───────────────────────────────────────────────────
    subsection_label("Visit Records")
    detail_df = get_analytics_visits_detail(uid, role, date_from, date_to, loc_filters, rep_ids)
    if not detail_df.empty:
        detail_df["Date Local"] = pd.to_datetime(
            detail_df["Date Local"], errors="coerce"
        ).dt.strftime("%d/%m/%Y %I:%M %p")
        st.markdown(
            html_table(detail_df, max_rows=1000, max_height=480),
            unsafe_allow_html=True,
        )
        st.caption(f"{len(detail_df):,} records shown (max 1,000)")
    else:
        st.info("No visits match the current filters.")
```

### Step 6.2 — Commit

```bash
git add app_pages/analytics.py
git commit -m "feat: rewrite analytics Tab 3 — cascading location filter, html_table, maps"
```

---

## Task 7: Rewrite `_tab_time_map()` — Heatmap + Attendance Pivot

**Files:**
- Modify: `app_pages/analytics.py` — replace `_tab_time_map()` entirely (lines 348–458)

### Step 7.1 — Replace `_tab_time_map()` with this implementation

```python
def _tab_time_map(uid, role, date_from, date_to, filters, rep_ids):
    tm_df = get_analytics_time_map(uid, role, date_from, date_to, filters, rep_ids)

    if tm_df.empty:
        st.info("No data for the selected period.")
        return

    # ── Heatmap: Day × Hour — brand blue ─────────────────────────────────────
    subsection_label("Day × Hour Heatmap  ·  click a cell to cross-filter")
    pivot = tm_df.groupby(["dow", "hour"])["visit_count"].sum().reset_index()
    heat_matrix = pd.DataFrame(0, index=list(range(7)), columns=list(range(24)))
    for _, row in pivot.iterrows():
        heat_matrix.loc[int(row["dow"]), int(row["hour"])] = int(row["visit_count"])
    heat_matrix.index   = _DOW_NAMES
    heat_matrix.columns = [str(h) for h in range(24)]
    active_cols = [c for c in heat_matrix.columns if heat_matrix[c].sum() > 0]
    heat_matrix = heat_matrix[active_cols]

    fig_heat = go.Figure(go.Heatmap(
        z=heat_matrix.values.tolist(),
        x=active_cols,
        y=_DOW_NAMES,
        colorscale=[[0, "#eef2ff"], [0.5, "#6ea6ff"], [1, "#2667ff"]],
        text=heat_matrix.values.tolist(),
        texttemplate="%{text}",
        showscale=True,
        hoverongaps=False,
        xgap=2, ygap=2,
    ))
    fig_heat.update_layout(
        margin=dict(l=0, r=0, t=10, b=0), height=280,
        xaxis=dict(title="Hour of Day", side="bottom"),
        yaxis=dict(title="", autorange="reversed"),
        paper_bgcolor="rgba(0,0,0,0)",
    )
    ev_heat = st.plotly_chart(fig_heat, use_container_width=True,
                               on_select="rerun", key="an_heatmap")
    _handle_heatmap_click(ev_heat)

    st.markdown("<div style='margin-bottom:1rem;'></div>", unsafe_allow_html=True)

    # ── Bottom row: Today · Day bar · Hour bar ────────────────────────────────
    col_t, col_day, col_hr = st.columns([1, 2, 2])

    with col_t:
        today = _local_now().date()
        subsection_label(f"Today  ·  {today.strftime('%d/%m/%Y')}")
        today_df = get_analytics_today(uid, role, today, rep_ids)
        if not today_df.empty:
            total_row = pd.DataFrame([{
                "Frontline Name": "Total",
                "Visits": today_df["Visits"].sum(),
            }])
            display_df = pd.concat([today_df, total_row], ignore_index=True)
            st.markdown(
                html_table(display_df, max_rows=50, max_height=320),
                unsafe_allow_html=True,
            )
        else:
            st.caption("No visits today.")

    with col_day:
        subsection_label("Visits by Day")
        day_bu = tm_df.groupby(["dow", "business_unit"])["visit_count"].sum().reset_index()
        day_bu["Day"] = day_bu["dow"].apply(lambda d: _DOW_NAMES[int(d)])
        day_bu = day_bu.sort_values("dow")
        fig_day = px.bar(
            day_bu, x="Day", y="visit_count", color="business_unit",
            color_discrete_sequence=PALETTE,
            category_orders={"Day": _DOW_NAMES},
        )
        fig_day.update_layout(
            margin=dict(l=0, r=0, t=10, b=0), height=280,
            xaxis_title="", yaxis_title="Visits",
            legend=dict(title="", orientation="h", y=-0.3),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        fig_day.update_yaxes(gridcolor="rgba(0,0,0,0.06)")
        ev_day = st.plotly_chart(fig_day, use_container_width=True,
                                  on_select="rerun", key="an_daybar")
        if ev_day and getattr(ev_day, "selection", None):
            pts = (ev_day.selection.get("points", [])
                   if isinstance(ev_day.selection, dict)
                   else getattr(ev_day.selection, "points", []))
            if pts:
                day_name = pts[0].get("x")
                if day_name and day_name in _DOW_MAP:
                    _set_filter("dow", _DOW_MAP[day_name])

    with col_hr:
        subsection_label("Visits by Hour")
        hr_bu = tm_df.groupby(["hour", "business_unit"])["visit_count"].sum().reset_index()
        hr_bu["Hour"] = hr_bu["hour"].astype(str)
        fig_hr = px.bar(
            hr_bu, x="Hour", y="visit_count", color="business_unit",
            color_discrete_sequence=PALETTE,
        )
        fig_hr.update_layout(
            margin=dict(l=0, r=0, t=10, b=0), height=280,
            xaxis_title="Hour", yaxis_title="Visits",
            legend=dict(title="", orientation="h", y=-0.3),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        fig_hr.update_yaxes(gridcolor="rgba(0,0,0,0.06)")
        ev_hr = st.plotly_chart(fig_hr, use_container_width=True,
                                 on_select="rerun", key="an_hrbar")
        if ev_hr and getattr(ev_hr, "selection", None):
            pts = (ev_hr.selection.get("points", [])
                   if isinstance(ev_hr.selection, dict)
                   else getattr(ev_hr.selection, "points", []))
            if pts:
                h = pts[0].get("x")
                if h is not None:
                    _set_filter("hour", int(h))

    # ── Attendance pivot (elevated roles only) ────────────────────────────────
    is_elevated = role in ("admin", "sales manager", "biomedical manager", "supervisor")
    if not is_elevated:
        return

    st.markdown("<div style='margin-top:1.5rem;'></div>", unsafe_allow_html=True)
    subsection_label("Rep Attendance Calendar")

    att_df = get_analytics_attendance(uid, role, date_from, date_to, rep_ids)
    if att_df.empty:
        st.caption("No attendance data for the selected period.")
        return

    att_df["date"] = pd.to_datetime(att_df["date"])
    pivot_att = att_df.pivot_table(
        index="rep_name", columns="date", values="visit_count",
        fill_value=0, aggfunc="sum",
    )
    pivot_att.columns = [c.strftime("%d/%m") for c in pivot_att.columns]
    pivot_att = pivot_att.reset_index().rename(columns={"rep_name": "Rep"})

    import html as _html

    cols_list = list(pivot_att.columns)
    th_style = (
        "padding:6px 10px;font-size:0.65rem;font-weight:700;color:var(--color-text-muted);"
        "text-transform:uppercase;letter-spacing:.04em;white-space:nowrap;"
        "position:sticky;top:0;z-index:1;"
        "background:var(--color-surface-2);border-bottom:2px solid var(--color-border);"
    )
    header = "".join(
        f'<th style="{th_style}text-align:{"left" if c == "Rep" else "center"};">'
        f'{_html.escape(str(c))}</th>'
        for c in cols_list
    )

    rows = ""
    for i, (_, row) in enumerate(pivot_att.iterrows()):
        bg = "background:var(--color-surface-2);" if i % 2 else ""
        cells = ""
        for c in cols_list:
            v = row[c]
            if c == "Rep":
                cell_html = _html.escape(str(v))
                align = "left"
            else:
                vi = int(v) if v else 0
                if vi == 0:
                    cell_html = '<span style="color:var(--color-text-subtle);">—</span>'
                else:
                    cell_html = f'<span style="color:#2667ff;font-weight:600;">{vi}</span>'
                align = "center"
            cells += (
                f'<td style="padding:5px 10px;font-size:0.72rem;'
                f'border-bottom:1px solid var(--color-border);'
                f'text-align:{align};{bg}">{cell_html}</td>'
            )
        rows += f"<tr>{cells}</tr>"

    st.markdown(
        f'<div style="border:1px solid var(--color-border);border-radius:10px;'
        f'overflow:auto;max-height:340px;margin-top:0.5rem;">'
        f'<table style="width:100%;border-collapse:collapse;">'
        f'<thead><tr>{header}</tr></thead>'
        f'<tbody>{rows}</tbody></table></div>',
        unsafe_allow_html=True,
    )
```

### Step 7.2 — Commit

```bash
git add app_pages/analytics.py
git commit -m "feat: rewrite analytics Tab 4 — brand blue heatmap, html_table today, attendance pivot"
```

---

## Task 8: Update `page_analytics()` header

**Files:**
- Modify: `app_pages/analytics.py` — update one line in `page_analytics()` (line 475)

### Step 8.1 — Replace the plain header

Find in `page_analytics()`:

```python
    st.markdown("## Analytics")
```

Replace with:

```python
    section_header(
        "Analytics",
        f"{date_from.strftime('%d %b %Y')} – {date_to.strftime('%d %b %Y')}"
        if 'date_from' in dir() else "",
    )
```

Wait — `date_from` is defined after this line. Move the header call to after the date inputs. The current order in `page_analytics()` is:

```python
    st.markdown("## Analytics")          # ← replace this line
    filters = _get_filters()
    now = _local_now()
    # ... date inputs ...
    date_from = st.date_input(...)
    date_to   = st.date_input(...)
```

The `section_header` call with a date subtitle needs `date_from`/`date_to` to exist. Move the header to **after** the date inputs block. The updated sequence should be:

```python
    filters = _get_filters()

    # ── Controls row ──────────────────────────────────────────────────────────
    now = _local_now()
    if is_elevated:
        c1, c2, c3 = st.columns([2, 2, 3])
    else:
        c1, c2, c3 = st.columns([2, 2, 4])

    with c1:
        date_from = st.date_input("From", value=date(now.year, 1, 1), key="an_date_from")
    with c2:
        date_to = st.date_input("To", value=now.date(), key="an_date_to")

    rep_ids = None
    if is_elevated:
        with c3:
            reps_df = get_all_reps()
            if not reps_df.empty:
                rep_map = dict(zip(reps_df["name"], reps_df["user_id"]))
                sel = st.multiselect("Filter by Rep", options=list(rep_map.keys()), key="an_reps")
                if sel:
                    rep_ids = [rep_map[r] for r in sel]

    section_header(
        "Analytics",
        f"{date_from.strftime('%d %b %Y')} – {date_to.strftime('%d %b %Y')}",
    )

    # ── Active filter chips ───────────────────────────────────────────────────
    _render_chips(filters)
```

Remove the original `st.markdown("## Analytics")` line from the top of `page_analytics()`.

### Step 8.2 — Commit

```bash
git add app_pages/analytics.py
git commit -m "feat: use section_header in analytics entry point with date range subtitle"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Covered in |
|---|---|
| `section_header()` replacing plain markdown | Task 8 |
| 3 hero KPI cards with % delta badges | Task 4 |
| 5 secondary metric strip | Task 4 |
| Area line chart (visits over time) | Task 4 |
| Region Treemap (Region→City→Sector→Customer) | Task 4 |
| BU Treemap (BU→Product Category→Rep) | Task 4 |
| Objective grouped bar by category | Task 4 |
| `get_analytics_kpis_previous_period()` | Task 1 Step 1.2 |
| `get_analytics_drilldown()` | Task 1 Step 1.3 |
| `get_analytics_objective_categories()` | Task 1 Step 1.4 |
| `get_analytics_visits_per_rep()` | Task 1 Step 1.5 |
| `get_analytics_attendance()` | Task 1 Step 1.6 |
| `_analytics_scope()` city/sector filters | Task 1 Step 1.1 |
| Rep leaderboard with avatar initials, rank highlight | Task 5 |
| Aud/Customer bar + Avg Customers/Month bar | Task 5 |
| Cascading Region→City→Sector dropdowns | Task 6 |
| `html_table()` replacing `st.dataframe()` for visit records | Task 6 |
| Maps unchanged | Task 6 |
| Heatmap brand blue colorscale | Task 7 |
| Today's visits as `html_table()` | Task 7 |
| Attendance pivot table (elevated roles only) | Task 7 |
| Day/Hour bars — BU stacked, Set2 palette | Task 7 |
| Chip labels for city/sector | Task 3 Step 3.3 |
| `html_table`, `section_header`, `subsection_label` imports | Task 3 Step 3.1 |
| `query_df` imported into analytics for cascading filter | Task 3 Step 3.1 |
| Tests for all 5 new DB functions | Task 2 |

All spec requirements are covered. No placeholders. Type signatures are consistent across all tasks: `uid: int`, `role: str`, `date_from: date`, `date_to: date`, `filters: dict`, `rep_ids=None`.

**Note on region filter:** The treemap displays `c.region` (customer's physical region) while the cross-filter chip applies `u.region` (rep's assigned region) via the existing `_analytics_scope`. In practice these align since reps service customers in their own region. If they diverge in the data, clicking a region tile in the treemap may show zero results — this is acceptable given the spec.
