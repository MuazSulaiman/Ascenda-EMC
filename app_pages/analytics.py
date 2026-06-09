# app_pages/analytics.py — Analytics Dashboard
from datetime import date
import html as _html
import io

import folium
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from streamlit_folium import st_folium

from auth import resolve_session_user
from db_ops import (
    get_all_reps,
    get_analytics_attendance,
    get_analytics_coverage_rate,
    get_analytics_drilldown,
    get_analytics_kpis,
    get_analytics_kpis_per_rep,
    get_analytics_kpis_previous_period,
    get_analytics_new_vs_repeat,
    get_analytics_objective_categories,
    get_analytics_customer_health,
    get_analytics_target_vs_actual,
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


@st.cache_data(ttl=600)
def _cached_customer_locations() -> pd.DataFrame:
    return get_customer_locations_for_map()


@st.cache_data(ttl=300)
def _cached_all_reps() -> pd.DataFrame:
    return get_all_reps()


BRAND   = "#2667ff"
CHART_COLORS = [
    "#2667ff",  # brand blue
    "#10b981",  # emerald
    "#f59e0b",  # amber
    "#ef4444",  # red
    "#8b5cf6",  # violet
    "#06b6d4",  # cyan
    "#f97316",  # orange
    "#64748b",  # slate
]
PALETTE = CHART_COLORS  # backwards-compat alias

_DOW_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
_DOW_MAP   = {name: i for i, name in enumerate(_DOW_NAMES)}

# Heroicons outline SVGs for hero KPI cards
_ICON_VISITS = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    ' stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/>'
    '<circle cx="12" cy="10" r="3"/></svg>'
)
_ICON_CUSTOMERS = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    ' stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>'
    '<circle cx="9" cy="7" r="4"/>'
    '<path d="M23 21v-2a4 4 0 0 0-3-3.87"/>'
    '<path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>'
)
_ICON_AUDIENCES = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    ' stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="2" y="7" width="20" height="14" rx="2"/>'
    '<path d="M16 3h-1a2 2 0 0 0-2 2v2H9V5a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v14"/>'
    '<path d="M12 12v5"/><path d="M8 12v5"/><path d="M16 12v5"/></svg>'
)

# Left-border accent colors for the 6 secondary metric cards
_MINI_ACCENTS = [
    "var(--color-primary)",       # Cust / Day
    "#0e8a4f",                    # Visits / Cust
    "#f59e0b",                    # Aud / Cust
    "var(--color-primary)",       # Cust / Month
    "#0e8a4f",                    # BL / Month
    "var(--color-border-strong)", # Coverage
]


def _analytics_css() -> None:
    """Inject analytics-scoped CSS once at page load."""
    st.markdown("""<style>
/* ── Analytics section labels ── */
.an-section-label {
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 14px 0 8px;
}
.an-section-label-bar {
    width: 3px;
    height: 14px;
    border-radius: 2px;
    background: var(--color-primary);
    flex-shrink: 0;
}
.an-section-label-text {
    font-size: 0.72rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    color: var(--color-text-subtle);
    white-space: nowrap;
}
.an-section-label-line {
    flex: 1;
    height: 1px;
    background: var(--color-border);
}
/* ── Progress bars ── */
.an-progress-wrap { margin-bottom: 10px; }
.an-progress-track {
    background: var(--color-surface-2);
    border-radius: 6px;
    height: 8px;
    overflow: hidden;
}
.an-progress-fill {
    height: 8px;
    border-radius: 6px;
    transition: width 0.3s ease;
}
/* ── Filter chip bar ── */
.an-chip-bar-label {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 4px;
}
.an-chip-label {
    font-size: 0.68rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--color-text-subtle);
    white-space: nowrap;
}
/* ── Empty states ── */
.an-empty-state {
    text-align: center;
    padding: 2rem 1rem;
    background: var(--color-surface-2);
    border: 1px dashed var(--color-border);
    border-radius: 10px;
    color: var(--color-text-subtle);
    margin: 4px 0 8px;
}
.an-empty-state-icon {
    display: block;
    margin: 0 auto 8px;
    opacity: 0.4;
}
.an-empty-state-msg {
    font-size: 0.875rem;
    font-weight: 500;
}
/* ── Leaderboard rank tiers ── */
.an-lb-row-1 { background: rgba(251,211,141,0.12); }
.an-lb-rank-1 { color: #b7791f; font-weight: 800; }
.an-lb-rank-2 { color: #71717a; font-weight: 700; }
.an-lb-rank-3 { color: #9a3412; font-weight: 700; }
</style>""", unsafe_allow_html=True)


