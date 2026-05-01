# auth.py
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import streamlit as st
from sqlalchemy import text, bindparam
from sqlalchemy.dialects.postgresql import JSONB

from db import engine
from config import SESSION_TTL_MIN


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _user_agent() -> Optional[str]:
    return st.session_state.get("user_agent")


def _client_ip() -> Optional[str]:
    ip = st.session_state.get("client_ip")
    return ip if ip and ip != "unknown" else None


def _log_event(conn, sid: str, evt: str, details=None):
    if details is None:
        details = {}
    stmt = text("""
        INSERT INTO app_session_events(session_id, event_type, ip, user_agent, details)
        VALUES (:sid, :evt, :ip, :ua, :details)
    """).bindparams(bindparam("details", type_=JSONB))
    conn.execute(stmt, {
        "sid": sid,
        "evt": evt,
        "ip": _client_ip(),
        "ua": _user_agent(),
        "details": details,
    })


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT * FROM users WHERE email = :email"),
            {"email": email},
        ).mappings().first()
        return dict(row) if row else None


def check_login_lockout(email: str) -> tuple[bool, int]:
    """Return (is_locked, seconds_remaining). Reads login_attempts table."""
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT locked_until FROM login_attempts WHERE username = :e"),
            {"e": email},
        ).mappings().first()
    if not row or not row["locked_until"]:
        return False, 0
    remaining = (row["locked_until"] - _utcnow()).total_seconds()
    return remaining > 0, max(0, int(remaining))


def record_failed_login(email: str) -> None:
    """Increment failed attempt count; lock account for 15 min after 5 failures."""
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO login_attempts (username, attempt_count, last_attempt_at)
                VALUES (:e, 1, NOW())
                ON CONFLICT (username) DO UPDATE SET
                    attempt_count   = login_attempts.attempt_count + 1,
                    last_attempt_at = NOW(),
                    locked_until    = CASE
                        WHEN login_attempts.attempt_count + 1 >= 5
                        THEN NOW() + INTERVAL '15 minutes'
                        ELSE login_attempts.locked_until
                    END
            """),
            {"e": email},
        )


def reset_login_attempts(email: str) -> None:
    """Clear the attempt record on successful login."""
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM login_attempts WHERE username = :e"),
            {"e": email},
        )


def get_user_by_id(uid: int) -> Optional[Dict[str, Any]]:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT * FROM users WHERE user_id = :uid"),
            {"uid": uid},
        ).mappings().first()
        return dict(row) if row else None


def create_session(user_id: int, role: str | None = None) -> str:
    """
    Create a new app session for the given user.
    Normal users: TTL = SESSION_TTL_MIN. Admins: TTL = 720 minutes (12 hours).
    """
    sid = uuid.uuid4().hex
    now = _utcnow()
    ttl_minutes = 720 if role and str(role).lower().strip() == "admin" else SESSION_TTL_MIN
    exp = now + timedelta(minutes=ttl_minutes)
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO app_sessions(
                    session_id, user_id, created_at_utc, expires_at_utc,
                    last_seen_utc, ip, user_agent
                )
                VALUES (:sid, :uid, :created, :expires, :last_seen, :ip, :ua)
            """),
            {
                "sid": sid, "uid": user_id, "created": now, "expires": exp,
                "last_seen": now, "ip": _client_ip(), "ua": _user_agent(),
            },
        )
        _log_event(conn, sid, "created", {"ttl_min": ttl_minutes})
    return sid


def purge_expired_sessions() -> None:
    """Mark expired sessions as closed, idempotently."""
    with engine.begin() as conn:
        res = conn.execute(
            text("""
                UPDATE app_sessions
                SET revoked_at_utc = NOW(), closed_reason = 'expired'
                WHERE expires_at_utc < NOW() AND revoked_at_utc IS NULL
                RETURNING session_id
            """)
        )
        for (sid,) in res.fetchall():
            _log_event(conn, sid, "expired", {"batch": True})


