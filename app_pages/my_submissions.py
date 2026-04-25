# pages/my_submissions.py  — "My Visits" card-list + detail view
import html
import pandas as pd
import streamlit as st

from auth import resolve_session_user, get_url_param, set_url_param
from config import TIMEZONE
from db_ops import query_df
from ui import section_header, visit_card, status_badge
from widgets import set_current_page

_EVAL_VARIANT = {
    "Positive": "success",
    "Negative": "danger",
    "Neutral":  "warning",
}

_EVAL_LABEL = {
    "Positive": "Positive",
    "Negative": "Negative",
    "Neutral":  "Neutral",
}

# ── Shared card shell ─────────────────────────────────────────────────────────
_CARD_WRAP  = (
    '<div style="background:#fff;border:1px solid #e4e8ec;border-radius:12px;'
    'padding:1rem 1.25rem;margin-bottom:0.75rem;">'
)
_CARD_CLOSE = '</div>'

_SECTION_TITLE = (
    '<div style="font-size:0.75rem;font-weight:600;color:#8b949e;'
    'text-transform:uppercase;letter-spacing:0.06em;margin-bottom:0.625rem;">'
    '{label}</div>'
)

_ROW = (
    '<div style="display:flex;justify-content:space-between;align-items:baseline;'
    'padding:0.3rem 0;border-bottom:1px solid #f3f4f6;">'
    '<span style="font-size:0.85rem;color:#57606a;min-width:120px;">{key}</span>'
    '<span style="font-size:0.875rem;color:#0d1117;font-weight:500;text-align:right;">{val}</span>'
    '</div>'
)


def _detail_card(title: str, rows: list[tuple]) -> str:
    """Build one info card with labelled rows. rows = [(key, value), ...]"""
    html = _CARD_WRAP + _SECTION_TITLE.format(label=title)
    for key, val in rows:
        html += _ROW.format(key=key, val=val or "—")
    html += _CARD_CLOSE
    return html


# ── Detail view ───────────────────────────────────────────────────────────────

