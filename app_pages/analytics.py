# app_pages/analytics.py — Analytics Dashboard
from datetime import date

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

BRAND   = "#2667ff"
PALETTE = px.colors.qualitative.Set2

_DOW_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
_DOW_MAP   = {name: i for i, name in enumerate(_DOW_NAMES)}


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
        h = int(hour_val)
        if filters.get("hour") == h:
            filters.pop("hour", None)
        else:
            filters["hour"] = h
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
    kpis   = get_analytics_kpis(uid, role, date_from, date_to, filters, rep_ids)
    breaks = get_analytics_breakdowns(uid, role, date_from, date_to, filters, rep_ids)

    # KPI cards
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Visits",        int(kpis.get("total_visits", 0)))
    c2.metric("Customers",           int(kpis.get("total_customers", 0)))
    c3.metric("Audiences",           int(kpis.get("total_audiences", 0)))
    c4.metric("Audience / Customer", f"{kpis.get('audiences_per_customer', 0):.1f}")
    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Customer / Day",      f"{kpis.get('customers_per_day', 0):.1f}")
    c6.metric("Visits / Customer",   f"{kpis.get('visits_per_customer', 0):.1f}")
    c7.metric("Customers / Month",   f"{kpis.get('avg_customers_per_month', 0):.1f}")
    c8.metric("BL / Month",          f"{kpis.get('avg_bl_per_month', 0):.1f}")

    st.markdown("---")

    # Time series with drill-down
    st.markdown("**Visits Over Time**")
    gran = st.radio("Granularity", ["Year", "Month", "Week"], horizontal=True, key="an_gran",
                    label_visibility="collapsed")
    ts_df = get_analytics_time_series(uid, role, date_from, date_to, gran, filters, rep_ids)
    if not ts_df.empty:
        # Reformat Month periods from "YYYY-MM" to "Mon YYYY" for display
        if gran == "Month":
            ts_df["period"] = pd.to_datetime(ts_df["period"], format="%Y-%m").dt.strftime("%b %Y")
        fig_ts = px.line(ts_df, x="period", y="visit_count", markers=True,
                         color_discrete_sequence=[BRAND])
        fig_ts.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=220,
                              xaxis_title="", yaxis_title="Visits",
                              plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
        fig_ts.update_xaxes(showgrid=False)
        fig_ts.update_yaxes(gridcolor="#f0f0f0")
        st.plotly_chart(fig_ts, use_container_width=True, key="an_ts")
    else:
        st.info("No visit data for the selected period.")

    st.markdown("---")

    # Breakdowns row
    col_r, col_bu = st.columns(2)

    with col_r:
        st.markdown("**By Region**")
        rdf = breaks["region"]
        if not rdf.empty:
            fig_r = px.pie(rdf, names="region", values="count", hole=0.4,
                           color_discrete_sequence=PALETTE)
            fig_r.update_traces(textposition="outside", textinfo="percent+label")
            fig_r.update_layout(margin=dict(l=0, r=0, t=10, b=30), height=270,
                                 showlegend=False,
                                 paper_bgcolor="rgba(0,0,0,0)")
            ev_r = st.plotly_chart(fig_r, use_container_width=True, on_select="rerun", key="an_region")
            _handle_pie_click(ev_r, "region")

    with col_bu:
        st.markdown("**By Business Unit**")
        bdf = breaks["business_unit"]
        if not bdf.empty:
            fig_bu = px.pie(bdf, names="business_unit", values="count", hole=0.4,
                            color_discrete_sequence=px.colors.qualitative.Pastel)
            fig_bu.update_traces(textposition="outside", textinfo="percent+label")
            fig_bu.update_layout(margin=dict(l=0, r=0, t=10, b=30), height=270,
                                  showlegend=False,
                                  paper_bgcolor="rgba(0,0,0,0)")
            ev_bu = st.plotly_chart(fig_bu, use_container_width=True, on_select="rerun", key="an_bu")
            _handle_pie_click(ev_bu, "business_unit")

    # Objective funnel bar
    st.markdown("**Visits by Objective**")
    odf = breaks["objective"]
    if not odf.empty:
        fig_obj = px.bar(odf, y="objective", x="count", orientation="h",
                         color_discrete_sequence=[BRAND])
        fig_obj.update_layout(
            margin=dict(l=0, r=0, t=10, b=0),
            height=max(200, len(odf) * 30),
            yaxis=dict(autorange="reversed", title=""),
            xaxis_title="Visits",
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        fig_obj.update_xaxes(gridcolor="#f0f0f0")
        ev_obj = st.plotly_chart(fig_obj, use_container_width=True, on_select="rerun", key="an_obj")
        _handle_hbar_click(ev_obj, "objective", axis="y")

    # Customer locations map
    st.markdown("**Customer Locations**")
    cust_df = get_customer_locations_for_map()
    if not cust_df.empty:
        center_lat = cust_df["latitude"].mean()
        center_lon = cust_df["longitude"].mean()
        m = folium.Map(location=[center_lat, center_lon], zoom_start=6, tiles="CartoDB positron")
        for _, row in cust_df.iterrows():
            folium.CircleMarker(
                location=[row["latitude"], row["longitude"]],
                radius=5, color=BRAND, fill=True, fill_opacity=0.7,
                tooltip=f"{row['account_name']} ({row.get('city', '')})",
            ).add_to(m)
        st_folium(m, width="100%", height=350, returned_objects=[])


# ─────────────────────────────────────────────────────────────────────────────
# Tab 2 – KPIs per Rep
# ─────────────────────────────────────────────────────────────────────────────

def _tab_kpis(uid, role, date_from, date_to, filters, rep_ids):
    rep_data = get_analytics_kpis_per_rep(uid, role, date_from, date_to, filters, rep_ids)

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("**Audiences Visited per Rep**")
        df = rep_data["audience_count"]
        if not df.empty:
            fig = px.bar(df, y="rep", x="count", orientation="h",
                         color_discrete_sequence=[BRAND])
            fig.update_layout(margin=dict(l=0, r=0, t=10, b=0),
                               height=max(220, len(df) * 30),
                               yaxis=dict(autorange="reversed", title=""),
                               xaxis_title="Audiences",
                               plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
            fig.update_xaxes(gridcolor="#f0f0f0")
            st.plotly_chart(fig, use_container_width=True, key="an_kpi_aud")

    with col2:
        st.markdown("**Audience / Customer by Rep**")
        df2 = rep_data["audience_per_customer"]
        if not df2.empty:
            df2["ratio"] = df2["ratio"].round(1)
            fig2 = px.bar(df2, y="rep", x="ratio", orientation="h",
                          color_discrete_sequence=["#6366f1"])
            fig2.update_layout(margin=dict(l=0, r=0, t=10, b=0),
                                height=max(220, len(df2) * 30),
                                yaxis=dict(autorange="reversed", title=""),
                                xaxis_title="Ratio",
                                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
            fig2.update_xaxes(gridcolor="#f0f0f0")
            st.plotly_chart(fig2, use_container_width=True, key="an_kpi_apc")

    col3, col4 = st.columns(2)

    with col3:
        st.markdown("**Avg Customers / Month per Rep**")
        df3 = rep_data["avg_customers_per_month"]
        if not df3.empty:
            df3["avg_monthly"] = df3["avg_monthly"].round(1)
            fig3 = px.bar(df3, y="rep", x="avg_monthly", orientation="h",
                          color_discrete_sequence=["#10b981"])
            fig3.update_layout(margin=dict(l=0, r=0, t=10, b=0),
                                height=max(220, len(df3) * 30),
                                yaxis=dict(autorange="reversed", title=""),
                                xaxis_title="Avg Customers/Month",
                                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
            fig3.update_xaxes(gridcolor="#f0f0f0")
            st.plotly_chart(fig3, use_container_width=True, key="an_kpi_acm")

    with col4:
        st.markdown("**Visits by Region**")
        df4 = rep_data["region"]
        if not df4.empty:
            fig4 = px.bar(df4, x="region", y="count",
                          color_discrete_sequence=[BRAND])
            fig4.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=260,
                                xaxis_title="", yaxis_title="Visits",
                                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
            fig4.update_yaxes(gridcolor="#f0f0f0")
            st.plotly_chart(fig4, use_container_width=True, key="an_kpi_reg")


# ─────────────────────────────────────────────────────────────────────────────
# Tab 3 – Visits Detail
# ─────────────────────────────────────────────────────────────────────────────

def _tab_visits_detail(uid, role, date_from, date_to, filters, rep_ids):
    col_m1, col_m2 = st.columns(2)

    with col_m1:
        st.markdown("**Customer Locations**")
        cust_df = get_customer_locations_for_map()
        if not cust_df.empty:
            m1 = folium.Map(location=[cust_df["latitude"].mean(), cust_df["longitude"].mean()],
                            zoom_start=5, tiles="CartoDB positron")
            for _, row in cust_df.iterrows():
                folium.CircleMarker(
                    location=[row["latitude"], row["longitude"]],
                    radius=4, color="#0ea5e9", fill=True, fill_opacity=0.6,
                    tooltip=row["account_name"],
                ).add_to(m1)
            st_folium(m1, width="100%", height=280, returned_objects=[])

    with col_m2:
        st.markdown("**Visit Locations**")
        visit_loc_df = get_visit_locations_for_map(uid, role, date_from, date_to, filters, rep_ids)
        if not visit_loc_df.empty:
            m2 = folium.Map(location=[visit_loc_df["latitude"].mean(),
                                       visit_loc_df["longitude"].mean()],
                             zoom_start=5, tiles="CartoDB positron")
            for _, row in visit_loc_df.iterrows():
                folium.CircleMarker(
                    location=[row["latitude"], row["longitude"]],
                    radius=3, color=BRAND, fill=True, fill_opacity=0.5,
                    tooltip=f"{row['customer']} — {row['rep']}",
                ).add_to(m2)
            st_folium(m2, width="100%", height=280, returned_objects=[])
        else:
            st.info("No visits with location data in selected range.")

    st.markdown("---")
    st.markdown("**Visit Records**")
    detail_df = get_analytics_visits_detail(uid, role, date_from, date_to, filters, rep_ids)
    if not detail_df.empty:
        detail_df["Date Local"] = pd.to_datetime(detail_df["Date Local"]).dt.strftime("%d/%m/%Y %I:%M %p")
        st.dataframe(detail_df, use_container_width=True, hide_index=True)
        st.caption(f"{len(detail_df):,} records shown (max 1,000)")
    else:
        st.info("No visits match the current filters.")


# ─────────────────────────────────────────────────────────────────────────────
# Tab 4 – Time Map
# ─────────────────────────────────────────────────────────────────────────────

def _tab_time_map(uid, role, date_from, date_to, filters, rep_ids):
    tm_df = get_analytics_time_map(uid, role, date_from, date_to, filters, rep_ids)

    if tm_df.empty:
        st.info("No data for the selected period.")
        return

    # ── Heatmap: Day × Hour ──────────────────────────────────────────────────
    st.markdown("**Day × Hour Heatmap**  *(click a cell to cross-filter)*")
    pivot = (
        tm_df.groupby(["dow", "hour"])["visit_count"].sum()
             .reset_index()
    )
    all_hours = list(range(24))
    all_dows  = list(range(7))
    heat_matrix = pd.DataFrame(0, index=all_dows, columns=all_hours)
    for _, row in pivot.iterrows():
        heat_matrix.loc[int(row["dow"]), int(row["hour"])] = int(row["visit_count"])

    heat_matrix.index   = _DOW_NAMES
    heat_matrix.columns = [str(h) for h in all_hours]
    # Drop always-zero hour columns for readability
    active_cols = [c for c in heat_matrix.columns if heat_matrix[c].sum() > 0]
    heat_matrix = heat_matrix[active_cols]

    fig_heat = go.Figure(go.Heatmap(
        z=heat_matrix.values.tolist(),
        x=active_cols,
        y=_DOW_NAMES,
        colorscale=[[0, "#f0f4ff"], [0.5, "#6ea6ff"], [1, BRAND]],
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
    ev_heat = st.plotly_chart(fig_heat, use_container_width=True, on_select="rerun", key="an_heatmap")
    _handle_heatmap_click(ev_heat)

    st.markdown("---")
    col_t, col_day, col_hr = st.columns([1, 2, 2])

    # Today's visits table
    with col_t:
        today = _local_now().date()
        st.markdown(f"**Today's Visits**  \n*{today.strftime('%d/%m/%Y')}*")
        today_df = get_analytics_today(uid, role, today, rep_ids)
        if not today_df.empty:
            total_row = pd.DataFrame([{"Frontline Name": "**Total**", "Visits": today_df["Visits"].sum()}])
            st.dataframe(pd.concat([today_df, total_row], ignore_index=True),
                         hide_index=True, use_container_width=True)
        else:
            st.caption("No visits today.")

    # Stacked bar: by Day Name × BU
    with col_day:
        st.markdown("**Visits by Day**")
        day_bu = (
            tm_df.groupby(["dow", "business_unit"])["visit_count"].sum()
                 .reset_index()
        )
        day_bu["Day"] = day_bu["dow"].apply(lambda d: _DOW_NAMES[int(d)])
        day_bu = day_bu.sort_values("dow")
        fig_day = px.bar(day_bu, x="Day", y="visit_count", color="business_unit",
                         color_discrete_sequence=PALETTE,
                         category_orders={"Day": _DOW_NAMES})
        fig_day.update_layout(
            margin=dict(l=0, r=0, t=10, b=0), height=280,
            xaxis_title="", yaxis_title="Visits",
            legend=dict(title="", orientation="h", y=-0.25),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        fig_day.update_yaxes(gridcolor="#f0f0f0")
        ev_day = st.plotly_chart(fig_day, use_container_width=True, on_select="rerun", key="an_daybar")
        if ev_day and getattr(ev_day, "selection", None):
            pts = ev_day.selection.get("points", []) if isinstance(ev_day.selection, dict) else getattr(ev_day.selection, "points", [])
            if pts:
                day_name = pts[0].get("x")
                if day_name and day_name in _DOW_MAP:
                    _set_filter("dow", _DOW_MAP[day_name])

    # Stacked bar: by Hour × BU
    with col_hr:
        st.markdown("**Visits by Hour**")
        hr_bu = (
            tm_df.groupby(["hour", "business_unit"])["visit_count"].sum()
                 .reset_index()
        )
        hr_bu["Hour"] = hr_bu["hour"].astype(str)
        fig_hr = px.bar(hr_bu, x="Hour", y="visit_count", color="business_unit",
                        color_discrete_sequence=PALETTE)
        fig_hr.update_layout(
            margin=dict(l=0, r=0, t=10, b=0), height=280,
            xaxis_title="Hour", yaxis_title="Visits",
            legend=dict(title="", orientation="h", y=-0.25),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        fig_hr.update_yaxes(gridcolor="#f0f0f0")
        ev_hr = st.plotly_chart(fig_hr, use_container_width=True, on_select="rerun", key="an_hrbar")
        if ev_hr and getattr(ev_hr, "selection", None):
            pts = ev_hr.selection.get("points", []) if isinstance(ev_hr.selection, dict) else getattr(ev_hr.selection, "points", [])
            if pts:
                h = pts[0].get("x")
                if h is not None:
                    _set_filter("hour", int(h))


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def page_analytics():
    user = st.session_state.get("user") or resolve_session_user()
    if not user:
        st.error("Not logged in.")
        return

    uid  = user["user_id"]
    role = (user.get("role") or "").lower().strip()
    is_elevated = role in ("admin", "sales manager", "biomedical manager", "supervisor")

    st.markdown("## Analytics")

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

    # ── Active filter chips ───────────────────────────────────────────────────
    _render_chips(filters)

    # ── Tabs ─────────────────────────────────────────────────────────────────
    tab1, tab2, tab3, tab4 = st.tabs(["Overview", "KPIs", "Visits Detail", "Time Map"])

    with tab1:
        _tab_overview(uid, role, date_from, date_to, filters, rep_ids)
    with tab2:
        _tab_kpis(uid, role, date_from, date_to, filters, rep_ids)
    with tab3:
        _tab_visits_detail(uid, role, date_from, date_to, filters, rep_ids)
    with tab4:
        _tab_time_map(uid, role, date_from, date_to, filters, rep_ids)
