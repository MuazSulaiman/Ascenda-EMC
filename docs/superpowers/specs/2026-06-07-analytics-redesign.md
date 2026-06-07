# Analytics Page Redesign — Design Spec
**Date:** 2026-06-07  
**Status:** Approved for implementation

---

## Goal

Replace the current plain-Streamlit analytics page (`app_pages/analytics.py`) with a professional **Performance Scorecard** — matching the visual quality of the rest of the Ascenda app and replicating all Power BI features that the database can support.

---

## Design Direction

**Performance Scorecard** aesthetic:
- Hero accent card (blue gradient) for the primary KPI with period-over-period % delta badges
- Bordered cards for secondary KPIs, also with % delta badges
- Consistent brand blue (`#2667ff`) palette across all charts
- Horizontal bar charts with branded progress bars replacing pie charts
- Plotly Treemap for drill-down breakdowns (replacing pie charts)
- Ranked leaderboard table for per-rep performance
- `section_header()` / `subsection_label()` from `ui.py` for all headings
- `html_table()` from `ui.py` replacing all `st.dataframe()` calls (dark-mode safe)

---

## Architecture

### Files Changed

#### `db_ops.py` — 4 new functions added

| Function | Purpose |
|---|---|
| `get_analytics_kpis_previous_period(uid, role, date_from, date_to, filters, rep_ids)` | Runs the same KPI query as `get_analytics_kpis()` but with dates shifted back by the length of the selected range. Returns same dict shape. Used for % delta calculation. |
| `get_analytics_drilldown(uid, role, date_from, date_to, filters, rep_ids)` | Returns a flat DataFrame with columns: `region, city, sector, customer_name, business_unit, product_category, rep, visit_count`. Used to build both treemaps. |
| `get_analytics_objective_categories(uid, role, date_from, date_to, filters, rep_ids)` | Returns DataFrame: `objective_category, objective_name, count`. Used for the grouped objective bar chart. |
| `get_analytics_attendance(uid, role, date_from, date_to, rep_ids)` | Returns DataFrame: `date, rep_name, visit_count`. Pivoted in UI for the attendance calendar. |
| `get_analytics_visits_per_rep(uid, role, date_from, date_to, filters, rep_ids)` | Returns DataFrame: `rep, total_visits, total_customers`. Used for the leaderboard Visits and Customers columns (not available from existing per-rep functions). |

#### `analytics.py` — full rewrite of all rendering functions

The cross-filter helpers (`_get_filters`, `_set_filter`, `_handle_pie_click`, `_handle_hbar_click`, `_handle_heatmap_click`, `_render_chips`) are **kept unchanged** — they work correctly.

`page_analytics()` entry point structure stays the same (date inputs, rep multiselect, chips, 4 tabs). Only the tab rendering functions are rewritten.

---

## Tab 1 — Overview

### Page Header
Replace `st.markdown("## Analytics")` with `section_header("Analytics", f"{date_from} – {date_to}")`.

### Period-over-Period Calculation
After fetching `kpis = get_analytics_kpis(...)`, also fetch `prev_kpis = get_analytics_kpis_previous_period(...)`.

Delta % for a metric:
```python
def _pct_delta(current, previous):
    if not previous:
        return None
    return round((current - previous) / previous * 100)
```

### KPI Scorecard Block (top of tab)

**Row 1 — 3 hero cards** rendered as custom HTML (not `st.metric()`):

| Card | Style | Fields |
|---|---|---|
| Total Visits | Blue gradient accent (`linear-gradient(135deg, #2667ff, #4d8ef0)`), white text | Value + % delta badge |
| Customers | Bordered white card | Value + green/red % delta badge |
| Audiences | Bordered white card | Value + green/red % delta badge |

Delta badge colours: `#e6f6ec / #0e8a4f` for positive, `#fdeceb / #c83333` for negative, grey neutral pill if `prev = 0`.

**Row 2 — 5 secondary metric strip** (compact bordered cards, no % delta):
`Customers/Day · Visits/Customer · Audiences/Customer · Avg Customers/Month · Avg BL/Month`

### Visits Over Time
Plotly area line chart (brand blue fill with opacity 0.12). Granularity radio stays (Year / Month / Week). Month labels formatted as "Jan 2026". Chart height 220px, no gridlines on X axis, light gridlines on Y.

### Breakdown Charts — Treemap (drill-down)

Replace both pie charts with two `px.treemap()` charts side by side.

**Region Treemap** — `path=['region', 'city', 'sector', 'customer_name']`, `values='visit_count'`  
Color scale: sequential blue (`#eef2ff` → `#2667ff`). Native Plotly click-to-drill behaviour (no extra session state needed). Cross-filter on click still sets `filters['region']`.

**Business Unit Treemap** — `path=['business_unit', 'product_category', 'rep']`, `values='visit_count'`  
Color scale: sequential green (`#f0fdf4` → `#10b981`). Click sets `filters['business_unit']`.

Data source: `get_analytics_drilldown()` — filtered and grouped by the relevant path columns for each treemap.

