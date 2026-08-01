# app_pages/notifications.py
"""Notifications page: view unread/read notifications and act on them.

Clicking a notification row marks it read and redirects to whatever
page/record it points at (link_page/link_params), mirroring the
st.query_params-driven detail-view routing used elsewhere in this app
(see app_pages/quotation_review.py).
"""
import json

import pandas as pd
import streamlit as st

from ui import section_header, status_badge
from db_ops import query_df
from app_pages.notification_helpers import list_notifications, mark_read, mark_all_read

PAGE_NS = "notifications"


def _load_notification(notification_id: int, uid: int) -> dict | None:
    """Direct lookup by id — used instead of list_notifications() so a click
    always resolves the target row even if it has fallen outside the list's
    default limit."""
    df = query_df(
        """
            SELECT notification_id, link_page, link_params
            FROM notifications
            WHERE notification_id = :nid AND recipient_user_id = :uid
        """,
        {"nid": notification_id, "uid": uid},
    )
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def _parse_link_params(raw) -> dict:
    """link_params comes back from the JSONB column either already as a dict
    or as a JSON string, depending on driver/deserialization behavior — handle
    both (same defensive pattern as app_pages/app_settings.py::_load_prefs)."""
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {}
    return raw if isinstance(raw, dict) else {}


def _redirect_to(link_page: str | None, link_params: dict) -> None:
    """Replace the current query params with the notification's target page
    + params, then rerun. This is an in-session soft redirect (no full page
    reload), so no _sid is needed here."""
    for k in list(st.query_params.keys()):
        st.query_params.pop(k, None)
    st.query_params["page"] = link_page or "Notifications"
    for k, v in link_params.items():
        if v is not None:
            st.query_params[k] = str(v)
    st.rerun()


def page_notifications():
    u = st.session_state.get("user")
    uid = int(u["user_id"] if "user_id" in u else u["id"]) if u else None
    if uid is None:
        st.error("You must be logged in to view notifications.")
        return

    # ── Click-through from a notification row (?notif_id=...) ────────────────
    notif_id_param = st.query_params.get("notif_id")
    if notif_id_param:
        try:
            nid = int(notif_id_param)
        except (ValueError, TypeError):
            st.query_params.pop("notif_id", None)
            st.error("Invalid notification.")
            return

        row = _load_notification(nid, uid)
        mark_read(nid, uid)
        if row is None:
            st.query_params.pop("notif_id", None)
            st.warning("Notification not found.")
            st.rerun()
            return

        link_params = _parse_link_params(row.get("link_params"))
        _redirect_to(row.get("link_page"), link_params)
        return

    # ── Notification list ─────────────────────────────────────────────────────
    section_header("Notifications", "Updates on your quotations and change requests")

    _, btn_col = st.columns([5, 2])
    with btn_col:
        if st.button("Mark all as read", key=f"{PAGE_NS}_mark_all", use_container_width=True):
            mark_all_read(uid)
            st.rerun()

    df = list_notifications(uid)
    if df.empty:
        st.info("You have no notifications yet.")
        return

    _nav_sid = st.session_state.get("_stored_sid", "")

    for _, row in df.iterrows():
        nid = int(row["notification_id"])
        is_read = bool(row.get("is_read"))
        title = row.get("title") or "Notification"
        body = row.get("body") or ""
        created = row.get("created_at")
        try:
            created_str = pd.to_datetime(created).strftime("%d %b %Y, %H:%M") if created is not None else ""
        except Exception:
            created_str = str(created) if created is not None else ""

        href = (
            f"?page=Notifications&notif_id={nid}&_sid={_nav_sid}"
            if _nav_sid else f"?page=Notifications&notif_id={nid}"
        )

        accent = "var(--color-primary)" if not is_read else "var(--color-border)"
        bg = "var(--color-primary-subtle)" if not is_read else "var(--color-surface)"
        weight = "700" if not is_read else "500"
        unread_pill = status_badge("New", "primary") if not is_read else ""
        body_html = (
            f'<div style="font-size:0.825rem;color:var(--color-text-muted);margin-top:2px;">{body}</div>'
            if body else ""
        )

        st.markdown(
            f'<a href="{href}" target="_self" style="text-decoration:none;display:block;margin-bottom:0.6rem;">'
            f'<div style="border:1px solid var(--color-border);border-left:4px solid {accent};'
            f'border-radius:10px;padding:0.75rem 1rem;background:{bg};transition:background 0.15s ease;">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">'
            f'<span style="font-size:0.9375rem;font-weight:{weight};color:var(--color-text);">{title}</span>'
            f'{unread_pill}'
            f'</div>'
            f'{body_html}'
            f'<div style="font-size:0.75rem;color:var(--color-text-subtle);margin-top:6px;">{created_str}</div>'
            f'</div>'
            f'</a>',
            unsafe_allow_html=True,
        )