def _show_visit_detail(visit_id_str: str, uid: int) -> None:
    try:
        visit_id = int(visit_id_str)
    except (ValueError, TypeError):
        st.error("Invalid visit ID.")
        return

    # ── Back button ───────────────────────────────────────────────────────────
    if st.button("← Back to My Visits", key="vd_back"):
        set_url_param("visit_id", None)
        st.rerun()

    # ── Load visit ────────────────────────────────────────────────────────────
    sql = """
        SELECT
            v.visit_id,
            v.submitted_at_local,
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
            ), 0) AS shelf_lines_count
        FROM visits v
        JOIN customers c              ON v.customer_id = c.customer_id
        LEFT JOIN target_audiences ta ON v.audience_id = ta.audience_id
        LEFT JOIN items i             ON v.product_id = i.product_id
        LEFT JOIN business_lines bl   ON bl.business_line_id = v.business_line_id
        LEFT JOIN business_units bu   ON bu.business_unit_id = bl.business_unit_id
        JOIN objectives o             ON v.objective_id = o.objective_id
        LEFT JOIN home_visits hv      ON hv.visit_id = v.visit_id
        WHERE v.visit_id = :vid AND v.user_id = :uid
    """
    df = query_df(sql, {"vid": visit_id, "uid": uid})

    if df.empty:
        st.error("Visit not found or you don't have permission to view it.")
        return

    row = df.iloc[0]

    # ── Header ────────────────────────────────────────────────────────────────
    customer_display = row.get("other_customer_name") or row.get("customer") or "—"
    section_header(f"V-{visit_id}", customer_display)

    # ── Evaluation badge ──────────────────────────────────────────────────────
    eval_val = (row.get("evaluation") or "").strip()
    variant  = _EVAL_VARIANT.get(eval_val, "neutral")
    label    = _EVAL_LABEL.get(eval_val, "Unrated")
    st.markdown(status_badge(label, variant), unsafe_allow_html=True)

    st.markdown("<div style='margin-top:1rem;'></div>", unsafe_allow_html=True)

    # ── Visit info card ───────────────────────────────────────────────────────
    try:
        dt = pd.to_datetime(row.get("submitted_at_local"), errors="coerce")
        date_str = dt.strftime("%d %b %Y, %H:%M") if dt and not pd.isnull(dt) else "—"
    except Exception:
        date_str = "—"

    visit_rows = [
        ("Date / Time", date_str),
        ("Objective",   row.get("objective")),
        ("Evaluation",  eval_val or "Unrated"),
    ]
    st.markdown(_detail_card("Visit Info", visit_rows), unsafe_allow_html=True)

    # ── Customer card ─────────────────────────────────────────────────────────
    customer_rows = [
        ("Name",       row.get("customer")),
        ("Account ID", row.get("account_id")),
        ("Region",     row.get("customer_region")),
        ("City",       row.get("customer_city")),
        ("Sector",     row.get("customer_sector")),
    ]
    if row.get("other_customer_name"):
        customer_rows.append(("New Customer", row.get("other_customer_name")))
    st.markdown(_detail_card("Customer", customer_rows), unsafe_allow_html=True)

    # ── Audience card ─────────────────────────────────────────────────────────
    audience_rows = [
        ("Name",       row.get("audience")),
        ("Department", row.get("audience_department")),
        ("Position",   row.get("audience_position")),
    ]
    if any(v for _, v in audience_rows):
        st.markdown(_detail_card("Audience", audience_rows), unsafe_allow_html=True)

    # ── Product & Business card ───────────────────────────────────────────────
    product_rows = [
        ("Business Unit",        row.get("business_unit")),
        ("Business Line",        row.get("business_line")),
        ("Product ID",           row.get("product_id")),
        ("Product Description",  row.get("product")),
    ]
    if any(v for _, v in product_rows):
        st.markdown(_detail_card("Product & Business", product_rows), unsafe_allow_html=True)

    # ── Notes card ────────────────────────────────────────────────────────────
    notes = (row.get("notes") or "").strip()
    if notes:
        notes_html = (
            _CARD_WRAP
            + _SECTION_TITLE.format(label="Notes")
            + f'<p style="font-size:0.9rem;color:#0d1117;line-height:1.6;margin:0;">{html.escape(notes)}</p>'
            + _CARD_CLOSE
        )
        st.markdown(notes_html, unsafe_allow_html=True)

    # ── Home visit card ───────────────────────────────────────────────────────
    if row.get("patient_name"):
        hv_rows = [
            ("Patient Name",  row.get("patient_name")),
            ("Phone",         row.get("patient_phone")),
            ("Serial No.",    row.get("serial_no")),
        ]
        st.markdown(_detail_card("Home Visit", hv_rows), unsafe_allow_html=True)

    # ── Shelf movement card ───────────────────────────────────────────────────
    if int(row.get("shelf_lines_count") or 0) > 0:
        shelf_sql = """
            SELECT
                COALESCE(i.description, '—') AS product,
                l.qty_checked,
                l.notes AS line_notes
            FROM shelf_movement_lines l
            JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
            LEFT JOIN items i ON i.product_id = l.product_id
            WHERE h.visit_id = :vid
            ORDER BY l.line_id
        """
        shelf_df = query_df(shelf_sql, {"vid": visit_id})
        if not shelf_df.empty:
            shelf_html = (
                _CARD_WRAP
                + _SECTION_TITLE.format(label="Shelf Movement")
                + '<div style="overflow-x:auto;">'
                + '<table style="width:100%;border-collapse:collapse;font-size:0.875rem;">'
                + '<thead><tr>'
                + '<th style="text-align:left;padding:0.35rem 0.5rem;color:#57606a;'
                  'font-weight:600;border-bottom:1px solid #e4e8ec;">Product</th>'
                + '<th style="text-align:right;padding:0.35rem 0.5rem;color:#57606a;'
                  'font-weight:600;border-bottom:1px solid #e4e8ec;">Qty</th>'
                + '<th style="text-align:left;padding:0.35rem 0.5rem;color:#57606a;'
                  'font-weight:600;border-bottom:1px solid #e4e8ec;">Notes</th>'
                + '</tr></thead><tbody>'
            )
            for _, srow in shelf_df.iterrows():
                shelf_html += (
                    '<tr>'
                    f'<td style="padding:0.35rem 0.5rem;color:#0d1117;border-bottom:1px solid #f3f4f6;">'
                    f'{html.escape(str(srow["product"]))}</td>'
                    f'<td style="padding:0.35rem 0.5rem;text-align:right;color:#0d1117;'
                    f'font-weight:600;border-bottom:1px solid #f3f4f6;">{srow["qty_checked"]}</td>'
                    f'<td style="padding:0.35rem 0.5rem;color:#57606a;border-bottom:1px solid #f3f4f6;">'
                    f'{html.escape(str(srow["line_notes"] or ""))}</td>'
                    '</tr>'
                )
            shelf_html += '</tbody></table></div>' + _CARD_CLOSE
            st.markdown(shelf_html, unsafe_allow_html=True)

    # ── Location card ─────────────────────────────────────────────────────────
    lat = row.get("latitude")
    lon = row.get("longitude")
    acc = row.get("accuracy_m")
    if lat and lon:
        maps_url = f"https://www.google.com/maps/search/{lat},{lon}"
        loc_rows = [
            ("Coordinates", f"{lat:.6f}, {lon:.6f}"),
            ("Accuracy",    f"{acc:.0f} m" if acc else "—"),
        ]
        loc_html = (
            _CARD_WRAP
            + _SECTION_TITLE.format(label="Location")
            + "".join(_ROW.format(key=k, val=v) for k, v in loc_rows)
            + f'<div style="margin-top:0.625rem;">'
            f'<a href="{maps_url}" target="_blank" '
            f'style="font-size:0.85rem;color:#2667ff;font-weight:500;text-decoration:none;">'
            f'Open in Google Maps →</a></div>'
            + _CARD_CLOSE
        )
        st.markdown(loc_html, unsafe_allow_html=True)


