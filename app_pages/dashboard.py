# pages/dashboard.py — Ascenda Dashboard
import html
import streamlit as st
from datetime import datetime

import pandas as pd

from auth import resolve_session_user
from config import TIMEZONE
from db_ops import query_df
from ui import kpi_card_v2, kpi_hero_block, section_header, subsection_label, status_badge, visit_card
from widgets import set_current_page
from utils import _local_now


# SVG icons for KPI cards (inline, currentColor — color set via icon_color param)
_ICON_LOCATION = (
    '<svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" '
    'viewBox="0 0 24 24"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/>'
    '<circle cx="12" cy="10" r="3"/></svg>'
)
_ICON_EDIT = (
    '<svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" '
    'viewBox="0 0 24 24">'
    '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>'
    '<path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>'
    '</svg>'
)
_ICON_CHECK = (
    '<svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" '
    'viewBox="0 0 24 24"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>'
    '<polyline points="22 4 12 14.01 9 11.01"/></svg>'
)
_ICON_ALERT = (
    '<svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" '
    'viewBox="0 0 24 24">'
    '<circle cx="12" cy="12" r="10"/>'
    '<line x1="12" y1="8" x2="12" y2="12"/>'
    '<line x1="12" y1="16" x2="12.01" y2="16"/></svg>'
)
_ICON_STAR_POS = (
    '<svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" '
    'viewBox="0 0 24 24">'
    '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 '
    '12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>'
    '</svg>'
)
_ICON_STAR_NEG = (
    '<svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" '
    'viewBox="0 0 24 24">'
    '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 '
    '12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>'
    '</svg>'
)
_ICON_USERS = (
    '<svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" '
    'viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>'
    '<circle cx="9" cy="7" r="4"/>'
    '<path d="M23 21v-2a4 4 0 0 0-3-3.87"/>'
    '<path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>'
)
_ICON_BUILDING = (
    '<svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" '
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

    section_header(f"Welcome back, {first_name}", date_str)

    # ── Period filter ─────────────────────────────────────────────────────────
    period = st.radio(
        "",
        ["Today", "This week", "This month", "All time"],
        horizontal=True,
        key="dash_period",
        label_visibility="collapsed",
    )

    # ── KPI queries ───────────────────────────────────────────────────────────
    period_filter = {
        "Today": """
            AND v.submitted_at_local >= CURRENT_DATE
            AND v.submitted_at_local < CURRENT_DATE + interval '1 day'
        """,
        "This week": """
            AND v.submitted_at_local >= 
            date_trunc('week', NOW() + interval '1 day') - interval '1 day'
        """,
        "This month": """
            AND v.submitted_at_local >= date_trunc('month', NOW())
        """,
        "All time": ""
    }.get(period, "")

    # Period total
    period_total = _safe_count(
        f"SELECT COUNT(*) FROM visits v WHERE v.user_id = :uid AND COALESCE(v.is_deleted, FALSE) IS FALSE {period_filter}",
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
        f"WHERE v.user_id = :uid AND COALESCE(v.is_deleted, FALSE) IS FALSE {period_filter}",
        {"uid": uid},
    )

    # Evaluation breakdown (positive rate)
    eval_df = query_df(
        f"SELECT evaluation, COUNT(*) AS cnt FROM visits v "
        f"WHERE v.user_id = :uid AND COALESCE(v.is_deleted, FALSE) IS FALSE {period_filter} "
        f"GROUP BY evaluation",
        {"uid": uid},
    ) if period_total > 0 else None

    positive_rate = 0
    if eval_df is not None and not eval_df.empty:
        pos = int(eval_df.loc[eval_df["evaluation"] == "Positive", "cnt"].sum())
        positive_rate = round(pos / period_total * 100) if period_total > 0 else 0

    # ── Render KPI cards ──────────────────────────────────────────────────────
    st.markdown(
        kpi_hero_block(
            primary_label=f"Total visits ({period.lower()})",
            primary_value=f"{period_total:,}",
            primary_delta=f"Across {customers_visited:,} customer{'s' if customers_visited != 1 else ''}",
            primary_delta_positive=True,
            primary_icon_svg=_ICON_CHECK,
            primary_icon_color="var(--status-success-text)",
            stat1_label="Open change requests",
            stat1_value=f"{pending_cr:,}",
            stat1_delta="Awaiting review" if pending_cr > 0 else "None pending",
            stat1_delta_positive=pending_cr == 0,
            stat1_icon_svg=_ICON_EDIT,
            stat1_icon_color="var(--status-warning-text)",
            stat2_label="Positive evaluation rate",
            stat2_value=f"{positive_rate}%" if period_total > 0 else "—",
            stat2_delta=f"{period_total:,} visits evaluated" if period_total > 0 else "No visits this period",
            stat2_delta_positive=positive_rate >= 60,
            stat2_icon_svg=_ICON_STAR_POS if (period_total == 0 or positive_rate >= 60) else _ICON_STAR_NEG,
            stat2_icon_color="var(--color-text-subtle)" if period_total == 0 else ("var(--status-danger-text)" if positive_rate < 60 else "var(--status-success-text)"),
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

    section_header("Command Center", f"Field activity and pending reviews, {date_str}.")

    # ── Period filter ─────────────────────────────────────────────────────────
    period = st.radio(
        "",
        ["Today", "This week", "This month", "All time"],
        horizontal=True,
        key="dash_admin_period",
        label_visibility="collapsed",
    )

    period_filter = {
        "Today": """
            AND v.submitted_at_local >= CURRENT_DATE
            AND v.submitted_at_local < CURRENT_DATE + interval '1 day'
        """,
        "This week": """
            AND v.submitted_at_local >= 
            date_trunc('week', NOW() + interval '1 day') - interval '1 day'
        """,
        "This month": """
            AND v.submitted_at_local >= date_trunc('month', NOW())
        """,
        "All time": ""
    }.get(period, "")

    # ── Field Activity KPIs ───────────────────────────────────────────────────
    subsection_label("Field Activity")

    total_visits = _safe_count(
        f"SELECT COUNT(*) FROM visits v WHERE COALESCE(v.is_deleted, FALSE) IS FALSE {period_filter}"
    )
    unique_customers = _safe_count(
        f"SELECT COUNT(DISTINCT v.customer_id) FROM visits v WHERE COALESCE(v.is_deleted, FALSE) IS FALSE {period_filter}"
    )
    active_reps = _safe_count(
        f"SELECT COUNT(DISTINCT v.user_id) FROM visits v WHERE COALESCE(v.is_deleted, FALSE) IS FALSE {period_filter}"
    )

    st.markdown(
        kpi_hero_block(
            primary_label=f"Total visits ({period.lower()})",
            primary_value=f"{total_visits:,}",
            primary_delta=f"{active_reps} rep{'s' if active_reps != 1 else ''} active",
            primary_delta_positive=True,
            primary_icon_svg=_ICON_LOCATION,
            primary_icon_color="var(--color-primary)",
            stat1_label="Unique customers",
            stat1_value=f"{unique_customers:,}",
            stat1_delta=f"{total_visits:,} total visit{'s' if total_visits != 1 else ''}",
            stat1_delta_positive=True,
            stat1_icon_svg=_ICON_BUILDING,
            stat1_icon_color="var(--status-success-text)",
            stat2_label="Active reps",
            stat2_value=f"{active_reps:,}",
            stat2_delta=f"{unique_customers:,} unique customer{'s' if unique_customers != 1 else ''}",
            stat2_delta_positive=True,
            stat2_icon_svg=_ICON_USERS,
            stat2_icon_color="var(--color-primary)",
        ),
        unsafe_allow_html=True,
    )

    # ── Pending Reviews ───────────────────────────────────────────────────────
    _render_admin_pending_reviews()


def _render_admin_pending_reviews() -> None:
    """Render the pending reviews section: summary badges + unified action list."""

    subsection_label("Pending Reviews")

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
          AND COALESCE(is_deleted, FALSE) IS FALSE
        """
    )
    oc_count = _safe_count(
        "SELECT COUNT(*) FROM visits WHERE customer_id = 807 AND COALESCE(is_deleted, FALSE) IS FALSE"
    )

    total_pending = cr_count + ta_count + oc_count

    # ── Filter buttons (act as clickable badges) ──────────────────────────────
    active_filter = st.session_state.get("pr_filter", None)

    _FILTER_OPTS = [
        (None,               f"All ({total_pending})",          "neutral"),
        ("Change Request",   f"Change Requests ({cr_count})",   "warning"),
        ("Target Audience",  f"Target Audiences ({ta_count})",  "info"),
        ("Other Customer",   f"Other Customers ({oc_count})",   "primary"),
    ]

    st.markdown("""
<style>
[data-testid="stMarkdownContainer"]:has(> #pr-chip-anchor)
  ~ * [data-testid="stButton"] button,
#pr-chip-anchor ~ * [data-testid="stButton"] button {
    border-radius: 20px !important;
    font-size: 0.8rem !important;
    font-weight: 600 !important;
    min-height: 36px !important;
    height: auto !important;
    line-height: 1.4 !important;
    transition: background 150ms ease-out, color 150ms ease-out, border-color 150ms ease-out !important;
}
</style>
<div id="pr-chip-anchor" style="display:none;"></div>
""", unsafe_allow_html=True)

    fcols = st.columns(4)
    for col, (fval, flabel, fvariant) in zip(fcols, _FILTER_OPTS):
        is_active = active_filter == fval
        with col:
            if st.button(
                flabel,
                key=f"pr_fbtn_{fval}",
                type="primary" if is_active else "secondary",
                use_container_width=True,
            ):
                st.session_state["pr_filter"] = None if is_active else fval
                st.session_state["pr_page"] = 0
                st.rerun()

    if total_pending == 0:
        st.markdown(
            '<div style="padding:1.25rem 1rem;border:1px solid var(--color-border);'
            'border-radius:10px;background:var(--color-surface);">'
            '<p style="margin:0;font-size:0.875rem;color:var(--status-success-text);font-weight:500;">'
            'All clear. No pending reviews.</p>'
            '</div>',
            unsafe_allow_html=True,
        )
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
              AND COALESCE(v.is_deleted, FALSE) IS FALSE

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
              AND COALESCE(v.is_deleted, FALSE) IS FALSE

            ORDER BY submitted_at ASC
            """
        )
    except Exception as e:
        st.warning(f"Could not load pending items: {e}")
        return

    if items_df.empty:
        st.markdown(
            '<div style="padding:1rem;border:1px solid var(--color-border);'
            'border-radius:10px;background:var(--color-surface);">'
            '<p style="margin:0;font-size:0.875rem;color:var(--color-text-muted);">'
            'Pending items could not be loaded.</p>'
            '</div>',
            unsafe_allow_html=True,
        )
        return

    items_df["submitted_at"] = pd.to_datetime(items_df["submitted_at"], errors="coerce")

    _TYPE_VARIANT = {
        "Change Request":  "warning",
        "Target Audience": "info",
        "Other Customer":  "primary",
    }

    # ── Apply filter ──────────────────────────────────────────────────────────
    active_filter = st.session_state.get("pr_filter", None)
    filtered_df = items_df if active_filter is None else items_df[items_df["type"] == active_filter]

    # ── Pagination ────────────────────────────────────────────────────────────
    PAGE_SIZE = 10
    total_items = len(filtered_df)
    total_pages = max(1, (total_items + PAGE_SIZE - 1) // PAGE_SIZE)

    current_page = st.session_state.get("pr_page", 0)
    current_page = min(current_page, total_pages - 1)

    start_idx = current_page * PAGE_SIZE
    end_idx   = min(start_idx + PAGE_SIZE, total_items)
    page_df   = filtered_df.iloc[start_idx:end_idx]

    if total_items > 0:
        st.markdown(
            f'<p style="font-size:0.8rem;color:var(--color-text-subtle);margin:6px 0 8px;">'
            f'Showing {start_idx + 1}–{end_idx} of {total_items:,} pending items</p>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div style="padding:1rem;border:1px solid var(--color-border);'
            'border-radius:10px;background:var(--color-surface);">'
            '<p style="margin:0;font-size:0.875rem;color:var(--color-text-muted);">'
            'No items match the selected filter.</p>'
            '</div>',
            unsafe_allow_html=True,
        )
        return

    _nav_sid = st.session_state.get("_stored_sid", "")
    cards_html = ""
    for _, row in page_df.iterrows():
        variant  = _TYPE_VARIANT.get(str(row["type"]), "neutral")
        target   = str(row["target_page"])
        item_id  = int(row["item_id"])
        href     = (
            f"?page={target.replace(' ', '+')}&_sid={_nav_sid}&preselect={item_id}"
            if _nav_sid else f"?page={target.replace(' ', '+')}&preselect={item_id}"
        )
        cards_html += visit_card(
            visit_id=html.escape(str(row["identifier"])),
            date_obj=row["submitted_at"],
            customer=html.escape(str(row["rep_name"])),
            subtitle=html.escape(str(row["type"])),
            status=html.escape(str(row["type"])),
            status_variant=variant,
            href=href,
        )
    if cards_html:
        st.markdown(cards_html, unsafe_allow_html=True)

    # ── Pagination controls ───────────────────────────────────────────────────
    if total_pages > 1:
        col_prev, col_info, col_next = st.columns([1, 4, 1])
        with col_prev:
            if st.button("←", key="pr_prev", disabled=(current_page == 0),
                         use_container_width=True):
                st.session_state["pr_page"] = current_page - 1
                st.rerun()
        with col_info:
            st.markdown(
                f'<p style="text-align:center;font-size:0.8rem;color:var(--color-text-subtle);'
                f'padding-top:0.45rem;margin:0;">{current_page + 1} of {total_pages}</p>',
                unsafe_allow_html=True,
            )
        with col_next:
            if st.button("→", key="pr_next", disabled=(current_page >= total_pages - 1),
                         use_container_width=True):
                st.session_state["pr_page"] = current_page + 1
                st.rerun()