### Objectives Bar Chart
Grouped horizontal bar chart showing individual objectives, colored by `objective_category`. Use `get_analytics_objective_categories()` which returns `(objective_category, objective_name, count)`. In Plotly: `px.bar(df, y="objective_name", x="count", color="objective_category", orientation="h")`. If `objective_category` is NULL, label it `"Uncategorised"`. Brand blue palette via `px.colors.qualitative.Set2` for categories.

---

## Tab 2 — KPIs (Per-Rep Performance)

### Rep Leaderboard Table
Custom HTML table (not `html_table()` — needs avatar initials). Rendered via `st.markdown(..., unsafe_allow_html=True)`.

Columns: Rank · Rep (avatar circle with initials + name) · Visits · Customers · Aud/Customer  
Data: `get_analytics_visits_per_rep()` for Visits + Customers columns; `get_analytics_kpis_per_rep()["audience_per_customer"]` for Aud/Customer. Merge on rep name, sort by Visits descending.  
Leader row (rank 1) gets a light blue background (`#f0f5ff`).  
Avatar: coloured circle, initials from `rep.split()`, primary blue for rank 1, grey for others.

### Supporting Charts (2-column grid below leaderboard)
1. **Audiences/Customer by Rep** — horizontal bar, blue
2. **Avg Customers/Month by Rep** — horizontal bar, blue

Remove the "Audiences Visited per Rep" and "Visits by Region" charts from the current KPIs tab — the leaderboard makes them redundant and region is covered in Tab 1.

---

## Tab 3 — Visits Detail

### Cascading Region Filter (NEW — from Power BI Visits Details slicer)
Above the maps, add a cascading filter row:
- `st.selectbox("Region", ["All"] + regions)` → filters city options
- `st.selectbox("City", ["All"] + cities_in_region)` → filters sector options  
- `st.selectbox("Sector", ["All"] + sectors_in_city)`

These cascade selections are merged into the existing `filters` dict as `filters['region']`, `filters['city']`, `filters['sector']` and passed to `_analytics_scope()`. Note: `city` and `sector` are on `customers` table — `_analytics_scope()` already joins `customers` via `LEFT JOIN customers c ON c.customer_id = v.customer_id`. Add WHERE clauses for city/sector to `_analytics_scope()`.

Distinct options for the dropdowns are fetched with a simple `query_df` scoped to the current date range — not cross-filtered (to avoid empty cascades).

### Maps
Unchanged — Folium maps work well. Wrap each in a bordered card `div` with `subsection_label()` header.

### Visit Records Table
Replace `st.dataframe(detail_df)` with `html_table(detail_df, max_rows=1000, max_height=480)` from `ui.py`. Preserves dark-mode theming, sticky header, striped rows.

---

## Tab 4 — Time Map

### Day × Hour Heatmap
Recolor to brand blue scale: `colorscale=[[0, "#eef2ff"], [0.5, "#6ea6ff"], [1, "#2667ff"]]`. Card border + rounded corners wrapping the chart. Cross-filter click behaviour unchanged.

### Attendance Pivot Table (NEW — from Power BI Time Map pivotTable)
Below the heatmap, a new section: **"Rep Attendance"**.

Data: `get_analytics_attendance()` → pivot to `rep × date` with visit counts.

Render as `html_table()` where rows = reps, columns = dates (formatted "DD/MM"), cell values = visit count (0 shown as "—", ≥1 shown as the count in blue). Max 31 columns (one month).  

Only shown for elevated roles (admin, sales manager, biomedical manager). Reps see only their own row.

### Today's Visits
Keep as-is but render as a small `html_table()` instead of `st.dataframe()`.

### Visits by Day / Visits by Hour
Recolor to all-blue palette (`color_discrete_sequence=[BRAND]`). Keep stacked BU colouring using Set2 palette but override the dominant colour.

---

## Cross-Filter Chips
Keep `_render_chips()` unchanged. Add `'city'` and `'sector'` to `chip_labels` dict:
```python
"city":   lambda v: f"City: {v}",
"sector": lambda v: f"Sector: {v}",
```

---

## `_analytics_scope()` Changes
Add optional city and sector filters:
```python
if filters.get("city"):
    clauses.append("c.city = :city")
    params["city"] = filters["city"]
if filters.get("sector"):
    clauses.append("c.sector = :sector")
    params["sector"] = filters["sector"]
```
The `customers` table is already joined via `LEFT JOIN customers c` in the existing scope helper.

---

## What Does NOT Change
- Cross-filter helpers (`_get_filters`, `_set_filter`, `_handle_*`, `_render_chips`) — unchanged
- `page_analytics()` entry point structure — unchanged (date inputs, rep filter, chips, tabs)
- Folium maps — unchanged
- All DB functions except the 4 new ones above
- All other pages in the app

---

## Dependency Notes
- `px.treemap` is available via `plotly.express` (already imported)
- No new pip dependencies required
- `html_table()` and `section_header()` / `subsection_label()` already exist in `ui.py`
