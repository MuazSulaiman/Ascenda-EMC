# pages/dashboard.py — Ascenda Dashboard
import html
import streamlit as st
from datetime import datetime

import pandas as pd

from auth import resolve_session_user
from config import TIMEZONE
from db_ops import query_df
from ui import kpi_card_v2, section_header, status_badge
from widgets import set_current_page
from utils import _local_now


# SVG icons for KPI cards (inline, monochrome, stroked)
_ICON_LOCATION = (
    '<svg width="18" height="18" fill="none" stroke="#2667ff" stroke-width="2" '
    'viewBox="0 0 24 24"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/>'
    '<circle cx="12" cy="10" r="3"/></svg>'
)
_ICON_CLOCK = (
    '<svg width="18" height="18" fill="none" stroke="#b5651d" stroke-width="2" '
    'viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/>'
    '<polyline points="12 6 12 12 16 14"/></svg>'
)
_ICON_CHECK = (
    '<svg width="18" height="18" fill="none" stroke="#0e8a4f" stroke-width="2" '
    'viewBox="0 0 24 24"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>'
    '<polyline points="22 4 12 14.01 9 11.01"/></svg>'
)
_ICON_ALERT = (
    '<svg width="18" height="18" fill="none" stroke="#c83333" stroke-width="2" '
    'viewBox="0 0 24 24">'
    '<circle cx="12" cy="12" r="10"/>'
    '<line x1="12" y1="8" x2="12" y2="12"/>'
    '<line x1="12" y1="16" x2="12.01" y2="16"/></svg>'
)
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