# ── List view ─────────────────────────────────────────────────────────────────

def page_my_submissions():
    set_current_page("my_submissions")

    u = st.session_state.get("user") or resolve_session_user()
    if not u:
        st.warning("Please sign in.")
        return

    uid = int(u.get("user_id") or u.get("id"))

    # ── Route to detail view if visit_id is in URL ────────────────────────────
    visit_id_param = st.query_params.get("visit_id")
    if visit_id_param:
        _show_visit_detail(visit_id_param, uid)
        return

    first_name = (u.get("name") or "").split()[0] if u.get("name") else "there"

    section_header("My Visits", f"Everything you've logged, {first_name}.")

    # ── Main query ────────────────────────────────────────────────────────────
    sql = """
        SELECT
            v.visit_id,
            v.submitted_at_local,
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
            ), 0) AS shelf_total_qty
        FROM visits v
        JOIN customers c              ON v.customer_id = c.customer_id
        LEFT JOIN target_audiences ta ON v.audience_id = ta.audience_id
        LEFT JOIN items i             ON v.product_id = i.product_id
        LEFT JOIN business_lines bl   ON bl.business_line_id = v.business_line_id
        LEFT JOIN business_units bu   ON bu.business_unit_id = bl.business_unit_id
        JOIN objectives o             ON v.objective_id = o.objective_id
        LEFT JOIN home_visits hv      ON hv.visit_id = v.visit_id
        WHERE v.user_id = :uid
        ORDER BY v.visit_id DESC
    """
    df = query_df(sql, {"uid": uid})

    # ── Search + New Visit row ────────────────────────────────────────────────
    col_search, col_btn = st.columns([3, 1])
    with col_search:
        search_q = st.text_input(
            "", placeholder="Search your visits…",
            key="mv_search", label_visibility="collapsed"
        )
    with col_btn:
        st.button("+ New Visit", key="mv_new_visit", type="primary",
                  use_container_width=True)

    if df.empty:
        st.info("No visits submitted yet.")
        return

    # ── Normalise evaluation ──────────────────────────────────────────────────
    df["evaluation"] = df["evaluation"].fillna("").str.strip()

    # ── Date filter ───────────────────────────────────────────────────────────
    df["_date"] = pd.to_datetime(df["submitted_at_local"], errors="coerce").dt.date

    col_from, col_to = st.columns(2)
    with col_from:
        date_from = st.date_input("From", value=None, key="mv_date_from",
                                  label_visibility="visible")
    with col_to:
        date_to = st.date_input("To", value=None, key="mv_date_to",
                                label_visibility="visible")

    if date_from:
        df = df[df["_date"] >= date_from]
    if date_to:
        df = df[df["_date"] <= date_to]

    # ── Count by status for filter tabs ──────────────────────────────────────
    cnt_total   = len(df)
    cnt_pos     = (df["evaluation"] == "Positive").sum()
    cnt_neg     = (df["evaluation"] == "Negative").sum()
    cnt_neutral = (df["evaluation"] == "Neutral").sum()
    cnt_unrated = ((df["evaluation"] == "") | df["evaluation"].isna()).sum()

    filter_labels = [
        f"All  {cnt_total}",
        f"Positive  {cnt_pos}",
        f"Negative  {cnt_neg}",
        f"Neutral  {cnt_neutral}",
        f"Unrated  {cnt_unrated}",
    ]

    active_filter = st.radio(
        "",
        filter_labels,
        horizontal=True,
        key="mv_filter",
        label_visibility="collapsed",
    )

    # ── Apply filter ──────────────────────────────────────────────────────────
    chosen = active_filter.split("  ")[0].strip() if "  " in active_filter else active_filter.strip()
    if chosen == "All":
        visible = df
    elif chosen == "Unrated":
        visible = df[df["evaluation"].isin(["", None]) | df["evaluation"].isna()]
    else:
        visible = df[df["evaluation"] == chosen]

    # ── Apply search ──────────────────────────────────────────────────────────
    if search_q:
        sq = search_q.strip().lower()
        mask = (
            visible["customer"].str.lower().str.contains(sq, na=False)
            | visible["visit_id"].astype(str).str.contains(sq, na=False)
            | visible["product"].fillna("").str.lower().str.contains(sq, na=False)
            | visible["business_unit"].fillna("").str.lower().str.contains(sq, na=False)
        )
        visible = visible[mask]

    # ── Pagination ────────────────────────────────────────────────────────────
    PAGE_SIZE = 10
    total_visible = len(visible)

    # Reset to page 1 when filter or search changes
    filter_key = (active_filter, search_q, date_from, date_to)
    if st.session_state.get("mv_filter_key") != filter_key:
        st.session_state["mv_filter_key"] = filter_key
        st.session_state["mv_page"] = 0

    current_page = st.session_state.get("mv_page", 0)
    total_pages  = max(1, (total_visible + PAGE_SIZE - 1) // PAGE_SIZE)
    current_page = min(current_page, total_pages - 1)

    start_idx = current_page * PAGE_SIZE
    end_idx   = min(start_idx + PAGE_SIZE, total_visible)
    page_df   = visible.iloc[start_idx:end_idx]

    st.markdown(
        f'<p style="font-size:0.8rem;color:#8b949e;margin:6px 0 8px;">'
        f'Showing {start_idx + 1}–{end_idx} of {total_visible:,} visits</p>',
        unsafe_allow_html=True,
    )

    # ── Build card HTML ───────────────────────────────────────────────────────
    cards_html = ""
    for _, row in page_df.iterrows():
        eval_val = row.get("evaluation") or ""
        variant  = _EVAL_VARIANT.get(eval_val, "neutral")
        label    = _EVAL_LABEL.get(eval_val, "Unrated")

        audience_name = row.get("audience") or ""
        audience_dept = row.get("audience_department") or ""
        audience_pos  = row.get("audience_position") or ""
        subtitle_parts = [p for p in [audience_name, audience_dept, audience_pos] if p]
        subtitle = " · ".join(subtitle_parts)

        raw_id = int(row["visit_id"])
        vid    = f"V-{raw_id}"
        href   = f"?page=My+Visits&visit_id={raw_id}"

        cards_html += visit_card(
            visit_id=vid,
            date_obj=row.get("submitted_at_local"),
            customer=row.get("customer") or "—",
            subtitle=subtitle,
            status=label,
            status_variant=variant,
            href=href,
        )

    if cards_html:
        st.markdown(cards_html, unsafe_allow_html=True)
    else:
        st.info("No visits match your search or filter.")

    # ── Pagination controls ───────────────────────────────────────────────────
    if total_pages > 1:
        col_prev, col_info, col_next = st.columns([1, 2, 1])
        with col_prev:
            if st.button("← Prev", key="mv_prev", disabled=(current_page == 0),
                         use_container_width=True):
                st.session_state["mv_page"] = current_page - 1
                st.rerun()
        with col_info:
            st.markdown(
                f'<p style="text-align:center;font-size:0.85rem;color:#57606a;'
                f'padding-top:0.4rem;">Page {current_page + 1} of {total_pages}</p>',
                unsafe_allow_html=True,
            )
        with col_next:
            if st.button("Next →", key="mv_next", disabled=(current_page >= total_pages - 1),
                         use_container_width=True):
                st.session_state["mv_page"] = current_page + 1
                st.rerun()

    # ── Download CSV (collapsed) ──────────────────────────────────────────────
    with st.expander("Export data"):
        df_export = df.copy()
        if "submitted_at_local" in df_export.columns:
            df_export["submitted_at_local"] = (
                pd.to_datetime(df_export["submitted_at_local"], errors="coerce")
                .dt.strftime("%d/%m/%Y %H:%M")
            )
        df_export["location_url"] = df_export.apply(
            lambda r: (
                f"https://www.google.com/maps/search/{r['latitude']},{r['longitude']}"
                if r["latitude"] and r["longitude"] else ""
            ),
            axis=1,
        )
        drop_cols = ["latitude", "longitude", "accuracy_m"]
        df_export = df_export.drop(columns=[c for c in drop_cols if c in df_export.columns])
        st.download_button(
            "Download CSV",
            df_export.to_csv(index=False).encode("utf-8-sig"),
            "my_visits.csv",
            "text/csv",
        )
