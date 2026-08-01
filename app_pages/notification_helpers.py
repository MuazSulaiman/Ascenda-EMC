# app_pages/notification_helpers.py
"""Central helper module for the in-app notification system.

Plain functions, SQLAlchemy `text()` queries, no classes — matches the
style established in app_pages/quotation_helpers.py. `notify_users` /
`notify_role` accept an already-open transactional `conn` (the caller's
`engine.begin()` block) and never commit/rollback themselves; the other
functions are standalone reads/writes that open their own connection via
the app's existing db_ops helpers (query_df / query_scalar / exec_sql),
same as elsewhere in this codebase.
"""
import json

import pandas as pd
from sqlalchemy import text

from db_ops import exec_sql, query_df, query_scalar


def notify_users(conn, user_ids: list[int], *, category: str, event_type: str,
                  title: str, body: str | None = None,
                  link_page: str | None = None,
                  link_params: dict | None = None,
                  actor_user_id: int | None = None) -> None:
    """Bulk-insert one notification row per user_id. No-ops on an empty list.
    `conn` is an already-open SQLAlchemy Connection/transaction (the caller's
    `engine.begin()` block) — this function must NOT open its own connection
    or call commit/rollback itself. link_params is serialized to JSON for the
    JSONB column."""
    if not user_ids:
        return

    params_json = json.dumps(link_params, ensure_ascii=False) if link_params is not None else None

    for recipient_id in user_ids:
        conn.execute(
            text("""
                INSERT INTO notifications
                    (recipient_user_id, actor_user_id, category, event_type,
                     title, body, link_page, link_params)
                VALUES
                    (:recipient_id, :actor_id, :category, :event_type,
                     :title, :body, :link_page, CAST(:link_params AS jsonb))
            """),
            {
                "recipient_id": recipient_id,
                "actor_id": actor_user_id,
                "category": category,
                "event_type": event_type,
                "title": title,
                "body": body,
                "link_page": link_page,
                "link_params": params_json,
            },
        )


def notify_role(conn, roles: list[str], *, exclude_user_id: int | None = None,
                 category: str, event_type: str, title: str,
                 body: str | None = None, link_page: str | None = None,
                 link_params: dict | None = None,
                 actor_user_id: int | None = None) -> None:
    """Resolve all active users (is_active = TRUE) whose role is in `roles`,
    excluding exclude_user_id if given, then call notify_users with that
    list. Uses the same `conn`."""
    if not roles:
        return

    result = conn.execute(
        text("""
            SELECT user_id FROM users
            WHERE role = ANY(:roles)
              AND is_active = TRUE
              AND (CAST(:exclude_id AS INTEGER) IS NULL OR user_id != CAST(:exclude_id AS INTEGER))
        """),
        {"roles": list(roles), "exclude_id": exclude_user_id},
    )
    user_ids = [row[0] for row in result]

    notify_users(
        conn,
        user_ids,
        category=category,
        event_type=event_type,
        title=title,
        body=body,
        link_page=link_page,
        link_params=link_params,
        actor_user_id=actor_user_id,
    )


def get_unread_count(uid: int) -> int:
    """Standalone read — opens its own connection via the app's existing
    db helper (mirror how other read-only helpers in quotation_helpers.py
    query, e.g. via `query_df`/engine access already used in that file).
    Returns 0 if uid is None or on no rows."""
    if uid is None:
        return 0
    count = query_scalar(
        "SELECT COUNT(*) FROM notifications WHERE recipient_user_id = :uid AND is_read = FALSE",
        {"uid": uid},
    )
    return int(count or 0)


def list_notifications(uid: int, *, unread_only: bool = False, limit: int = 50) -> pd.DataFrame:
    """Returns a DataFrame of the user's notifications, newest first
    (ORDER BY created_at DESC), each row carrying notification_id, category,
    event_type, title, body, link_page, link_params, is_read, created_at."""
    columns = [
        "notification_id", "category", "event_type", "title", "body",
        "link_page", "link_params", "is_read", "created_at",
    ]
    if uid is None:
        return pd.DataFrame(columns=columns)

    unread_clause = "AND is_read = FALSE" if unread_only else ""
    return query_df(
        f"""
            SELECT notification_id, category, event_type, title, body,
                   link_page, link_params, is_read, created_at
            FROM notifications
            WHERE recipient_user_id = :uid {unread_clause}
            ORDER BY created_at DESC
            LIMIT :limit
        """,
        {"uid": uid, "limit": limit},
    )


def mark_read(notification_id: int, uid: int) -> None:
    """UPDATE notifications SET is_read = TRUE, read_at = NOW()
    WHERE notification_id = :nid AND recipient_user_id = :uid — the
    recipient check is load-bearing: a user must never be able to mark
    another user's notification read by guessing an id."""
    exec_sql(
        """
            UPDATE notifications
            SET is_read = TRUE, read_at = NOW()
            WHERE notification_id = :nid AND recipient_user_id = :uid
        """,
        {"nid": notification_id, "uid": uid},
    )


def mark_all_read(uid: int) -> None:
    """UPDATE notifications SET is_read = TRUE, read_at = NOW()
    WHERE recipient_user_id = :uid AND is_read = FALSE."""
    exec_sql(
        """
            UPDATE notifications
            SET is_read = TRUE, read_at = NOW()
            WHERE recipient_user_id = :uid AND is_read = FALSE
        """,
        {"uid": uid},
    )