def _safe_count(sql: str, params: dict = None) -> int:
    try:
        r = query_df(sql, params or {})
        return int(r.iloc[0, 0]) if not r.empty else 0
    except Exception:
        return 0


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
        _render_admin_dashboard()
        return

    # ── Greeting header ───────────────────────────────────────────────────────
    try:
        now_local = _local_now()
        date_str  = f"{now_local.strftime('%A, %B')} {now_local.day}"
    except Exception:
        date_str = datetime.now().strftime("%A, %B %d")

    section_header(f"Welcome back, {first_name}", f"Here's what's happening in the field, {date_str}.")

    # ── Period filter ─────────────────────────────────────────────────────────
    period = st.radio(
        "",
        ["This week", "This month", "All time"],
        horizontal=True,
        key="dash_period",
        label_visibility="collapsed",
    )

    # ── KPI queries ───────────────────────────────────────────────────────────
    period_filter = {
        "This week":  "AND v.submitted_at_local >= date_trunc('week',  NOW() AT TIME ZONE 'Asia/Riyadh')",
        "This month": "AND v.submitted_at_local >= date_trunc('month', NOW() AT TIME ZONE 'Asia/Riyadh')",
        "All time":   "",
    }.get(period, "")

    # Today's visits
    today_count = _safe_count(
        "SELECT COUNT(*) FROM visits v WHERE v.user_id = :uid "
        "AND DATE(v.submitted_at_local) = CURRENT_DATE",
        {"uid": uid},
    )
    yesterday_count = _safe_count(
        "SELECT COUNT(*) FROM visits v WHERE v.user_id = :uid "
        "AND DATE(v.submitted_at_local) = CURRENT_DATE - 1",
        {"uid": uid},
    )
    today_delta_val = today_count - yesterday_count
    today_delta = (
        f"+{today_delta_val} vs yesterday" if today_delta_val >= 0
        else f"{today_delta_val} vs yesterday"
    )

    # Period total
    period_total = _safe_count(
        f"SELECT COUNT(*) FROM visits v WHERE v.user_id = :uid {period_filter}",
        {"uid": uid},
    )

    # Pending change requests (visits with open IN_REVIEW requests by this user)
    pending_cr = _safe_count(
        "SELECT COUNT(*) FROM request_changes rc "
        "WHERE rc.requested_by = :uid AND rc.status = 'IN_REVIEW'",
        {"uid": uid},
    )

    # Distinct customers visited in period
    customers_visited = _safe_count(
        f"SELECT COUNT(DISTINCT v.customer_id) FROM visits v "
        f"WHERE v.user_id = :uid {period_filter}",
        {"uid": uid},
    )

    # Evaluation breakdown (positive rate)
    eval_df = query_df(
        f"SELECT evaluation, COUNT(*) AS cnt FROM visits v "
        f"WHERE v.user_id = :uid {period_filter} "
        f"GROUP BY evaluation",
        {"uid": uid},
    ) if period_total > 0 else None

    positive_rate = 0
    if eval_df is not None and not eval_df.empty:
        pos = int(eval_df.loc[eval_df["evaluation"] == "Positive", "cnt"].sum())
        positive_rate = round(pos / period_total * 100) if period_total > 0 else 0

    # ── Render KPI cards ──────────────────────────────────────────────────────
    st.markdown(
        kpi_card_v2(
            label="Today's Visits",
            value=str(today_count),
            delta=today_delta,
            delta_positive=today_delta_val >= 0,
            icon_svg=_ICON_LOCATION,
            icon_bg="#eef2ff",
        ),
        unsafe_allow_html=True,
    )
    st.markdown(
        kpi_card_v2(
            label=f"Total Visits ({period})",
            value=str(period_total),
            delta=f"Across {customers_visited} customer{'s' if customers_visited != 1 else ''}",
            delta_positive=True,
            icon_svg=_ICON_CHECK,
            icon_bg="#e6f6ec",
        ),
        unsafe_allow_html=True,
    )
    st.markdown(
        kpi_card_v2(
            label="Open Change Requests",
            value=str(pending_cr),
            delta="Awaiting review" if pending_cr > 0 else "None pending",
            delta_positive=pending_cr == 0,
            icon_svg=_ICON_CLOCK,
            icon_bg="#fdf2e4",
        ),
        unsafe_allow_html=True,
    )
    if period_total > 0:
        st.markdown(
            kpi_card_v2(
                label="Positive Evaluation Rate",
                value=f"{positive_rate}%",
                delta=f"{period_total} visits evaluated",
                delta_positive=positive_rate >= 60,
                icon_svg=_ICON_ALERT if positive_rate < 60 else _ICON_CHECK,
                icon_bg="#fdeceb" if positive_rate < 60 else "#e6f6ec",
            ),
            unsafe_allow_html=True,
        )


def _render_admin_dashboard() -> None:
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
                delta="Submitted ≥1 visit",
                delta_positive=True,
                icon_svg=_ICON_USERS,
                icon_bg="#eef2ff",
            ),
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # ── Pending Reviews ───────────────────────────────────────────────────────
    _render_admin_pending_reviews()


def _render_admin_pending_reviews() -> None:
    """Render the pending reviews section: summary badges + unified action list."""

    st.markdown("#### Pending Reviews")

    # ── Summary counts ────────────────────────────────────────────────────────
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
        st.info("Pending items could not be loaded.")
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
        variant    = _TYPE_VARIANT.get(str(row["type"]), "neutral")
        badge      = status_badge(html.escape(str(row["type"])), variant)
        target     = str(row["target_page"])
        identifier = html.escape(str(row["identifier"]))
        rep_name   = html.escape(str(row["rep_name"]))

        col_info, col_btn = st.columns([5, 1])
        with col_info:
            st.markdown(
                f'<div style="display:flex;align-items:center;gap:10px;'
                f'padding:10px 0;border-bottom:1px solid #e4e8ec;">'
                f'{badge}'
                f'<span style="font-weight:600;font-size:0.9rem;color:#0d1117;">'
                f'{identifier}</span>'
                f'<span style="font-size:0.85rem;color:#57606a;">'
                f'{rep_name}</span>'
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
                st.query_params["page"] = target
                st.rerun()