def _an_label(title: str) -> None:
    """Analytics-local section label: left blue accent bar + uppercase text + divider."""
    st.markdown(
        f'<div class="an-section-label">'
        f'<div class="an-section-label-bar"></div>'
        f'<span class="an-section-label-text">{_html.escape(title)}</span>'
        f'<div class="an-section-label-line"></div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def _an_empty(message: str) -> None:
    """Render a styled empty state block."""
    _info_icon = (
        '<svg class="an-empty-state-icon" width="32" height="32" viewBox="0 0 24 24"'
        ' fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"'
        ' stroke-linejoin="round">'
        '<circle cx="12" cy="12" r="10"/>'
        '<line x1="12" y1="8" x2="12" y2="12"/>'
        '<line x1="12" y1="16" x2="12.01" y2="16"/>'
        '</svg>'
    )
    st.markdown(
        f'<div class="an-empty-state">'
        f'{_info_icon}'
        f'<div class="an-empty-state-msg">{_html.escape(message)}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def _chart_style(fig) -> None:
    """Apply consistent Inter font, hover label, and axis styling to all analytics charts."""
    fig.update_layout(
        font=dict(family="Inter, system-ui, sans-serif", size=11.5, color="#57606a"),
        hoverlabel=dict(
            bgcolor="white",
            bordercolor="#e4e8ec",
            font=dict(family="Inter, sans-serif", size=12, color="#0d1117"),
        ),
    )
    fig.update_xaxes(zeroline=False, tickfont=dict(size=11))
    fig.update_yaxes(zeroline=False, tickfont=dict(size=11))


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


# ─────────────────────────────────────────────────────────────────────────────
# Cross-filter helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_filters() -> dict:
    return st.session_state.setdefault("analytics_filters", {})


def _set_filter(key: str, value):
    filters = _get_filters()
    if filters.get(key) == value:
        filters.pop(key, None)   # toggle off on second click
    else:
        filters[key] = value
    st.rerun()


def _handle_pie_click(ev, filter_key: str):
    if not ev or not getattr(ev, "selection", None):
        return
    pts = ev.selection.get("points", []) if isinstance(ev.selection, dict) else getattr(ev.selection, "points", [])
    if not pts:
        return
    label = pts[0].get("label")
    if label:
        _set_filter(filter_key, label)


def _handle_hbar_click(ev, filter_key: str, axis: str = "y"):
    if not ev or not getattr(ev, "selection", None):
        return
    pts = ev.selection.get("points", []) if isinstance(ev.selection, dict) else getattr(ev.selection, "points", [])
    if not pts:
        return
    label = pts[0].get(axis) or pts[0].get("label")
    if label:
        _set_filter(filter_key, label)


def _handle_heatmap_click(ev):
    if not ev or not getattr(ev, "selection", None):
        return
    pts = ev.selection.get("points", []) if isinstance(ev.selection, dict) else getattr(ev.selection, "points", [])
    if not pts:
        return
    pt      = pts[0]
    day_name = pt.get("y")
    hour_val = pt.get("x")
    filters  = _get_filters()
    if day_name and day_name in _DOW_MAP:
        dow = _DOW_MAP[day_name]
        if filters.get("dow") == dow:
            filters.pop("dow", None)
        else:
            filters["dow"] = dow
    if hour_val is not None:
        import re as _re
        try:
            if isinstance(hour_val, str):
                m = _re.match(r"(\d+)\s*(AM|PM)", str(hour_val), _re.IGNORECASE)
                if m:
                    h_num, period = int(m.group(1)), m.group(2).upper()
                    if period == "AM":
                        h = 0 if h_num == 12 else h_num
                    else:
                        h = 12 if h_num == 12 else h_num + 12
                else:
                    h = int(hour_val)
            else:
                h = int(hour_val)
            if filters.get("hour") == h:
                filters.pop("hour", None)
            else:
                filters["hour"] = h
        except Exception:
            pass
    st.rerun()


def _render_chips(filters: dict):
    if not filters:
        return
    chip_labels = {
        "region":        lambda v: f"Region: {v}",
        "business_unit": lambda v: f"BU: {v}",
        "objective":     lambda v: f"Objective: {v}",
        "city":          lambda v: f"City: {v}",
        "sector":        lambda v: f"Sector: {v}",
        "dow":           lambda v: f"Day: {_DOW_NAMES[v]}",
        "hour":          lambda v: f"Hour: {v}:00",
    }
    st.markdown(
        '<div class="an-chip-bar-label">'
        '<span class="an-chip-label">Active Filters</span>'
        '</div>',
        unsafe_allow_html=True,
    )
    keys = list(filters.keys())
    cols = st.columns(len(keys) + 1)
    for i, key in enumerate(keys):
        label = chip_labels.get(key, lambda v: f"{key}: {v}")(filters[key])
        with cols[i]:
            if st.button(f"{label}  ✕", key=f"chip_{key}", use_container_width=True):
                st.session_state.analytics_filters.pop(key, None)
                st.rerun()
    with cols[-1]:
        if st.button("Clear All", key="chip_clear_all", use_container_width=True):
            st.session_state.analytics_filters.clear()
            st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# Tab 1 – Overview
# ─────────────────────────────────────────────────────────────────────────────

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

    cov = get_analytics_coverage_rate(uid, role, date_from, date_to, filters, rep_ids)
    cov_label = f"{cov['coverage_pct']}% ({cov['visited']:,}/{cov['total_active']:,})"

    nvr    = get_analytics_new_vs_repeat(uid, role, date_from, date_to, filters, rep_ids)
    obj_df = get_analytics_objective_categories(uid, role, date_from, date_to, filters, rep_ids)

    # ── Hero KPI scorecard ────────────────────────────────────────────────────
    st.markdown(f"""
<div style="display:grid;grid-template-columns:1.6fr 1fr 1fr;gap:12px;margin-bottom:10px;">
  <div style="background:linear-gradient(135deg,#2667ff 0%,#4d8ef0 100%);border-radius:14px;
              padding:20px 22px;color:#fff;box-shadow:0 4px 14px rgba(38,103,255,.3);">
    <div style="display:flex;align-items:center;gap:6px;font-size:0.65rem;font-weight:700;
                text-transform:uppercase;letter-spacing:.07em;opacity:.85;margin-bottom:8px;">
      {_ICON_VISITS}<span>Total Visits</span>
    </div>
    <div style="font-size:2.6rem;font-weight:700;letter-spacing:-.03em;line-height:1;">{tv:,}</div>
    <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:10px;">
      {tv_badge}
    </div>
  </div>
  <div style="background:var(--color-surface);border:1px solid var(--color-border);
              border-radius:14px;padding:18px 20px;box-shadow:var(--shadow-card);">
    <div style="display:flex;align-items:center;gap:6px;font-size:0.65rem;font-weight:700;
                text-transform:uppercase;letter-spacing:.07em;color:var(--color-text-subtle);margin-bottom:6px;">
      {_ICON_CUSTOMERS}<span>Customers</span>
    </div>
    <div style="font-size:2rem;font-weight:700;color:var(--color-text);
                letter-spacing:-.02em;line-height:1.1;">{tc:,}</div>
    <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:8px;">
      {tc_badge}
    </div>
  </div>
  <div style="background:var(--color-surface);border:1px solid var(--color-border);
              border-radius:14px;padding:18px 20px;box-shadow:var(--shadow-card);">
    <div style="display:flex;align-items:center;gap:6px;font-size:0.65rem;font-weight:700;
                text-transform:uppercase;letter-spacing:.07em;color:var(--color-text-subtle);margin-bottom:6px;">
      {_ICON_AUDIENCES}<span>Audiences</span>
    </div>
    <div style="font-size:2rem;font-weight:700;color:var(--color-text);
                letter-spacing:-.02em;line-height:1.1;">{ta:,}</div>
    <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:8px;">
      {ta_badge}
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

    # ── Secondary metric strip ────────────────────────────────────────────────
    def _secondary_card(label, val, border_color="var(--color-border)"):
        return (
            f'<div style="flex:1;background:var(--color-surface);border:1px solid var(--color-border);'
            f'border-left:3px solid {border_color};'
            f'border-radius:10px;padding:10px 12px;">'
            f'<div style="font-size:0.6rem;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:.06em;color:var(--color-text-subtle);margin-bottom:4px;">{label}</div>'
            f'<div style="font-size:1.2rem;font-weight:700;color:var(--color-text);">{val}</div>'
            f'</div>'
        )

    st.markdown(
        '<div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px;">'
        + _secondary_card("Cust / Day",    f"{cpd:.1f}",  _MINI_ACCENTS[0])
        + _secondary_card("Visits / Cust", f"{vpc:.1f}",  _MINI_ACCENTS[1])
        + _secondary_card("Aud / Cust",    f"{apc:.1f}",  _MINI_ACCENTS[2])
        + _secondary_card("Cust / Month",  f"{acm:.1f}",  _MINI_ACCENTS[3])
        + _secondary_card("BL / Month",    f"{blm:.1f}",  _MINI_ACCENTS[4])
        + _secondary_card("Coverage",      cov_label,     _MINI_ACCENTS[5])
        + '</div>',
        unsafe_allow_html=True,
    )

    # ── Engagement funnel ─────────────────────────────────────────────────────
    _an_label("Engagement Funnel")
    funnel_df = pd.DataFrame({
        "Stage": ["Total Visits", "Unique Customers", "Unique Audiences"],
        "Count": [tv, tc, ta],
    })
    funnel_df["Stage"] = pd.Categorical(
        funnel_df["Stage"],
        categories=["Total Visits", "Unique Customers", "Unique Audiences"],
        ordered=True,
    )
    fig_funnel = px.funnel(funnel_df, x="Stage", y="Count",
                            color_discrete_sequence=[BRAND])
    fig_funnel.update_layout(
        margin=dict(l=0, r=0, t=10, b=0), height=180,
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        yaxis_title="",
    )
    fig_funnel.update_traces(hovertemplate="%{y}<br>%{x}<extra></extra>")
    fig_funnel.update_layout(height=220)
    _chart_style(fig_funnel)
    st.plotly_chart(fig_funnel, use_container_width=True, key="an_funnel")

    # ── Visits over time — area chart ─────────────────────────────────────────
    _an_label("Visits Over Time")
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
        fig_ts.update_traces(
            hovertemplate="%{y} visits<br>%{x}<extra></extra>",
            fillcolor="rgba(38,103,255,0.18)",
        )
        _chart_style(fig_ts)
        st.plotly_chart(fig_ts, use_container_width=True, key="an_ts")
    else:
        _an_empty("No visit data for the selected period.")

    # ── New vs Repeat + Objective mix donuts ─────────────────────────────────────
    col_nr, col_obj_donut = st.columns(2)

    with col_nr:
        _an_label("New vs Repeat Visits")
        if nvr["new_visits"] + nvr["repeat_visits"] > 0:
            fig_nvr = px.pie(
                names=["New", "Repeat"],
                values=[nvr["new_visits"], nvr["repeat_visits"]],
                color_discrete_sequence=[BRAND, "#e2e8f0"],
                hole=0.55,
            )
            fig_nvr.update_traces(
                textinfo="percent+label",
                hovertemplate="%{label}: %{value} (%{percent})<extra></extra>",
                textfont=dict(family="Inter, sans-serif", size=11),
            )
            fig_nvr.update_layout(
                margin=dict(l=0, r=0, t=10, b=0), height=200,
                paper_bgcolor="rgba(0,0,0,0)",
                showlegend=False,
            )
            _chart_style(fig_nvr)
            st.plotly_chart(fig_nvr, use_container_width=True, key="an_nvr")
        else:
            _an_empty("No visit data.")

    with col_obj_donut:
        _an_label("Objective Mix")
        if not obj_df.empty:
            cat_totals = obj_df.groupby("objective_category")["count"].sum().reset_index()
            fig_od = px.pie(
                cat_totals,
                names="objective_category",
                values="count",
                color_discrete_sequence=CHART_COLORS,
                hole=0.5,
            )
            fig_od.update_traces(
                textinfo="percent+label",
                hovertemplate="%{label}: %{value} (%{percent})<extra></extra>",
                textfont=dict(family="Inter, sans-serif", size=11),
            )
            fig_od.update_layout(
                margin=dict(l=0, r=0, t=10, b=0), height=200,
                paper_bgcolor="rgba(0,0,0,0)",
                showlegend=False,
            )
            _chart_style(fig_od)
            st.plotly_chart(fig_od, use_container_width=True, key="an_obj_donut")

    # ── Treemap drill-downs ───────────────────────────────────────────────────
    _an_label("Breakdown by Region & Business Unit")
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
            fig_r.update_traces(
                textinfo="label+value",
                root_color="rgba(0,0,0,0)",
                hovertemplate="%{label}<br>Visits: %{value}<extra></extra>",
            )
            fig_r.update_layout(
                margin=dict(l=0, r=0, t=10, b=0), height=320,
                coloraxis_showscale=False,
                paper_bgcolor="rgba(0,0,0,0)",
            )
            _chart_style(fig_r)
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
            fig_bu.update_traces(
                textinfo="label+value",
                root_color="rgba(0,0,0,0)",
                hovertemplate="%{label}<br>Visits: %{value}<extra></extra>",
            )
            fig_bu.update_layout(
                margin=dict(l=0, r=0, t=10, b=0), height=320,
                coloraxis_showscale=False,
                paper_bgcolor="rgba(0,0,0,0)",
            )
            _chart_style(fig_bu)
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
    _an_label("Visits by Objective")
    if not obj_df.empty:
        fig_obj = px.bar(
            obj_df, y="objective_name", x="count",
            color="objective_category", orientation="h",
            color_discrete_sequence=CHART_COLORS,
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
        fig_obj.update_traces(
            hovertemplate="%{y}<br>%{x} visits<extra></extra>",
            marker_line_width=0,
        )
        _chart_style(fig_obj)
        ev_obj = st.plotly_chart(fig_obj, use_container_width=True,
                                 on_select="rerun", key="an_obj")
        _handle_hbar_click(ev_obj, "objective", axis="y")


# ─────────────────────────────────────────────────────────────────────────────
# Tab 2 – KPIs per Rep
# ─────────────────────────────────────────────────────────────────────────────

def _tab_kpis(uid, role, date_from, date_to, filters, rep_ids):
    visits_df = get_analytics_visits_per_rep(uid, role, date_from, date_to, filters, rep_ids)
    rep_data  = get_analytics_kpis_per_rep(uid, role, date_from, date_to, filters, rep_ids)

    from datetime import timedelta as _td
    _delta     = date_to - date_from
    _prev_to   = date_from - _td(days=1)
    _prev_from = _prev_to - _delta
    prev_visits_df = get_analytics_visits_per_rep(uid, role, _prev_from, _prev_to, filters, rep_ids)
    prev_map = dict(zip(prev_visits_df["rep"], prev_visits_df["total_visits"])) if not prev_visits_df.empty else {}

    # ── Rep leaderboard ───────────────────────────────────────────────────────
    _an_label("Rep Leaderboard")

    apc_df = rep_data["audience_per_customer"].copy()
    apc_df["ratio"] = apc_df["ratio"].round(2)
    leaderboard = visits_df.merge(apc_df[["rep", "ratio"]], on="rep", how="left")
    leaderboard["ratio"] = leaderboard["ratio"].fillna(0).round(2)

    def _initials(name: str) -> str:
        parts = (name or "?").split()
        raw = "".join(p[0].upper() for p in parts[:2])
        return _html.escape(raw)

    rows_html = ""
    for idx_r, row in leaderboard.iterrows():
        rank      = idx_r + 1
        name      = str(row["rep"])
        visits    = int(row["total_visits"])
        custs     = int(row["total_customers"])
        ratio     = float(row["ratio"])
        prev_v = prev_map.get(name, 0)
        if prev_v and visits > prev_v:
            trend_html = '<span style="color:#0e8a4f;font-size:0.7rem;">&#9650;</span>'
        elif prev_v and visits < prev_v:
            trend_html = '<span style="color:#c83333;font-size:0.7rem;">&#9660;</span>'
        else:
            trend_html = '<span style="color:#999;font-size:0.7rem;">&#8211;</span>'
        initials = _initials(name)
        if rank == 1:
            row_cls  = "an-lb-row-1"
            av_bg    = "#d97706"
            av_fg    = "#ffffff"
            rank_cls = "an-lb-rank-1"
            weight   = "700"
        elif rank == 2:
            row_cls  = ""
            av_bg    = "var(--color-surface-2)"
            av_fg    = "#71717a"
            rank_cls = "an-lb-rank-2"
            weight   = "600"
        elif rank == 3:
            row_cls  = ""
            av_bg    = "var(--color-surface-2)"
            av_fg    = "#9a3412"
            rank_cls = "an-lb-rank-3"
            weight   = "600"
        else:
            row_cls  = ""
            av_bg    = "var(--color-surface-2)"
            av_fg    = "var(--color-text-subtle)"
            rank_cls = ""
            weight   = "500"
        rows_html += (
            f'<tr class="{row_cls}">'
            f'<td style="padding:9px 14px;font-size:0.72rem;">'
            f'<span class="{rank_cls}">{rank}</span></td>'
            f'<td style="padding:9px 14px;">'
            f'<div style="display:flex;align-items:center;gap:8px;">'
            f'<div style="width:28px;height:28px;border-radius:50%;background:{av_bg};'
            f'display:flex;align-items:center;justify-content:center;'
            f'font-size:0.58rem;font-weight:700;color:{av_fg};flex-shrink:0;">'
            f'{initials}</div>'
            f'<span style="font-size:0.78rem;font-weight:{weight};'
            f'color:var(--color-text);">{_html.escape(name)}</span>'
            f'</div></td>'
            f'<td style="padding:9px 14px;font-size:0.78rem;font-weight:700;'
            f'color:var(--color-text);text-align:right;">{visits:,} {trend_html}</td>'
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
        _an_label("Audiences / Customer by Rep")
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
            fig.update_traces(
                hovertemplate="%{y}<br>Ratio: %{x:.2f}<extra></extra>",
                marker_line_width=0,
            )
            if not df.empty:
                mean_ratio = df["ratio"].mean()
                fig.add_vline(
                    x=mean_ratio,
                    line_dash="dash",
                    line_color="rgba(100,100,100,0.5)",
                    annotation_text=f"Avg {mean_ratio:.2f}",
                    annotation_position="top right",
                    annotation_font_size=10,
                )
            _chart_style(fig)
            st.plotly_chart(fig, use_container_width=True, key="an_kpi_apc")

    with col2:
        _an_label("Avg Customers / Month by Rep")
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
            fig3.update_traces(
                hovertemplate="%{y}<br>Avg/Month: %{x:.1f}<extra></extra>",
                marker_line_width=0,
            )
            if not df3.empty:
                mean_monthly = df3["avg_monthly"].mean()
                fig3.add_vline(
                    x=mean_monthly,
                    line_dash="dash",
                    line_color="rgba(100,100,100,0.5)",
                    annotation_text=f"Avg {mean_monthly:.1f}",
                    annotation_position="top right",
                    annotation_font_size=10,
                )
            _chart_style(fig3)
            st.plotly_chart(fig3, use_container_width=True, key="an_kpi_acm")

    # ── Target progress bars ──────────────────────────────────────────────────
    import datetime as _dt
    current_year = _dt.date.today().year
    tgt_df = get_analytics_target_vs_actual(current_year, rep_ids)
    if not tgt_df.empty and tgt_df["target_visits"].sum() > 0:
        _an_label(f"Visit Target Progress — {current_year}")
        for _, trow in tgt_df.iterrows():
            rep_name = str(trow["rep"])
            target_v = int(trow["target_visits"])
            actual_v = int(trow["actual_visits"])
            if target_v == 0:
                continue
            pct_float = actual_v / target_v
            pct_capped = min(pct_float, 1.0)
            bar_w     = f"{pct_capped * 100:.0f}%"
            pct_label = f"{pct_float * 100:.0f}%"
            if pct_float >= 1.0:
                bar_clr   = "var(--status-success-text)"
                badge_bg  = "var(--status-success-bg)"
                badge_fg  = "var(--status-success-text)"
            elif pct_float >= 0.70:
                bar_clr   = "#f59e0b"
                badge_bg  = "var(--status-warning-bg)"
                badge_fg  = "var(--status-warning-text)"
            else:
                bar_clr   = "var(--color-primary)"
                badge_bg  = "var(--color-primary-subtle)"
                badge_fg  = "var(--color-primary)"
            count_label = f"{actual_v:,} / {target_v:,}"
            st.markdown(
                f'<div class="an-progress-wrap">'
                f'<div style="display:flex;justify-content:space-between;align-items:center;'
                f'font-size:0.75rem;margin-bottom:4px;">'
                f'<span style="font-weight:600;color:var(--color-text);">'
                f'{_html.escape(rep_name)}</span>'
                f'<div style="display:flex;align-items:center;gap:6px;">'
                f'<span style="color:var(--color-text-muted);font-size:0.72rem;">{count_label}</span>'
                f'<span style="background:{badge_bg};color:{badge_fg};font-size:0.65rem;'
                f'font-weight:700;padding:1px 7px;border-radius:4px;">{pct_label}</span>'
                f'</div></div>'
                f'<div class="an-progress-track">'
                f'<div class="an-progress-fill" style="background:{bar_clr};width:{bar_w};"></div>'
                f'</div></div>',
                unsafe_allow_html=True,
            )


@st.cache_data(ttl=300)
def _get_region_opts() -> list:
    try:
        return query_df(
            "SELECT DISTINCT region FROM customers WHERE region IS NOT NULL ORDER BY region"
        )["region"].tolist()
    except Exception:
        return []


@st.cache_data(ttl=300)
def _get_city_opts(region: str) -> list:
    try:
        if region != "All":
            return query_df(
                "SELECT DISTINCT city FROM customers WHERE region = :r AND city IS NOT NULL ORDER BY city",
                {"r": region},
            )["city"].tolist()
        return query_df(
            "SELECT DISTINCT city FROM customers WHERE city IS NOT NULL ORDER BY city"
        )["city"].tolist()
    except Exception:
        return []


@st.cache_data(ttl=300)
def _get_sector_opts(city: str) -> list:
    try:
        if city != "All":
            return query_df(
                "SELECT DISTINCT sector FROM customers WHERE city = :c AND sector IS NOT NULL ORDER BY sector",
                {"c": city},
            )["sector"].tolist()
        return query_df(
            "SELECT DISTINCT sector FROM customers WHERE sector IS NOT NULL ORDER BY sector"
        )["sector"].tolist()
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Tab 3 – Visits Detail
# ─────────────────────────────────────────────────────────────────────────────

def _tab_visits_detail(uid, role, date_from, date_to, filters, rep_ids):
    # ── Cascading region → city → sector filter ───────────────────────────────
    _an_label("Filter by Location")
    col_reg, col_city, col_sec = st.columns(3)

    region_opts = _get_region_opts()

    with col_reg:
        sel_region = st.selectbox("Region", ["All"] + region_opts, key="vd_region")

    city_opts = _get_city_opts(sel_region)

    with col_city:
        sel_city = st.selectbox("City", ["All"] + city_opts, key="vd_city")

    sector_opts = _get_sector_opts(sel_city)

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
        _an_label("Customer Locations")
        cust_df = _cached_customer_locations()
        if not cust_df.empty:
            m1 = folium.Map(
                location=[cust_df["latitude"].mean(), cust_df["longitude"].mean()],
                zoom_start=5, tiles="CartoDB positron",
            )
            from folium.plugins import MarkerCluster as _MC
            cluster1 = _MC().add_to(m1)
            for _, row in cust_df.iterrows():
                folium.CircleMarker(
                    location=[row["latitude"], row["longitude"]],
                    radius=4, color="#0ea5e9", fill=True, fill_opacity=0.6,
                    tooltip=row["account_name"],
                ).add_to(cluster1)
            st_folium(m1, width="100%", height=280, returned_objects=[])

    with col_m2:
        _an_label("Visit Locations")
        visit_loc_df = get_visit_locations_for_map(
            uid, role, date_from, date_to, loc_filters, rep_ids
        )
        if not visit_loc_df.empty:
            m2 = folium.Map(
                location=[visit_loc_df["latitude"].mean(), visit_loc_df["longitude"].mean()],
                zoom_start=5, tiles="CartoDB positron",
            )
            cluster2 = _MC().add_to(m2)
            for _, row in visit_loc_df.iterrows():
                folium.CircleMarker(
                    location=[row["latitude"], row["longitude"]],
                    radius=3, color=BRAND, fill=True, fill_opacity=0.5,
                    tooltip=f"{row['customer']} — {row['rep']}",
                ).add_to(cluster2)
            st_folium(m2, width="100%", height=280, returned_objects=[])
        else:
            _an_empty("No visits with location data in selected range.")

    # ── Visit records table ───────────────────────────────────────────────────
    _an_label("Visit Records")
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
        csv_buf = io.StringIO()
        detail_df.to_csv(csv_buf, index=False)
        st.download_button(
            label="Download CSV",
            data=csv_buf.getvalue().encode("utf-8"),
            file_name=f"visits_{date_from}_{date_to}.csv",
            mime="text/csv",
            key="an_csv_download",
        )
    else:
        _an_empty("No visits match the current filters.")


# ─────────────────────────────────────────────────────────────────────────────
# Tab 4 – Time Map
# ─────────────────────────────────────────────────────────────────────────────

def _tab_time_map(uid, role, date_from, date_to, filters, rep_ids):
    tm_df = get_analytics_time_map(uid, role, date_from, date_to, filters, rep_ids)

    if tm_df.empty:
        _an_empty("No data for the selected period.")
        return

    # ── Heatmap: Day × Hour — brand blue ─────────────────────────────────────
    _an_label("Day × Hour Heatmap  ·  click a cell to cross-filter")
    pivot = tm_df.groupby(["dow", "hour"])["visit_count"].sum().reset_index()
    heat_matrix = pd.DataFrame(0, index=list(range(7)), columns=list(range(24)))
    for _, row in pivot.iterrows():
        heat_matrix.loc[int(row["dow"]), int(row["hour"])] = int(row["visit_count"])
    heat_matrix.index   = _DOW_NAMES
    heat_matrix.columns = [str(h) for h in range(24)]
    active_cols = [c for c in heat_matrix.columns if heat_matrix[c].sum() > 0]
    heat_matrix = heat_matrix[active_cols]

    def _hour_label(h: str) -> str:
        hi = int(h)
        if hi == 0:    return "12 AM"
        if hi < 12:    return f"{hi} AM"
        if hi == 12:   return "12 PM"
        return f"{hi - 12} PM"

    labelled_cols = [_hour_label(c) for c in active_cols]

    fig_heat = go.Figure(go.Heatmap(
        z=heat_matrix.values.tolist(),
        x=labelled_cols,
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
        xaxis=dict(title="Hour of Day", side="bottom", tickfont=dict(size=10)),
        yaxis=dict(title="", autorange="reversed"),
        paper_bgcolor="rgba(0,0,0,0)",
    )
    fig_heat.update_traces(colorbar=dict(
        thickness=10,
        len=0.8,
        tickfont=dict(family="Inter, sans-serif", size=10, color="#57606a"),
        outlinewidth=0,
        borderwidth=0,
    ))
    import numpy as _np
    peak_val = _np.array(heat_matrix.values).max()
    if peak_val > 0:
        peak_arr = _np.array(heat_matrix.values)
        peak_row, peak_col = _np.unravel_index(peak_arr.argmax(), peak_arr.shape)
        fig_heat.add_annotation(
            x=labelled_cols[peak_col],
            y=_DOW_NAMES[peak_row],
            text="★",
            showarrow=False,
            font=dict(size=14, color="#ffffff"),
            xanchor="center",
            yanchor="middle",
        )

    _chart_style(fig_heat)
    ev_heat = st.plotly_chart(fig_heat, use_container_width=True,
                               on_select="rerun", key="an_heatmap")
    _handle_heatmap_click(ev_heat)

    st.markdown("<div style='margin-bottom:1rem;'></div>", unsafe_allow_html=True)

    # ── Bottom row: Today · Day bar · Hour bar ────────────────────────────────
    col_t, col_day, col_hr = st.columns([1, 2, 2])

    with col_t:
        today = _local_now().date()
        _an_label(f"Today  ·  {today.strftime('%d/%m/%Y')}")
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
            _an_empty("No visits today.")

    with col_day:
        _an_label("Visits by Day")
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
        fig_day.update_traces(
            hovertemplate="%{x}: %{y} visits<extra></extra>",
            marker_line_width=0,
        )
        _chart_style(fig_day)
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
        _an_label("Visits by Hour")
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
        fig_hr.update_traces(
            hovertemplate="Hour %{x}: %{y} visits<extra></extra>",
            marker_line_width=0,
        )
        _chart_style(fig_hr)
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
    _an_label("Rep Attendance Calendar")

    att_df = get_analytics_attendance(uid, role, date_from, date_to, rep_ids)
    if att_df.empty:
        _an_empty("No attendance data for the selected period.")
        return

    att_df["date"] = pd.to_datetime(att_df["date"])
    pivot_att = att_df.pivot_table(
        index="rep_name", columns="date", values="visit_count",
        fill_value=0, aggfunc="sum",
    )
    pivot_att.columns = [c.strftime("%d/%m") for c in pivot_att.columns]
    pivot_att = pivot_att.reset_index().rename(columns={"rep_name": "Rep"})

    # cap at 31 most recent dates so the table stays usable
    if pivot_att.shape[1] > 32:  # 1 "Rep" column + up to 31 date columns
        pivot_att = pd.concat(
            [pivot_att.iloc[:, :1], pivot_att.iloc[:, -31:]], axis=1
        )

    heat_reps = sorted(pivot_att["Rep"].tolist())
    date_cols = [c for c in pivot_att.columns if c != "Rep"]

    z_matrix = []
    for rep in heat_reps:
        row_data = pivot_att[pivot_att["Rep"] == rep].iloc[0]
        z_matrix.append([int(row_data[d]) if (str(row_data.get(d, 0)) != "0" and row_data.get(d, 0)) else 0
                          for d in date_cols])

    fig_att = go.Figure(go.Heatmap(
        z=z_matrix,
        x=date_cols,
        y=heat_reps,
        colorscale=[[0, "#f0f0f0"], [0.01, "#bfdbfe"], [0.5, "#6ea6ff"], [1, "#2667ff"]],
        showscale=False,
        xgap=2, ygap=2,
        hovertemplate="<b>%{y}</b><br>%{x}: %{z} visits<extra></extra>",
    ))
    fig_att.update_layout(
        margin=dict(l=0, r=0, t=10, b=0),
        height=max(200, len(heat_reps) * 26 + 60),
        xaxis=dict(title="", tickfont=dict(size=9, family="Inter, sans-serif"), tickangle=-45),
        yaxis=dict(title="", tickfont=dict(size=10, family="Inter, sans-serif")),
        paper_bgcolor="rgba(0,0,0,0)",
    )
    _chart_style(fig_att)
    st.plotly_chart(fig_att, use_container_width=True, key="an_att_cal")


# ─────────────────────────────────────────────────────────────────────────────
# Tab 5 – Customer Health
# ─────────────────────────────────────────────────────────────────────────────

def _tab_customer_health(uid, role, rep_ids):
    health_df = get_analytics_customer_health(uid, role, rep_ids)

    if health_df.empty:
        _an_empty("No customer data available.")
        return

    # ── Summary strip ─────────────────────────────────────────────────────────
    total_active  = len(health_df["customer_name"].unique())
    never_visited = int(health_df["last_visit_date"].isna().sum())
    visited_30d   = int((health_df["days_since_visit"].fillna(9999) <= 30).sum())
    stale_60d     = int(
        ((health_df["days_since_visit"].fillna(9999) > 60) &
         (health_df["last_visit_date"].notna())).sum()
    )

    def _hcard(label, val, border_color="var(--color-primary)", text_color="var(--color-text)"):
        return (
            f'<div style="flex:1;min-width:130px;background:var(--color-surface);'
            f'border:1px solid var(--color-border);'
            f'border-left:4px solid {border_color};'
            f'border-radius:10px;padding:10px 14px;">'
            f'<div style="font-size:0.6rem;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:.06em;color:var(--color-text-subtle);margin-bottom:4px;">{label}</div>'
            f'<div style="font-size:1.4rem;font-weight:700;color:{text_color};">{val}</div>'
            f'</div>'
        )

    st.markdown(
        '<div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px;">'
        + _hcard("Active Customers",  f"{total_active:,}",
                 "var(--color-primary)",       "var(--color-primary)")
        + _hcard("Visited ≤ 30 days", f"{visited_30d:,}",
                 "var(--status-success-text)", "var(--status-success-text)")
        + _hcard("Stale > 60 days",   f"{stale_60d:,}",
                 "var(--status-warning-text)", "var(--status-warning-text)")
        + _hcard("Never Visited",     f"{never_visited:,}",
                 "var(--color-border-strong)", "var(--color-text-muted)")
        + '</div>',
        unsafe_allow_html=True,
    )

    # ── Days-since-visit histogram ────────────────────────────────────────────
    _an_label("Days Since Last Visit Distribution")
    hist_df = health_df.dropna(subset=["days_since_visit"]).copy()
    if not hist_df.empty:
        hist_df["days_since_visit"] = hist_df["days_since_visit"].astype(int)
        fig_hist = px.histogram(
            hist_df, x="days_since_visit", nbins=20,
            color_discrete_sequence=[BRAND],
        )
        fig_hist.update_layout(
            margin=dict(l=0, r=0, t=10, b=0), height=200,
            xaxis_title="Days Since Last Visit", yaxis_title="Customers",
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        fig_hist.update_yaxes(gridcolor="rgba(0,0,0,0.06)")
        fig_hist.update_traces(
            hovertemplate="%{x} days: %{y} customers<extra></extra>",
            marker_line_width=0,
        )
        _chart_style(fig_hist)
        st.plotly_chart(fig_hist, use_container_width=True, key="an_ch_hist")

    # ── Detail table ──────────────────────────────────────────────────────────
    _an_label("Customer List — Sorted by Days Inactive")
    display_df = health_df.copy()
    display_df["last_visit_date"] = display_df["last_visit_date"].fillna("Never").astype(str)
    display_df["days_since_visit"] = display_df["days_since_visit"].fillna("—").astype(str)
    display_df.columns = [
        "Customer", "Region", "City", "Last Rep", "Last Visit", "Days Inactive"
    ]
    st.markdown(
        html_table(display_df, max_rows=500, max_height=420),
        unsafe_allow_html=True,
    )
    st.caption(f"{len(display_df):,} active customers shown")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def page_analytics():
    _analytics_css()
    user = st.session_state.get("user") or resolve_session_user()
    if not user:
        st.error("Not logged in.")
        return

    uid  = user["user_id"]
    role = (user.get("role") or "").lower().strip()
    is_elevated = role in ("admin", "sales manager", "biomedical manager", "supervisor")

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
            reps_df = _cached_all_reps()
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

    # ── Tabs ─────────────────────────────────────────────────────────────────
    tab1, tab2, tab3, tab4, tab5 = st.tabs(["Overview", "KPIs", "Visits Detail", "Time Map", "Customer Health"])

    with tab1:
        _tab_overview(uid, role, date_from, date_to, filters, rep_ids)
    with tab2:
        _tab_kpis(uid, role, date_from, date_to, filters, rep_ids)
    with tab3:
        _tab_visits_detail(uid, role, date_from, date_to, filters, rep_ids)
    with tab4:
        _tab_time_map(uid, role, date_from, date_to, filters, rep_ids)
    with tab5:
        _tab_customer_health(uid, role, rep_ids)