def revoke_session(sid: str, reason: str = "manual_revoke") -> None:
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE app_sessions
                SET revoked_at_utc = :now, closed_reason = :reason
                WHERE session_id = :sid AND revoked_at_utc IS NULL
            """),
            {"sid": sid, "now": _utcnow(), "reason": reason},
        )
        _log_event(conn, sid, "revoked", {"reason": reason})


def delete_session(sid: str) -> None:
    revoke_session(sid, reason="manual_revoke")


_sessions_table_ensured = False


def _ensure_sessions_table_exists():
    global _sessions_table_ensured
    if _sessions_table_ensured:
        return
    # DDL must stay in sync with init_db_v11.py, which is the authoritative schema.
    ddl = """
    CREATE TABLE IF NOT EXISTS app_sessions (
      session_id TEXT PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
      created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      expires_at_utc TIMESTAMPTZ NOT NULL,
      revoked_at_utc TIMESTAMPTZ,
      closed_reason TEXT,
      last_seen_utc TIMESTAMPTZ,
      ip INET,
      user_agent TEXT
    )
    """
    with engine.begin() as conn:
        conn.execute(text(ddl))
    _sessions_table_ensured = True


def set_url_param(name: str, value: str | None):
    if value is None:
        st.query_params.pop(name, None)
    else:
        st.query_params[name] = value


def get_url_param(name: str, default: str | None = None) -> str | None:
    return st.query_params.get(name, default)


def set_url_session_param(sid: Optional[str]):
    # SID is stored in browser sessionStorage, not the URL.
    # Always ensure URL is clean.
    st.query_params.pop("sid", None)
    if sid:
        st.session_state["_stored_sid"] = sid
    else:
        st.session_state.pop("_stored_sid", None)


def resolve_session_user():
    # Prefer URL param (backward-compat / first load after old bookmark),
    # then fall back to the value read from browser sessionStorage.
    sid = st.query_params.get("sid") or st.query_params.get("_sid")
    if not sid:
        sid = st.session_state.get("_stored_sid")

    # Strip SID from URL immediately so it is never visible in the address bar.
    st.query_params.pop("sid", None)
    st.query_params.pop("_sid", None)

    if not sid:
        return None

    now = _utcnow()
    with engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT s.session_id, s.user_id, s.expires_at_utc, s.revoked_at_utc,
                       u.name, u.region, u.role, u.email
                FROM app_sessions s
                JOIN users u ON u.user_id = s.user_id
                WHERE s.session_id = :sid
            """),
            {"sid": sid},
        ).mappings().first()

        if not row:
            _log_event(conn, sid, "failed_validation", {"reason": "not_found"})
            st.query_params.pop("sid", None)
            return None

        if row["revoked_at_utc"] is not None or row["expires_at_utc"] <= now:
            conn.execute(
                text("""
                    UPDATE app_sessions
                    SET revoked_at_utc = GREATEST(COALESCE(revoked_at_utc, NOW()), NOW()),
                        closed_reason = CASE
                            WHEN expires_at_utc <= :now THEN 'expired'
                            ELSE 'revoked'
                        END
                    WHERE session_id = :sid
                """),
                {"sid": sid, "now": now},
            )
            _log_event(conn, sid, "expired" if row["expires_at_utc"] <= now else "revoked")
            st.query_params.pop("sid", None)
            return None

        conn.execute(
            text("UPDATE app_sessions SET last_seen_utc = :now WHERE session_id = :sid"),
            {"now": now, "sid": sid},
        )
        _log_event(conn, sid, "validated", {"note": "ok"})

        # Repopulate _stored_sid so that subsequent nav links can carry it,
        # and mark _sid_checked so capture_client_fingerprints() does not run
        # its localStorage JS eval (which could overwrite _stored_sid with a
        # stale SID from a previous browser session).
        st.session_state["_stored_sid"] = sid
        st.session_state["_sid_checked"] = True

        return {
            "session_id": row["session_id"],
            "user_id": int(row["user_id"]),
            "name": row.get("name"),
            "region": row.get("region"),
            "role": row.get("role"),
            "email": row.get("email"),
        }
