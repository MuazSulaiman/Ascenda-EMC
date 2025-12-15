# app_v11.py — Sales Daily Feedback — v11 (+Business Lines, Shelf Movement)
# - Home Visit fields right after Audience (label startswith "Home Visit")
# - Business Unit → Business Line → (optional) Item
# - Shelf Movement objective shows items of the chosen Business Line with Qty Checked
# - Atomic insert: visit + optional home_visit + optional shelf_movement(header+lines)
# - Power BI push BEFORE rerun; includes shelf aggregates when present
# - Secrets fallback: env["PBI_PUSH_URL"] or st.secrets["PBI_PUSH_URL"]
# - Duplicate check uses submitted_at_utc
# - Render + PostgreSQL (psycopg v3) — no SQLite, no settings.get 

# Standard library
import base64
import io
import json
import math
import os
import re
import secrets
import string
import time
import unicodedata
import uuid
import zipfile
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Third-party
import folium
import pandas as pd
import requests
import sqlalchemy as sa
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image
from dateutil import tz
from passlib.hash import pbkdf2_sha256
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from streamlit_autorefresh import st_autorefresh
from streamlit_folium import st_folium
from streamlit_geolocation import streamlit_geolocation
from streamlit_js_eval import get_geolocation as _get_geo_js

# Local
from db import engine

# =============================
# Config
# =============================
APP_TITLE = "Ascenda"
TIMEZONE = "Asia/Riyadh"
SESSION_TTL_MIN = 20       # 20 mins (1 hour)
DUP_MINUTES = 15            # duplicate detection lookback (minutes)

# Power BI push URL:
#   - Prefer environment variable (Render → Environment)
#   - Fallback to Streamlit secrets if present (for local dev)
try:
    PBI_PUSH_URL = os.environ.get("PBI_PUSH_URL") or st.secrets["PBI_PUSH_URL"]
except Exception:
    PBI_PUSH_URL = os.environ.get("PBI_PUSH_URL", "")  # env only if no secrets.toml
st.set_page_config(page_title=APP_TITLE, layout="centered")

# =============================
# App Icons for Home Screen Addition
# =============================
# After st.set_page_config(...)

components.html(f"""
<script>
(function() {{
  const head = document.head;
  function add(tag, attrs) {{
    const el = document.createElement(tag);
    Object.entries(attrs).forEach(([k,v]) => el.setAttribute(k, v));
    head.appendChild(el);
  }}
  // iOS icon + title
  add('link', {{ rel: 'apple-touch-icon', href: '/static/ascenda_180.png' }});
  add('meta', {{ name: 'apple-mobile-web-app-capable', content: 'yes' }});
  add('meta', {{ name: 'apple-mobile-web-app-title', content: 'Ascenda' }});

  // Android / PWA
  add('link', {{ rel: 'manifest', href: '/static/manifest.webmanifest' }});
  add('meta', {{ name: 'theme-color', content: '#0ea5e9' }});

  // (Optional) regular favicon fallback
  add('link', {{ rel: 'icon', type: 'image/png', sizes: '192x192', href: '/static/ascenda_192.png' }});
}})();
</script>
""", height=0)

# Global layout tweak: reduce the big top padding on all pages
st.markdown(
    """
    <style>
    /* Streamlit main view container – shrink vertical padding everywhere */
    [data-testid="stAppViewContainer"] .block-container {
        padding-top: 1.5rem !important;    /* try 0–2rem */
        padding-bottom: 1.5rem !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.set_page_config(
    page_title=APP_TITLE,
    page_icon=Image.open("static/ascenda_180.png"),
    layout="centered"
)

# =============================
# Removing anchor links
# =============================

# hide anchor/permalink icons on all headings (st.title, st.header, markdown headings)
st.markdown("""
<style>
/* st.title / st.header */
[data-testid="stHeading"] a { 
  display: none !important;
  visibility: hidden !important;
  pointer-events: none !important;
}
/* markdown-rendered headings inside st.markdown */
.stMarkdown h1 a, .stMarkdown h2 a, .stMarkdown h3 a,
.stMarkdown h4 a, .stMarkdown h5 a, .stMarkdown h6 a {
  display: none !important;
  visibility: hidden !important;
  pointer-events: none !important;
}
</style>
""", unsafe_allow_html=True)

def get_logo_base64() -> str:
    """
    Read logo from your Git repo static folder and return it as base64 string.
    Adjust the relative path if your app file is in a subfolder.
    """
    logo_path = Path(__file__).parent / "static" / "Login_Logo.png"
    try:
        with open(logo_path, "rb") as f:
            data = f.read()
        return base64.b64encode(data).decode("utf-8")
    except Exception:
        return ""

# --- Client fingerprint capture ---
import streamlit as st

try:
    # library provides handy helpers
    from streamlit_js_eval import get_user_agent, streamlit_js_eval
except Exception:
    get_user_agent = None
    streamlit_js_eval = None

def capture_client_fingerprints():
    """Populate st.session_state['client_ip'] and ['user_agent'] from the browser."""
    # User Agent
    if "user_agent" not in st.session_state:
        ua_val = None
        try:
            if get_user_agent:
                ua = get_user_agent()
                # get_user_agent returns a dict like {"userAgent": "..."} in recent versions
                ua_val = (ua or {}).get("userAgent") if isinstance(ua, dict) else ua
        except Exception:
            ua_val = None
        if ua_val:
            st.session_state["user_agent"] = str(ua_val)

    # Public IP (must be fetched client-side; server-side requests return the server IP)
    if "client_ip" not in st.session_state:
        ip_val = None
        try:
            if streamlit_js_eval:
                ip_val = streamlit_js_eval(
                    js_expressions=(
                        "await fetch('https://api.ipify.org?format=json')"
                        ".then(r=>r.json()).then(j=>j.ip)"
                    ),
                    key="client_ip_fetch"
                )
        except Exception:
            ip_val = None
        if ip_val:
            st.session_state["client_ip"] = str(ip_val)

# Call this once early (e.g., at the start of your main script, before any login UI)
capture_client_fingerprints()

def apply_role_based_layout():
    """
    Make layout 'wider' for manager/admin on larger screens
    but keep default behavior on mobile.
    """
    u = st.session_state.get("user") or resolve_session_user()
    if not u:
        return

    role = (u.get("role") or "").lower().strip()

    # Later you can read this from DB / user settings instead of hard-coding
    prefers_wide = role in ("manager", "admin")

    if not prefers_wide:
        return

    # CSS: only apply on wider viewports (e.g. laptops/desktops)
    st.markdown(
        """
        <style>
        /* Only screens >= 900px width: act like "wide" mode */
        @media (min-width: 900px) {
          .block-container {
            max-width: 1200px;   /* you can increase this if you want */
            padding-left: 2rem;
            padding-right: 2rem;
          }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

# =============================
# DB Utilities (PostgreSQL)
# =============================

def _get_secret(name: str, default: str = "") -> str:
    # Prefer env var (Render). Fall back to st.secrets only if present locally.
    val = os.environ.get(name)
    if val:
        return val
    try:
        return st.secrets[name]  # works only if you have .streamlit/secrets.toml locally
    except Exception:
        return default

def query_df(sql: str, params: Optional[dict] = None) -> pd.DataFrame:
    """Run a read query and return a DataFrame (PostgreSQL)."""
    with engine.begin() as conn:
        return pd.read_sql_query(text(sql), conn, params=params or {})

def exec_sql(sql: str, params: Optional[dict] = None):
    """Execute a write DDL/DML statement (PostgreSQL)."""
    with engine.begin() as conn:
        conn.execute(text(sql), params or {})

def insert_visit_returning_id(row: dict) -> int:
    """
    Insert a visit row into PostgreSQL and return the new visit_id via RETURNING.
    `row` is a dict of column -> value.
    """
    cols = list(row.keys())
    named = [f":{c}" for c in cols]
    sql = f"INSERT INTO visits ({', '.join(cols)}) VALUES ({', '.join(named)}) RETURNING visit_id"
    with engine.begin() as conn:
        vid = conn.execute(text(sql), row).scalar_one()
    return int(vid)

# --- Atomic insert for visit (+ optional home_visit + optional shelf movement)
def insert_visit_atomic(
    visit_row: dict,
    home_visit: Optional[dict] = None,            # {"patient_name":..., "patient_phone":..., "serial_no":...}
    shelf_lines: Optional[List[dict]] = None      # [{"product_id": "...", "qty_checked": number}, ...]
) -> int:
    """
    Insert a visit and the optional related entities atomically (PostgreSQL).
    Returns the new visit_id. Rolls back everything on any failure.
    """
    visit_cols = list(visit_row.keys())
    visit_vals_named = [f":{c}" for c in visit_cols]
    sql_visit = f"""
        INSERT INTO visits ({', '.join(visit_cols)})
        VALUES ({', '.join(visit_vals_named)})
        RETURNING visit_id
    """

    with engine.begin() as conn:
        # 1) visit
        vid = conn.execute(text(sql_visit), visit_row).scalar_one()

        # 2) optional home visit
        if home_visit:
            conn.execute(
                text("""
                INSERT INTO home_visits(visit_id, patient_name, patient_phone, serial_no)
                VALUES (:visit_id, :patient_name, :patient_phone, :serial_no)
                """),
                {
                    "visit_id": vid,
                    "patient_name": home_visit["patient_name"].strip(),
                    "patient_phone": home_visit["patient_phone"].strip(),
                    "serial_no": home_visit["serial_no"].strip().upper(),
                },
            )

        # 3) optional shelf movement (header + lines)
        if shelf_lines:
            movement_id = conn.execute(
                text("""
                    INSERT INTO shelf_movement_headers(visit_id)
                    VALUES (:visit_id)
                    RETURNING movement_id
                """),
                {"visit_id": vid},
            ).scalar_one()

            # lines (only >=0 qty)
            for ln in shelf_lines:
                pid = str(ln["product_id"])
                qty = float(ln["qty_checked"])
                if qty < 0:
                    continue
                conn.execute(
                    text("""
                        INSERT INTO shelf_movement_lines(movement_id, product_id, qty_checked)
                        VALUES (:movement_id, :product_id, :qty_checked)
                    """),
                    {"movement_id": movement_id, "product_id": pid, "qty_checked": qty},
                )

    return int(vid)

def insert_project(project_row: dict) -> int:
    """
    Inserts a new project and returns its project_id.
    Also updates adj_name = project_id || '. ' || name if you decide to add that column.
    """
    with engine.begin() as conn:
        # Insert project
        result = conn.execute(
            text("""
                INSERT INTO projects (
                    name,
                    description,
                    assigned_by_id,
                    assigned_to_id,
                    business_line_id,
                    product_id,
                    customer_id,
                    planned_start_date,
                    planned_end_date,
                    actual_end_date,
                    status,
                    project_objective_id,
                    created_at,
                    updated_at
                )
                VALUES (
                    :name,
                    :description,
                    :assigned_by_id,
                    :assigned_to_id,
                    :business_line_id,
                    :product_id,
                    :customer_id,
                    :planned_start_date,
                    :planned_end_date,
                    :actual_end_date,
                    :status,
                    :project_objective_id,
                    :created_at,
                    :updated_at
                )
                RETURNING project_id
            """),
            project_row,
        )
        new_id = result.scalar_one()

        # OPTIONAL: if you add adj_name column to projects, update it here:
        # conn.execute(
        #     text("""
        #         UPDATE projects
        #         SET adj_name = CONCAT(project_id::text, '. ', name)
        #         WHERE project_id = :pid
        #     """),
        #     {"pid": new_id},
        # )

        return int(new_id)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _client_ip() -> Optional[str]:
    # If you capture IP via reverse proxy header, read it here; else keep None.
    return st.session_state.get("client_ip") if "client_ip" in st.session_state else None

def _local_now() -> datetime:
    return datetime.now(tz.gettz(TIMEZONE))

def _local_now_str() -> str:
    return _local_now().strftime("%Y-%m-%d %H:%M:%S")

# Safe, Render-friendly secret access for Power BI
PBI_PUSH_URL = _get_secret("PBI_PUSH_URL", "")

def push_visit_to_pbi(row: dict) -> Tuple[bool, Optional[str]]:
    """
    Push a single visit to your Power BI streaming/push dataset.
    Returns (ok, err_msg).
    """
    push_url = PBI_PUSH_URL
    if not push_url:
        return False, "Missing PBI_PUSH_URL (env var or secrets)."

    payload = {"rows": [row]}
    try:
        r = requests.post(push_url, json=payload, timeout=8)
        if r.status_code in (200, 202):
            return True, None
        return False, f"{r.status_code} {r.text}"
    except Exception as e:
        return False, str(e)

# =============================
# Auth helpers (PostgreSQL)
# =============================

def _user_agent() -> Optional[str]:
    return st.session_state.get("user_agent")

import json
from sqlalchemy import text, bindparam
from sqlalchemy.dialects.postgresql import JSONB

def _log_event(conn, sid: str, evt: str, details=None):
    # Always bind a value for :details so it never goes missing
    if details is None:
        details = {}

    stmt = text("""
        INSERT INTO app_session_events(session_id, event_type, ip, user_agent, details)
        VALUES (:sid, :evt, :ip, :ua, :details)
    """).bindparams(
        bindparam("details", type_=JSONB)
    )

    conn.execute(stmt, {
        "sid": sid,
        "evt": evt,
        "ip": _client_ip(),
        "ua": _user_agent(),
        "details": details,   # raw dict; SQLAlchemy adapts it to JSONB
    })

def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT * FROM users WHERE email = :email"),
            {"email": email},
        ).mappings().first()
        return dict(row) if row else None

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

    - Normal users: TTL = SESSION_TTL_MIN
    - Admins: TTL = 12 hours (720 minutes)
    """
    sid = uuid.uuid4().hex
    now = _utcnow()

    # ----- Role-based TTL -----
    if role and str(role).lower().strip() == "admin":
        ttl_minutes = 720  # 12 hours for admins
    else:
        ttl_minutes = SESSION_TTL_MIN  # default for others

    exp = now + timedelta(minutes=ttl_minutes)

    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO app_sessions(
                    session_id,
                    user_id,
                    created_at_utc,
                    expires_at_utc,
                    last_seen_utc,
                    ip,
                    user_agent
                )
                VALUES (
                    :sid,
                    :uid,
                    :created,
                    :expires,
                    :last_seen,
                    :ip,
                    :ua
                )
            """),
            {
                "sid": sid,
                "uid": user_id,
                "created": now,
                "expires": exp,
                "last_seen": now,
                "ip": _client_ip(),
                "ua": _user_agent(),
            },
        )
        # Log with the actual TTL used
        _log_event(conn, sid, "created", {"ttl_min": ttl_minutes})

    return sid

def purge_expired_sessions() -> None:
    """No hard delete; just mark any that have passed expiry as closed, idempotently."""
    with engine.begin() as conn:
        res = conn.execute(
            text("""
                UPDATE app_sessions
                SET revoked_at_utc = NOW(), closed_reason = 'expired'
                WHERE expires_at_utc < NOW() AND revoked_at_utc IS NULL
                RETURNING session_id
            """)
        )
        sids = [r[0] for r in res.fetchall()]
        for sid in sids:
            _log_event(conn, sid, "expired", {"batch": True})

def delete_session(sid: str) -> None:
    revoke_session(sid, reason="manual_revoke")

# Optional: guard if someone runs the app before init_db_v11.py locally
def _ensure_sessions_table_exists():
    ddl = """
    CREATE TABLE IF NOT EXISTS app_sessions (
      session_id TEXT PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
      created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      expires_at_utc TIMESTAMPTZ NOT NULL
    )
    """
    with engine.begin() as conn:
        conn.execute(text(ddl))

# =============================
# URL helpers (STABLE sid in URL)
# =============================

def set_url_param(name: str, value: str | None):
    if value is None:
        st.query_params.pop(name, None)
    else:
        st.query_params[name] = value

def get_url_param(name: str, default: str | None = None) -> str | None:
    return st.query_params.get(name, default)

def set_url_session_param(sid: Optional[str]):
    if sid:
        st.query_params["sid"] = sid
    else:
        st.query_params.pop("sid", None)

# =============================
# App state — restore session
# =============================

def resolve_session_user():
    sid = st.query_params.get("sid") or st.query_params.get("_sid")
    if st.query_params.get("_sid"):
        st.query_params["sid"] = st.query_params["_sid"]
        st.query_params.pop("_sid", None)
        sid = st.query_params.get("sid")
    if not sid:
        return None

    now = _utcnow()
    with engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT s.session_id,
                       s.user_id,
                       s.expires_at_utc,
                       s.revoked_at_utc,
                       u.name         AS name,
                       u.region       AS region,
                       u.role         AS role,
                       u.email        AS email
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

        # bump last_seen and log
        conn.execute(text("UPDATE app_sessions SET last_seen_utc = :now WHERE session_id = :sid"),
                     {"now": now, "sid": sid})
        _log_event(conn, sid, "validated", {"note": "ok"})

        # return a plain dict with the fields your UI expects
        return {
            "session_id": row["session_id"],
            "user_id": int(row["user_id"]),
            "name": row.get("name"),
            "region": row.get("region"),
            "role": row.get("role"),
            "email": row.get("email"),
        }
    
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

# Run a tiny guard (safe no-op if table already exists), then load session
_ensure_sessions_table_exists()
if "user" not in st.session_state:
    purge_expired_sessions()
    st.session_state.user = resolve_session_user()

# =============================
# Data helpers
# =============================

def recent_visit_minutes(uid: int, customer_id: int) -> Optional[int]:
    """
    Return minutes since the most recent visit by user for given customer, or None.
    """
    with engine.begin() as conn:
        r = conn.execute(
            text("""
                SELECT submitted_at_utc
                FROM visits
                WHERE user_id = :uid AND customer_id = :cid
                ORDER BY visit_id DESC
                LIMIT 1
            """),
            {"uid": uid, "cid": customer_id},
        ).fetchone()
    if not r:
        return None

    last = r[0]
    if isinstance(last, str):
        try:
            last = datetime.fromisoformat(last)
        except Exception:
            return None
    last = last if getattr(last, "tzinfo", None) else last.replace(tzinfo=timezone.utc)
    delta = _utcnow() - last
    return int(delta.total_seconds() // 60)

def _gen_tmp_pw(length: int = 12) -> str:
    # mixed case + digits + a symbol
    alphabet = string.ascii_letters + string.digits
    core = "".join(secrets.choice(alphabet) for _ in range(length - 1))
    return core + secrets.choice("!@#$%^&*")

def _img_b64(p: Path) -> str:
    return base64.b64encode(p.read_bytes()).decode("utf-8")

# ====== dependency reset callbacks ======
def _on_customer_change():
    st.session_state.pop("aud_sel", None)

def _on_bu_change():
    st.session_state.pop("bl_sel", None)
    st.session_state.pop("prod_sel", None)

def _on_line_change():
    st.session_state.pop("prod_sel", None)

# ====== reset pages for location ======


# Namespaced keys we use inside get_location_block()
_GEO_KEYS = ("geo_try", "geo_start_ts", "geo_autorefresh", "geo_map")

def _reset_location_state_for_page(page_ns: str):
    """Delete any geo-related session_state keys for this page namespace (any nonce)."""
    prefix = f"{page_ns}/"
    for k in list(st.session_state.keys()):
        if k.startswith(prefix) and any(gk in k for gk in _GEO_KEYS):
            st.session_state.pop(k, None)

def _reset_geo_on_user_or_page_change(page_ns: str, uid: int):
    """
    If user or page changed since last render, clear geo state so the flow starts fresh.
    Works even if you navigated away and came back.
    """
    last_uid_key  = f"__{page_ns}_last_uid"
    last_page_key = "__current_page"

    current_page  = page_ns
    last_uid      = st.session_state.get(last_uid_key)
    last_page     = st.session_state.get(last_page_key)

    if (last_uid is None) or (last_uid != uid) or (last_page != current_page):
        _reset_location_state_for_page(page_ns)
        st.session_state[last_uid_key]  = uid
        st.session_state[last_page_key] = current_page
    
def set_current_page(page_ns: str):
    """Update the global page marker so other pages can detect navigation."""
    prev = st.session_state.get("__current_page")
    if prev != page_ns:
        # Optionally: reset *this* page’s geo right when you enter it
        _reset_location_state_for_page(page_ns)
    st.session_state["__current_page"] = page_ns


# =============================
# UI — Login / Logout (blocks inactive accounts)
# =============================
def login_block():
    
    # ---- paths (logo in ./static/Login_Logo.png next to this script)
    app_root = Path(__file__).parent
    logo_path = app_root / "static" / "Login_Logo.png"

    # ---- logo
    if logo_path.exists():
        b64 = _img_b64(logo_path)
        st.markdown(
            f"""
            <div style="text-align:center;">
              <img src="data:image/png;base64,{b64}" alt="Ascenda" style="width:220px;"/>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.warning(f"Logo not found at: {logo_path}")

    # ---- title
    st.markdown(
        "<h2 style='text-align:center; margin-top:8px;'>Login</h2>",
        unsafe_allow_html=True
    )
    
    with st.form("login"):
        email = st.text_input("Email", placeholder="you@company.com")
        pw = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")

    if submitted:
        em = (email or "").strip().lower()
        if not em or not pw:
            st.error("Please enter both email and password.")
            return

        u = get_user_by_email(em)
        if not u:
            st.error("User not found.")
            return

        # Postgres BOOLEAN: treat False/None as inactive
        if not bool(u.get("is_active", True)):
            st.error("Your account is inactive. Please contact the administrator.")
            return

        if not pbkdf2_sha256.verify(pw, u["password_hash"]):
            st.error("Invalid password.")
            return

        st.session_state.user = u
        sid = create_session(int(u["user_id"]), u.get("role"))
        set_url_session_param(sid)

        st.session_state["_current_page"] = "Submit Visit"
        set_url_param("page", "Submit Visit")

        st.success(f"Welcome, {u.get('name') or u.get('email')}!")
        st.rerun()

def logout_button():
    if st.sidebar.button("Logout"):
        # Clear submit page geo state when logging out
        _reset_location_state_for_page("submit_visit")

        sid = st.query_params.get("sid")
        if sid:
            delete_session(sid)
        set_url_session_param(None)
        st.session_state.user = None
        st.session_state.pop("_current_page", None)
        st.session_state.pop("__current_page", None)  # <— ensure page tracker is cleared
        set_url_param("page", None)
        st.rerun()

def sidebar_nav():
    # -----------------------------
    # Sidebar CSS (tight layout + no circles + highlight selected)
    # -----------------------------
    st.sidebar.markdown(
        """
        <style>

        /* Reduce overall sidebar top padding */
        section[data-testid="stSidebar"] > div:first-child {
            padding-top: 0.5rem !important;
        }

        /* Center the logo with tighter spacing */
        .ascenda-logo-wrap {
            display: flex;
            justify-content: center;
            align-items: center;
            margin-top: 0.25rem !important;
            margin-bottom: 0.5rem !important;
        }

        /* Tighter title spacing */
        section[data-testid="stSidebar"] h1, 
        section[data-testid="stSidebar"] h2, 
        section[data-testid="stSidebar"] h3 {
            margin-top: 0.2rem !important;
            margin-bottom: 0.6rem !important;
            padding: 0 !important;
        }

        /* Make radio look like modern menu */

        /* 1) Remove the radio circle completely */
        div[role="radiogroup"] > label > div:first-child {
            display: none !important;
        }

        /* 2) Base style for each menu item (FULL WIDTH ROW) */
        div[role="radiogroup"] > label {
            display: block !important;
            width: 100% !important;
            padding: 6px 10px !important;
            margin: 2px 0 !important;
            border-radius: 6px;
            cursor: pointer;
            box-sizing: border-box !important;
            transition: background-color 0.15s ease-in-out, color 0.15s ease-in-out;
        }

        /* 3) Hover effect */
        div[role="radiogroup"] > label:hover {
            background-color: #eceff4 !important;
        }

        /* 4) Selected item — full-width highlight, consistent length */
        div[role="radiogroup"] > label:has(input:checked) {
            background-color: #1c4e8020 !important;
            border-left: 4px solid #1c4e80 !important;
            font-weight: 600 !important;
            padding-left: 6px !important;
            color: #1c4e80 !important;
        }

        </style>
        """,
        unsafe_allow_html=True,
    )

    # -----------------------------
    # Logo from static (base64)
    # -----------------------------
    logo_b64 = get_logo_base64()
    if logo_b64:
        st.sidebar.markdown(
            f"""
            <div class="ascenda-logo-wrap">
                <img src="data:image/png;base64,{logo_b64}"
                     alt="Ascenda"
                     style="width:150px; height:auto;" />
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.sidebar.markdown("### Ascenda")

    # -----------------------------
    # Navigation title (tighter)
    # -----------------------------
    st.sidebar.markdown(" ")
    st.sidebar.markdown("### Navigation")

    user = st.session_state.get("user")
    role = (user.get("role") if user else "").lower().strip()

    # Base pages
    pages = ["Submit Visit", "My Submissions"]

    if role == "rep":
        pages += ["Projects View", "User Settings"]

    if role == "manager":
        pages += ["Project Creation", "Project Management", "Projects View", "User Settings"]

    if role == "admin":
        pages += [
            "Project Creation",
            "Project Management",
            "Projects View",
            "Admin: Import Lookups",
            "Admin: Data Browser",
            "Admin: Users",
            "Review Target Audiences",
            "User Settings",
        ]

    # -----------------------------
    # Resolve current page
    # -----------------------------
    url_page = get_url_param("page")
    sess_page = st.session_state.get("_current_page")

    if url_page in pages:
        current = url_page
    elif sess_page in pages:
        current = sess_page
    else:
        current = pages[0]

    idx = pages.index(current)

    # -----------------------------
    # Navigation radio (styled as menu)
    # -----------------------------
    def _on_change():
        old = st.session_state.get("_current_page")
        chosen = st.session_state["_nav_choice"]

        st.session_state["_current_page"] = chosen
        set_url_param("page", chosen)

        if old == "Submit Visit" and chosen != "Submit Visit":
            _reset_location_state_for_page("submit_visit")

    choice = st.sidebar.radio(
        "",
        pages,
        index=idx,
        key="_nav_choice",
        on_change=_on_change,
        label_visibility="collapsed",
    )

    # Sync state ↔ URL
    if st.session_state.get("_current_page") != choice:
        st.session_state["_current_page"] = choice
    if get_url_param("page") != choice:
        set_url_param("page", choice)

    return choice

# =============================
# Location block (auto-flow, minimal UI) – required
# =============================
# Prefer JS geolocation (no tiny Leaflet button needed)
try:
    from streamlit_js_eval import get_geolocation as _get_geo_js
except Exception:
    _get_geo_js = None


def _acc_str(v: Optional[float]) -> str:
    return f" (~{v:.0f} m accuracy)" if isinstance(v, (int, float)) and math.isfinite(v) else ""


def get_location_block(k) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    UX:
      1) First screen: a single 'Get location' button.
      2) After click: poll once/second for up to TIMEOUT_S seconds.
         - Show spinner + 'Waiting for permission…'
         - If coords arrive => success + map
         - If timeout => show friendly warning + Retry button
    """
    TIMEOUT_S = 15  # adjust if you want longer/shorter

    with st.expander("📍 Location (auto) — required for check-in", expanded=True):
        tried_key   = k("geo_try")
        start_key   = k("geo_start_ts")
        refresh_key = k("geo_autorefresh")

        st.session_state.setdefault(tried_key, False)

        # Screen 1 — just the “Get location” button
        if not st.session_state[tried_key]:
            st.caption("Allow your browser to share location. We only capture it for this visit submission.")
            if st.button("📍 Get location", key=k("btn_get_loc"), type="primary"):
                st.session_state[tried_key] = True
                st.session_state[start_key] = time.time()
                st.rerun()
            return (None, None, None)

        # Screen 2 — actively trying to get geolocation
        lat = lon = acc = None

        # 1) Try the JS path (non-blocking; returns immediately)
        if _get_geo_js is not None:
            try:
                geo = _get_geo_js() or {}
                lat = (geo.get("coords") or {}).get("latitude")
                lon = (geo.get("coords") or {}).get("longitude")
                acc = (geo.get("coords") or {}).get("accuracy")
            except Exception:
                pass

        # If still no coords, keep polling until timeout
        if lat is None or lon is None:
            # Fire an auto-rerun every 1s while we wait, but only until timeout
            started = st.session_state.get(start_key) or time.time()
            elapsed = time.time() - started

            if elapsed < TIMEOUT_S:
                with st.spinner("Waiting for permission…"):
                    st.progress(min(1.0, elapsed / TIMEOUT_S))
                # auto-refresh in 1s to re-check
                st_autorefresh(interval=1000, key=refresh_key, limit=TIMEOUT_S + 2)
                # Offer cancel/retry immediately if user wants
                # if st.button("🔁 Retry location", key=k("btn_retry_loc")):
                #     st.session_state.pop(tried_key, None)
                #     st.session_state.pop(start_key, None)
                #     st.rerun()
                return (None, None, None)

            # Timeout reached → show the warning (only now)
            st.warning(
                "We couldn't read your location.\n\n"
                "• Allow **Location** (and **Precise location** on iOS) in browser permissions.\n"
                "• Make sure you’re on **HTTPS** and device location is **ON**.\n"
                "• Then tap **Retry location**."
            )
            if st.button("🔁 Retry location", key=k("btn_retry_after_timeout")):
                st.session_state.pop(tried_key, None)
                st.session_state.pop(start_key, None)
                st.rerun()
            return (None, None, None)

        # Validate numeric
        try:
            flat = float(lat); flon = float(lon)
            facc = float(acc) if acc is not None else None
        except Exception:
            st.warning("Location values looked invalid. Please try again.")
            if st.button("🔁 Retry location", key=k("btn_retry_invalid")):
                st.session_state.pop(tried_key, None)
                st.session_state.pop(start_key, None)
                st.rerun()
            return (None, None, None)

        # Success UI
        st.success(f"Captured location: {flat:.6f}, {flon:.6f}{_acc_str(facc)}")

        # Use a local PNG (put your marker in ./static/location_marker.png)
        marker_icon_path = "static/location_marker.png"
        custom_icon = None
        try:
            custom_icon = folium.CustomIcon(marker_icon_path, icon_size=(40, 40))
        except Exception:
            pass  # fallback to default if file missing

        m = folium.Map(location=[flat, flon], zoom_start=16, control_scale=True)
        folium.Marker(
            [flat, flon],
            tooltip="Your location",
            icon=custom_icon if custom_icon else None
        ).add_to(m)
        st_folium(m, height=300, key=k("geo_map"))

        if st.button("🔁 Capture again", key=k("btn_retry_after_ok")):
            st.session_state.pop(tried_key, None)
            st.session_state.pop(start_key, None)
            st.rerun()

        return (flat, flon, facc)

# =============================
# Page — Submit Visit (sticky submit + dedupe guard + optional projects)
# =============================
try:
    # psycopg 3 error class (optional, for finer duplicate checks)
    from psycopg.errors import UniqueViolation
except Exception:
    UniqueViolation = None

def page_submit_visit():
    st.title("📝 Submit Visit")

    # ---- tiny CSS for floating submit ----
    st.markdown(
        """
        <style>
          .sticky-submit-wrap{position:fixed; right:16px; bottom:16px; z-index:1000;}
          @media (max-width:640px){
            .sticky-submit-wrap{left:16px; right:16px;}
            .sticky-submit-wrap button{width:100%;}
          }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # ---- Red asterisk legend ----
    st.markdown(
        '<div style="margin:.25rem 0 1rem 0;">'
        'Fields marked with <span style="color:#d00000;font-weight:700">*</span> are required.'
        "</div>",
        unsafe_allow_html=True,
    )

    PAGE_NS = "submit_visit"
    nonce_key        = f"_{PAGE_NS}_form_nonce"
    saved_ok_key     = f"_{PAGE_NS}_saved_ok"
    geo_nonce_key    = f"_{PAGE_NS}_geo_nonce"
    geo_captured_key = f"_{PAGE_NS}_geo_captured"
    busy_key         = f"_{PAGE_NS}_busy"
    intent_key       = f"_{PAGE_NS}_submit_intent"
    prev_proj_key    = f"_{PAGE_NS}_prev_project_label"

    TITLE_OPTIONS = ["", "Dr.", "Mr.", "Ms.", "Mrs.", "Prof.", "Eng.", "Other"]
    
    st.session_state.setdefault(nonce_key, 0)
    st.session_state.setdefault(geo_nonce_key, 0)
    st.session_state.setdefault(busy_key, False)
    st.session_state.setdefault(intent_key, False)
    st.session_state.setdefault(prev_proj_key, "")

    def k(name: str) -> str:
        return f"{PAGE_NS}/{name}_{st.session_state[nonce_key]}"

    # ---- cascade clear helpers (use current nonce'd keys) ----
    def _on_region_change():
        for n in ("city_sel", "sector_sel", "cust_sel", "aud_sel"):
            st.session_state.pop(k(n), None)

    def _on_city_change():
        for n in ("sector_sel", "cust_sel", "aud_sel"):
            st.session_state.pop(k(n), None)

    def _on_sector_change():
        for n in ("cust_sel", "aud_sel"):
            st.session_state.pop(k(n), None)

    def _on_bu_change():
        for n in ("cat_sel", "bl_sel", "prod_sel"):
            st.session_state.pop(k(n), None)

    def _on_line_change():
        for n in ("prod_sel",):
            st.session_state.pop(k(n), None)

    # Clear project-dependent fields by forcing them to ""
    def _clear_project_dependent_fields():
        for n in (
            "region_sel", "city_sel", "sector_sel", "cust_sel", "aud_sel",
            "bu_sel", "cat_sel", "bl_sel", "prod_sel"
        ):
            st.session_state[k(n)] = ""

    set_current_page(PAGE_NS)

    # --- Resolve logged-in user safely ---
    u = st.session_state.get("user") or resolve_session_user()
    if not u:
        st.warning("Please sign in to continue.")
        st.stop()

    uid  = int(u.get("user_id") or u.get("id"))
    role = (u.get("role") or "").lower().strip()

    # --- Defensive fallbacks ---
    display_name   = u.get("name") or u.get("email") or f"User #{u.get('user_id', '?')}"
    display_region = u.get("region") or "—"
    display_role   = u.get("role") or "—"

    # --- Display info ---
    st.caption(
        f"Logged in as **{display_name}** · Region: **{display_region}** · Role: **{display_role}**"
    )

    # Ensure location flow is reset when user or page changes
    _reset_geo_on_user_or_page_change(PAGE_NS, uid)

    if st.session_state.pop(saved_ok_key, False):
        st.success("Saved ✅ — fields cleared, you can enter a new visit.")

    # Focus first text box
    components.html(
        """<script>
        const wait=()=>{const el=window.parent.document.querySelector('input[type="search"], input[type="text"]');
        if(!el){setTimeout(wait,250);return;} el.focus();}; wait();
        </script>""",
        height=0,
    )

    # =====================================================
    # SECTION 1 — Visit Location (REQUIRED)
    # =====================================================
    st.markdown("### 1️⃣ Visit Location")
    lat, lon, acc = get_location_block(k)
    if lat is None or lon is None:
        st.info("📍 Location is required before you can submit.")
        return

    # =====================================================
    # SECTION 2 — Project (optional)
    # =====================================================
    st.markdown("### 2️⃣ Project (optional)")

    project_df          = pd.DataFrame()
    selected_project    = None
    selected_project_id = None

    where_clauses: list[str] = ["p.status IN ('Not Started', 'Open')"]
    params: dict[str, object] = {}

    if role == "rep":
        where_clauses.append("p.assigned_to_id = :uid")
        params["uid"] = uid
    elif role == "manager":
        where_clauses.append("p.assigned_by_id = :uid")
        params["uid"] = uid
    elif role == "admin":
        # no extra filter
        pass

    if role in ("rep", "manager", "admin"):
        project_df = query_df(
            f"""
            SELECT
                p.project_id,
                p.name AS project_name,
                p.customer_id,
                c.account_name,
                c.region,
                c.city,
                c.sector,
                p.business_line_id,
                bl.name AS business_line_name,
                bl.business_unit_id AS business_unit_id,
                bu.name AS business_unit_name,
                bl.category,
                p.product_id,
                i.article_number,
                i.description AS item_description
            FROM projects p
            JOIN customers      c  ON c.customer_id       = p.customer_id
            JOIN business_lines bl ON bl.business_line_id = p.business_line_id
            LEFT JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
            LEFT JOIN items      i  ON i.product_id        = p.product_id
            WHERE {" AND ".join(where_clauses)}
            ORDER BY p.project_id, p.name, c.account_name
            """,
            params,
        )

    proj_labels: list[str] = [""]  # first = no project
    proj_label_to_id: dict[str, int] = {}

    if not project_df.empty:
        for r in project_df.itertuples(index=False):
            base = f"{r.project_id}. {r.project_name}"
            parts = [base, str(r.account_name)]
            if getattr(r, "business_line_name", None):
                parts.append(str(r.business_line_name))
            label = " — ".join(parts)
            proj_labels.append(label)
            proj_label_to_id[label] = int(r.project_id)

    project_choice = st.selectbox(
        "Project (optional)",
        proj_labels,
        index=0,
        key=k("proj_sel"),
        help="Link this visit to a project. Customer and product context will follow the project.",
    )

    # Detect transitions: project selected / switched / cleared
    prev_label = st.session_state.get(prev_proj_key, "")
    curr_label = project_choice or ""

    if curr_label != prev_label:
        # Any change (None→something, something→other, something→None)
        _clear_project_dependent_fields()

    # Persist the new label for next run
    st.session_state[prev_proj_key] = curr_label

    # Resolve selected project (if any)
    if curr_label:
        selected_project_id = proj_label_to_id.get(curr_label)
        if selected_project_id is not None:
            sel_rows = project_df[project_df["project_id"] == selected_project_id]
            if not sel_rows.empty:
                selected_project = sel_rows.iloc[0].to_dict()
                proj_label = f"{selected_project['project_id']}. {selected_project.get('project_name', '')}"
                st.info(
                    f"🔒 Linked to project **{proj_label}**. "
                    "Customer and product context are locked to the project."
                )

    project_locked = selected_project is not None

    # Pre-extract project fields (used to seed state)
    proj_region        = selected_project.get("region")             if project_locked else None
    proj_city          = selected_project.get("city")               if project_locked else None
    proj_sector        = selected_project.get("sector")             if project_locked else None
    proj_customer_id   = int(selected_project["customer_id"])       if project_locked else None
    proj_customer_name = selected_project.get("account_name")       if project_locked else None
    proj_bu_id         = int(selected_project["business_unit_id"])  if project_locked and selected_project.get("business_unit_id") is not None else None
    proj_bu_name       = selected_project.get("business_unit_name") if project_locked else None
    proj_cat           = selected_project.get("category")           if project_locked else None
    proj_bl_id         = int(selected_project["business_line_id"])  if project_locked and selected_project.get("business_line_id") is not None else None
    proj_bl_name       = selected_project.get("business_line_name") if project_locked else None
    proj_prod_id       = selected_project.get("product_id")         if project_locked else None

    # =====================================================
    # SECTION 3 — Customer & Target Audience
    # =====================================================
    st.markdown("### 3️⃣ Customer & Target Audience")

    # helper: keep "Other"/"OTHER"/"other" at the very end
    def _order_with_other_last(values: list[str]) -> list[str]:
        normal_vals = []
        other_vals  = []
        for v in values:
            if isinstance(v, str) and v.strip().lower() == "other":
                other_vals.append(v)   # keep original casing
            else:
                normal_vals.append(v)
        return normal_vals + other_vals

    # ---- Region ----
    reg_df = query_df(
        """
        SELECT DISTINCT region
        FROM customers
        WHERE is_active IS TRUE
          AND region IS NOT NULL AND trim(region) <> ''
        ORDER BY region
        """
    )

    region_list = reg_df["region"].tolist()
    region_list = _order_with_other_last(region_list)
    region_opts = [""] + region_list

    # if project-locked, force region = project region
    if project_locked and proj_region:
        st.session_state[k("region_sel")] = proj_region

    region_choice = st.selectbox(
        "Region *",
        region_opts,
        index=0,
        key=k("region_sel"),
        disabled=project_locked,
        on_change=None if project_locked else _on_region_change,
    )
    if project_locked:
        region_choice = proj_region

    # ---- City ----
    if region_choice:
        city_df = query_df(
            """
            SELECT DISTINCT city
            FROM customers
            WHERE is_active IS TRUE
              AND region = :r
              AND city IS NOT NULL AND trim(city) <> ''
            ORDER BY city
            """,
            {"r": region_choice},
        )
        city_list = city_df["city"].tolist()
        city_list = _order_with_other_last(city_list)
        city_opts = [""] + city_list
    else:
        city_df = pd.DataFrame(columns=["city"])
        city_opts = [""]

    if project_locked and proj_city:
        st.session_state[k("city_sel")] = proj_city

    city_choice = st.selectbox(
        "City *",
        city_opts,
        index=0,
        key=k("city_sel"),
        disabled=project_locked or not region_choice,
        on_change=None if project_locked else _on_city_change,
        help=None if region_choice else "Select a Region first",
    )
    if project_locked:
        city_choice = proj_city

    # ---- Sector ----
    if region_choice and city_choice:
        sec_df = query_df(
            """
            SELECT DISTINCT sector
            FROM customers
            WHERE is_active IS TRUE
              AND region = :r
              AND city   = :c
              AND sector IS NOT NULL AND trim(sector) <> ''
            ORDER BY sector
            """,
            {"r": region_choice, "c": city_choice},
        )
        sector_list = sec_df["sector"].tolist()
        sector_list = _order_with_other_last(sector_list)
        sector_opts = [""] + sector_list
    else:
        sec_df = pd.DataFrame(columns=["sector"])
        sector_opts = [""]

    if project_locked and proj_sector:
        st.session_state[k("sector_sel")] = proj_sector

    sector_choice = st.selectbox(
        "Sector *",
        sector_opts,
        index=0,
        key=k("sector_sel"),
        disabled=project_locked or not (region_choice and city_choice),
        on_change=None if project_locked else _on_sector_change,
        help=None if (region_choice and city_choice) else "Select a City first",
    )
    if project_locked:
        sector_choice = proj_sector

    # ---- Customer ----
    if region_choice and city_choice and sector_choice:
        cust_df = query_df(
            """
            SELECT customer_id, account_name
            FROM customers
            WHERE is_active IS TRUE
              AND region = :r
              AND city   = :c
              AND sector = :s
            ORDER BY account_name
            """,
            {"r": region_choice, "c": city_choice, "s": sector_choice},
        )
        cust_list = cust_df["account_name"].tolist()
        cust_list = _order_with_other_last(cust_list)
        cust_names = [""] + cust_list
    else:
        cust_df = pd.DataFrame(columns=["customer_id", "account_name"])
        cust_names = [""]

    if project_locked and proj_customer_name:
        st.session_state[k("cust_sel")] = proj_customer_name

    cust_choice = st.selectbox(
        "Customer *",
        cust_names,
        index=0,
        key=k("cust_sel"),
        disabled=project_locked or not (region_choice and city_choice and sector_choice),
        help=None if (region_choice and city_choice and sector_choice) else "Select Sector first",
    )

    customer_id = None
    if project_locked and proj_customer_id:
        customer_id = proj_customer_id
        cust_choice = proj_customer_name
    elif cust_choice:
        match = cust_df.loc[cust_df["account_name"] == cust_choice, "customer_id"]
        customer_id = int(match.iloc[0]) if not match.empty else None

    # ---------------- Target Audience (with “Other”) ----------------
    audience_id      = None
    aud_choice_label = ""
    aud_choice_name  = None

    aud_labels: list[str] = [""]  # main dropdown labels
    aud_rows   = []              # (label, id, raw_name)

    # extra fields when TA = Other
    other_ta_title      = None
    other_ta_name       = None
    other_ta_department = None
    other_ta_position   = None
    other_ta_phone      = None
    other_ta_email      = None

    # 🔹 Global department & position lists from ALL target_audiences
    dept_choices_base: list[str] = []
    pos_choices_base:  list[str] = []

    dept_df = query_df(
        """
        SELECT DISTINCT department
        FROM target_audiences
        WHERE department IS NOT NULL
          AND trim(department) <> ''
        ORDER BY department
        """
    )
    if not dept_df.empty:
        dept_choices_base = dept_df["department"].astype(str).str.strip().tolist()

    pos_df = query_df(
        """
        SELECT DISTINCT position
        FROM target_audiences
        WHERE position IS NOT NULL
          AND trim(position) <> ''
        ORDER BY position
        """
    )
    if not pos_df.empty:
        pos_choices_base = pos_df["position"].astype(str).str.strip().tolist()

    # 🔹 Customer-specific target audiences for the main dropdown
    if customer_id:
        aud_df = query_df(
            """
            SELECT audience_id, title, name, department, position
            FROM target_audiences
            WHERE is_active IS TRUE AND customer_id=:cid
            ORDER BY name
            """,
            {"cid": customer_id},
        )

        def _fmt_audience(row) -> str:
            parts = []
            title = (str(row.title).strip() + " ") if pd.notna(row.title) and str(row.title).strip() else ""
            name  = str(row.name).strip()          if pd.notna(row.name)  else ""
            parts.append((title + name).strip())
            if pd.notna(row.department) and str(row.department).strip():
                parts.append(str(row.department).strip())
            if pd.notna(row.position) and str(row.position).strip():
                parts.append(str(row.position).strip())
            parts = [p for p in parts if p]
            return " || ".join(parts) if parts else name

        for r in aud_df.itertuples(index=False):
            label = _fmt_audience(r)
            aud_rows.append((label, int(r.audience_id), str(r.name).strip() if pd.notna(r.name) else ""))

        aud_labels = [""] + [x[0] for x in aud_rows]

        if len(aud_labels) == 1:
            st.warning("This customer has no Target Audiences.")

        # Always add "Other" at the end
        aud_labels.append("Other")

    aud_choice_label = st.selectbox(
        "Target Audience *",
        aud_labels,
        index=0,
        key=k("aud_sel"),
        disabled=(customer_id is None),
        help=None if customer_id else "Select a Customer first",
    )

    if customer_id and aud_choice_label and aud_choice_label not in ("", "Other"):
        for lbl, aid, raw_name in aud_rows:
            if lbl == aud_choice_label:
                audience_id     = aid
                aud_choice_name = raw_name
                break

    # If "Other" is selected → capture full new TA details
    if customer_id and aud_choice_label == "Other":
        st.markdown("##### ➕ New Target Audience Details")

        # Title (optional)
        other_ta_title = st.selectbox(
            "Title (optional)",
            TITLE_OPTIONS,   # ["", "Dr.", "Mr.", "Ms.", "Mrs.", "Prof.", "Eng.", "Other"]
            index=0,
            key=k("other_ta_title"),
        )

        # Name (required)
        other_ta_name = st.text_input(
            "Target Audience Name *",
            key=k("other_ta_name"),
            help="Name of the person you are meeting.",
        )

        dept_opts = [""] + dept_choices_base + ["Other"]
        pos_opts  = [""] + pos_choices_base  + ["Other"]

        # Department (required)
        other_ta_department = st.selectbox(
            "Department *",
            dept_opts,
            index=0,
            key=k("other_ta_dept_sel"),
            help="Select the department or choose 'Other'.",
        )

        # Position (required)
        other_ta_position = st.selectbox(
            "Position *",
            pos_opts,
            index=0,
            key=k("other_ta_pos_sel"),
            help="Select the position or choose 'Other'.",
        )

        # Phone (optional)
        other_ta_phone = st.text_input(
            "Phone # (optional)",
            key=k("other_ta_phone"),
            help="Optional – KSA mobile like 05XXXXXXXX.",
        )

        # Email (optional)
        other_ta_email = st.text_input(
            "Email (optional)",
            key=k("other_ta_email"),
            help="Optional – must be a valid email address.",
        )

    # -------- Home Visit block --------
    is_home_visit  = bool(aud_choice_label and aud_choice_label.strip().lower().startswith("home visit"))
    patient_name   = None
    patient_phone  = None
    serial_no      = None
    if is_home_visit:
        with st.container():
            patient_name  = st.text_input("Patient Name *", key=k("pat_name"))
            patient_phone = st.text_input("Patient Phone # *", key=k("pat_phone"))
            serial_no     = st.text_input("Device Serial # *", key=k("serial_no"))

    # =====================================================
    # SECTION 4 — Product & Business Line
    # =====================================================
    st.markdown("### 4️⃣ Product Details")

    # ---- Business Unit ----
    bu_df = query_df(
        """
        SELECT business_unit_id, name
        FROM business_units
        WHERE is_active IS TRUE
        ORDER BY name
        """
    )
    bu_names = [""] + bu_df["name"].tolist()

    if project_locked and proj_bu_name:
        st.session_state[k("bu_sel")] = proj_bu_name

    bu_choice = st.selectbox(
        "Business Unit *",
        bu_names,
        index=0,
        key=k("bu_sel"),
        disabled=project_locked,
        on_change=None if project_locked else _on_bu_change,
    )

    bu_id = None
    if project_locked and proj_bu_id:
        bu_id     = proj_bu_id
        bu_choice = proj_bu_name
    elif bu_choice:
        match = bu_df.loc[bu_df["name"] == bu_choice, "business_unit_id"]
        bu_id = int(match.iloc[0]) if not match.empty else None

    # ---- Category ----
    cat_df    = pd.DataFrame()
    cat_names = [""]

    if bu_id:
        cat_df = query_df(
            """
            SELECT DISTINCT category
            FROM business_lines
            WHERE is_active IS TRUE
              AND business_unit_id = :bid
              AND category IS NOT NULL
              AND trim(category) <> ''
            ORDER BY category
            """,
            {"bid": bu_id},
        )
        cat_names = [""] + cat_df["category"].tolist()

    if project_locked and proj_cat:
        st.session_state[k("cat_sel")] = proj_cat

    cat_choice = st.selectbox(
        "Category *",
        cat_names,
        index=0,
        key=k("cat_sel"),
        disabled=project_locked or bu_id is None,
        help=None if bu_id else "Select a Business Unit first",
    )
    if project_locked:
        cat_choice = proj_cat

    # ---- Business Line ----
    bl_df   = pd.DataFrame()
    bl_names = [""]

    bl_choice = ""
    business_line_id = None

    if bu_id and cat_choice:
        bl_df = query_df(
            """
            SELECT business_line_id, name
            FROM business_lines
            WHERE is_active IS TRUE
              AND business_unit_id = :bid
              AND category = :cat
            ORDER BY name
            """,
            {"bid": bu_id, "cat": cat_choice},
        )
        bl_names = [""] + bl_df["name"].tolist()

    if project_locked and proj_bl_name:
        st.session_state[k("bl_sel")] = proj_bl_name

    bl_choice = st.selectbox(
        "Business Line *",
        bl_names,
        index=0,
        key=k("bl_sel"),
        disabled=project_locked or bu_id is None or not cat_choice,
        on_change=None if project_locked else _on_line_change,
        help=None if (bu_id and cat_choice) else "Select a Category first",
    )

    if project_locked and proj_bl_id:
        business_line_id = proj_bl_id
        bl_choice        = proj_bl_name
    elif bu_id and cat_choice and bl_choice:
        match = bl_df.loc[bl_df["name"] == bl_choice, "business_line_id"]
        business_line_id = int(match.iloc[0]) if not match.empty else None

    # ---- Product (Article Number) ----
    prod_labels: list[str] = [""]
    prod_df = pd.DataFrame()
    product_id  = None
    prod_choice = ""
    prod_disabled = False

    if business_line_id:
        prod_df = query_df(
            """
            SELECT product_id, article_number, description
            FROM items
            WHERE is_active IS TRUE
              AND business_line_id = :blid
            ORDER BY COALESCE(article_number, product_id)
            """,
            {"blid": business_line_id},
        )
        prod_labels = [
            ""
        ] + [
            (
                f"{(r.article_number or r.product_id)} — {r.description}"
                if pd.notna(r.description) and str(r.description).strip()
                else f"{(r.article_number or r.product_id)}"
            )
            for r in prod_df.itertuples(index=False)
        ]

    # Seed fixed product if project has one
    prod_index = 0
    if project_locked and proj_prod_id and not prod_df.empty:
        label_to_pid = {}
        for r in prod_df.itertuples(index=False):
            label = (
                f"{(r.article_number or r.product_id)} — {r.description}"
                if pd.notna(r.description) and str(r.description).strip()
                else f"{(r.article_number or r.product_id)}"
            )
            label_to_pid[label] = r.product_id

        for lbl, pid in label_to_pid.items():
            if str(pid) == str(proj_prod_id) and lbl in prod_labels:
                prod_index = prod_labels.index(lbl)
                st.session_state[k("prod_sel")] = lbl
                break

        prod_disabled = True

    prod_choice = st.selectbox(
        "Article Number/Product (optional)",
        prod_labels,
        index=prod_index,
        key=k("prod_sel"),
        disabled=(business_line_id is None) or prod_disabled,
        help=None if business_line_id else "Select Business Line first",
    )

    if business_line_id and prod_choice:
        label_to_pid = {}
        for r in prod_df.itertuples(index=False):
            label = (
                f"{(r.article_number or r.product_id)} — {r.description}"
                if pd.notna(r.description) and str(r.description).strip()
                else f"{(r.article_number or r.product_id)}"
            )
            label_to_pid[label] = r.product_id
        product_id = label_to_pid.get(prod_choice)

    if project_locked and proj_prod_id and not product_id:
        product_id = proj_prod_id

    # =====================================================
    # SECTION 5 — Visit Details & Outcome
    # =====================================================
    st.markdown("### 5️⃣ Visit Details & Outcome")

    obj_df = query_df(
        """
        SELECT objective_id, name
        FROM objectives
        WHERE COALESCE(is_active, TRUE) IS TRUE
        ORDER BY name
        """
    )
    obj_names  = [""] + obj_df["name"].tolist()
    obj_choice = st.selectbox("Business Objective *", obj_names, index=0, key=k("obj_sel"))

    objective_id = None
    if obj_choice:
        match = obj_df.loc[obj_df["name"] == obj_choice, "objective_id"]
        objective_id = int(match.iloc[0]) if not match.empty else None

    is_shelf_movement = bool(obj_choice and ("shelf movement" in obj_choice.strip().lower()))
    notes             = st.text_area("Notes (optional)", key=k("notes"))

    allowed_evals = {"Positive", "Negative", "Neutral"}
    evaluation_choice = st.selectbox(
        "Evaluation *",
        [""] + sorted(list(allowed_evals)),
        index=0,
        key=k("eval_sel"),
    )
    evaluation_val = evaluation_choice if evaluation_choice in allowed_evals else None

    # ---------------- Shelf Movement grid ----------------
    shelf_df    = pd.DataFrame()
    shelf_editor = None
    if is_shelf_movement:
        st.subheader("🧾 Shelf Movement — Quantities Checked")
        if not bu_id:
            st.info("Select a Business Unit to load items.")
        elif not cat_choice:
            st.info("Select a Category to load items.")
        else:
            shelf_df = query_df(
                """
                SELECT i.product_id,
                       COALESCE(i.article_number, i.product_id) AS article_number,
                       COALESCE(i.description, '') AS description
                FROM items i
                JOIN business_lines bl ON bl.business_line_id = i.business_line_id
                WHERE i.is_active IS TRUE
                  AND bl.is_active IS TRUE
                  AND bl.business_unit_id = :bid
                  AND bl.category = :cat
                ORDER BY COALESCE(i.article_number, i.product_id)
                """,
                {"bid": bu_id, "cat": cat_choice},
            )
            if shelf_df.empty:
                st.warning("No active items found for this Category.")
            else:
                shelf_df = shelf_df.assign(qty_checked=pd.Series([None] * len(shelf_df)))
                shelf_editor = st.data_editor(
                    shelf_df,
                    key=k("sm_editor"),
                    width='stretch',
                    hide_index=True,
                    num_rows="fixed",
                    column_config={
                        "product_id":     st.column_config.TextColumn("Product ID", disabled=True),
                        "article_number": st.column_config.TextColumn("Article #",  disabled=True),
                        "description":    st.column_config.TextColumn("Description", disabled=True),
                        "qty_checked":    st.column_config.NumberColumn(
                            "Qty Checked",
                            help="Leave blank if not checked. Enter 0 if none on shelf.",
                            min_value=0,
                            step=1,
                        ),
                    },
                )

    # ---------------- Potential duplicate banner ----------------
    if customer_id:
        mins = recent_visit_minutes(uid, customer_id)
        if mins is not None and mins < DUP_MINUTES:
            st.info(
                f"You submitted for **{cust_choice}** {mins} minutes ago — potential duplicate."
            )

    # ---------------- Submit button ----------------
    inline_click = st.button(
        "Submit",
        type="primary",
        key=k("submit_btn_inline"),
        disabled=st.session_state[busy_key],
        help="Saves immediately. You’ll see a spinner while saving.",
    )

    if inline_click and not st.session_state[busy_key]:
        st.session_state[intent_key] = True
        st.session_state[busy_key]   = True
        st.rerun()

    if not st.session_state[intent_key]:
        return

    # ---------------- Process submission ----------------
    with st.spinner("Saving your visit…"):
        errors: list[str] = []

        if not region_choice:
            errors.append("Please choose a **Region**.")
        if not city_choice:
            errors.append("Please choose a **City**.")
        if not sector_choice:
            errors.append("Please choose a **Sector**.")
        if not customer_id:
            errors.append("Please choose a **Customer**.")

        # Target audience validation
        if not aud_choice_label:
            errors.append("Please choose a **Target Audience** for the selected customer.")
        elif aud_choice_label == "Other":
            # Required fields
            if not other_ta_name or not other_ta_name.strip():
                errors.append("For **Other Target Audience**, please enter **Target Audience Name**.")
            if not other_ta_department:
                errors.append("For **Other Target Audience**, please select a **Department**.")
            if not other_ta_position:
                errors.append("For **Other Target Audience**, please select a **Position**.")

            # Optional phone validation (KSA format)
            if other_ta_phone and other_ta_phone.strip():
                phone_clean = other_ta_phone.strip()
                if not re.fullmatch(r"(?:\+966|00966|0)?5\d{8}", phone_clean):
                    errors.append(
                        "For **Other Target Audience**, **Phone #** looks invalid "
                        "(expected KSA mobile like 05XXXXXXXX)."
                    )

            # Optional email validation
            if other_ta_email and other_ta_email.strip():
                email_clean = other_ta_email.strip()
                # simple email structure check
                if not re.fullmatch(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_clean):
                    errors.append("For **Other Target Audience**, **Email** looks invalid.")
        elif not audience_id:
            errors.append("Please choose a valid **Target Audience** for the selected customer.")

        if is_home_visit:
            if not patient_name:
                errors.append("For **Home Visit**, please enter **Patient Name**.")
            if not patient_phone:
                errors.append("For **Home Visit**, please enter **Patient Phone #**.")
            elif not re.fullmatch(r"(?:\+966|00966|0)?5\d{8}", patient_phone.strip()):
                errors.append("**Patient Phone #** looks invalid (expected KSA mobile like 05XXXXXXXX).")
            if not serial_no:
                errors.append("For **Home Visit**, please enter **Serial #**.")

        if not bu_id:
            errors.append("Please choose a **Business Unit**.")
        if not cat_choice:
            errors.append("Please choose a **Category**.")
        if not business_line_id:
            errors.append("Please choose a **Business Line**.")

        if objective_id is None:
            errors.append("Please choose a **Business Objective**.")
        if evaluation_val is None:
            errors.append("Please choose an **Evaluation** (Positive/Negative/Neutral/IDK).")

        # Shelf Movement validations
        shelf_lines_payload = None
        filled_rows = None

        if is_shelf_movement:
            if shelf_editor is None or shelf_editor.empty:
                errors.append(
                    "**Shelf Movement** grid is empty. Load items by selecting Business Unit and Category."
                )
            else:
                shelf_lines_payload = []
                any_qty = False
                invalid_qty_found = False
                negative_qty_found = False

                digit_map = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

                for _, row in shelf_editor.iterrows():
                    raw = row.get("qty_checked", None)
                    if raw is None:
                        continue

                    txt = str(raw).strip()
                    if txt == "" or txt.lower() == "none":
                        continue

                    txt = txt.translate(digit_map)
                    digits_only = "".join(ch for ch in txt if ch in "0123456789")

                    if digits_only == "":
                        invalid_qty_found = True
                        continue

                    qty = int(digits_only)

                    if qty < 0:
                        negative_qty_found = True
                        continue

                    any_qty = True
                    shelf_lines_payload.append(
                        {
                            "product_id": int(row["product_id"]),
                            "qty_checked": float(qty),
                        }
                    )

                filled_rows = pd.DataFrame(shelf_lines_payload)

                if negative_qty_found:
                    errors.append("Quantities in **Shelf Movement** cannot be negative.")

                if invalid_qty_found:
                    errors.append(
                        "Some values in **Shelf Movement** are not numeric or are out of range. "
                        "Please enter only valid numbers or leave blank."
                    )

                if not any_qty and not invalid_qty_found and not negative_qty_found:
                    errors.append(
                        "Enter at least **one** quantity in the **Shelf Movement** grid "
                        "(blank = not checked; 0 is allowed)."
                    )

        if errors:
            for msg in errors:
                st.error(msg)
            st.session_state[busy_key]   = False
            st.session_state[intent_key] = False
            return

        # ----- All validations passed → persist -----
        visit_row = {
            "user_id":             uid,
            "submitted_at_utc":    _utcnow(),
            "submitted_at_local":  _local_now_str(),
            "latitude":            lat,
            "longitude":           lon,
            "accuracy_m":          acc,
            "customer_id":         int(customer_id),
            "audience_id":         int(audience_id) if audience_id else None,
            "business_line_id":    int(business_line_id),
            "product_id":          (None if is_shelf_movement else product_id),
            "objective_id":        int(objective_id),
            "notes":               (notes.strip() if notes else None),
            "evaluation":          evaluation_val,
            "project_id":          int(selected_project_id) if selected_project_id else None,

            # New fields for "Other" Target Audience
            "other_audience_title":      (other_ta_title.strip() if other_ta_title else None) or None,
            "other_audience_name":       (other_ta_name.strip() if other_ta_name else None),
            "other_audience_department": (other_ta_department.strip() if other_ta_department else None),
            "other_audience_position":   (other_ta_position.strip() if other_ta_position else None),
            "other_audience_phone":      (other_ta_phone.strip() if other_ta_phone else None) or None,
            "other_audience_email":      (other_ta_email.strip() if other_ta_email else None) or None,
        }

        home_payload = None
        if is_home_visit:
            home_payload = {
                "patient_name":  patient_name,
                "patient_phone": patient_phone,
                "serial_no":     serial_no,
            }

        try:
            visit_id = insert_visit_atomic(visit_row, home_payload, shelf_lines_payload)

            # Power BI row
            def _article_from_label(lbl: str | None) -> str:
                if not lbl:
                    return ""
                return str(lbl).split(" — ", 1)[0].strip()

            shelf_lines_count = int(len(filled_rows)) if (is_shelf_movement and filled_rows is not None) else 0
            shelf_total_qty   = int(filled_rows["qty_checked"].sum()) if (is_shelf_movement and filled_rows is not None) else 0

            pbi_row = {
                "submitted_at_utc":   datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "submitted_at_local": datetime.now().isoformat(),
                "user_name":          str(u.get("name") or ""),
                "user_region":        str(u.get("region") or ""),
                "customer_name":      str(cust_choice or ""),
                "audience_name":      ("Home Visit" if is_home_visit else str(aud_choice_label or "")),
                "business_unit":      str(bu_choice or ""),
                "category":           str(cat_choice or ""),
                "business_line":      str(bl_choice or ""),
                "article_number": (
                    "" if is_shelf_movement else _article_from_label(
                        prod_choice if (business_line_id and prod_choice) else None
                    )
                ),
                "objective":          str(obj_choice or ""),
                "evaluation":         str(evaluation_val or ""),
                "latitude":           float(lat) if lat is not None else 0.0,
                "longitude":          float(lon) if lon is not None else 0.0,
                "accuracy_m": (
                    f"{acc:.1f}" if isinstance(acc, (int, float)) else (str(acc) if acc is not None else "")
                ),
                "notes":              (notes.strip() if notes else ""),
                "shelf_lines_count":  shelf_lines_count,
                "shelf_total_qty":    shelf_total_qty,
            }
            if is_home_visit:
                pbi_row.update({
                    "patient_name":  patient_name.strip(),
                    "patient_phone": patient_phone.strip(),
                    "serial_no":     serial_no.strip().upper(),
                })

            ok, err = push_visit_to_pbi(pbi_row)
            if not ok:
                st.warning(f"Saved, but Power BI push failed → {err}")
            else:
                st.toast("Pushed to Power BI ✅", icon="✅")

            # reset form
            st.session_state[nonce_key]        += 1
            st.session_state[geo_nonce_key]    += 1
            st.session_state.pop(geo_captured_key, None)
            st.session_state[saved_ok_key]      = True
            st.session_state[intent_key]        = False
            st.session_state[busy_key]          = False
            st.session_state[prev_proj_key]     = ""  # reset project tracker after successful submit
            st.rerun()

        except IntegrityError as e:
            emsg = str(e).lower()
            if (
                UniqueViolation and isinstance(e.orig, UniqueViolation)
            ) or (
                "duplicate key value violates unique constraint" in emsg
            ) or (
                "unique constraint" in emsg and "home_visits_serial_no" in emsg
            ):
                st.error("Serial # already exists. Please verify and try again.")
            else:
                st.error("Could not save your submission.")
                st.caption(str(e))
            st.session_state[intent_key] = False
            st.session_state[busy_key]   = False
        except Exception as e:
            st.error("Could not save your submission.")
            st.caption(str(e))
            st.session_state[intent_key] = False
            st.session_state[busy_key]   = False

# =============================
# Page — My Submissions
# =============================
def page_my_submissions():
    st.title("📄 My Submissions")
    set_current_page("my_submissions")
    u = st.session_state.user
    uid = int(u.get("user_id")) if u.get("user_id") is not None else int(u["id"])
    
    # --- Defensive fallbacks ---
    display_name = u.get("name") or u.get("email") or f"User #{u.get('user_id', '?')}"
    display_region = u.get("region") or "—"
    display_role = u.get("role") or "—"

    # --- Display info ---
    st.caption(f"Logged in as **{display_name}** · Region: **{display_region}** · Role: **{display_role}**")   

    sql = """
        SELECT v.visit_id,
               v.submitted_at_local,
               to_char(v.submitted_at_local, 'Day') AS day_name,
               c.account_name AS customer,
               ta.name AS audience,
               v.latitude, v.longitude, v.accuracy_m,
               i.article_number, i.description,
               bu.name AS business_unit,
               bl.name AS business_line,
               bl.category AS category,
               o.name AS objective,
               v.evaluation,
               v.notes,
               hv.patient_name, hv.patient_phone, hv.serial_no,
               -- Shelf movement aggregates
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
        JOIN business_lines bl        ON bl.business_line_id = v.business_line_id
        JOIN business_units bu        ON bu.business_unit_id = bl.business_unit_id
        JOIN objectives o             ON v.objective_id = o.objective_id
        LEFT JOIN home_visits hv      ON hv.visit_id = v.visit_id
        WHERE v.user_id = :uid
        ORDER BY v.visit_id DESC
    """

    df = query_df(sql, {"uid": uid})

    if df.empty:
        st.info("No submissions yet.")
    else:
        # ---- Format date ----
        if "submitted_at_local" in df.columns:
            df["submitted_at_local"] = (
                pd.to_datetime(df["submitted_at_local"], errors="coerce")
                .dt.strftime("%d/%m/%Y %H:%M")
            )

        # --- Create Google Maps URL column ---
        df["location_url"] = df.apply(
            lambda r: f"https://www.google.com/maps/search/{r['latitude']},{r['longitude']}?sa=X&ved=1t:242&ictx=111"
            if r["latitude"] and r["longitude"] else "",
            axis=1
        )

        # --- Reorder so Location is before latitude ---
        cols = df.columns.tolist()
        if "location_url" in cols and "latitude" in cols:
            cols.insert(cols.index("latitude"), cols.pop(cols.index("location_url")))
        df = df[cols]

        # Remove lat/long/accuracy from final output
        cols_to_remove = ["latitude", "longitude", "accuracy_m"]
        df_display = df.drop(columns=[c for c in cols_to_remove if c in df.columns])

        
        st.markdown(f"**Total: {len(df):,}**")
        st.dataframe(
            df_display,
            width='stretch',
            hide_index=True,
            column_config={
                "location_url": st.column_config.LinkColumn(
                    "Location",
                    help="Open location in Google Maps",
                    display_text="Location"
                )
            }
        )

        st.download_button(
            "Download CSV",
            df.to_csv(index=False).encode("utf-8-sig"),
            "my_submissions.csv",
            "text/csv"
        )

# =============================
# Page — User Settings
# =============================
def page_user_settings():

    st.title("👤 User Settings")
    
    set_current_page("user_settings")

    u = st.session_state.user
    uid = int(u["user_id"] if "user_id" in u else u["id"])

    # Load fresh user row (and BU name for display)
    me = query_df("""
        SELECT u.user_id, u.email, u.name, u.region, u.role, u.is_active,
               bu.name AS business_unit, u.password_hash
        FROM users u
        LEFT JOIN business_units bu ON bu.business_unit_id = u.business_unit_id
        WHERE u.user_id = :uid
    """, {"uid": uid})
    if me.empty:
        st.error("Could not load your profile.")
        return

    row = me.iloc[0]

    # Read-only profile block
    st.subheader("My Profile (read-only)")
    c1, c2 = st.columns(2)
    with c1:
        st.text_input("Name", value=row.get("name") or "", disabled=True)
        st.text_input("Email", value=row.get("email") or "", disabled=True)
        st.text_input("Region", value=row.get("region") or "", disabled=True)
    with c2:
        st.text_input("Role", value=row.get("role") or "", disabled=True)
        st.text_input("Business Unit", value=row.get("business_unit") or "", disabled=True)
        st.text_input("Status", value=("Active" if bool(row.get("is_active", True)) else "Inactive"), disabled=True)

    st.divider()

    # Change password form
    st.subheader("Change Password")
    with st.form("change_pw_form", clear_on_submit=True):
        old_pw = st.text_input("Current Password *", type="password")
        new_pw = st.text_input("New Password *", type="password", help="Min 8 chars, include a letter and a number.")
        new_pw2 = st.text_input("Confirm New Password *", type="password")
        submit = st.form_submit_button("Update Password", type="primary")

    # Validation + update (in field order)
    if submit:
        # 1) Old password present?
        if not old_pw:
            st.error("Please enter your current password.")
            st.stop()

        # 2) Verify old password
        if not pbkdf2_sha256.verify(old_pw, row["password_hash"]):
            st.error("Current password is incorrect.")
            st.stop()

        # 3) New password present?
        if not new_pw:
            st.error("Please enter a new password.")
            st.stop()

        # 4) Confirm present?
        if not new_pw2:
            st.error("Please confirm your new password.")
            st.stop()

        # 5) Match?
        if new_pw != new_pw2:
            st.error("New password and confirmation do not match.")
            st.stop()

        # 6) Strength checks
        if len(new_pw) < 8:
            st.error("New password must be at least 8 characters long.")
            st.stop()
        if not re.search(r"[A-Za-z]", new_pw) or not re.search(r"\d", new_pw):
            st.error("New password must include at least one letter and one number.")
            st.stop()

        # 7) Prevent reusing the same password
        if pbkdf2_sha256.verify(new_pw, row["password_hash"]):
            st.error("New password must be different from the current password.")
            st.stop()

        # 8) Save (PostgreSQL: use named params with exec_sql)
        try:
            new_hash = pbkdf2_sha256.hash(new_pw)
            exec_sql(
                "UPDATE users SET password_hash = :ph WHERE user_id = :uid",
                {"ph": new_hash, "uid": uid}
            )
            st.success("Password updated ✅")
        except Exception as e:
            st.error("Could not update password.")
            st.caption(str(e))

# =============================
# Page — Create Project
# =============================         
def page_create_project():
    st.title("📌 Create Project")
    set_current_page("create_project")

    PAGE_NS = "create_project"
    nonce_key    = f"_{PAGE_NS}_form_nonce"
    saved_ok_key = f"_{PAGE_NS}_saved_ok"
    busy_key     = f"_{PAGE_NS}_busy"
    intent_key   = f"_{PAGE_NS}_submit_intent"

    st.session_state.setdefault(nonce_key, 0)
    st.session_state.setdefault(busy_key, False)
    st.session_state.setdefault(intent_key, False)

    def k(name: str) -> str:
        return f"{PAGE_NS}/{name}_{st.session_state[nonce_key]}"

    # ---- cascade clear helpers ----
    def _on_region_change():
        for n in ("city_sel", "sector_sel", "cust_sel"):
            st.session_state.pop(k(n), None)

    def _on_city_change():
        for n in ("sector_sel", "cust_sel"):
            st.session_state.pop(k(n), None)

    def _on_sector_change():
        for n in ("cust_sel",):
            st.session_state.pop(k(n), None)

    def _on_bu_change():
        for n in ("cat_sel", "bl_sel", "prod_sel"):
            st.session_state.pop(k(n), None)

    def _on_line_change():
        for n in ("prod_sel",):
            st.session_state.pop(k(n), None)

    # ---- Resolve logged-in user (manager) ---
    u = st.session_state.get("user") or resolve_session_user()
    if not u:
        st.warning("Please sign in to continue.")
        st.stop()

    manager_id     = int(u.get("user_id") or u.get("id"))
    display_name   = u.get("name") or u.get("email") or f"User #{manager_id}"
    display_role   = u.get("role") or "—"
    display_region = u.get("region") or "—"

    st.caption(
        f"Logged in as **{display_name}** · Region: **{display_region}** · Role: **{display_role}**"
    )

    if st.session_state.pop(saved_ok_key, False):
        st.success("Project created ✅")

    # ---- Form fields ----
    st.markdown(
        '<div style="margin:.25rem 0 1rem 0;">'
        'Fields marked with <span style="color:#d00000;font-weight:700">*</span> are required.'
        '</div>',
        unsafe_allow_html=True,
    )

    # ---------------- Basic Info ----------------
    name = st.text_input("Project Name *", key=k("name"))
    description = st.text_area("Description (optional)", key=k("desc"))

    # Assign to (Reps only)
    reps_df = query_df("""
        SELECT user_id, name, email
        FROM users
        WHERE is_active IS TRUE
          AND role = 'rep'
        ORDER BY name
    """)
    rep_labels = [""]
    rep_map = {}
    for r in reps_df.itertuples(index=False):
        lbl = f"{r.name} ({r.email})" if getattr(r, "email", None) else r.name
        rep_labels.append(lbl)
        rep_map[lbl] = int(r.user_id)

    assign_to_label = st.selectbox(
        "Assign To (Rep) *",
        rep_labels,
        index=0,
        key=k("rep_sel")
    )
    assigned_to_id = rep_map.get(assign_to_label)

    # Dates
    planned_start_date = st.date_input("Planned Start Date *", key=k("psd"))
    planned_end_date   = st.date_input("Planned End Date *", key=k("ped"))

    # ---------------- Region → City → Sector → Customer ----------------
    reg_df = query_df("""
        SELECT DISTINCT region
        FROM customers
        WHERE is_active IS TRUE
          AND region IS NOT NULL AND trim(region) <> ''
        ORDER BY region
    """)
    region_opts = [""] + reg_df["region"].tolist()
    region_choice = st.selectbox(
        "Region *",
        region_opts,
        index=0,
        key=k("region_sel"),
        on_change=_on_region_change
    )

    if region_choice:
        city_df = query_df("""
            SELECT DISTINCT city
            FROM customers
            WHERE is_active IS TRUE
              AND region = :r
              AND city IS NOT NULL AND trim(city) <> ''
            ORDER BY city
        """, {"r": region_choice})
        city_opts = [""] + city_df["city"].tolist()
    else:
        city_df = pd.DataFrame(columns=["city"])
        city_opts = [""]

    city_choice = st.selectbox(
        "City *",
        city_opts,
        index=0,
        key=k("city_sel"),
        disabled=(not region_choice),
        on_change=_on_city_change,
        help=None if region_choice else "Select a Region first",
    )

    if region_choice and city_choice:
        sec_df = query_df("""
            SELECT DISTINCT sector
            FROM customers
            WHERE is_active IS TRUE
              AND region = :r
              AND city   = :c
              AND sector IS NOT NULL AND trim(sector) <> ''
            ORDER BY sector
        """, {"r": region_choice, "c": city_choice})
        sector_opts = [""] + sec_df["sector"].tolist()
    else:
        sec_df = pd.DataFrame(columns=["sector"])
        sector_opts = [""]

    sector_choice = st.selectbox(
        "Sector *",
        sector_opts,
        index=0,
        key=k("sector_sel"),
        disabled=(not (region_choice and city_choice)),
        on_change=_on_sector_change,
        help=None if (region_choice and city_choice) else "Select a City first",
    )

    # Customer
    if region_choice and city_choice and sector_choice:
        cust_df = query_df("""
            SELECT customer_id, account_name
            FROM customers
            WHERE is_active IS TRUE
              AND region = :r
              AND city   = :c
              AND sector = :s
            ORDER BY account_name
        """, {"r": region_choice, "c": city_choice, "s": sector_choice})
        cust_opts = [""] + cust_df["account_name"].tolist()
    else:
        cust_df = pd.DataFrame(columns=["customer_id", "account_name"])
        cust_opts = [""]

    cust_choice = st.selectbox(
        "Customer *",
        cust_opts,
        index=0,
        key=k("cust_sel"),
        disabled=(not (region_choice and city_choice and sector_choice)),
        help=None if (region_choice and city_choice and sector_choice) else "Select Sector first",
    )

    customer_id = None
    if cust_choice:
        match = cust_df.loc[cust_df["account_name"] == cust_choice, "customer_id"]
        customer_id = int(match.iloc[0]) if not match.empty else None

    # ---------------- Business Unit → Category → Business Line → Article ----------------
    bu_df = query_df("""
        SELECT business_unit_id, name
        FROM business_units
        WHERE is_active IS TRUE
        ORDER BY name
    """)
    bu_names = [""] + bu_df["name"].tolist()
    bu_choice = st.selectbox(
        "Business Unit *",
        bu_names,
        index=0,
        key=k("bu_sel"),
        on_change=_on_bu_change,
    )
    bu_id = None
    if bu_choice:
        match = bu_df.loc[bu_df["name"] == bu_choice, "business_unit_id"]
        bu_id = int(match.iloc[0]) if not match.empty else None

    # Category
    if bu_id:
        cat_df = query_df("""
            SELECT DISTINCT category
            FROM business_lines
            WHERE is_active IS TRUE
              AND business_unit_id = :bid
              AND category IS NOT NULL
              AND trim(category) <> ''
            ORDER BY category
        """, {"bid": bu_id})
        cat_names = [""] + cat_df["category"].tolist()
    else:
        cat_df = pd.DataFrame(columns=["category"])
        cat_names = [""]

    cat_choice = st.selectbox(
        "Category *",
        cat_names,
        index=0,
        key=k("cat_sel"),
        disabled=(bu_id is None),
        help=None if bu_id else "Select a Business Unit first",
    )

    # Business Line
    bl_df = pd.DataFrame()
    bl_names = [""]
    bl_choice = ""
    business_line_id = None
    if bu_id and cat_choice:
        bl_df = query_df("""
            SELECT business_line_id, name
            FROM business_lines
            WHERE is_active IS TRUE
              AND business_unit_id = :bid
              AND category = :cat
            ORDER BY name
        """, {"bid": bu_id, "cat": cat_choice})
        bl_names = [""] + bl_df["name"].tolist()

    bl_choice = st.selectbox(
        "Business Line *",
        bl_names,
        index=0,
        key=k("bl_sel"),
        disabled=(bu_id is None or not cat_choice),
        on_change=_on_line_change,
        help=None if (bu_id and cat_choice) else "Select a Category first",
    )
    if bu_id and cat_choice and bl_choice:
        match = bl_df.loc[bl_df["name"] == bl_choice, "business_line_id"]
        business_line_id = int(match.iloc[0]) if not match.empty else None

    # Article Number (optional)
    prod_labels = [""]
    prod_df = pd.DataFrame()
    product_id = None
    if business_line_id:
        prod_df = query_df("""
            SELECT product_id, article_number, description
            FROM items
            WHERE is_active IS TRUE
              AND business_line_id = :blid
            ORDER BY COALESCE(article_number, product_id)
        """, {"blid": business_line_id})
        for r in prod_df.itertuples(index=False):
            label = (
                f"{(r.article_number or r.product_id)} — {r.description}"
                if pd.notna(r.description) and str(r.description).strip()
                else f"{(r.article_number or r.product_id)}"
            )
            prod_labels.append(label)

    prod_choice = st.selectbox(
        "Article Number (Product) — optional",
        prod_labels,
        index=0,
        key=k("prod_sel"),
        disabled=(business_line_id is None),
        help=None if business_line_id else "Select Business Line first",
    )
    if business_line_id and prod_choice:
        label_to_pid = {}
        for r in prod_df.itertuples(index=False):
            label = (
                f"{(r.article_number or r.product_id)} — {r.description}"
                if pd.notna(r.description) and str(r.description).strip()
                else f"{(r.article_number or r.product_id)}"
            )
            label_to_pid[label] = r.product_id
        product_id = label_to_pid.get(prod_choice)  # may be None (optional)

    # ---------------- Project Objective (with “Other” last & inactive custom) ----------------
    pobj_df = query_df("""
        SELECT project_objective_id, name
        FROM project_objectives
        WHERE COALESCE(is_active, TRUE) IS TRUE
        ORDER BY name
    """)
    existing_obj_names = pobj_df["name"].tolist()

    # "" (blank) + all active objectives + "Other" at the END
    pobj_names = [""] + existing_obj_names + ["Other"]

    pobj_choice = st.selectbox(
        "Project Objective *",
        pobj_names,
        index=0,
        key=k("pobj_sel"),
    )

    project_objective_id = None
    custom_objective_text = None

    if pobj_choice == "Other":
        custom_objective_text = st.text_input(
            "Specify Objective *",
            key=k("custom_obj"),
            placeholder="Enter custom project objective..."
        )
    elif pobj_choice:
        match = pobj_df.loc[pobj_df["name"] == pobj_choice, "project_objective_id"]
        project_objective_id = int(match.iloc[0]) if not match.empty else None

    # ---------------- Submit button (sticky / dedupe) ----------------
    inline_click = st.button(
        "Create Project",
        type="primary",
        key=k("submit_btn"),
        disabled=st.session_state[busy_key],
        help="Saves immediately. You’ll see a spinner while saving."
    )

    if inline_click and not st.session_state[busy_key]:
        st.session_state[intent_key] = True
        st.session_state[busy_key]   = True
        st.rerun()

    if not st.session_state[intent_key]:
        return

    # ---------------- Validation + Save ----------------
    errors = []

    if not name.strip():
        errors.append("Please enter a **Project Name**.")
    if assigned_to_id is None:
        errors.append("Please choose an **Assign To (Rep)**.")
    if planned_end_date < planned_start_date:
        errors.append("**Planned End Date** cannot be before **Planned Start Date**.")
    if not region_choice:
        errors.append("Please choose a **Region**.")
    if not city_choice:
        errors.append("Please choose a **City**.")
    if not sector_choice:
        errors.append("Please choose a **Sector**.")
    if not customer_id:
        errors.append("Please choose a **Customer**.")
    if not bu_id:
        errors.append("Please choose a **Business Unit**.")
    if not cat_choice:
        errors.append("Please choose a **Category**.")
    if not business_line_id:
        errors.append("Please choose a **Business Line**.")

    # Objective validation
    if pobj_choice == "":
        errors.append("Please choose a **Project Objective**.")
    elif pobj_choice == "Other" and (not custom_objective_text or not custom_objective_text.strip()):
        errors.append("Please enter the **custom Objective**.")
    elif pobj_choice != "Other" and project_objective_id is None:
        errors.append("Please choose a valid **Project Objective**.")

    if errors:
        for msg in errors:
            st.error(msg)
        st.session_state[busy_key] = False
        st.session_state[intent_key] = False
        return

    with st.spinner("Creating project…"):
        # If manager entered custom objective, insert it into project_objectives as inactive
        if pobj_choice == "Other":
            try:
                new_obj_name = custom_objective_text.strip()
                # Avoid duplicates (case-insensitive, any active/inactive)
                existing = query_df(
                    """
                    SELECT project_objective_id
                    FROM project_objectives
                    WHERE lower(trim(name)) = lower(trim(:nm))
                    """,
                    {"nm": new_obj_name},
                )
                if not existing.empty:
                    project_objective_id = int(existing["project_objective_id"].iloc[0])
                else:
                    res = query_df(
                        """
                        INSERT INTO project_objectives (name, is_active)
                        VALUES (:n, FALSE)
                        RETURNING project_objective_id
                        """,
                        {"n": new_obj_name},
                    )
                    project_objective_id = int(res.iloc[0]["project_objective_id"])
            except Exception as e:
                st.error("Could not save the custom project objective.")
                st.caption(str(e))
                st.session_state[intent_key] = False
                st.session_state[busy_key] = False
                return

        project_row = {
            "name": name.strip(),
            "description": (description.strip() if description else None),
            "assigned_by_id": manager_id,
            "assigned_to_id": assigned_to_id,
            "business_line_id": int(business_line_id),
            "product_id": product_id,  # can be None
            "customer_id": int(customer_id),
            "planned_start_date": planned_start_date,
            "planned_end_date": planned_end_date,
            "actual_end_date": None,
            "status": "Not Started",
            "project_objective_id": int(project_objective_id),
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

        try:
            pid = insert_project(project_row)  # returns project_id
            st.session_state[nonce_key]    += 1
            st.session_state[saved_ok_key]  = True
            st.session_state[intent_key]    = False
            st.session_state[busy_key]      = False
            st.success("Project created successfully ✅")
            st.rerun()
        except Exception as e:
            st.error("Could not create the project.")
            st.caption(str(e))
            st.session_state[intent_key] = False
            st.session_state[busy_key] = False

# =============================
# Page — Projects (List + Visit Details)
# =============================

def page_project_view():
    import pandas as pd
    from datetime import datetime

    PAGE_NS = "projects_basic"
    set_current_page(PAGE_NS)

    def k(name: str) -> str:
        return f"{PAGE_NS}/{name}"

    # --- Resolve logged-in user ---
    u = st.session_state.get("user") or resolve_session_user()
    if not u:
        st.warning("Please sign in to continue.")
        st.stop()

    uid  = int(u.get("user_id") or u.get("id"))
    role = (u.get("role") or "").lower().strip()
    display_name   = u.get("name") or u.get("email") or f"User #{uid}"
    display_role   = u.get("role") or "—"
    display_region = u.get("region") or "—"

    st.title("📂 Projects")
    st.caption(
        f"Logged in as **{display_name}** · Region: **{display_region}** · Role: **{display_role}**"
    )

    # --- Simple view state: list or detail ---
    st.session_state.setdefault(k("mode"), "list")
    st.session_state.setdefault(k("selected_project_id"), None)

    mode = st.session_state[k("mode")]

    # =========================================
    # 1) Base projects query (role-scoped)
    # =========================================
    where_clauses = ["1=1"]
    params: dict[str, object] = {}

    if role == "rep":
        where_clauses.append("p.assigned_to_id = :uid")
        params["uid"] = uid
    elif role == "manager":
        where_clauses.append("p.assigned_by_id = :uid")
        params["uid"] = uid
    elif role == "admin":
        # see all projects
        pass
    else:
        # fallback: only projects where user is involved
        where_clauses.append("(p.assigned_to_id = :uid OR p.assigned_by_id = :uid)")
        params["uid"] = uid

    projects_df = query_df(
        f"""
        WITH visit_agg AS (
            SELECT
                project_id,
                COUNT(*)                AS total_visits,
                MAX(submitted_at_local) AS last_visit_at
            FROM visits
            WHERE project_id IS NOT NULL
            GROUP BY project_id
        )
        SELECT
            p.project_id,
            p.name                AS project_name,
            p.description,
            p.status,
            p.planned_start_date,
            p.planned_end_date,
            p.actual_end_date,
            p.assigned_to_id,
            p.assigned_by_id,
            c.customer_id,
            c.account_name        AS customer_name,
            c.region,
            c.city,
            c.sector,
            bl.business_line_id,
            bl.name               AS business_line,
            bl.category,
            bu.business_unit_id,
            bu.name               AS business_unit,
            i.product_id,
            i.article_number,
            i.description         AS product_description,
            COALESCE(va.total_visits, 0)     AS total_visits,
            va.last_visit_at,
            uto.name              AS assigned_to_name,
            uba.name              AS assigned_by_name
        FROM projects p
        JOIN customers          c  ON c.customer_id           = p.customer_id
        JOIN business_lines     bl ON bl.business_line_id     = p.business_line_id
        LEFT JOIN business_units bu ON bu.business_unit_id    = bl.business_unit_id
        LEFT JOIN items          i  ON i.product_id           = p.product_id
        LEFT JOIN visit_agg      va ON va.project_id          = p.project_id
        LEFT JOIN users          uto ON uto.user_id           = p.assigned_to_id
        LEFT JOIN users          uba ON uba.user_id           = p.assigned_by_id
        WHERE {" AND ".join(where_clauses)}
        ORDER BY p.planned_end_date NULLS LAST, p.project_id DESC
        """,
        params,
    )

    if projects_df.empty:
        st.info("No projects assigned.")
        return

    # Normalize datetime/date columns for display
    for col in ["planned_start_date", "planned_end_date", "actual_end_date", "last_visit_at"]:
        if col in projects_df.columns:
            projects_df[col] = pd.to_datetime(projects_df[col], errors="coerce")

    # Small helper for date formatting (DD/MM/YYYY)
    def _fmt_date(val):
        if pd.isna(val):
            return "—"
        try:
            return pd.to_datetime(val).strftime("%d/%m/%Y")
        except Exception:
            return str(val)

    # =====================================================
    # MODE: LIST — show simple table + choose project
    # =====================================================
    if mode == "list":
        st.subheader("Projects List")

        # Very light search (optional)
        search_text = st.text_input(
            "Search (optional)",
            placeholder="Search by project or customer name...",
            key=k("search"),
        ).strip()

        df_list = projects_df.copy()

        if search_text:
            mask = (
                df_list["project_name"].str.contains(search_text, case=False, na=False)
                | df_list["customer_name"].str.contains(search_text, case=False, na=False)
            )
            df_list = df_list[mask]

        if df_list.empty:
            st.warning("No projects match your search.")
            return

        # Build a clean table for display
        table = df_list[[
            "project_id",
            "project_name",
            "customer_name",
            "status",
            "planned_end_date",
            "total_visits",
            "last_visit_at",
        ]].copy()

        table.rename(
            columns={
                "project_id": "ID",
                "project_name": "Project",
                "customer_name": "Customer",
                "status": "Status",
                "planned_end_date": "Planned End",
                "total_visits": "Visits",
                "last_visit_at": "Last Visit",
            },
            inplace=True,
        )

        table["Planned End"] = table["Planned End"].apply(_fmt_date)
        table["Last Visit"]  = table["Last Visit"].apply(_fmt_date)

        st.dataframe(
            table,
            width='stretch',
            hide_index=True,
        )

        # --- Simple select + button to view visits ---
        st.markdown("### View Project Visits")

        proj_options = [
            f"{int(row.project_id)} — {row.project_name} ({row.customer_name})"
            for row in df_list.itertuples(index=False)
        ]
        proj_id_map = {
            label: int(row.project_id)
            for label, row in zip(proj_options, df_list.itertuples(index=False))
        }

        default_idx = 0
        current_selected = st.session_state.get(k("selected_project_id"))
        if current_selected is not None:
            for i, row in enumerate(df_list.itertuples(index=False)):
                if int(row.project_id) == int(current_selected):
                    default_idx = i
                    break

        selected_label = st.selectbox(
            "Select a project:",
            options=proj_options,
            index=default_idx,
            key=k("project_select"),
        )
        selected_project_id = proj_id_map[selected_label]

        if st.button("🔍 View Visits for Selected Project", key=k("view_btn")):
            st.session_state[k("selected_project_id")] = int(selected_project_id)
            st.session_state[k("mode")] = "detail"
            st.rerun()

        return  # end of list mode

    # =====================================================
    # MODE: DETAIL — show single project + visits
    # =====================================================
    st.subheader("Project Details & Visits")

    pid = st.session_state.get(k("selected_project_id"))
    if pid is None:
        st.info("No project selected. Go back to the projects list.")
        if st.button("⬅ Back to Projects List", key=k("back_no_pid")):
            st.session_state[k("mode")] = "list"
            st.rerun()
        return

    # Back button
    if st.button("⬅ Back to Projects List", key=k("back_btn")):
        st.session_state[k("mode")] = "list"
        st.rerun()

    # Find the project row
    proj_rows = projects_df[projects_df["project_id"] == pid]
    if proj_rows.empty:
        st.error("Selected project not found.")
        return

    proj = proj_rows.iloc[0]

    # ---- Project summary ----
    st.markdown(f"### {proj['project_name']}")
    if isinstance(proj.get("description"), str) and proj["description"].strip():
        st.write(proj["description"])

    st.caption(
        f"Customer: **{proj['customer_name']}** · "
        f"Region: {proj.get('region', '—')} · "
        f"City: {proj.get('city', '—')} · "
        f"Sector: {proj.get('sector', '—')}"
    )
    st.caption(
        f"BU: {proj.get('business_unit', '—')} · "
        f"Business Line: {proj.get('business_line', '—')} · "
        f"Category: {proj.get('category', '—')}"
    )
    if isinstance(proj.get("article_number"), str) and proj["article_number"].strip():
        st.caption(
            f"Product: {proj['article_number']} — "
            f"{proj.get('product_description', '')}"
        )

    col1, col2, col3 = st.columns(3)
    col1.metric("Status", proj["status"] or "—")
    col2.metric("Planned End", _fmt_date(proj["planned_end_date"]))
    col3.metric("Total Visits", int(proj.get("total_visits", 0) or 0))

    # ---- Visit history ----
    st.markdown("---")
    st.markdown("#### Visit History")

    visits_df = query_df(
        """
        SELECT
            v.visit_id,
            v.submitted_at_local,
            u.name      AS rep_name,
            c.account_name AS customer_name,
            o.name      AS objective,
            v.evaluation,
            v.notes,
            v.latitude,
            v.longitude,
            v.accuracy_m
        FROM visits v
        LEFT JOIN users      u ON u.user_id      = v.user_id
        LEFT JOIN customers  c ON c.customer_id  = v.customer_id
        LEFT JOIN objectives o ON o.objective_id = v.objective_id
        WHERE v.project_id = :pid
        ORDER BY v.submitted_at_local DESC
        """,
        {"pid": int(pid)},
    )

    if visits_df.empty:
        st.info("No visits linked to this project yet.")
        return

    # Submitted date/time: DD/MM/YYYY HH:MM
    visits_df["submitted_at_local"] = pd.to_datetime(
        visits_df["submitted_at_local"], errors="coerce"
    ).dt.strftime("%d/%m/%Y %H:%M")

    # Create Google Maps URL column (like My Submissions)
    visits_df["location_url"] = visits_df.apply(
        lambda r: (
            f"https://www.google.com/maps/search/{r['latitude']},{r['longitude']}?sa=X&ved=1t:242&ictx=111"
            if pd.notna(r["latitude"]) and pd.notna(r["longitude"])
            else ""
        ),
        axis=1,
    )

    # Remove raw lat/long/accuracy from the display
    cols_to_remove = ["latitude", "longitude", "accuracy_m"]
    visits_clean = visits_df.drop(
        columns=[c for c in cols_to_remove if c in visits_df.columns]
    )

    # Prepare display table with renamed columns
    visits_display = visits_clean.rename(
        columns={
            "visit_id": "Visit ID",
            "submitted_at_local": "Date/Time",
            "rep_name": "Rep",
            "customer_name": "Customer",
            "objective": "Objective",
            "evaluation": "Evaluation",
            "notes": "Notes",
            "location_url": "Location",
        }
    )

    visits_display = visits_display[
        ["Visit ID", "Date/Time", "Rep", "Customer", "Objective", "Evaluation", "Notes", "Location"]
    ]

    st.dataframe(
        visits_display,
        width='stretch',
        hide_index=True,
        column_config={
            "Location": st.column_config.LinkColumn(
                "Location",
                help="Open location in Google Maps",
                display_text="Location",
            )
        },
    )

# =============================
# Page — Project Management
# =============================  
import pandas as pd
import streamlit as st
from datetime import datetime
from sqlalchemy import text

# ---------------- Timezone: Riyadh local time ----------------
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Asia/Riyadh")
except Exception:
    LOCAL_TZ = None  # fallback if zoneinfo not available

def local_now():
    """
    Return a naive datetime representing Riyadh local time (Asia/Riyadh).
    This is what will be saved into the DB for created_at/updated_at/changed_at.
    """
    if LOCAL_TZ is not None:
        return datetime.now(LOCAL_TZ).replace(tzinfo=None)
    return datetime.now()  # fallback, still local server time


# ---------------- Constants + page namespace ----------------
PROJECT_STATUSES = [
    "Not Started",
    "Open",
    "Postponed",
    "Completed",
    "Cancelled",
]

PAGE_NS = "project_mgmt"
st.session_state.setdefault(f"{PAGE_NS}_nonce", 0)

def k(name: str) -> str:
    """Key helper with nonce so fields reset when nonce increments."""
    return f"{PAGE_NS}/{name}_{st.session_state[f'{PAGE_NS}_nonce']}"


# ---------------- DB helpers ----------------
def _fetch_projects_for_management(manager_user: dict) -> pd.DataFrame:
    """
    Projects visible to the current manager/admin.
    Admin: all projects
    Manager: only projects where they are assigned_by_id.
    """
    role = (manager_user.get("role") or "").lower().strip()
    params = {}
    where = "1=1"

    if role == "manager":
        where += " AND p.assigned_by_id = :uid"
        params["uid"] = int(manager_user.get("user_id") or manager_user.get("id"))

    sql = f"""
        WITH visit_counts AS (
            SELECT project_id, COUNT(*)::int AS total_visits
            FROM visits
            WHERE project_id IS NOT NULL
            GROUP BY project_id
        )
        SELECT
            p.project_id,
            p.name,
            (p.project_id::text || '. ' || p.name) AS adj_name,
            p.description,
            p.status,
            p.planned_start_date,
            p.planned_end_date,
            p.actual_end_date,
            u_to.name AS rep_name,
            c.account_name AS customer_name,
            bl.name AS business_line_name,
            po.name AS objective_name,
            COALESCE(vc.total_visits, 0) AS total_visits
        FROM projects p
        JOIN users u_to        ON u_to.user_id = p.assigned_to_id
        JOIN customers c       ON c.customer_id = p.customer_id
        JOIN business_lines bl ON bl.business_line_id = p.business_line_id
        JOIN project_objectives po ON po.project_objective_id = p.project_objective_id
        LEFT JOIN visit_counts vc  ON vc.project_id = p.project_id
        WHERE {where}
        ORDER BY p.project_id DESC
    """
    return query_df(sql, params)


def _fetch_project_row(project_id: int):
    df = query_df(
        """
        SELECT 
            p.*,
            u.name AS assigned_by_name
        FROM projects p
        LEFT JOIN users u ON u.user_id = p.assigned_by_id
        WHERE p.project_id = :pid
        """,
        {"pid": project_id},
    )
    if df.empty:
        return None
    return df.iloc[0].to_dict()

def _fetch_project_history(project_id: int) -> pd.DataFrame:
    """
    Minimal change history for a project (most recent first).
    """
    sql = """
        SELECT
            e.event_id,
            e.changed_at,
            u.name AS changed_by_name,
            e.note,
            string_agg(
                d.field_name || ': ' ||
                COALESCE(d.old_value, 'NULL') || ' → ' ||
                COALESCE(d.new_value, 'NULL'),
                E'\n'
                ORDER BY d.detail_id
            ) AS changes_summary
        FROM project_change_events e
        JOIN users u ON u.user_id = e.changed_by_id
        LEFT JOIN project_change_details d ON d.event_id = e.event_id
        WHERE e.project_id = :pid
        GROUP BY e.event_id, e.changed_at, u.name, e.note
        ORDER BY e.changed_at DESC
        LIMIT 50
    """
    return query_df(sql, {"pid": project_id})


def _update_project_with_history(
    project_id: int,
    new_values: dict,
    changed_by_id: int,
    change_note: str,
):
    """
    Compare existing row with new_values for:
      - name
      - description
      - planned_start_date
      - planned_end_date
      - status
      - actual_end_date

    If anything changed:
      1. Insert into project_change_events (with local Riyadh time)
      2. Insert per-field rows into project_change_details
      3. Update projects (with updated_at in local Riyadh time)
    """
    cur = _fetch_project_row(project_id)
    if not cur:
        raise ValueError("Project not found")

    fields_to_track = [
        "name",
        "description",
        "planned_start_date",
        "planned_end_date",
        "status",
        "actual_end_date",
    ]

    def _norm(v):
        if v is None:
            return None
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return str(v)

    changes = []
    for fld in fields_to_track:
        old_val = cur.get(fld)
        new_val = new_values.get(fld)
        old_val_str = _norm(old_val)
        new_val_str = _norm(new_val)

        if old_val_str != new_val_str:
            changes.append(
                {
                    "field_name": fld,
                    "old_value": old_val_str,
                    "new_value": new_val_str,
                }
            )

    if not changes:
        # Nothing changed
        return

    if not change_note or not change_note.strip():
        raise ValueError("Change note is required when modifying a project.")

    now_local = local_now()

    with engine.begin() as conn:
        # 1) Insert event in local (Riyadh) time
        res = conn.execute(
            text(
                """
                INSERT INTO project_change_events (
                    project_id,
                    changed_by_id,
                    changed_at,
                    note
                )
                VALUES (:pid, :uid, :ts, :note)
                RETURNING event_id
                """
            ),
            {
                "pid": project_id,
                "uid": changed_by_id,
                "ts": now_local,
                "note": change_note.strip(),
            },
        )
        event_id = res.scalar_one()

        # 2) Insert details
        for ch in changes:
            conn.execute(
                text(
                    """
                    INSERT INTO project_change_details (
                        event_id,
                        field_name,
                        old_value,
                        new_value
                    )
                    VALUES (:eid, :field_name, :old_value, :new_value)
                    """
                ),
                {
                    "eid": event_id,
                    "field_name": ch["field_name"],
                    "old_value": ch["old_value"],
                    "new_value": ch["new_value"],
                },
            )

        # 3) Update project row (with updated_at in local Riyadh time)
        conn.execute(
            text(
                """
                UPDATE projects
                SET
                    name               = :name,
                    description        = :description,
                    planned_start_date = :planned_start_date,
                    planned_end_date   = :planned_end_date,
                    actual_end_date    = :actual_end_date,
                    status             = :status,
                    updated_at         = :updated_at
                WHERE project_id = :pid
                """
            ),
            {
                "pid": project_id,
                "name": new_values["name"],
                "description": new_values.get("description"),
                "planned_start_date": new_values["planned_start_date"],
                "planned_end_date": new_values["planned_end_date"],
                "actual_end_date": new_values.get("actual_end_date"),
                "status": new_values["status"],
                "updated_at": now_local,
            },
        )


# ---------------- Main Page: Project Management (Panel Style) ----------------
def page_project_management():
    import pandas as pd
    from sqlalchemy import text

    st.title("🛠 Project Management")
    set_current_page("project_management")

    # --- Auth & role check ---
    u = st.session_state.get("user") or resolve_session_user()
    if not u:
        st.warning("Please sign in to continue.")
        st.stop()

    role = (u.get("role") or "").lower().strip()
    if role not in ("manager", "admin"):
        st.warning("You do not have access to this page.")
        st.stop()

    manager_id = int(u.get("user_id") or u.get("id"))

    # --- Defensive fallbacks ---
    display_name = u.get("name") or u.get("email") or f"User #{u.get('user_id', '?')}"
    display_region = u.get("region") or "—"
    display_role = u.get("role") or "—"

    # -- Formatter --
    def fmt_date(val):
        if val is None or val == "" or pd.isna(val):
            return "—"
        try:
            return pd.to_datetime(val).strftime("%d/%m/%Y")
        except:
            return str(val)

    # --- Display info ---
    st.caption(
        f"Logged in as **{display_name}** · Region: **{display_region}** · Role: **{display_role}**"
    )

    # --- Load projects for this manager/admin ---
    df = _fetch_projects_for_management(u)
    if df.empty:
        st.info("No projects found.")
        return

    # ===================== Panel 1 — Select Project =====================
    st.markdown("### 1️⃣ Select Project")

    with st.expander("Filter projects", expanded=False):
        col_f1, col_f2, col_f3 = st.columns(3)

        with col_f1:
            status_filter = st.multiselect(
                "Status", PROJECT_STATUSES, default=[], key="pm_status_filter"
            )

        with col_f2:
            rep_filter = st.multiselect(
                "Assigned To",
                sorted(df["rep_name"].dropna().unique().tolist()),
                default=[],
                key="pm_rep_filter",
            )

        with col_f3:
            search_text = st.text_input(
                "Search",
                "",
                key="pm_search_text",
                placeholder="Project or customer name…",
            )

        fdf = df.copy()
        if status_filter:
            fdf = fdf[fdf["status"].isin(status_filter)]
        if rep_filter:
            fdf = fdf[fdf["rep_name"].isin(rep_filter)]
        if search_text.strip():
            s = search_text.lower().strip()
            fdf = fdf[
                fdf["name"].str.lower().str.contains(s)
                | fdf["customer_name"].str.lower().str.contains(s)
            ]

    if fdf.empty:
        st.info("No projects match your filters.")
        return

    proj_labels = []
    id_list = []
    for r in fdf.itertuples(index=False):
        lbl = f"{r.adj_name} — {r.customer_name}"
        proj_labels.append(lbl)
        id_list.append(int(r.project_id))

    options = [""] + proj_labels
    label_to_id = dict(zip(proj_labels, id_list))

    sel_label = st.selectbox(
        "Project", options, index=0, key=k("project_sel")
    )

    if not sel_label:
        st.info("Select a project above to continue.")
        return

    selected_pid = label_to_id.get(sel_label)
    if not selected_pid:
        st.error("Could not resolve selected project.")
        return

    cur = _fetch_project_row(selected_pid)
    if not cur:
        st.error("Project not found.")
        return

    row_match = fdf.loc[fdf["project_id"] == selected_pid]
    row = row_match.iloc[0] if not row_match.empty else None

    # ===================== Panel 2 — Project Summary =====================
    st.markdown("---")
    st.markdown("### 2️⃣ Project Summary")

    ps = fmt_date(cur.get("planned_start_date"))
    pe = fmt_date(cur.get("planned_end_date"))
    ae = fmt_date(cur.get("actual_end_date"))

    c1, c2 = st.columns(2)

    with c1:
        st.markdown(
            f"""
            **Project Name:** {cur['name']}  
            **Assigned By:** {cur.get('assigned_by_name') or '—'}  
            **Assigned To:** {row.rep_name if row is not None else '—'}  
            **Customer:** {row.customer_name if row is not None else '—'}  
            **Business Line:** {row.business_line_name if row is not None else '—'}  
            """
        )

    with c2:
        st.markdown(
            f"""
            **Status:** `{cur.get('status')}`  
            **Planned:** {ps} → {pe}  
            **Actual End:** {ae}  
            **Total Visits:** {int(row.total_visits) if row is not None else 0}  
            """
        )

    # View-only mode for completed/cancelled
    is_view_only = (cur.get("status") in ("Completed", "Cancelled")) and role != "admin"

    if is_view_only:
        st.markdown("---")
        st.info(f"This project is **{cur.get('status')}** and view-only.")
        return

    # ===================== Panel 3 — Edit Details & Status =====================
    st.markdown("---")
    st.markdown("### 3️⃣ Edit Details & Status")

    name_key = f"pm_name_{selected_pid}"
    desc_key = f"pm_desc_{selected_pid}"
    status_key = f"pm_status_{selected_pid}"
    aed_key = f"pm_aed_{selected_pid}"
    aed_dis_key = f"pm_aed_dis_{selected_pid}"
    note_key = f"pm_note_{selected_pid}"

    col1, col2 = st.columns(2)

    with col1:
        new_name = st.text_input(
            "Project Name *",
            value=cur["name"],
            key=name_key,
        )

        new_desc = st.text_area(
            "Description",
            value=cur.get("description") or "",
            key=desc_key,
        )

        # Planned dates removed from editing (shown only above)

    with col2:
        status_index = (
            PROJECT_STATUSES.index(cur.get("status"))
            if cur.get("status") in PROJECT_STATUSES
            else 0
        )

        new_status = st.selectbox(
            "Status *",
            PROJECT_STATUSES,
            index=status_index,
            key=status_key,
        )

        default_aed = (
            cur.get("actual_end_date")
            if cur.get("actual_end_date")
            else local_now().date()
        )

        if new_status == "Completed":
            new_aed = st.date_input(
                "Actual End Date *",
                value=default_aed,
                key=aed_key,
            )
        else:
            new_aed = None
            st.date_input(
                "Actual End Date",
                value=default_aed,
                key=aed_dis_key,
                disabled=True,
            )

    st.markdown("### 4️⃣ Change Note")
    change_note = st.text_area(
        "Change Note *",
        placeholder="Why are you changing this project?",
        key=note_key,
    )

    # ===================== Save =====================
    if st.button("💾 Save Changes", type="primary", key=f"pm_save_btn_{selected_pid}"):

        errs = []

        if not new_name.strip():
            errs.append("Please enter a **Project Name**.")

        if new_status == "Completed" and not new_aed:
            errs.append("Completed project requires an **Actual End Date**.")

        today = local_now().date()
        if new_status == "Completed" and new_aed and new_aed > today:
            errs.append("Actual End Date cannot be in the future.")

        if not change_note.strip():
            errs.append("Change Note is required.")

        if errs:
            for e in errs:
                st.error(e)
            return

        new_vals = {
            "name": new_name.strip(),
            "description": new_desc.strip() or None,
            "status": new_status,
            "actual_end_date": new_aed if new_status == "Completed" else None,
        }

        try:
            _update_project_with_history(selected_pid, new_vals, manager_id, change_note)
            st.success("Project updated successfully.")
            st.rerun()

        except Exception as e:
            st.error("Could not update project.")
            st.caption(str(e))

    # ===================== Panel 5 — History =====================
    st.markdown("---")
    with st.expander("📜 View Change History", expanded=False):
        hist_df = _fetch_project_history(selected_pid)
        if hist_df.empty:
            st.info("No history yet.")
        else:
            if "changed_at" in hist_df.columns:
                hist_df["changed_at"] = pd.to_datetime(
                    hist_df["changed_at"], errors="coerce"
                ).dt.strftime("%d/%m/%Y %H:%M")

            st.dataframe(
                hist_df.rename(
                    columns={
                        "changed_at": "When",
                        "changed_by_name": "By",
                        "note": "Note",
                        "changes_summary": "Changes",
                    }
                ),
                width='stretch',
                hide_index=True,
            )

# =============================
# Page — Admin: Import Lookups (PostgreSQL + live progress)
# =============================
def page_admin_import():
    st.title("🛠️ Admin — Import Lookups (Excel/CSV)")
    st.caption("Files should have exact column names shown below. Existing rows are kept; duplicates are skipped.")

    set_current_page("admin_import")

    # -----------------------
    # Progress UI helpers
    # -----------------------
    def _mk_status(title: str):
        """Return (status_ctx_or_spinner, progress_widget, line_widget, has_status)."""
        has_status = hasattr(st, "status")
        if has_status:
            sts = st.status(title, expanded=True)
            pb = st.progress(0)
            ln = st.empty()
            return (sts, pb, ln, True)
        sp = st.spinner(title + "…")
        pb = st.progress(0)
        ln = st.empty()
        return (sp, pb, ln, False)

    def _update_progress(pb, ln, i, total, inserted=0, updated=0, skipped=0, label_prefix=""):
        frac = max(0.0, min(1.0, (i / float(total)))) if total else 0.0
        pb.progress(frac)
        ln.write(f"{label_prefix} {i}/{total} · Inserted: {inserted} · Updated: {updated} · Skipped: {skipped}")

    def _finish_status(sts_or_spinner, has_status: bool, final_text: str, ok: bool = True):
        if has_status:
            state = "complete" if ok else "error"
            sts_or_spinner.update(label=final_text, state=state)
        else:
            (st.success if ok else st.error)(final_text)

    # -----------------------
    # Flash + utilities
    # -----------------------
    if "flash_admin" in st.session_state:
        level, msg = st.session_state.pop("flash_admin")
        getattr(st, level)(msg)

    def popout(label: str):
        """Used later for e.g. Danger Zone; safe wrapper around popover/expander."""
        if hasattr(st, "popover"):
            return st.popover(label)
        st.markdown(f"**{label}**")
        return st.expander(label, expanded=False)

    if "danger_nonce" not in st.session_state:
        st.session_state["danger_nonce"] = 0

    def _refcount(sql: str, params: dict) -> int:
        with engine.begin() as conn:
            r = conn.execute(text(sql), params).fetchone()
            return int(r[0]) if r and r[0] is not None else 0

    def _parts_join(*parts):
        return " - ".join([p for p in [str(x).strip() for x in parts] if p and p != "None"])

    def _norm_col(s: str) -> str:
        if s is None:
            return ""
        s = unicodedata.normalize("NFKC", str(s))
        s = s.replace("\u00A0", " ")
        s = s.strip().lower()
        s = re.sub(r"\s+", " ", s)
        s = s.replace(" ", "_")
        return s

    def _read_df_upload(file):
        if file.name.endswith(".xlsx"):
            df = pd.read_excel(file, dtype=str)
        else:
            df = pd.read_csv(file, dtype=str)
        df.columns = [_norm_col(c) for c in df.columns]
        return df

    def _norm_or_empty(v):
        return (v.strip() if isinstance(v, str) else v) or ""

    # =====================================================================
    # MAIN TABS FOR ENTITIES
    # =====================================================================
    main_tabs = st.tabs(
        ["Customers", "Target Audiences", "Business Units", "Business Lines", "Items", "Objectives"]
    )

    # =====================================================================
    # 1) CUSTOMERS
    # =====================================================================
    with main_tabs[0]:
        st.subheader("Customers")

        # Use radio instead of nested tabs so selection persists on rerun
        mode = st.radio(
            "Mode",
            ["➕ Add / Import", "📝 Manage"],
            index=0,
            key="cust_mode",
            horizontal=True,
        )

        # ---------------------------------------------------------------
        # Common dropdown data for sectors / regions (from existing data)
        # ---------------------------------------------------------------
        sec_df = query_df(
            """
            SELECT DISTINCT sector
            FROM customers
            WHERE sector IS NOT NULL AND sector <> ''
            ORDER BY sector
            """
        )
        sector_values = [str(r.sector).strip() for r in sec_df.itertuples(index=False) if str(r.sector).strip()]
        sector_options = [""] + sector_values + ["OTHER"]

        reg_df = query_df(
            """
            SELECT DISTINCT region
            FROM customers
            WHERE region IS NOT NULL AND region <> ''
            ORDER BY region
            """
        )
        region_values = [str(r.region).strip() for r in reg_df.itertuples(index=False) if str(r.region).strip()]
        region_options = [""] + region_values + ["OTHER"]

        # -------------------------
        # MODE 1: Add / Import
        # -------------------------
        if mode == "➕ Add / Import":
            st.markdown("### ➕ Add single customer")
            st.caption("Required field: **Account Name**. Sector, Region, and City are optional.")

            # init add-state
            st.session_state.setdefault("cust_add_acc", "")
            st.session_state.setdefault("cust_add_sector_opt", "")
            st.session_state.setdefault("cust_add_sector_other", "")
            st.session_state.setdefault("cust_add_region_opt", "")
            st.session_state.setdefault("cust_add_region_other", "")
            st.session_state.setdefault("cust_add_city_opt", "")
            st.session_state.setdefault("cust_add_city_other", "")

            # --- Account Name ---
            acc = st.text_input("Account Name *", key="cust_add_acc")

            # --- Sector ---
            if st.session_state["cust_add_sector_opt"] not in sector_options:
                st.session_state["cust_add_sector_opt"] = ""
            sec_idx = sector_options.index(st.session_state["cust_add_sector_opt"])

            sector_sel = st.selectbox(
                "Sector",
                sector_options,
                index=sec_idx,
                key="cust_add_sector_opt",
            )
            if sector_sel == "OTHER":
                sector_other = st.text_input("Other sector", key="cust_add_sector_other")
            else:
                sector_other = st.session_state.get("cust_add_sector_other", "")

            # --- Region ---
            if st.session_state["cust_add_region_opt"] not in region_options:
                st.session_state["cust_add_region_opt"] = ""
            reg_idx = region_options.index(st.session_state["cust_add_region_opt"])

            region_sel = st.selectbox(
                "Region",
                region_options,
                index=reg_idx,
                key="cust_add_region_opt",
            )
            if region_sel == "OTHER":
                region_other = st.text_input("Other region", key="cust_add_region_other")
            else:
                region_other = st.session_state.get("cust_add_region_other", "")

            # --- City (depends on region) ---
            if region_sel not in ("", "OTHER"):
                city_df = query_df(
                    """
                    SELECT DISTINCT city
                    FROM customers
                    WHERE region = :r
                      AND city IS NOT NULL AND city <> ''
                    ORDER BY city
                    """,
                    {"r": region_sel},
                )
                city_values = [str(r.city).strip() for r in city_df.itertuples(index=False) if str(r.city).strip()]
                city_options = [""] + city_values + ["OTHER"]
            else:
                city_options = ["", "OTHER"]

            if st.session_state["cust_add_city_opt"] not in city_options:
                st.session_state["cust_add_city_opt"] = ""
            city_idx = city_options.index(st.session_state["cust_add_city_opt"])

            city_sel = st.selectbox(
                "City",
                city_options,
                index=city_idx,
                key="cust_add_city_opt",
            )
            if city_sel == "OTHER":
                city_other = st.text_input("Other city", key="cust_add_city_other")
            else:
                city_other = st.session_state.get("cust_add_city_other", "")

            # --- Save button ---
            if st.button("Save Customer", type="primary", key="cust_add_save"):
                if not acc.strip():
                    st.error("Account Name is required.")
                else:
                    try:
                        acc_v = acc.strip()

                        # resolve sector
                        if sector_sel == "":
                            sector_v = None
                        elif sector_sel == "OTHER":
                            sector_v = (sector_other or "").strip() or None
                        else:
                            sector_v = sector_sel

                        # resolve region
                        if region_sel == "":
                            region_v = None
                        elif region_sel == "OTHER":
                            region_v = (region_other or "").strip() or None
                        else:
                            region_v = region_sel

                        # resolve city
                        if city_sel == "":
                            city_v = None
                        elif city_sel == "OTHER":
                            city_v = (city_other or "").strip() or None
                        else:
                            city_v = city_sel

                        with engine.begin() as conn:
                            res = conn.execute(
                                text(
                                    """
                                    INSERT INTO customers(account_name, sector, region, city)
                                    SELECT :acc, :sector, :region, :city
                                    WHERE NOT EXISTS (
                                        SELECT 1
                                        FROM customers c
                                        WHERE lower(coalesce(c.account_name, '')) = lower(coalesce(:acc, ''))
                                          AND lower(coalesce(c.sector,       '')) = lower(coalesce(:sector, ''))
                                          AND lower(coalesce(c.region,       '')) = lower(coalesce(:region, ''))
                                          AND lower(coalesce(c.city,         '')) = lower(coalesce(:city, ''))
                                    )
                                    """
                                ),
                                {"acc": acc_v, "sector": sector_v, "region": region_v, "city": city_v},
                            )

                        if (res.rowcount or 0) > 0:
                            st.success("Customer added ✅")
                            # reset form
                            for key in (
                                "cust_add_acc",
                                "cust_add_sector_opt",
                                "cust_add_sector_other",
                                "cust_add_region_opt",
                                "cust_add_region_other",
                                "cust_add_city_opt",
                                "cust_add_city_other",
                            ):
                                st.session_state.pop(key, None)
                        else:
                            st.info(
                                "A customer with the same **Name + Sector + Region + City** already exists — nothing added."
                            )
                    except Exception as e:
                        st.error("Could not add customer.")
                        st.caption(str(e))

            st.markdown("---")
            st.markdown("### ⬆️ Bulk upload customers (Excel/CSV)")
            st.write("Columns: **account_name**, sector, region, city")

            f1 = st.file_uploader(
                "Upload Customers file", type=["xlsx", "csv"], key="cust_upload"
            )
            if f1 is not None:
                df = _read_df_upload(f1)

                if "account_name" not in df.columns:
                    st.error("Missing required column: account_name")
                else:
                    total = len(df)
                    inserted = 0
                    skipped = 0
                    sts, pb, ln, has_status = _mk_status("Importing Customers…")

                    try:
                        with engine.begin() as conn:
                            for i, r in enumerate(df.itertuples(index=False), start=1):
                                acc_raw = getattr(r, "account_name", "")
                                acc_v = str(acc_raw).strip() if pd.notna(acc_raw) else ""
                                if not acc_v:
                                    skipped += 1
                                    if i % 200 == 0 or i == total:
                                        _update_progress(
                                            pb, ln, i, total, inserted, 0, skipped, label_prefix="Customers"
                                        )
                                    continue

                                sector_v = (
                                    str(getattr(r, "sector")).strip()
                                    if hasattr(r, "sector") and pd.notna(getattr(r, "sector"))
                                    else None
                                )
                                region_v = (
                                    str(getattr(r, "region")).strip()
                                    if hasattr(r, "region") and pd.notna(getattr(r, "region"))
                                    else None
                                )
                                city_v = (
                                    str(getattr(r, "city")).strip()
                                    if hasattr(r, "city") and pd.notna(getattr(r, "city"))
                                    else None
                                )

                                res = conn.execute(
                                    text(
                                        """
                                        INSERT INTO customers(account_name, sector, region, city)
                                        SELECT :acc, :sector, :region, :city
                                        WHERE NOT EXISTS (
                                            SELECT 1
                                            FROM customers c
                                            WHERE lower(coalesce(c.account_name, '')) = lower(coalesce(:acc, ''))
                                              AND lower(coalesce(c.sector,       '')) = lower(coalesce(:sector, ''))
                                              AND lower(coalesce(c.region,       '')) = lower(coalesce(:region, ''))
                                              AND lower(coalesce(c.city,         '')) = lower(coalesce(:city, ''))
                                        )
                                        """
                                    ),
                                    {"acc": acc_v, "sector": sector_v, "region": region_v, "city": city_v},
                                )

                                if (res.rowcount or 0) > 0:
                                    inserted += 1
                                else:
                                    skipped += 1

                                if i % 200 == 0 or i == total:
                                    _update_progress(
                                        pb, ln, i, total, inserted, 0, skipped, label_prefix="Customers"
                                    )
                                    time.sleep(0.001)

                        _finish_status(
                            sts,
                            has_status,
                            f"Customers import ✅ Inserted: {inserted} | Skipped: {skipped}",
                            ok=True,
                        )
                    except Exception as e:
                        _finish_status(sts, has_status, "Customers import failed ❌", ok=False)
                        st.caption(str(e))

        # -------------------------
        # MODE 2: Manage
        # -------------------------
        else:  # mode == "📝 Manage"
            st.markdown("### 📝 Manage customers")

            cdf = query_df(
                """
                SELECT customer_id,
                       account_name,
                       sector,
                       region,
                       city,
                       COALESCE(is_active, TRUE) AS is_active
                FROM customers
                ORDER BY account_name
                """
            )

            if cdf.empty:
                st.info("No customers yet.")
            else:
                options = [
                    _parts_join(r.customer_id, r.account_name, r.region, r.city)
                    + f" ({'active' if bool(r.is_active) else 'inactive'})"
                    for r in cdf.itertuples(index=False)
                ]
                options = [""] + options

                sel_label = st.selectbox(
                    "Select customer", options, index=0, key="mg_cust_sel"
                )

                if sel_label == "":
                    st.info("Please select a customer.")
                else:
                    row_idx = options.index(sel_label) - 1
                    row = cdf.iloc[row_idx]
                    cid = int(row["customer_id"])

                    # quick refs / status
                    colA, colB = st.columns([1, 1])
                    with colA:
                        v_cnt = _refcount("SELECT COUNT(*) FROM visits WHERE customer_id=:cid", {"cid": cid})
                        a_cnt = _refcount("SELECT COUNT(*) FROM target_audiences WHERE customer_id=:cid", {"cid": cid})
                        st.caption(f"Refs → Visits: **{v_cnt}** · Audiences: **{a_cnt}**")
                    with colB:
                        st.caption("Status")
                        st.write("✅ Active" if bool(row["is_active"]) else "🚫 Inactive")

                    st.markdown("---")
                    st.markdown("#### Edit customer")

                    base_key = f"mg_cust_{cid}"

                    # ----- Account Name FIRST -----
                    acc_edit = st.text_input(
                        "Account Name *",
                        value=row["account_name"] or "",
                        key=f"{base_key}_acc"
                    )

                    # ----- Sector -----
                    sec_key = base_key + "_sector_opt"
                    sec_other_key = base_key + "_sector_other"

                    existing_sec = (row["sector"] or "").strip() if row["sector"] else ""

                    if sec_key not in st.session_state:
                        if existing_sec and existing_sec in sector_options:
                            st.session_state[sec_key] = existing_sec
                        elif existing_sec:
                            st.session_state[sec_key] = "OTHER"
                            st.session_state[sec_other_key] = existing_sec
                        else:
                            st.session_state[sec_key] = ""

                    # keep state valid against current options
                    if st.session_state[sec_key] not in sector_options:
                        st.session_state[sec_key] = ""

                    sector_sel_edit = st.selectbox(
                        "Sector",
                        sector_options,
                        key=sec_key,   # ❗ no index here
                    )
                    if sector_sel_edit == "OTHER":
                        sector_other_edit = st.text_input("Other sector", key=sec_other_key)
                    else:
                        sector_other_edit = st.session_state.get(sec_other_key, "")

                    # ----- Region -----
                    reg_key = base_key + "_region_opt"
                    reg_other_key = base_key + "_region_other"

                    existing_reg = (row["region"] or "").strip() if row["region"] else ""

                    if reg_key not in st.session_state:
                        if existing_reg and existing_reg in region_options:
                            st.session_state[reg_key] = existing_reg
                        elif existing_reg:
                            st.session_state[reg_key] = "OTHER"
                            st.session_state[reg_other_key] = existing_reg
                        else:
                            st.session_state[reg_key] = ""

                    if st.session_state[reg_key] not in region_options:
                        st.session_state[reg_key] = ""

                    region_sel_edit = st.selectbox(
                        "Region",
                        region_options,
                        key=reg_key,   # ❗ no index here
                    )
                    if region_sel_edit == "OTHER":
                        region_other_edit = st.text_input("Other region", key=reg_other_key)
                    else:
                        region_other_edit = st.session_state.get(reg_other_key, "")

                    # ----- City (dependent) -----
                    city_key = base_key + "_city_opt"
                    city_other_key = base_key + "_city_other"

                    if region_sel_edit not in ("", "OTHER"):
                        city_df = query_df(
                            """
                            SELECT DISTINCT city
                            FROM customers
                            WHERE region = :r
                              AND city IS NOT NULL AND city <> ''
                            ORDER BY city
                            """,
                            {"r": region_sel_edit},
                        )
                        city_vals = [str(r.city).strip() for r in city_df.itertuples(index=False) if str(r.city).strip()]
                        city_options_edit = [""] + city_vals + ["OTHER"]
                    else:
                        city_options_edit = ["", "OTHER"]

                    existing_city = (row["city"] or "").strip() if row["city"] else ""

                    if city_key not in st.session_state:
                        if existing_city and existing_city in city_options_edit:
                            st.session_state[city_key] = existing_city
                        elif existing_city:
                            st.session_state[city_key] = "OTHER"
                            st.session_state[city_other_key] = existing_city
                        else:
                            st.session_state[city_key] = ""

                    if st.session_state[city_key] not in city_options_edit:
                        st.session_state[city_key] = ""

                    city_sel_edit = st.selectbox(
                        "City",
                        city_options_edit,
                        key=city_key,   # ❗ no index here
                    )
                    if city_sel_edit == "OTHER":
                        city_other_edit = st.text_input("Other city", key=city_other_key)
                    else:
                        city_other_edit = st.session_state.get(city_other_key, "")

                    # ----- Active toggle -----
                    active_flag = st.checkbox(
                        "Active",
                        value=bool(row["is_active"]),
                        key=f"{base_key}_active",
                        help="Uncheck to deactivate this customer.",
                    )

                    # ----- Save button -----
                    if st.button("Save changes", type="primary", key=f"{base_key}_save"):
                        acc_clean = acc_edit.strip()
                        if not acc_clean:
                            st.error("Account Name is required.")
                        else:
                            # resolve sector
                            if sector_sel_edit == "":
                                sector_v = None
                            elif sector_sel_edit == "OTHER":
                                sector_v = (sector_other_edit or "").strip() or None
                            else:
                                sector_v = sector_sel_edit

                            # resolve region
                            if region_sel_edit == "":
                                region_v = None
                            elif region_sel_edit == "OTHER":
                                region_v = (region_other_edit or "").strip() or None
                            else:
                                region_v = region_sel_edit

                            # resolve city
                            if city_sel_edit == "":
                                city_v = None
                            elif city_sel_edit == "OTHER":
                                city_v = (city_other_edit or "").strip() or None
                            else:
                                city_v = city_sel_edit

                            dup = query_df(
                                """
                                SELECT 1
                                FROM customers
                                WHERE lower(account_name)=lower(:n)
                                  AND customer_id<>:id
                                """,
                                {"n": acc_clean, "id": cid},
                            )
                            if not dup.empty:
                                st.error("Account Name already exists.")
                            else:
                                try:
                                    exec_sql(
                                        """
                                        UPDATE customers
                                        SET account_name=:acc,
                                            sector=:s,
                                            region=:r,
                                            city=:c,
                                            is_active=:b
                                        WHERE customer_id=:id
                                        """,
                                        {
                                            "acc": acc_clean,
                                            "s": sector_v,
                                            "r": region_v,
                                            "c": city_v,
                                            "b": bool(active_flag),
                                            "id": cid,
                                        },
                                    )
                                    st.success("Customer updated ✅")
                                except Exception as e:
                                    st.error("Could not update customer.")
                                    st.caption(str(e))

                    st.markdown("---")
                    st.markdown("#### 🔴 Danger Zone")
                    st.write("Delete this customer permanently (only if not referenced by visits/audiences).")

                    del_conf_key = f"{base_key}_del_conf"
                    del_confirm = st.checkbox(
                        "I understand this cannot be undone.",
                        key=del_conf_key,
                    )

                    if st.button(
                        "Delete Customer",
                        type="primary",
                        disabled=not del_confirm,
                        key=f"{base_key}_del",
                    ):
                        if v_cnt > 0 or a_cnt > 0:
                            st.error(
                                "Cannot delete: this customer is referenced by visits and/or target audiences. "
                                "Deactivate instead."
                            )
                        else:
                            try:
                                exec_sql("DELETE FROM customers WHERE customer_id=:id", {"id": cid})
                                st.success("Customer deleted ✅")

                                # reset customer selection safely
                                st.session_state.pop("mg_cust_sel", None)

                                # optional: refresh UI so the deleted customer disappears from lists
                                st.rerun()

                            except Exception as e:
                                st.error("Delete failed.")
                                st.caption(str(e))

    # =====================================================================
    # 2) TARGET AUDIENCES
    # =====================================================================
    with main_tabs[1]:
        st.subheader("Target Audiences")

        mode = st.radio(
            "Mode",
            ["➕ Add / Import", "📝 Manage"],
            index=0,
            key="aud_mode",
            horizontal=True,
        )

        # -----------------------------
        # Common lookup data
        # -----------------------------
        # Customers (for both add & manage)
        cust_df = query_df(
            """
            SELECT customer_id,
                   account_name,
                   region,
                   city,
                   COALESCE(is_active, TRUE) AS is_active
            FROM customers
            ORDER BY account_name
            """
        )

        # Distinct departments and positions (for dropdowns)
        dept_df = query_df(
            """
            SELECT DISTINCT department
            FROM target_audiences
            WHERE department IS NOT NULL AND department <> ''
            ORDER BY department
            """
        )
        dept_values = [str(r.department).strip() for r in dept_df.itertuples(index=False) if str(r.department).strip()]
        dept_options = [""] + dept_values + ["OTHER"]

        pos_df = query_df(
            """
            SELECT DISTINCT position
            FROM target_audiences
            WHERE position IS NOT NULL AND position <> ''
            ORDER BY position
            """
        )
        pos_values = [str(r.position).strip() for r in pos_df.itertuples(index=False) if str(r.position).strip()]
        pos_options = [""] + pos_values + ["OTHER"]

        TITLE_OPTIONS = ["", "Dr.", "Mr.", "Ms.", "Mrs.", "Prof.", "Eng.", "Other"]

        # Helper to build customer labels
        def _fmt_cust_label(r):
            base = _parts_join(r.customer_id, r.account_name, r.region, r.city)
            return base + f" ({'active' if bool(r.is_active) else 'inactive'})"

        # ==============================================================
        # MODE 1: Add / Import Target Audiences
        # ==============================================================
        if mode == "➕ Add / Import":
            st.markdown("### ➕ Add single target audience")

            if cust_df.empty:
                st.warning("No customers found. Please add customers first.")
            else:
                # -------- Customer select --------
                cust_labels = [""] + [_fmt_cust_label(r) for r in cust_df.itertuples(index=False)]
                cust_choice = st.selectbox(
                    "Customer *",
                    cust_labels,
                    index=0,
                    key="aud_add_cust",
                )

                if cust_choice:
                    cust_row = cust_df.iloc[cust_labels.index(cust_choice) - 1]
                    cid = int(cust_row["customer_id"])
                else:
                    cid = None

                # -------- Title dropdown --------
                title_choice = st.selectbox("Title", TITLE_OPTIONS, index=0, key="aud_add_title_opt")
                title_other_val = ""
                if title_choice == "Other":
                    title_other_val = st.text_input("Other title", key="aud_add_title_other")

                # -------- Name (required) --------
                name_val = st.text_input("Name *", key="aud_add_name")

                # -------- Department dropdown --------
                dept_choice = st.selectbox(
                    "Department",
                    dept_options,
                    index=0,
                    key="aud_add_dept_opt",
                )
                dept_other_val = ""
                if dept_choice == "OTHER":
                    dept_other_val = st.text_input("Other department", key="aud_add_dept_other")

                # -------- Position dropdown --------
                pos_choice = st.selectbox(
                    "Position",
                    pos_options,
                    index=0,
                    key="aud_add_pos_opt",
                )
                pos_other_val = ""
                if pos_choice == "OTHER":
                    pos_other_val = st.text_input("Other position", key="aud_add_pos_other")

                # -------- Other fields --------
                pot_val = st.text_input("Potentiality", key="aud_add_pot")
                loy_val = st.text_input("Loyalty", key="aud_add_loy")
                mob_val = st.text_input("Mobile", key="aud_add_mobile")
                land_val = st.text_input("Landline", key="aud_add_landline")
                ext_val = st.text_input("External Number", key="aud_add_ext")
                email_val = st.text_input("Email", key="aud_add_email")

                if st.button("Save Target Audience", type="primary", key="aud_add_save"):
                    if not cid:
                        st.error("Customer is required.")
                    elif not name_val.strip():
                        st.error("Name is required.")
                    else:
                        try:
                            # Resolve title
                            if title_choice == "":
                                title_v = None
                            elif title_choice == "Other":
                                title_v = (title_other_val or "").strip() or None
                            else:
                                title_v = title_choice

                            # Resolve department
                            if dept_choice == "":
                                dept_v = None
                            elif dept_choice == "OTHER":
                                dept_v = (dept_other_val or "").strip() or None
                            else:
                                dept_v = dept_choice

                            # Resolve position
                            if pos_choice == "":
                                pos_v = None
                            elif pos_choice == "OTHER":
                                pos_v = (pos_other_val or "").strip() or None
                            else:
                                pos_v = pos_choice

                            nm_clean = name_val.strip()

                            # Duplicate check: same (customer + name + dept + position)
                            dup = query_df(
                                """
                                SELECT 1
                                FROM target_audiences
                                WHERE customer_id=:cid
                                  AND lower(coalesce(name, '')) = lower(:nm)
                                  AND lower(coalesce(department, '')) = lower(coalesce(:dept, ''))
                                  AND lower(coalesce(position, '')) = lower(coalesce(:pos, ''))
                                LIMIT 1
                                """,
                                {"cid": cid, "nm": nm_clean, "dept": (dept_v or ""), "pos": (pos_v or "")},
                            )
                            if not dup.empty:
                                st.info(
                                    "This combination (Customer + Name + Department + Position) already exists — skipped."
                                )
                            else:
                                with engine.begin() as conn:
                                    conn.execute(
                                        text(
                                            """
                                            INSERT INTO target_audiences(
                                                customer_id, title, name, department, position,
                                                potentiality, loyalty,
                                                mobile, landline, external_number, email, is_active
                                            )
                                            VALUES (
                                                :cid, :title, :name, :dept, :pos,
                                                :pot, :loy,
                                                :mob, :land, :extn, :email, TRUE
                                            )
                                            """
                                        ),
                                        {
                                            "cid": cid,
                                            "title": title_v,
                                            "name": nm_clean,
                                            "dept": dept_v,
                                            "pos": pos_v,
                                            "pot": (pot_val.strip() or None),
                                            "loy": (loy_val.strip() or None),
                                            "mob": (mob_val.strip() or None),
                                            "land": (land_val.strip() or None),
                                            "extn": (ext_val.strip() or None),
                                            "email": (email_val.strip() or None),
                                        },
                                    )
                                st.success("Target audience added ✅")
                        except Exception as e:
                            st.error("Could not add target audience.")
                            st.caption(str(e))

            # -------- Bulk upload (same as before) --------
            st.markdown("---")
            st.markdown("### ⬆️ Bulk upload target audiences (Excel/CSV)")
            st.write("Columns: **customer_name**, name, title, department, position, potentiality, loyalty, mobile, landline, external_number, email")

            f2 = st.file_uploader("Upload Target Audiences", type=["xlsx", "csv"], key="aud_upload")
            if f2 is not None:
                df = _read_df_upload(f2)
                needed = {"customer_name", "name"}
                if not needed.issubset(df.columns):
                    st.error("Missing required columns: customer_name, name")
                else:
                    total = len(df)
                    inserted = 0
                    skipped = 0
                    sts, pb, ln, has_status = _mk_status("Importing Target Audiences…")

                    try:
                        with engine.begin() as conn:
                            cdf = pd.read_sql_query(
                                text("SELECT customer_id, account_name FROM customers"),
                                conn,
                            )
                            cmap = {
                                str(r.account_name).strip().lower(): int(r.customer_id)
                                for r in cdf.itertuples(index=False)
                            }

                            for i, r in enumerate(df.itertuples(index=False), start=1):
                                cname = str(getattr(r, "customer_name", "")).strip()
                                aname = str(getattr(r, "name", "")).strip()
                                if not (cname and aname):
                                    skipped += 1
                                    continue

                                cid = cmap.get(cname.lower())
                                if not cid:
                                    skipped += 1
                                    continue

                                title_v = (
                                    str(getattr(r, "title")).strip()
                                    if hasattr(r, "title") and pd.notna(getattr(r, "title"))
                                    else None
                                )
                                dept_v = (
                                    str(getattr(r, "department")).strip()
                                    if hasattr(r, "department") and pd.notna(getattr(r, "department"))
                                    else None
                                )
                                pos_v = (
                                    str(getattr(r, "position")).strip()
                                    if hasattr(r, "position") and pd.notna(getattr(r, "position"))
                                    else None
                                )
                                pot_v = (
                                    str(getattr(r, "potentiality")).strip()
                                    if hasattr(r, "potentiality") and pd.notna(getattr(r, "potentiality"))
                                    else None
                                )
                                loy_v = (
                                    str(getattr(r, "loyalty")).strip()
                                    if hasattr(r, "loyalty") and pd.notna(getattr(r, "loyalty"))
                                    else None
                                )
                                mob_v = (
                                    str(getattr(r, "mobile")).strip()
                                    if hasattr(r, "mobile") and pd.notna(getattr(r, "mobile"))
                                    else None
                                )
                                land_v = (
                                    str(getattr(r, "landline")).strip()
                                    if hasattr(r, "landline") and pd.notna(getattr(r, "landline"))
                                    else None
                                )
                                extn_v = (
                                    str(getattr(r, "external_number")).strip()
                                    if hasattr(r, "external_number") and pd.notna(getattr(r, "external_number"))
                                    else None
                                )
                                email_v = (
                                    str(getattr(r, "email")).strip()
                                    if hasattr(r, "email") and pd.notna(getattr(r, "email"))
                                    else None
                                )

                                dup = conn.execute(
                                    text(
                                        """
                                        SELECT 1
                                        FROM target_audiences
                                        WHERE customer_id = :cid
                                          AND lower(coalesce(name, '')) = lower(:name)
                                          AND lower(coalesce(department, '')) = lower(coalesce(:dept, ''))
                                          AND lower(coalesce(position, '')) = lower(coalesce(:pos, ''))
                                        LIMIT 1
                                        """
                                    ),
                                    {"cid": cid, "name": aname, "dept": (dept_v or ""), "pos": (pos_v or "")},
                                ).fetchone()

                                if dup:
                                    skipped += 1
                                    continue

                                conn.execute(
                                    text(
                                        """
                                        INSERT INTO target_audiences(
                                            customer_id, title, name, department, position,
                                            potentiality, loyalty,
                                            mobile, landline, external_number, email, is_active
                                        )
                                        VALUES (
                                            :cid, :title, :name, :dept, :pos,
                                            :pot, :loy,
                                            :mob, :land, :extn, :email, TRUE
                                        )
                                        """
                                    ),
                                    {
                                        "cid": cid,
                                        "title": title_v,
                                        "name": aname,
                                        "dept": dept_v,
                                        "pos": pos_v,
                                        "pot": pot_v,
                                        "loy": loy_v,
                                        "mob": mob_v,
                                        "land": land_v,
                                        "extn": extn_v,
                                        "email": email_v,
                                    },
                                )
                                inserted += 1

                                if i % 200 == 0 or i == total:
                                    _update_progress(
                                        pb, ln, i, total, inserted, 0, skipped, label_prefix="Audiences"
                                    )
                                    time.sleep(0.001)

                        _finish_status(
                            sts,
                            has_status,
                            f"✅ Target audiences import done. Inserted: {inserted} | Skipped: {skipped}",
                            ok=True,
                        )
                    except Exception as e:
                        _finish_status(sts, has_status, "❌ Target audiences import failed.", ok=False)
                        st.caption(str(e))

        # ==============================================================
        # MODE 2: Manage Target Audiences
        # ==============================================================
        else:  # mode == "📝 Manage"
            st.markdown("### 📝 Manage target audiences")

            if cust_df.empty:
                st.info("No customers yet.")
            else:
                # First select customer (same style as Customers tab)
                cust_labels = [""] + [_fmt_cust_label(r) for r in cust_df.itertuples(index=False)]
                cust_choice = st.selectbox(
                    "Select customer",
                    cust_labels,
                    index=0,
                    key="mg_aud_cust_sel",
                )

                if not cust_choice:
                    st.info("Please select a customer.")
                else:
                    cust_row = cust_df.iloc[cust_labels.index(cust_choice) - 1]
                    cid = int(cust_row["customer_id"])

                    # Load audiences for this customer
                    adf = query_df(
                        """
                        SELECT audience_id,
                               customer_id,
                               title,
                               name,
                               department,
                               position,
                               potentiality,
                               loyalty,
                               mobile,
                               landline,
                               external_number,
                               email,
                               COALESCE(is_active, TRUE) AS is_active
                        FROM target_audiences
                        WHERE customer_id = :cid
                        ORDER BY name
                        """,
                        {"cid": cid},
                    )

                    if adf.empty:
                        st.info("No target audiences for this customer yet.")
                    else:
                        def _fmt_aud_label(r):
                            # ID first, then (title + name), department, position – without customer name
                            title_name = (
                                ((str(r.title).strip() + " ") if r.title else "")
                                + (str(r.name).strip() if r.name else "")
                            ).strip()
                            parts = [str(r.audience_id), title_name]
                            if r.department and str(r.department).strip():
                                parts.append(str(r.department).strip())
                            if r.position and str(r.position).strip():
                                parts.append(str(r.position).strip())
                            base = " - ".join([p for p in parts if p])
                            return base + f" ({'active' if bool(r.is_active) else 'inactive'})"

                        aud_labels = [""] + [_fmt_aud_label(r) for r in adf.itertuples(index=False)]
                        aud_choice = st.selectbox(
                            "Select target audience",
                            aud_labels,
                            index=0,
                            key="mg_aud_sel",
                        )

                        if not aud_choice:
                            st.info("Please select a target audience.")
                        else:
                            row = adf.iloc[aud_labels.index(aud_choice) - 1]
                            aid = int(row["audience_id"])

                            # --- Refs & status ---
                            colA, colB = st.columns([1, 1])
                            with colA:
                                v_cnt = _refcount(
                                    "SELECT COUNT(*) FROM visits WHERE audience_id=:aid",
                                    {"aid": aid},
                                )
                                st.caption(f"Refs → Visits: **{v_cnt}**")
                            with colB:
                                st.caption("Status")
                                st.write("✅ Active" if bool(row["is_active"]) else "🚫 Inactive")

                            st.markdown("---")
                            st.markdown("#### Edit target audience")

                            base_key = f"mg_aud_{aid}"

                            # ----- Name (required) -----
                            name_edit = st.text_input(
                                "Name *",
                                value=row["name"] or "",
                                key=f"{base_key}_name",
                            )

                            # ----- Title dropdown -----
                            current_title = (row["title"] or "").strip() if row["title"] else ""
                            if current_title and current_title in TITLE_OPTIONS:
                                title_idx = TITLE_OPTIONS.index(current_title)
                                title_default_other = ""
                            elif current_title:
                                title_idx = TITLE_OPTIONS.index("Other")
                                title_default_other = current_title
                            else:
                                title_idx = 0
                                title_default_other = ""

                            title_sel = st.selectbox(
                                "Title",
                                TITLE_OPTIONS,
                                index=title_idx,
                                key=f"{base_key}_title_opt",
                            )
                            title_other_edit = ""
                            if title_sel == "Other":
                                title_other_edit = st.text_input(
                                    "Other title",
                                    value=title_default_other,
                                    key=f"{base_key}_title_other",
                                )

                            # ----- Department dropdown -----
                            current_dept = (row["department"] or "").strip() if row["department"] else ""
                            if current_dept and current_dept in dept_options:
                                dept_idx = dept_options.index(current_dept)
                                dept_default_other = ""
                            elif current_dept:
                                dept_idx = dept_options.index("OTHER")
                                dept_default_other = current_dept
                            else:
                                dept_idx = 0
                                dept_default_other = ""

                            dept_sel = st.selectbox(
                                "Department",
                                dept_options,
                                index=dept_idx,
                                key=f"{base_key}_dept_opt",
                            )
                            dept_other_edit = ""
                            if dept_sel == "OTHER":
                                dept_other_edit = st.text_input(
                                    "Other department",
                                    value=dept_default_other,
                                    key=f"{base_key}_dept_other",
                                )

                            # ----- Position dropdown -----
                            current_pos = (row["position"] or "").strip() if row["position"] else ""
                            if current_pos and current_pos in pos_options:
                                pos_idx = pos_options.index(current_pos)
                                pos_default_other = ""
                            elif current_pos:
                                pos_idx = pos_options.index("OTHER")
                                pos_default_other = current_pos
                            else:
                                pos_idx = 0
                                pos_default_other = ""

                            pos_sel = st.selectbox(
                                "Position",
                                pos_options,
                                index=pos_idx,
                                key=f"{base_key}_pos_opt",
                            )
                            pos_other_edit = ""
                            if pos_sel == "OTHER":
                                pos_other_edit = st.text_input(
                                    "Other position",
                                    value=pos_default_other,
                                    key=f"{base_key}_pos_other",
                                )

                            # ----- Other fields -----
                            pot_edit = st.text_input(
                                "Potentiality",
                                value=row["potentiality"] or "",
                                key=f"{base_key}_pot",
                            )
                            loy_edit = st.text_input(
                                "Loyalty",
                                value=row["loyalty"] or "",
                                key=f"{base_key}_loy",
                            )
                            mob_edit = st.text_input(
                                "Mobile",
                                value=row["mobile"] or "",
                                key=f"{base_key}_mob",
                            )
                            land_edit = st.text_input(
                                "Landline",
                                value=row["landline"] or "",
                                key=f"{base_key}_land",
                            )
                            ext_edit = st.text_input(
                                "External Number",
                                value=row["external_number"] or "",
                                key=f"{base_key}_ext",
                            )
                            email_edit = st.text_input(
                                "Email",
                                value=row["email"] or "",
                                key=f"{base_key}_email",
                            )

                            active_flag = st.checkbox(
                                "Active",
                                value=bool(row["is_active"]),
                                key=f"{base_key}_active",
                                help="Uncheck to deactivate this target audience.",
                            )

                            # ----- Save button -----
                            if st.button("Save changes", type="primary", key=f"{base_key}_save"):
                                nm_clean = name_edit.strip()
                                if not nm_clean:
                                    st.error("Name is required.")
                                else:
                                    # Resolve title
                                    if title_sel == "":
                                        title_v = None
                                    elif title_sel == "Other":
                                        title_v = (title_other_edit or "").strip() or None
                                    else:
                                        title_v = title_sel

                                    # Resolve department
                                    if dept_sel == "":
                                        dept_v = None
                                    elif dept_sel == "OTHER":
                                        dept_v = (dept_other_edit or "").strip() or None
                                    else:
                                        dept_v = dept_sel

                                    # Resolve position
                                    if pos_sel == "":
                                        pos_v = None
                                    elif pos_sel == "OTHER":
                                        pos_v = (pos_other_edit or "").strip() or None
                                    else:
                                        pos_v = pos_sel

                                    # Duplicate check (same customer + name + dept + position, excluding this audience)
                                    dup = query_df(
                                        """
                                        SELECT 1
                                        FROM target_audiences
                                        WHERE customer_id = :cid
                                          AND lower(coalesce(name, '')) = lower(:nm)
                                          AND lower(coalesce(department, '')) = lower(coalesce(:dept, ''))
                                          AND lower(coalesce(position, '')) = lower(coalesce(:pos, ''))
                                          AND audience_id <> :aid
                                        LIMIT 1
                                        """,
                                        {
                                            "cid": cid,
                                            "nm": nm_clean,
                                            "dept": (dept_v or ""),
                                            "pos": (pos_v or ""),
                                            "aid": aid,
                                        },
                                    )

                                    if not dup.empty:
                                        st.error(
                                            "Another target audience with the same (Name + Department + Position) already exists for this customer."
                                        )
                                    else:
                                        try:
                                            exec_sql(
                                                """
                                                UPDATE target_audiences
                                                SET title=:title,
                                                    name=:name,
                                                    department=:dept,
                                                    position=:pos,
                                                    potentiality=:pot,
                                                    loyalty=:loy,
                                                    mobile=:mob,
                                                    landline=:land,
                                                    external_number=:extn,
                                                    email=:email,
                                                    is_active=:b
                                                WHERE audience_id=:aid
                                                """,
                                                {
                                                    "title": title_v,
                                                    "name": nm_clean,
                                                    "dept": dept_v,
                                                    "pos": pos_v,
                                                    "pot": (pot_edit.strip() or None),
                                                    "loy": (loy_edit.strip() or None),
                                                    "mob": (mob_edit.strip() or None),
                                                    "land": (land_edit.strip() or None),
                                                    "extn": (ext_edit.strip() or None),
                                                    "email": (email_edit.strip() or None),
                                                    "b": bool(active_flag),
                                                    "aid": aid,
                                                },
                                            )
                                            st.success("Target audience updated ✅")
                                        except Exception as e:
                                            st.error("Could not update target audience.")
                                            st.caption(str(e))

                            st.markdown("---")
                            st.markdown("#### 🔴 Danger Zone")
                            st.write("Delete this target audience permanently (only if not referenced by visits).")

                            del_conf_key = f"{base_key}_del_conf"
                            del_confirm = st.checkbox(
                                "I understand this cannot be undone.",
                                key=del_conf_key,
                            )

                            if st.button(
                                "Delete Target Audience",
                                type="primary",
                                disabled=not del_confirm,
                                key=f"{base_key}_del",
                            ):
                                if v_cnt > 0:
                                    st.error(
                                        "Cannot delete: this target audience is referenced by visits. Deactivate instead."
                                    )
                                else:
                                    try:
                                        exec_sql(
                                            "DELETE FROM target_audiences WHERE audience_id=:id",
                                            {"id": aid},
                                        )
                                        st.success("Target audience deleted ✅")
                                        # reset the selection by removing the widget state
                                        for key in ("mg_aud_sel",):
                                            st.session_state.pop(key, None)

                                        # optional but nice: force UI refresh
                                        st.rerun()
                                    except Exception as e:
                                        st.error("Delete failed.")
                                        st.caption(str(e))

    # =====================================================================
    # 3) BUSINESS UNITS
    # =====================================================================
    with main_tabs[2]:
        st.subheader("Business Units")

        bu_mode = st.radio(
            "Mode",
            ["➕ Add / Import", "📝 Manage"],
            index=0,
            key="bu_mode",
            horizontal=True,
        )

        # -------------------------
        # MODE 1: Add / Import
        # -------------------------
        if bu_mode == "➕ Add / Import":
            st.markdown("### ➕ Add single business unit")
            st.caption("Required field: **Business Unit Name**.")

            st.session_state.setdefault("bu_add_name", "")

            bu_name = st.text_input(
                "Business Unit Name *",
                key="bu_add_name",
            )

            if st.button("Save Business Unit", type="primary", key="bu_add_save"):
                nm = bu_name.strip()
                if not nm:
                    st.error("Business Unit Name is required.")
                else:
                    try:
                        with engine.begin() as conn:
                            res = conn.execute(
                                text(
                                    """
                                    INSERT INTO business_units(name, is_active)
                                    VALUES (:name, TRUE)
                                    ON CONFLICT (name) DO NOTHING
                                    """
                                ),
                                {"name": nm},
                            )
                        if (res.rowcount or 0) > 0:
                            st.success("Business Unit added ✅")
                            # reset widget state safely
                            st.session_state.pop("bu_add_name", None)
                        else:
                            st.info("That Business Unit already exists — nothing added.")
                    except Exception as e:
                        st.error("Could not add Business Unit.")
                        st.caption(str(e))

            st.markdown("---")
            st.markdown("### ⬆️ Bulk upload business units (Excel/CSV)")
            st.write("Columns: **name**")

            fbu = st.file_uploader(
                "Upload Business Units file",
                type=["xlsx", "csv"],
                key="bu_upload",
            )
            if fbu is not None:
                df = _read_df_upload(fbu)
                if "name" not in df.columns:
                    st.error("Missing required column: name")
                else:
                    total = len(df)
                    inserted = 0
                    skipped = 0
                    sts, pb, ln, has_status = _mk_status("Importing Business Units…")

                    try:
                        with engine.begin() as conn:
                            for i, r in enumerate(df.itertuples(index=False), start=1):
                                nm_raw = getattr(r, "name", "")
                                nm = str(nm_raw).strip() if pd.notna(nm_raw) else ""
                                if not nm:
                                    skipped += 1
                                else:
                                    res = conn.execute(
                                        text(
                                            """
                                            INSERT INTO business_units(name, is_active)
                                            VALUES (:name, TRUE)
                                            ON CONFLICT (name) DO NOTHING
                                            """
                                        ),
                                        {"name": nm},
                                    )
                                    if (res.rowcount or 0) > 0:
                                        inserted += 1
                                    else:
                                        skipped += 1

                                if i % 200 == 0 or i == total:
                                    _update_progress(
                                        pb,
                                        ln,
                                        i,
                                        total,
                                        inserted,
                                        0,
                                        skipped,
                                        label_prefix="Business Units",
                                    )
                                    time.sleep(0.001)

                        _finish_status(
                            sts,
                            has_status,
                            f"Business Units import ✅ Inserted: {inserted} | Skipped: {skipped}",
                            ok=True,
                        )
                    except Exception as e:
                        _finish_status(
                            sts,
                            has_status,
                            "Business Units import failed ❌",
                            ok=False,
                        )
                        st.caption(str(e))

        # -------------------------
        # MODE 2: Manage
        # -------------------------
        else:  # bu_mode == "📝 Manage"
            st.markdown("### 📝 Manage business units")

            bdf = query_df(
                """
                SELECT business_unit_id,
                       name,
                       COALESCE(is_active, TRUE) AS is_active
                FROM business_units
                ORDER BY name
                """
            )

            if bdf.empty:
                st.info("No business units yet.")
            else:
                # show id + name + status
                bu_options = [
                    _parts_join(r.business_unit_id, r.name)
                    + f" ({'active' if bool(r.is_active) else 'inactive'})"
                    for r in bdf.itertuples(index=False)
                ]
                bu_options = [""] + bu_options

                sel_bu_label = st.selectbox(
                    "Select business unit",
                    bu_options,
                    index=0,
                    key="mg_bu_sel",
                )

                if sel_bu_label == "":
                    st.info("Please select a business unit.")
                else:
                    idx = bu_options.index(sel_bu_label) - 1
                    row = bdf.iloc[idx]
                    buid = int(row["business_unit_id"])

                    colA, colB = st.columns([1, 1])
                    with colA:
                        u_cnt = _refcount(
                            "SELECT COUNT(*) FROM users WHERE business_unit_id=:id",
                            {"id": buid},
                        )
                        bl_cnt = _refcount(
                            "SELECT COUNT(*) FROM business_lines WHERE business_unit_id=:id",
                            {"id": buid},
                        )
                        st.caption(
                            f"Refs → Users: **{u_cnt}** · Business Lines: **{bl_cnt}**"
                        )

                    with colB:
                        st.caption("Status")
                        st.write(
                            "✅ Active"
                            if bool(row["is_active"])
                            else "🚫 Inactive"
                        )

                    st.markdown("---")
                    st.markdown("#### Edit business unit")

                    base_key = f"mg_bu_{buid}"

                    bu_name_edit = st.text_input(
                        "Business Unit Name *",
                        value=row["name"] or "",
                        key=f"{base_key}_name",
                    )

                    active_flag = st.checkbox(
                        "Active",
                        value=bool(row["is_active"]),
                        key=f"{base_key}_active",
                        help="Uncheck to deactivate this business unit.",
                    )

                    if st.button("Save changes", type="primary", key=f"{base_key}_save"):
                        nm_clean = bu_name_edit.strip()
                        if not nm_clean:
                            st.error("Name is required.")
                        else:
                            dup = query_df(
                                """
                                SELECT 1
                                FROM business_units
                                WHERE lower(name)=lower(:n)
                                  AND business_unit_id<>:id
                                """,
                                {"n": nm_clean, "id": buid},
                            )
                            if not dup.empty:
                                st.error(
                                    "A business unit with that name already exists."
                                )
                            else:
                                try:
                                    exec_sql(
                                        """
                                        UPDATE business_units
                                        SET name=:n,
                                            is_active=:b
                                        WHERE business_unit_id=:id
                                        """,
                                        {
                                            "n": nm_clean,
                                            "b": bool(active_flag),
                                            "id": buid,
                                        },
                                    )
                                    st.success("Business Unit updated ✅")
                                except Exception as e:
                                    st.error("Could not update Business Unit.")
                                    st.caption(str(e))

                    st.markdown("---")
                    st.markdown("#### 🔴 Danger Zone")
                    st.write(
                        "Delete this business unit permanently (only if not referenced by users/business lines)."
                    )

                    del_conf_key = f"{base_key}_del_conf"
                    del_confirm = st.checkbox(
                        "I understand this cannot be undone.",
                        key=del_conf_key,
                    )

                    if st.button(
                        "Delete Business Unit",
                        type="primary",
                        disabled=not del_confirm,
                        key=f"{base_key}_del",
                    ):
                        if u_cnt > 0 or bl_cnt > 0:
                            st.error(
                                "Cannot delete: this business unit is referenced by users and/or business lines. "
                                "Deactivate instead."
                            )
                        else:
                            try:
                                exec_sql(
                                    "DELETE FROM business_units WHERE business_unit_id=:id",
                                    {"id": buid},
                                )
                                st.success("Business Unit deleted ✅")

                                # reset selection safely
                                st.session_state.pop("mg_bu_sel", None)

                                # optional: refresh UI so deleted BU disappears immediately
                                st.rerun()

                            except Exception as e:
                                st.error("Delete failed.")
                                st.caption(str(e))

    # =====================================================================
    # 4) BUSINESS LINES
    # =====================================================================
    with main_tabs[3]:
        st.subheader("Business Lines")

        bl_mode = st.radio(
            "Mode",
            ["➕ Add / Import", "📝 Manage"],
            index=0,
            key="bl_mode",
            horizontal=True,
        )

        # ---------------------------------------------------------------
        # Common dropdown data (from existing business_lines)
        # ---------------------------------------------------------------
        sup_df = query_df(
            """
            SELECT DISTINCT supplier
            FROM business_lines
            WHERE supplier IS NOT NULL AND supplier <> ''
            ORDER BY supplier
            """
        )
        supplier_values = [str(r.supplier).strip() for r in sup_df.itertuples(index=False) if str(r.supplier).strip()]
        supplier_options = [""] + supplier_values + ["OTHER"]

        cat_df = query_df(
            """
            SELECT DISTINCT category
            FROM business_lines
            WHERE category IS NOT NULL AND category <> ''
            ORDER BY category
            """
        )
        category_values = [str(r.category).strip() for r in cat_df.itertuples(index=False) if str(r.category).strip()]
        category_options = [""] + category_values + ["OTHER"]

        pg_df = query_df(
            """
            SELECT DISTINCT product_group
            FROM business_lines
            WHERE product_group IS NOT NULL AND product_group <> ''
            ORDER BY product_group
            """
        )
        prod_group_values = [str(r.product_group).strip() for r in pg_df.itertuples(index=False) if str(r.product_group).strip()]
        prod_group_options = [""] + prod_group_values + ["OTHER"]

        # -------------------------
        # MODE 1: Add / Import
        # -------------------------
        if bl_mode == "➕ Add / Import":
            st.markdown("### ➕ Add single business line")

            # ---- Business Units for selection ----
            bu_df_for_bl = query_df(
                """
                SELECT business_unit_id, name
                FROM business_units
                WHERE COALESCE(is_active, TRUE) IS TRUE
                ORDER BY name
                """
            )
            if bu_df_for_bl.empty:
                st.warning("No active Business Units found. Add a Business Unit first.")
            else:
                bu_labels = [""] + bu_df_for_bl["name"].tolist()
                bu_name_to_id = {r.name: int(r.business_unit_id) for r in bu_df_for_bl.itertuples(index=False)}

                # init add state
                st.session_state.setdefault("bl_add_bu", "")
                st.session_state.setdefault("bl_add_name", "")
                st.session_state.setdefault("bl_add_supplier_opt", "")
                st.session_state.setdefault("bl_add_supplier_other", "")
                st.session_state.setdefault("bl_add_category_opt", "")
                st.session_state.setdefault("bl_add_category_other", "")
                st.session_state.setdefault("bl_add_pg_opt", "")
                st.session_state.setdefault("bl_add_pg_other", "")

                # ---- Business Unit ----
                if st.session_state["bl_add_bu"] not in bu_labels:
                    st.session_state["bl_add_bu"] = ""
                bu_sel = st.selectbox(
                    "Business Unit *",
                    bu_labels,
                    key="bl_add_bu",
                )

                # ---- Line Name ----
                bl_name = st.text_input(
                    "Business Line Name *",
                    key="bl_add_name",
                )

                # ---- Supplier ----
                if st.session_state["bl_add_supplier_opt"] not in supplier_options:
                    st.session_state["bl_add_supplier_opt"] = ""
                sup_idx = supplier_options.index(st.session_state["bl_add_supplier_opt"])

                supplier_sel = st.selectbox(
                    "Supplier",
                    supplier_options,
                    index=sup_idx,
                    key="bl_add_supplier_opt",
                )
                if supplier_sel == "OTHER":
                    supplier_other = st.text_input("Other supplier", key="bl_add_supplier_other")
                else:
                    supplier_other = st.session_state.get("bl_add_supplier_other", "")

                # ---- Category (required) ----
                if st.session_state["bl_add_category_opt"] not in category_options:
                    st.session_state["bl_add_category_opt"] = ""
                cat_idx = category_options.index(st.session_state["bl_add_category_opt"])

                category_sel = st.selectbox(
                    "Category *",
                    category_options,
                    index=cat_idx,
                    key="bl_add_category_opt",
                    help="Category is required.",
                )
                if category_sel == "OTHER":
                    category_other = st.text_input("Other category", key="bl_add_category_other")
                else:
                    category_other = st.session_state.get("bl_add_category_other", "")

                # ---- Product Group ----
                if st.session_state["bl_add_pg_opt"] not in prod_group_options:
                    st.session_state["bl_add_pg_opt"] = ""
                pg_idx = prod_group_options.index(st.session_state["bl_add_pg_opt"])

                pg_sel = st.selectbox(
                    "Product Group",
                    prod_group_options,
                    index=pg_idx,
                    key="bl_add_pg_opt",
                )
                if pg_sel == "OTHER":
                    pg_other = st.text_input("Other product group", key="bl_add_pg_other")
                else:
                    pg_other = st.session_state.get("bl_add_pg_other", "")

                # ---- Save button ----
                if st.button("Save Business Line", type="primary", key="bl_add_save"):
                    if not bu_sel:
                        st.error("Business Unit is required.")
                    elif not bl_name.strip():
                        st.error("Business Line Name is required.")
                    elif category_sel == "" or (category_sel == "OTHER" and not (category_other or "").strip()):
                        st.error("Category is required.")
                    else:
                        try:
                            bu_id = bu_name_to_id.get(bu_sel)
                            if not bu_id:
                                st.error("Selected Business Unit not found.")
                            else:
                                # resolve supplier
                                if supplier_sel == "":
                                    supplier_v = None
                                elif supplier_sel == "OTHER":
                                    supplier_v = (supplier_other or "").strip() or None
                                else:
                                    supplier_v = supplier_sel

                                # resolve category
                                if category_sel == "":
                                    category_v = None  # covered by validation above
                                elif category_sel == "OTHER":
                                    category_v = (category_other or "").strip() or None
                                else:
                                    category_v = category_sel

                                # resolve product group
                                if pg_sel == "":
                                    pg_v = None
                                elif pg_sel == "OTHER":
                                    pg_v = (pg_other or "").strip() or None
                                else:
                                    pg_v = pg_sel

                                with engine.begin() as conn:
                                    res = conn.execute(
                                        text(
                                            """
                                            INSERT INTO business_lines(
                                                business_unit_id, name, supplier, category, product_group, is_active
                                            )
                                            VALUES (:bid, :name, :supplier, :category, :pg, TRUE)
                                            ON CONFLICT (business_unit_id, name) DO NOTHING
                                            """
                                        ),
                                        {
                                            "bid": bu_id,
                                            "name": bl_name.strip(),
                                            "supplier": supplier_v,
                                            "category": category_v,
                                            "pg": pg_v,
                                        },
                                    )

                                if (res.rowcount or 0) > 0:
                                    st.success("Business Line added ✅")
                                    for key in (
                                        "bl_add_bu",
                                        "bl_add_name",
                                        "bl_add_supplier_opt",
                                        "bl_add_supplier_other",
                                        "bl_add_category_opt",
                                        "bl_add_category_other",
                                        "bl_add_pg_opt",
                                        "bl_add_pg_other",
                                    ):
                                        st.session_state.pop(key, None)
                                    st.rerun()     # optional but nice
                                else:
                                    st.info("That Business Unit + Business Line Name already exists — nothing added.")
                        except Exception as e:
                            st.error("Could not add Business Line.")
                            st.caption(str(e))

            st.markdown("---")
            st.markdown("### ⬆️ Bulk upload business lines (Excel/CSV)")
            st.write("Columns: **business_unit**, **name**, **category**  (optional: supplier, product_group)")

            fbl = st.file_uploader("Upload Business Lines file", type=["xlsx", "csv"], key="blines")
            if fbl is not None:
                df = _read_df_upload(fbl)
                st.caption(f"Detected columns: {list(df.columns)}")
                needed = {"business_unit", "name", "category"}
                if not needed.issubset(set(df.columns)):
                    missing = sorted(list(needed - set(df.columns)))
                    st.error(f"Missing required columns: {', '.join(missing)}")
                else:
                    total = len(df)
                    inserted, skipped = 0, 0
                    sts, pb, ln, has_status = _mk_status("Importing Business Lines…")
                    try:
                        with engine.begin() as conn:
                            budf = pd.read_sql_query(text("SELECT business_unit_id, name FROM business_units"), conn)
                            bumap = {str(r.name).strip().lower(): int(r.business_unit_id) for r in budf.itertuples(index=False)}

                            for i, r in enumerate(df.itertuples(index=False), start=1):
                                bu_name_raw = (
                                    str(getattr(r, "business_unit")) if hasattr(r, "business_unit") and pd.notna(getattr(r, "business_unit")) else ""
                                ).strip()
                                bl_name_raw = (
                                    str(getattr(r, "name")) if hasattr(r, "name") and pd.notna(getattr(r, "name")) else ""
                                ).strip()
                                cat_raw = (
                                    str(getattr(r, "category")) if hasattr(r, "category") and pd.notna(getattr(r, "category")) else ""
                                ).strip()

                                if not (bu_name_raw and bl_name_raw and cat_raw):
                                    skipped += 1
                                    if i % 200 == 0 or i == total:
                                        _update_progress(pb, ln, i, total, inserted, 0, skipped, label_prefix="Business Lines")
                                    continue

                                bu_id_tmp = bumap.get(bu_name_raw.lower())
                                if not bu_id_tmp:
                                    skipped += 1
                                    if i % 200 == 0 or i == total:
                                        _update_progress(pb, ln, i, total, inserted, 0, skipped, label_prefix="Business Lines")
                                    continue

                                supplier_v = (
                                    str(getattr(r, "supplier")).strip()
                                    if hasattr(r, "supplier") and pd.notna(getattr(r, "supplier"))
                                    else None
                                )
                                prod_group_v = (
                                    str(getattr(r, "product_group")).strip()
                                    if hasattr(r, "product_group") and pd.notna(getattr(r, "product_group"))
                                    else None
                                )

                                res = conn.execute(
                                    text(
                                        """
                                        INSERT INTO business_lines(
                                            business_unit_id, name, supplier, category, product_group, is_active
                                        )
                                        VALUES (:bid, :name, :supplier, :category, :pg, TRUE)
                                        ON CONFLICT (business_unit_id, name) DO NOTHING
                                        """
                                    ),
                                    {
                                        "bid": bu_id_tmp,
                                        "name": bl_name_raw,
                                        "supplier": supplier_v,
                                        "category": cat_raw,
                                        "pg": prod_group_v,
                                    },
                                )
                                if (res.rowcount or 0) > 0:
                                    inserted += 1
                                else:
                                    skipped += 1

                                if i % 200 == 0 or i == total:
                                    _update_progress(pb, ln, i, total, inserted, 0, skipped, label_prefix="Business Lines")
                                    time.sleep(0.001)

                        _finish_status(
                            sts,
                            has_status,
                            f"Business Lines import ✅ Inserted: {inserted} | Skipped: {skipped}",
                            ok=True,
                        )
                    except Exception as e:
                        _finish_status(sts, has_status, "Business Lines import failed ❌", ok=False)
                        st.caption(str(e))

        # -------------------------
        # MODE 2: Manage
        # -------------------------
        else:  # bl_mode == "📝 Manage"
            st.markdown("### 📝 Manage business lines")

            bll = query_df(
                """
                SELECT bl.business_line_id,
                       bl.name,
                       bl.supplier,
                       bl.category,
                       bl.product_group,
                       COALESCE(bl.is_active, TRUE) AS is_active,
                       bl.business_unit_id,
                       bu.name AS business_unit
                FROM business_lines bl
                JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
                ORDER BY bu.name, bl.name
                """
            )

            if bll.empty:
                st.info("No business lines yet.")
            else:
                def _fmt_bl(r):
                    return " - ".join(
                        [
                            str(r.business_unit),
                            str(r.name),
                            str(r.category or ""),
                            str(r.product_group or ""),
                        ]
                    ).replace(" - None", "").replace("None", "").strip(" -")

                options = [
                    f"{r.business_line_id} - {_fmt_bl(r)}  ({'active' if bool(r.is_active) else 'inactive'})"
                    for r in bll.itertuples(index=False)
                ]
                options = [""] + options

                sel_label = st.selectbox("Select business line", options, index=0, key="mg_bl_sel")

                if sel_label == "":
                    st.info("Please select a business line.")
                else:
                    row_idx = options.index(sel_label) - 1
                    row = bll.iloc[row_idx]
                    blid = int(row["business_line_id"])

                    # quick refs / status
                    colA, colB = st.columns([1, 1])
                    with colA:
                        i_cnt = _refcount("SELECT COUNT(*) FROM items WHERE business_line_id=:id", {"id": blid})
                        v_cnt = _refcount("SELECT COUNT(*) FROM visits WHERE business_line_id=:id", {"id": blid})
                        st.caption(f"Refs → Items: **{i_cnt}** · Visits: **{v_cnt}**")
                    with colB:
                        st.caption("Status")
                        st.write("✅ Active" if bool(row["is_active"]) else "🚫 Inactive")

                    st.markdown("---")
                    st.markdown("#### Edit business line")

                    base_key = f"mg_bl_{blid}"

                    # ---- Business Unit dropdown ----
                    bu_df = query_df(
                        """
                        SELECT business_unit_id, name
                        FROM business_units
                        WHERE COALESCE(is_active, TRUE) IS TRUE
                        ORDER BY name
                        """
                    )
                    bu_labels = bu_df["name"].tolist()
                    if not bu_labels:
                        st.warning("No active Business Units found.")
                        bu_idx = 0
                    else:
                        bu_idx = 0
                        if pd.notna(row["business_unit_id"]):
                            for i, r2 in enumerate(bu_df.itertuples(index=False)):
                                if int(r2.business_unit_id) == int(row["business_unit_id"]):
                                    bu_idx = i
                                    break

                    sel_bu_label = st.selectbox(
                        "Business Unit *",
                        bu_labels if bu_labels else [],
                        index=bu_idx if bu_labels else 0,
                        key=f"{base_key}_bu",
                    )

                    if bu_labels:
                        new_bu_id = int(
                            bu_df.loc[bu_df["name"] == sel_bu_label, "business_unit_id"].iloc[0]
                        )
                    else:
                        new_bu_id = None

                    # ---- Name ----
                    bl_name_edit = st.text_input(
                        "Business Line Name *",
                        value=row["name"] or "",
                        key=f"{base_key}_name",
                    )

                    # ---- Supplier dropdown + OTHER ----
                    sup_key = base_key + "_supplier_opt"
                    sup_other_key = base_key + "_supplier_other"

                    if sup_key not in st.session_state:
                        existing_sup = (row["supplier"] or "").strip() if row["supplier"] else ""
                        if existing_sup and existing_sup in supplier_options:
                            st.session_state[sup_key] = existing_sup
                        elif existing_sup:
                            st.session_state[sup_key] = "OTHER"
                            st.session_state[sup_other_key] = existing_sup
                        else:
                            st.session_state[sup_key] = ""

                    if st.session_state[sup_key] not in supplier_options:
                        st.session_state[sup_key] = ""

                    sup_idx_edit = supplier_options.index(st.session_state[sup_key])
                    supplier_sel_edit = st.selectbox(
                        "Supplier",
                        supplier_options,
                        index=sup_idx_edit,
                        key=sup_key,
                    )
                    if supplier_sel_edit == "OTHER":
                        supplier_other_edit = st.text_input("Other supplier", key=sup_other_key)
                    else:
                        supplier_other_edit = st.session_state.get(sup_other_key, "")

                    # ---- Category dropdown + OTHER (required) ----
                    cat_key = base_key + "_category_opt"
                    cat_other_key = base_key + "_category_other"

                    if cat_key not in st.session_state:
                        existing_cat = (row["category"] or "").strip() if row["category"] else ""
                        if existing_cat and existing_cat in category_options:
                            st.session_state[cat_key] = existing_cat
                        elif existing_cat:
                            st.session_state[cat_key] = "OTHER"
                            st.session_state[cat_other_key] = existing_cat
                        else:
                            st.session_state[cat_key] = ""

                    if st.session_state[cat_key] not in category_options:
                        st.session_state[cat_key] = ""

                    cat_idx_edit = category_options.index(st.session_state[cat_key])
                    category_sel_edit = st.selectbox(
                        "Category *",
                        category_options,
                        index=cat_idx_edit,
                        key=cat_key,
                        help="Category is required.",
                    )
                    if category_sel_edit == "OTHER":
                        category_other_edit = st.text_input("Other category", key=cat_other_key)
                    else:
                        category_other_edit = st.session_state.get(cat_other_key, "")

                    # ---- Product Group dropdown + OTHER ----
                    pg_key = base_key + "_pg_opt"
                    pg_other_key = base_key + "_pg_other"

                    if pg_key not in st.session_state:
                        existing_pg = (row["product_group"] or "").strip() if row["product_group"] else ""
                        if existing_pg and existing_pg in prod_group_options:
                            st.session_state[pg_key] = existing_pg
                        elif existing_pg:
                            st.session_state[pg_key] = "OTHER"
                            st.session_state[pg_other_key] = existing_pg
                        else:
                            st.session_state[pg_key] = ""

                    if st.session_state[pg_key] not in prod_group_options:
                        st.session_state[pg_key] = ""

                    pg_idx_edit = prod_group_options.index(st.session_state[pg_key])
                    pg_sel_edit = st.selectbox(
                        "Product Group",
                        prod_group_options,
                        index=pg_idx_edit,
                        key=pg_key,
                    )
                    if pg_sel_edit == "OTHER":
                        pg_other_edit = st.text_input("Other product group", key=pg_other_key)
                    else:
                        pg_other_edit = st.session_state.get(pg_other_key, "")

                    # ---- Active toggle ----
                    active_flag = st.checkbox(
                        "Active",
                        value=bool(row["is_active"]),
                        key=f"{base_key}_active",
                        help="Uncheck to deactivate this business line.",
                    )

                    # ---- Save button ----
                    if st.button("Save changes", type="primary", key=f"{base_key}_save"):
                        if not new_bu_id:
                            st.error("Business Unit is required.")
                        elif not bl_name_edit.strip():
                            st.error("Business Line Name is required.")
                        elif category_sel_edit == "" or (category_sel_edit == "OTHER" and not (category_other_edit or "").strip()):
                            st.error("Category is required.")
                        else:
                            # resolve supplier
                            if supplier_sel_edit == "":
                                supplier_v = None
                            elif supplier_sel_edit == "OTHER":
                                supplier_v = (supplier_other_edit or "").strip() or None
                            else:
                                supplier_v = supplier_sel_edit

                            # resolve category
                            if category_sel_edit == "":
                                category_v = None
                            elif category_sel_edit == "OTHER":
                                category_v = (category_other_edit or "").strip() or None
                            else:
                                category_v = category_sel_edit

                            # resolve product group
                            if pg_sel_edit == "":
                                pg_v = None
                            elif pg_sel_edit == "OTHER":
                                pg_v = (pg_other_edit or "").strip() or None
                            else:
                                pg_v = pg_sel_edit

                            # check duplicate name within BU
                            dup = query_df(
                                """
                                SELECT 1
                                FROM business_lines
                                WHERE business_unit_id=:bid
                                  AND lower(name)=lower(:nm)
                                  AND business_line_id<>:id
                                """,
                                {"bid": new_bu_id, "nm": bl_name_edit.strip(), "id": blid},
                            )
                            if not dup.empty:
                                st.error("A business line with that name already exists in the selected Business Unit.")
                            else:
                                try:
                                    exec_sql(
                                        """
                                        UPDATE business_lines
                                        SET business_unit_id=:bid,
                                            name=:name,
                                            supplier=:supplier,
                                            category=:cat,
                                            product_group=:pg,
                                            is_active=:b
                                        WHERE business_line_id=:id
                                        """,
                                        {
                                            "bid": new_bu_id,
                                            "name": bl_name_edit.strip(),
                                            "supplier": supplier_v,
                                            "cat": category_v,
                                            "pg": pg_v,
                                            "b": bool(active_flag),
                                            "id": blid,
                                        },
                                    )
                                    st.success("Business Line updated ✅")
                                except Exception as e:
                                    st.error("Could not update Business Line.")
                                    st.caption(str(e))

                    st.markdown("---")
                    st.markdown("#### 🔴 Danger Zone")
                    st.write("Delete this business line permanently (only if not referenced by items/visits).")

                    del_conf_key = f"{base_key}_del_conf"
                    del_confirm = st.checkbox(
                        "I understand this cannot be undone.",
                        key=del_conf_key,
                    )

                    if st.button(
                        "Delete Business Line",
                        type="primary",
                        disabled=not del_confirm,
                        key=f"{base_key}_del",
                    ):
                        if i_cnt > 0 or v_cnt > 0:
                            st.error(
                                "Cannot delete: this business line is referenced by items and/or visits. "
                                "Deactivate instead."
                            )
                        else:
                            try:
                                exec_sql("DELETE FROM business_lines WHERE business_line_id=:id", {"id": blid})
                                st.success("Business Line deleted ✅")
                                st.session_state.pop("mg_bl_sel", None)
                                st.rerun()
                            except Exception as e:
                                st.error("Delete failed.")
                                st.caption(str(e))

    # =====================
    # 5) Items (Products)
    # =====================
    with main_tabs[4]:
        st.subheader("Items (Products)")
        st.caption("Items are tied to Business Lines, which are tied to Business Units.")

        item_mode = st.radio(
            "Mode",
            ["➕ Add / Import", "📝 Manage"],
            index=0,
            key="item_mode",
            horizontal=True,
        )

        # Common BU data (active only)
        bu_df_all = query_df(
            """
            SELECT business_unit_id, name
            FROM business_units
            WHERE COALESCE(is_active, TRUE) IS TRUE
            ORDER BY name
            """
        )

        # -------------------------
        # MODE 1: Add / Import
        # -------------------------
        if item_mode == "➕ Add / Import":
            st.markdown("### ➕ Add single item")
            st.caption("Required fields: **Product ID**, **Article Number**, **Business Unit**, **Business Line**.")

            if bu_df_all.empty:
                st.warning("No active Business Units found. Add one in the Business Units tab first.")
            else:
                # ---- Business Unit select ----
                bu_labels = [""] + bu_df_all["name"].tolist()
                bu_ids = [None] + bu_df_all["business_unit_id"].astype(int).tolist()

                bu_idx = st.selectbox(
                    "Business Unit *",
                    options=list(range(len(bu_labels))),
                    format_func=lambda i: bu_labels[i],
                    index=0,
                    key="item_add_bu_idx",
                )
                selected_bu_id = bu_ids[bu_idx]

                # ---- Business Line select (depends on BU) ----
                if selected_bu_id is not None:
                    bl_df = query_df(
                        """
                        SELECT business_line_id, name
                        FROM business_lines
                        WHERE COALESCE(is_active, TRUE) IS TRUE
                          AND business_unit_id = :bid
                        ORDER BY name
                        """,
                        {"bid": int(selected_bu_id)},
                    )
                else:
                    bl_df = pd.DataFrame(columns=["business_line_id", "name"])

                bl_labels = [""] + (bl_df["name"].tolist() if not bl_df.empty else [])
                bl_ids = [None] + (bl_df["business_line_id"].astype(int).tolist() if not bl_df.empty else [])

                bl_idx = st.selectbox(
                    "Business Line *",
                    options=list(range(len(bl_labels))),
                    format_func=lambda i: bl_labels[i],
                    index=0,
                    key="item_add_bl_idx",
                    help="Pick a Business Unit first to see its lines.",
                )
                selected_bl_id = bl_ids[bl_idx]

                # ---- Item fields ----
                pid = st.text_input("Product ID * (must be unique)", key="item_add_pid")
                article = st.text_input("Article Number *", key="item_add_article")
                desc = st.text_input("Description", key="item_add_desc")

                if st.button("Save Item", type="primary", key="item_add_save"):
                    if not pid.strip():
                        st.error("Product ID is required.")
                    elif not article.strip():
                        st.error("Article Number is required.")
                    elif selected_bu_id is None or selected_bl_id is None:
                        st.error("Business Unit and Business Line are required.")
                    else:
                        try:
                            with engine.begin() as conn:
                                res = conn.execute(
                                    text(
                                        """
                                        INSERT INTO items(
                                            product_id, article_number, description, business_line_id, is_active
                                        ) VALUES (
                                            :pid, :article, :desc, :blid, TRUE
                                        )
                                        ON CONFLICT (product_id) DO NOTHING
                                        """
                                    ),
                                    {
                                        "pid": pid.strip(),
                                        "article": article.strip(),
                                        "desc": (desc.strip() or None),
                                        "blid": int(selected_bl_id),
                                    },
                                )
                            if (res.rowcount or 0) > 0:
                                st.success("Item added ✅")
                                # reset widget-backed keys by removing them from session_state
                                for key in (
                                    "item_add_pid",
                                    "item_add_article",
                                    "item_add_desc",
                                    "item_add_bu_idx",
                                    "item_add_bl_idx",
                                ):
                                    st.session_state.pop(key, None)
                                st.rerun()
                            else:
                                st.error("That Product ID already exists.")
                        except Exception as e:
                            st.error("Could not add item.")
                            st.caption(str(e))

            st.markdown("---")
            st.markdown("### ⬆️ Bulk upload items (Excel/CSV)")
            st.write("Columns: **product_id**, **business_unit**, **business_line**, article_number, description")

            # Build resolver map for BU + BL name → business_line_id
            _bl_map_df = query_df(
                """
                SELECT bu.name AS bu_name,
                       bl.name AS bl_name,
                       bl.business_line_id AS bl_id
                FROM business_lines bl
                JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
                WHERE COALESCE(bu.is_active, TRUE) IS TRUE
                  AND COALESCE(bl.is_active, TRUE) IS TRUE
                ORDER BY bu.name, bl.name
                """
            )
            bu_to_bls = {}
            for r in _bl_map_df.itertuples(index=False):
                bu_to_bls.setdefault(str(r.bu_name).strip(), []).append((str(r.bl_name).strip(), int(r.bl_id)))

            f3 = st.file_uploader("Upload Items file", type=["xlsx", "csv"], key="items_upload")
            if f3 is not None:
                df = _read_df_upload(f3)
                needed = {"product_id", "business_unit", "business_line"}
                if not needed.issubset(df.columns):
                    st.error("Missing required columns: product_id, business_unit, business_line")
                else:
                    total = len(df)
                    inserted = 0
                    updated = 0
                    skipped = 0
                    sts, pb, ln, has_status = _mk_status("Importing Items…")
                    try:
                        with engine.begin() as conn:
                            existing = set(
                                pd.read_sql_query(text("SELECT product_id FROM items"), conn)[
                                    "product_id"
                                ].astype(str).tolist()
                            )

                            for i, r in enumerate(df.itertuples(index=False), start=1):
                                pid_raw = getattr(r, "product_id", None)
                                pid = (str(pid_raw).strip() if pd.notna(pid_raw) else "")

                                bu_name_raw = (
                                    str(getattr(r, "business_unit", "")).strip()
                                    if hasattr(r, "business_unit")
                                    else ""
                                )
                                bl_name_raw = (
                                    str(getattr(r, "business_line", "")).strip()
                                    if hasattr(r, "business_line")
                                    else ""
                                )

                                if not (pid and bu_name_raw and bl_name_raw):
                                    skipped += 1
                                    if i % 200 == 0 or i == total:
                                        _update_progress(
                                            pb, ln, i, total, inserted, updated, skipped, label_prefix="Items"
                                        )
                                    continue

                                # resolve BL from mapping
                                bl_id = None
                                if bu_name_raw in bu_to_bls:
                                    for name, _id in bu_to_bls[bu_name_raw]:
                                        if name == bl_name_raw:
                                            bl_id = _id
                                            break

                                if not bl_id:
                                    skipped += 1
                                    if i % 200 == 0 or i == total:
                                        _update_progress(
                                            pb, ln, i, total, inserted, updated, skipped, label_prefix="Items"
                                        )
                                    continue

                                article_v = (
                                    str(getattr(r, "article_number")).strip()
                                    if hasattr(r, "article_number") and pd.notna(getattr(r, "article_number"))
                                    else None
                                )
                                desc_v = (
                                    str(getattr(r, "description")).strip()
                                    if hasattr(r, "description") and pd.notna(getattr(r, "description"))
                                    else None
                                )

                                conn.execute(
                                    text(
                                        """
                                        INSERT INTO items(product_id, article_number, description, business_line_id, is_active)
                                        VALUES (:pid, :article, :desc, :blid, TRUE)
                                        ON CONFLICT (product_id) DO UPDATE
                                        SET article_number   = EXCLUDED.article_number,
                                            description      = EXCLUDED.description,
                                            business_line_id = EXCLUDED.business_line_id,
                                            is_active        = TRUE
                                        """
                                    ),
                                    {"pid": pid, "article": article_v, "desc": desc_v, "blid": int(bl_id)},
                                )

                                if pid in existing:
                                    updated += 1
                                else:
                                    inserted += 1

                                if i % 200 == 0 or i == total:
                                    _update_progress(
                                        pb, ln, i, total, inserted, updated, skipped, label_prefix="Items"
                                    )
                                    time.sleep(0.001)

                        _finish_status(
                            sts,
                            has_status,
                            f"Items import ✅ Inserted: {inserted} | Updated: {updated} | Skipped: {skipped}",
                            ok=True,
                        )
                    except Exception as e:
                        _finish_status(sts, has_status, "Items import failed ❌", ok=False)
                        st.caption(str(e))

        # -------------------------
        # MODE 2: Manage
        # -------------------------
        else:  # item_mode == "📝 Manage"
            st.markdown("### 📝 Manage items")

            idf = query_df(
                """
                SELECT i.product_id,
                       i.article_number,
                       i.description,
                       COALESCE(i.is_active, TRUE) AS is_active,
                       bl.business_line_id,
                       bl.name AS business_line,
                       bu.name AS business_unit
                FROM items i
                JOIN business_lines bl ON bl.business_line_id = i.business_line_id
                JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
                ORDER BY COALESCE(i.article_number, i.product_id)
                """
            )

            if idf.empty:
                st.info("No items yet.")
            else:
                def _fmt_item(r):
                    art = (str(r.article_number).strip()
                           if pd.notna(r.article_number) and str(r.article_number).strip()
                           else "")
                    bu = (str(r.business_unit).strip()
                          if pd.notna(r.business_unit) and str(r.business_unit).strip()
                          else "")
                    bl = (str(r.business_line).strip()
                          if pd.notna(r.business_line) and str(r.business_line).strip()
                          else "")
                    desc = (str(r.description).strip()
                            if pd.notna(r.description) and str(r.description).strip()
                            else "")
                    base = " - ".join([p for p in [art, bu, bl, desc] if p])
                    return base or str(r.product_id)

                options = [""] + [
                    f"{_fmt_item(r)}  ({'active' if r.is_active else 'inactive'})"
                    for r in idf.itertuples(index=False)
                ]

                sel_label = st.selectbox(
                    "Select item",
                    options,
                    index=0,
                    key="mg_item_sel",
                )

                if sel_label == "":
                    st.info("Please select an item.")
                else:
                    row_idx = options.index(sel_label) - 1
                    row = idf.iloc[row_idx]
                    pid = str(row["product_id"])

                    colA, colB = st.columns([1, 1])
                    with colA:
                        v_cnt = _refcount(
                            "SELECT COUNT(*) FROM visits WHERE product_id=:pid",
                            {"pid": pid},
                        )
                        st.caption(f"Refs → Visits: **{v_cnt}**")
                    with colB:
                        st.caption("Status")
                        st.write("✅ Active" if bool(row["is_active"]) else "🚫 Inactive")

                    st.markdown("---")
                    st.markdown("#### Edit item")

                    base_key = f"mg_item_{pid}"

                    # ---- Account / article / description ----
                    art_edit = st.text_input(
                        "Article Number (unique)",
                        value=row["article_number"] or "",
                        key=f"{base_key}_article",
                    )
                    desc_edit = st.text_input(
                        "Description",
                        value=row["description"] or "",
                        key=f"{base_key}_desc",
                    )

                    # ---- Business Unit dropdown ----
                    bu_df = query_df(
                        """
                        SELECT business_unit_id, name
                        FROM business_units
                        WHERE COALESCE(is_active, TRUE) IS TRUE
                        ORDER BY name
                        """
                    )
                    bu_labels = bu_df["name"].tolist()
                    bu_ids = bu_df["business_unit_id"].astype(int).tolist()

                    current_bu_name = row["business_unit"]
                    try:
                        bu_idx = bu_labels.index(current_bu_name)
                    except ValueError:
                        bu_idx = 0 if bu_labels else 0

                    bu_label = st.selectbox(
                        "Business Unit *",
                        bu_labels,
                        index=bu_idx if bu_labels else 0,
                        key=f"{base_key}_bu",
                    )
                    sel_bu_id = int(bu_df.loc[bu_df["name"] == bu_label, "business_unit_id"].iloc[0]) if not bu_df.empty else None

                    # ---- Business Line dropdown (depends on BU) ----
                    if sel_bu_id is not None:
                        bl_df = query_df(
                            """
                            SELECT business_line_id, name
                            FROM business_lines
                            WHERE COALESCE(is_active, TRUE) IS TRUE
                              AND business_unit_id = :bid
                            ORDER BY name
                            """,
                            {"bid": sel_bu_id},
                        )
                    else:
                        bl_df = pd.DataFrame(columns=["business_line_id", "name"])

                    bl_labels = bl_df["name"].tolist() if not bl_df.empty else []
                    bl_ids = bl_df["business_line_id"].astype(int).tolist() if not bl_df.empty else []

                    current_bl_id = int(row["business_line_id"])
                    bl_idx = 0
                    for i, r in enumerate(bl_df.itertuples(index=False)):
                        if int(r.business_line_id) == current_bl_id:
                            bl_idx = i
                            break

                    bl_label = st.selectbox(
                        "Business Line *",
                        bl_labels,
                        index=bl_idx if bl_labels else 0,
                        key=f"{base_key}_bl",
                    )
                    sel_bl_id = (
                        int(bl_df.loc[bl_df["name"] == bl_label, "business_line_id"].iloc[0])
                        if bl_labels
                        else None
                    )

                    # ---- Active flag ----
                    active_flag = st.checkbox(
                        "Active",
                        value=bool(row["is_active"]),
                        key=f"{base_key}_active",
                    )

                    if st.button("Save changes", type="primary", key=f"{base_key}_save"):
                        if not sel_bl_id:
                            st.error("Business Line is required.")
                        else:
                            try:
                                if art_edit.strip():
                                    dup = query_df(
                                        """
                                        SELECT 1
                                        FROM items
                                        WHERE lower(article_number)=lower(:a)
                                          AND product_id<>:pid
                                        """,
                                        {"a": art_edit.strip(), "pid": pid},
                                    )
                                    if not dup.empty:
                                        st.error("Article Number already exists.")
                                    else:
                                        exec_sql(
                                            """
                                            UPDATE items
                                            SET article_number=:a,
                                                description=:d,
                                                business_line_id=:bl,
                                                is_active=:b
                                            WHERE product_id=:pid
                                            """,
                                            {
                                                "a": art_edit.strip(),
                                                "d": (desc_edit.strip() or None),
                                                "bl": sel_bl_id,
                                                "b": bool(active_flag),
                                                "pid": pid,
                                            },
                                        )
                                        st.success("Item updated ✅")
                                else:
                                    exec_sql(
                                        """
                                        UPDATE items
                                        SET article_number=NULL,
                                            description=:d,
                                            business_line_id=:bl,
                                            is_active=:b
                                        WHERE product_id=:pid
                                        """,
                                        {
                                            "d": (desc_edit.strip() or None),
                                            "bl": sel_bl_id,
                                            "b": bool(active_flag),
                                            "pid": pid,
                                        },
                                    )
                                    st.success("Item updated ✅")
                            except Exception as e:
                                st.error("Could not update item.")
                                st.caption(str(e))

                    st.markdown("---")
                    st.markdown("#### 🔴 Danger Zone")
                    st.write("Delete this item permanently (only if not referenced by visits).")

                    del_conf_key = f"{base_key}_del_conf"
                    del_confirm = st.checkbox(
                        "I understand this cannot be undone.",
                        key=del_conf_key,
                    )

                    if st.button(
                        "Delete Item",
                        type="primary",
                        disabled=not del_confirm,
                        key=f"{base_key}_del",
                    ):
                        if v_cnt > 0:
                            st.error(
                                "Cannot delete: this item is referenced by visits. "
                                "Deactivate instead."
                            )
                        else:
                            try:
                                exec_sql("DELETE FROM items WHERE product_id=:pid", {"pid": pid})
                                st.success("Item deleted ✅")
                                st.session_state["mg_item_sel"] = ""
                            except Exception as e:
                                st.error("Delete failed.")
                                st.caption(str(e))

    # =====================================================================
    # 6) OBJECTIVES
    # =====================================================================
    with main_tabs[5]:
        st.subheader("Objectives")

        # Stable mode selector
        mode = st.radio(
            "Mode",
            ["➕ Add / Import", "📝 Manage"],
            index=0,
            key="obj_mode",
            horizontal=True
        )

        # ---------------------------------------------------
        # Load base category options
        # ---------------------------------------------------
        cat_df = query_df("""
            SELECT DISTINCT category
            FROM objectives
            WHERE category IS NOT NULL AND category <> ''
            ORDER BY category
        """)

        existing_cats = [str(r.category).strip() for r in cat_df.itertuples(index=False) if str(r.category).strip()]
        category_options = [""] + existing_cats + ["OTHER"]

        # ---------------------------------------------------
        # MODE 1 — ADD / IMPORT
        # ---------------------------------------------------
        if mode == "➕ Add / Import":
            st.markdown("### ➕ Add new Objective")
            st.caption("Required fields: **Name**. Category is optional.")

            # init state
            st.session_state.setdefault("obj_add_name", "")
            st.session_state.setdefault("obj_add_cat_opt", "")
            st.session_state.setdefault("obj_add_cat_other", "")

            # Name
            obj_name = st.text_input("Objective Name *", key="obj_add_name")

            # Category dropdown
            if st.session_state["obj_add_cat_opt"] not in category_options:
                st.session_state["obj_add_cat_opt"] = ""
            cat_idx = category_options.index(st.session_state["obj_add_cat_opt"])

            cat_sel = st.selectbox(
                "Category",
                category_options,
                index=cat_idx,
                key="obj_add_cat_opt"
            )
            if cat_sel == "OTHER":
                cat_other = st.text_input("Other category", key="obj_add_cat_other")
            else:
                cat_other = st.session_state.get("obj_add_cat_other", "")

            # Save objective
            if st.button("Save Objective", type="primary", key="obj_add_save"):
                if not obj_name.strip():
                    st.error("Objective name is required.")
                else:
                    name_v = obj_name.strip()

                    # resolve category
                    if cat_sel == "":
                        cat_v = None
                    elif cat_sel == "OTHER":
                        cat_v = (cat_other or "").strip() or None
                    else:
                        cat_v = cat_sel

                    try:
                        with engine.begin() as conn:
                            res = conn.execute(
                                text("""
                                    INSERT INTO objectives(name, category, is_active)
                                    SELECT :n, :c, TRUE
                                    WHERE NOT EXISTS (
                                        SELECT 1 FROM objectives
                                        WHERE lower(name)=lower(:n)
                                        AND lower(coalesce(category,''))=lower(coalesce(:c,''))
                                    )
                                """),
                                {"n": name_v, "c": cat_v}
                            )

                        if (res.rowcount or 0) > 0:
                            st.success("Objective added ✅")
                            # Reset widget-backed keys safely
                            for key in (
                                "obj_add_name",
                                "obj_add_cat_opt",
                                "obj_add_cat_other",
                            ):
                                st.session_state.pop(key, None)
                            st.rerun()
                        else:
                            st.info("This objective already exists — nothing added.")
                    except Exception as e:
                        st.error("Could not add objective.")
                        st.caption(str(e))

            # Bulk import
            st.markdown("---")
            st.markdown("### ⬆️ Bulk upload objectives (Excel/CSV)")
            st.write("Columns: **name**, category (optional)")

            fobj = st.file_uploader("Upload file", type=["xlsx", "csv"], key="obj_file")
            if fobj is not None:
                df = _read_df_upload(fobj)

                if "name" not in df.columns:
                    st.error("Missing required column: `name`")
                else:
                    total = len(df)
                    inserted = 0
                    skipped = 0
                    sts, pb, ln, has_status = _mk_status("Importing Objectives…")

                    try:
                        with engine.begin() as conn:
                            for i, r in enumerate(df.itertuples(index=False), start=1):
                                nm = str(getattr(r, "name", "")).strip()
                                cat_raw = getattr(r, "category", None)

                                if not nm:
                                    skipped += 1
                                else:
                                    cat_v = str(cat_raw).strip() if cat_raw and pd.notna(cat_raw) else None

                                    res = conn.execute(
                                        text("""
                                            INSERT INTO objectives(name, category, is_active)
                                            SELECT :n, :c, TRUE
                                            WHERE NOT EXISTS (
                                                SELECT 1 FROM objectives
                                                WHERE lower(name)=lower(:n)
                                                AND lower(coalesce(category,''))=lower(coalesce(:c,''))
                                            )
                                        """),
                                        {"n": nm, "c": cat_v}
                                    )
                                    if (res.rowcount or 0) > 0:
                                        inserted += 1
                                    else:
                                        skipped += 1

                                if i % 200 == 0 or i == total:
                                    _update_progress(pb, ln, i, total, inserted, 0, skipped, "Objectives")
                                    time.sleep(0.001)

                        _finish_status(
                            sts,
                            has_status,
                            f"Objectives import ✅ Inserted: {inserted} | Skipped: {skipped}",
                            True,
                        )

                    except Exception as e:
                        _finish_status(sts, has_status, "Objectives import failed ❌", False)
                        st.caption(str(e))

        # ---------------------------------------------------
        # MODE 2 — MANAGE
        # ---------------------------------------------------
        else:
            st.markdown("### 📝 Manage Objectives")

            odf = query_df("""
                SELECT objective_id,
                    name,
                    category,
                    COALESCE(is_active, TRUE) AS is_active
                FROM objectives
                ORDER BY name
            """)

            if odf.empty:
                st.info("No objectives yet.")
            else:
                # display format: [ID] Name (status)
                display = [""] + [
                    f"[{r.objective_id}] {r.name} ({'active' if bool(r.is_active) else 'inactive'})"
                    for r in odf.itertuples(index=False)
                ]

                sel = st.selectbox("Select objective", display, index=0, key="mg_obj_sel")

                if sel == "":
                    st.info("Select an objective to edit or update.")
                else:
                    row_idx = display.index(sel) - 1
                    row = odf.iloc[row_idx]
                    oid = int(row["objective_id"])

                    # reference count
                    v_cnt = _refcount(
                        "SELECT COUNT(*) FROM visits WHERE objective_id=:id", {"id": oid}
                    )

                    st.caption(f"Referenced in visits: **{v_cnt}**")
                    st.markdown("---")
                    st.markdown("#### Edit Objective")

                    base_key = f"mg_obj_{oid}"

                    # ---- Name ----
                    name_edit = st.text_input(
                        "Objective name *",
                        value=row["name"] or "",
                        key=f"{base_key}_name"
                    )

                    # ---- Category ----
                    cat_key = f"{base_key}_cat_opt"
                    cat_other_key = f"{base_key}_cat_other"

                    existing_cat = (row["category"] or "").strip()

                    if cat_key not in st.session_state:
                        if existing_cat and existing_cat in category_options:
                            st.session_state[cat_key] = existing_cat
                        elif existing_cat:
                            st.session_state[cat_key] = "OTHER"
                            st.session_state[cat_other_key] = existing_cat
                        else:
                            st.session_state[cat_key] = ""

                    if st.session_state[cat_key] not in category_options:
                        st.session_state[cat_key] = ""

                    cat_idx_edit = category_options.index(st.session_state[cat_key])
                    cat_sel_edit = st.selectbox(
                        "Category",
                        category_options,
                        index=cat_idx_edit,
                        key=cat_key
                    )

                    if cat_sel_edit == "OTHER":
                        cat_other_edit = st.text_input("Other category", key=cat_other_key)
                    else:
                        cat_other_edit = st.session_state.get(cat_other_key, "")

                    # ---- Active Flag ----
                    active_edit = st.checkbox(
                        "Active",
                        value=bool(row["is_active"]),
                        key=f"{base_key}_active",
                    )

                    # Save changes
                    if st.button("Save changes", type="primary", key=f"{base_key}_save"):
                        if not name_edit.strip():
                            st.error("Objective name is required.")
                        else:
                            nm_clean = name_edit.strip()

                            # resolve category
                            if cat_sel_edit == "":
                                cat_v = None
                            elif cat_sel_edit == "OTHER":
                                cat_v = (cat_other_edit or "").strip() or None
                            else:
                                cat_v = cat_sel_edit

                            # check duplicates
                            dup = query_df("""
                                SELECT 1
                                FROM objectives
                                WHERE lower(name)=lower(:n)
                                AND lower(coalesce(category,''))=lower(coalesce(:c,''))
                                AND objective_id<>:id
                            """, {"n": nm_clean, "c": cat_v, "id": oid})

                            if not dup.empty:
                                st.error("An objective with the same name and category already exists.")
                            else:
                                try:
                                    exec_sql("""
                                        UPDATE objectives
                                        SET name=:n,
                                            category=:c,
                                            is_active=:b
                                        WHERE objective_id=:id
                                    """, {
                                        "n": nm_clean,
                                        "c": cat_v,
                                        "b": bool(active_edit),
                                        "id": oid
                                    })
                                    st.success("Objective updated ✅")
                                except Exception as e:
                                    st.error("Could not update objective.")
                                    st.caption(str(e))

                    st.markdown("---")
                    st.markdown("#### 🔴 Danger Zone")

                    del_conf = st.checkbox(
                        "I understand this cannot be undone.",
                        key=f"{base_key}_del_conf"
                    )

                    if st.button("Delete Objective", type="primary", disabled=not del_conf, key=f"{base_key}_del"):
                        if v_cnt > 0:
                            st.error("Cannot delete: objective is referenced by visits.")
                        else:
                            try:
                                exec_sql("DELETE FROM objectives WHERE objective_id=:id", {"id": oid})
                                st.success("Objective deleted ✅")
                                st.session_state.pop("mg_obj_sel", None)
                                st.rerun()
                            except Exception as e:
                                st.error("Delete failed.")
                                st.caption(str(e))

# =============================
# Page — Admin: Data Browser
# =============================
def page_admin_data():
    st.title("📊 Admin — Data Browser")
    set_current_page("admin_data")
    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9, tab10 = st.tabs([
        "Visits", "Users", "Customers", "Target Audiences",
        "Business Units", "Business Lines",  # ← added Business Lines right after Business Units
        "Items", "Objectives", "Home Visits", "Shelf Movement",
    ])

    # ---------- Visits ----------
    with tab1:
        df = query_df(
            """
            SELECT
                v.visit_id,
                v.submitted_at_local,
                u.name AS rep,
                c.account_name AS customer,
                ta.name AS audience,
                -- product fields (may be NULL if Shelf Movement)
                i.article_number,
                i.description,
                -- BU/BL resolved from the visit's business_line_id
                bu.name AS business_unit,
                bl.name AS business_line,
                o.name AS objective,
                v.evaluation,
                v.latitude,
                v.longitude,
                v.accuracy_m,
                v.notes,
                hv.patient_name,
                hv.patient_phone,
                hv.serial_no,
                COALESCE((
                  SELECT COUNT(*)
                  FROM shelf_movement_lines l
                  JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                  WHERE h.visit_id = v.visit_id
                ),0) AS shelf_lines_count,
                COALESCE((
                  SELECT SUM(l.qty_checked)
                  FROM shelf_movement_lines l
                  JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                  WHERE h.visit_id = v.visit_id
                ),0) AS shelf_total_qty
            FROM visits v
            JOIN users u            ON v.user_id = u.user_id
            JOIN customers c        ON v.customer_id = c.customer_id
            LEFT JOIN target_audiences ta ON v.audience_id = ta.audience_id
            LEFT JOIN items i       ON v.product_id = i.product_id
            JOIN business_lines bl  ON bl.business_line_id = v.business_line_id
            JOIN business_units bu  ON bu.business_unit_id = bl.business_unit_id
            JOIN objectives o       ON v.objective_id = o.objective_id
            LEFT JOIN home_visits hv ON hv.visit_id = v.visit_id
            ORDER BY v.visit_id DESC
            """
        )
        st.markdown(f"**Total: {len(df):,}**")
        st.dataframe(df, width="stretch", hide_index=True)
        if not df.empty:
            st.download_button(
                "Download CSV",
                df.to_csv(index=False).encode("utf-8-sig"),
                "visits.csv",
                "text/csv",
                key="dl_visits",
            )

    # ---------- Users ----------
    with tab2:
        df = query_df("SELECT user_id, email, name, region, role, is_active FROM users ORDER BY user_id DESC")
        st.markdown(f"**Total: {len(df):,}**")
        st.dataframe(df, width="stretch", hide_index=True)
        if not df.empty:
            st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                               "users.csv", "text/csv", key="dl_users")

    # ---------- Customers ----------
    with tab3:
        df = query_df("SELECT * FROM customers ORDER BY account_name")
        st.markdown(f"**Total: {len(df):,}**")
        st.dataframe(df, width="stretch", hide_index=True)
        if not df.empty:
            st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                               "customers.csv", "text/csv", key="dl_customers")

    # ---------- Target Audiences ----------
    with tab4:
        df = query_df("""
            SELECT
                ta.audience_id,
                ta.customer_id,
                c.account_name,
                ta.name,
                ta.department,
                ta.position,
                ta.is_active
            FROM target_audiences ta
            LEFT JOIN customers c
                ON c.customer_id = ta.customer_id
            ORDER BY ta.audience_id DESC
        """)
        st.markdown(f"**Total: {len(df):,}**")
        st.dataframe(df, width="stretch", hide_index=True)
        if not df.empty:
            st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                               "target_audiences.csv", "text/csv", key="dl_audiences")

    # ---------- Business Units ----------
    with tab5:
        df = query_df("SELECT business_unit_id, name, is_active FROM business_units ORDER BY name")
        st.markdown(f"**Total: {len(df):,}**")
        st.dataframe(df, width="stretch", hide_index=True)
        if not df.empty:
            st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                               "business_units.csv", "text/csv", key="dl_business_units")

    # ---------- Business Lines (NEW) ----------
    with tab6:
        df = query_df(
            """
            SELECT
                bl.business_line_id,
                bu.name AS business_unit,
                bl.name AS business_line,
                bl.category,
                bl.supplier,
                bl.product_group,
                bl.is_active
            FROM business_lines bl
            JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
            ORDER BY bu.name, bl.name
            """
        )
        st.markdown(f"**Total: {len(df):,}**")
        st.dataframe(df, width="stretch", hide_index=True)
        if not df.empty:
            st.download_button(
                "Download CSV",
                df.to_csv(index=False).encode("utf-8-sig"),
                "business_lines.csv",
                "text/csv",
                key="dl_business_lines"
            )

    # ---------- Items ----------
    with tab7:
        df = query_df(
            """
            SELECT
                i.product_id,
                i.article_number,
                i.description,
                i.is_active,
                bl.name AS business_line,
                bu.name AS business_unit
            FROM items i
            JOIN business_lines bl ON bl.business_line_id = i.business_line_id
            JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
            ORDER BY COALESCE(i.article_number, i.product_id)
           """
        )
        st.markdown(f"**Total: {len(df):,}**")
        st.dataframe(df, width="stretch", hide_index=True)
        if not df.empty:
            st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                               "items.csv", "text/csv", key="dl_items")

    # ---------- Objectives ----------
    with tab8:
        df = query_df("SELECT * FROM objectives ORDER BY objective_id")
        st.markdown(f"**Total: {len(df):,}**")
        st.dataframe(df, width="stretch", hide_index=True)
        if not df.empty:
            st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                               "objectives.csv", "text/csv", key="dl_objectives")

    # ---------- Home Visits ----------
    with tab9:
        df = query_df(
            """
            SELECT hv.home_visit_id,
                   v.visit_id,
                   v.submitted_at_local,
                   u.name AS rep,
                   c.account_name AS customer,
                   hv.patient_name,
                   hv.patient_phone,
                   hv.serial_no,
                   v.latitude, v.longitude, v.accuracy_m,
                   o.name AS objective
            FROM home_visits hv
            JOIN visits v           ON v.visit_id = hv.visit_id
            JOIN users  u           ON u.user_id  = v.user_id
            JOIN customers c        ON c.customer_id = v.customer_id
            JOIN objectives o       ON o.objective_id = v.objective_id
            ORDER BY hv.home_visit_id DESC
            """
        )
        st.markdown(f"**Total Home Visits: {len(df):,}**")
        st.dataframe(df, width="stretch", hide_index=True)
        if not df.empty:
            st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                               "home_visits.csv", "text/csv", key="dl_home_visits")

    # ---------- Shelf Movement ----------
    with tab10:
        sub1, sub2 = st.tabs(["Headers (per visit)", "Lines (per product)"])

        # Headers with aggregates
        with sub1:
            df = query_df(
                """
                SELECT
                    h.movement_id,
                    v.visit_id,
                    v.submitted_at_local,
                    u.name AS rep,
                    c.account_name AS customer,
                    bu.name AS business_unit,
                    bl.name AS business_line,
                    o.name AS objective,
                    COALESCE((
                      SELECT COUNT(*)
                      FROM shelf_movement_lines l WHERE l.movement_id = h.movement_id
                    ),0) AS lines_count,
                    COALESCE((
                      SELECT SUM(l.qty_checked)
                      FROM shelf_movement_lines l WHERE l.movement_id = h.movement_id
                    ),0) AS total_qty
                FROM shelf_movement_headers h
                JOIN visits v       ON v.visit_id = h.visit_id
                JOIN users u        ON u.user_id  = v.user_id
                JOIN customers c    ON c.customer_id = v.customer_id
                JOIN business_lines bl ON bl.business_line_id = v.business_line_id
                JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
                JOIN objectives o   ON o.objective_id = v.objective_id
                ORDER BY h.movement_id DESC
                """
            )
            st.markdown(f"**Total Movements: {len(df):,}**")
            st.dataframe(df, width="stretch", hide_index=True)
            if not df.empty:
                st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                                   "shelf_movement_headers.csv", "text/csv", key="dl_sm_headers")

        # Lines detail
        with sub2:
            df = query_df(
                """
                SELECT
                    h.movement_id,
                    v.visit_id,
                    v.submitted_at_local,
                    u.name AS rep,
                    c.account_name AS customer,
                    i.product_id,
                    COALESCE(i.article_number, i.product_id) AS article_number,
                    i.description,
                    bu.name AS business_unit,
                    bl.name AS business_line,
                    l.qty_checked
                FROM shelf_movement_lines l
                JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                JOIN visits v                 ON v.visit_id = h.visit_id
                JOIN users u                  ON u.user_id  = v.user_id
                JOIN customers c              ON c.customer_id = v.customer_id
                LEFT JOIN items i             ON i.product_id = l.product_id
                LEFT JOIN business_lines bl   ON bl.business_line_id = i.business_line_id
                LEFT JOIN business_units bu   ON bu.business_unit_id = bl.business_unit_id
                ORDER BY h.movement_id DESC, article_number
                """
            )
            st.markdown(f"**Total Lines: {len(df):,}**")
            st.dataframe(df, width="stretch", hide_index=True)
            if not df.empty:
                st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                                   "shelf_movement_lines.csv", "text/csv", key="dl_sm_lines")

    # ---------- Export all ----------
    st.divider()
    if st.button("Export all tables (zip)", type="secondary", key="export_zip"):
        try:
            # Safer timestamp for filenames (no ":" which breaks on Windows)
            ts = datetime.now().strftime("%Y-%m-%d_%H%M")

            # Build dataframes
            tables = {
                "visits": query_df("""
                    SELECT
                        v.*,
                        c.account_name AS customer_name,
                        i.article_number,
                        i.description,
                        bl.name AS business_line,
                        bu.name AS business_unit,
                        o.name AS objective_name,
                        hv.patient_name,
                        hv.patient_phone,
                        hv.serial_no,
                        COALESCE((
                        SELECT COUNT(*)
                        FROM shelf_movement_lines l
                        JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                        WHERE h.visit_id = v.visit_id
                        ),0) AS shelf_lines_count,
                        COALESCE((
                        SELECT SUM(l.qty_checked)
                        FROM shelf_movement_lines l
                        JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                        WHERE h.visit_id = v.visit_id
                        ),0) AS shelf_total_qty
                    FROM visits v
                    JOIN customers c        ON v.customer_id = c.customer_id
                    LEFT JOIN items i        ON v.product_id = i.product_id
                    JOIN business_lines bl   ON bl.business_line_id = v.business_line_id
                    JOIN business_units bu   ON bu.business_unit_id = bl.business_unit_id
                    JOIN objectives o        ON v.objective_id = o.objective_id
                    LEFT JOIN home_visits hv ON hv.visit_id = v.visit_id
                    ORDER BY v.visit_id DESC
                """),
                "users": query_df("SELECT * FROM users ORDER BY user_id DESC"),
                "customers": query_df("SELECT * FROM customers ORDER BY account_name"),
                "target_audiences": query_df("SELECT * FROM target_audiences ORDER BY audience_id DESC"),
                "business_units": query_df("SELECT * FROM business_units ORDER BY business_unit_id"),
                "business_lines": query_df("""
                    SELECT bl.*, bu.name AS business_unit
                    FROM business_lines bl
                    JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
                    ORDER BY bu.name, bl.name
                """),
                "items": query_df("""
                    SELECT
                        i.product_id,
                        i.article_number,
                        i.description,
                        i.is_active,
                        bl.name AS business_line,
                        bu.name AS business_unit
                    FROM items i
                    JOIN business_lines bl ON bl.business_line_id = i.business_line_id
                    JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
                    ORDER BY COALESCE(i.article_number, i.product_id)
                """),
                "objectives": query_df("SELECT * FROM objectives ORDER BY objective_id"),
                "home_visits": query_df("""
                    SELECT hv.*, v.submitted_at_local, u.name AS rep, c.account_name AS customer
                    FROM home_visits hv
                    JOIN visits v ON v.visit_id = hv.visit_id
                    JOIN users u  ON u.user_id  = v.user_id
                    JOIN customers c ON c.customer_id = v.customer_id
                    ORDER BY hv.home_visit_id DESC
                """),
                "shelf_movement_headers": query_df("SELECT * FROM shelf_movement_headers ORDER BY movement_id DESC"),
                "shelf_movement_lines": query_df("SELECT * FROM shelf_movement_lines ORDER BY line_id DESC"),
            }

            # Write a zip in-memory
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                for name, df in tables.items():
                    # Make sure we don't emit "nan" strings in Excel
                    csv_bytes = df.to_csv(index=False, na_rep="").encode("utf-8-sig")
                    zf.writestr(f"{name}_{ts}.csv", csv_bytes)

            data = buf.getvalue()
            size_mb = len(data) / (1024 * 1024)
            st.success(f"Export ready (~{size_mb:.2f} MB).")
            st.download_button(
                "Download export.zip",
                data=data,
                file_name=f"export_pack_{ts}.zip",
                mime="application/zip",
                key="dl_zip_all",
            )
        except Exception as e:
            st.error("Export failed ❌")
            st.caption(str(e))

    # ---------- Full database backup options (auto schema from pg_dump) ----------
    st.divider()
    col_sql, col_zip = st.columns(2)

    def _db_url():
        # Prefer env; fall back to secrets (you already have _get_secret, reuse if you like)
        for k in ("DATABASE_URL", "POSTGRES_URL", "POSTGRES_CONNECTION_STRING"):
            v = os.environ.get(k) or _get_secret(k, "")
            if v:
                return v
        return None

    def _normalize_pg_url(url: str) -> str:
        # Some pg tools prefer 'postgresql://' over 'postgres://'
        return url.replace("postgres://", "postgresql://", 1) if url.startswith("postgres://") else url

    def _pg_dump_available() -> bool:
        import shutil
        return shutil.which("pg_dump") is not None

    with col_sql:
        if st.button("Download full DB (.sql via pg_dump)", key="export_pg_dump"):
            try:
                db_url = _db_url()
                if not db_url:
                    raise RuntimeError("DATABASE_URL / POSTGRES_URL not set.")

                if not _pg_dump_available():
                    raise FileNotFoundError("pg_dump not found in PATH.")

                # Plain SQL, no owner/privs for easy import
                import subprocess
                cmd = [
                    "pg_dump",
                    "--no-owner",
                    "--no-privileges",
                    "--format=plain",
                    _normalize_pg_url(db_url),
                ]
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

                if proc.returncode != 0 or not proc.stdout:
                    err = proc.stderr.decode("utf-8", errors="ignore")
                    raise RuntimeError(f"pg_dump failed.\n{err.strip() or 'No error text.'}")

                ts = datetime.now().strftime("%Y-%m-%d_%H%M")
                st.success("Full SQL dump ready.")
                st.download_button(
                    label="Download database.sql",
                    data=proc.stdout,                      # bytes
                    file_name=f"database_backup_{ts}.sql",
                    mime="application/sql",
                    key="dl_pg_dump_sql",
                )
            except FileNotFoundError as e:
                st.error("`pg_dump` is not available in this environment.")
                st.info("Use the portable backup (right column) or add `pg_dump` to your image.")
            except Exception as e:
                st.error("Full SQL dump failed ❌")
                st.caption(str(e))

    with col_zip:
        if st.button("Download portable backup (schema+CSVs .zip)", key="export_portable_zip"):
            try:
                ts = datetime.now().strftime("%Y-%m-%d_%H%M")

                # 1) Prepare dataframes (same queries you already use)
                tables = {
                    "visits": query_df("""
                        SELECT
                            v.*,
                            c.account_name AS customer_name,
                            i.article_number,
                            i.description,
                            bl.name AS business_line,
                            bu.name AS business_unit,
                            o.name AS objective_name,
                            hv.patient_name,
                            hv.patient_phone,
                            hv.serial_no,
                            COALESCE((
                            SELECT COUNT(*)
                            FROM shelf_movement_lines l
                            JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                            WHERE h.visit_id = v.visit_id
                            ),0) AS shelf_lines_count,
                            COALESCE((
                            SELECT SUM(l.qty_checked)
                            FROM shelf_movement_lines l
                            JOIN shelf_movement_headers h ON h.movement_id = l.movement_id
                            WHERE h.visit_id = v.visit_id
                            ),0) AS shelf_total_qty
                        FROM visits v
                        JOIN customers c        ON v.customer_id = c.customer_id
                        LEFT JOIN items i        ON v.product_id = i.product_id
                        JOIN business_lines bl   ON bl.business_line_id = v.business_line_id
                        JOIN business_units bu   ON bu.business_unit_id = bl.business_unit_id
                        JOIN objectives o        ON v.objective_id = o.objective_id
                        LEFT JOIN home_visits hv ON hv.visit_id = v.visit_id
                        ORDER BY v.visit_id DESC
                    """),
                    "users": query_df("SELECT * FROM users ORDER BY user_id DESC"),
                    "customers": query_df("SELECT * FROM customers ORDER BY account_name"),
                    "target_audiences": query_df("SELECT * FROM target_audiences ORDER BY audience_id DESC"),
                    "business_units": query_df("SELECT * FROM business_units ORDER BY business_unit_id"),
                    "business_lines": query_df("""
                        SELECT bl.*, bu.name AS business_unit
                        FROM business_lines bl
                        JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
                        ORDER BY bu.name, bl.name
                    """),
                    "items": query_df("""
                        SELECT
                            i.product_id,
                            i.article_number,
                            i.description,
                            i.is_active,
                            bl.name AS business_line,
                            bu.name AS business_unit
                        FROM items i
                        JOIN business_lines bl ON bl.business_line_id = i.business_line_id
                        JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
                        ORDER BY COALESCE(i.article_number, i.product_id)
                    """),
                    "objectives": query_df("SELECT * FROM objectives ORDER BY objective_id"),
                    "home_visits": query_df("""
                        SELECT hv.*, v.submitted_at_local, u.name AS rep, c.account_name AS customer
                        FROM home_visits hv
                        JOIN visits v ON v.visit_id = hv.visit_id
                        JOIN users u  ON u.user_id  = v.user_id
                        JOIN customers c ON c.customer_id = v.customer_id
                        ORDER BY hv.home_visit_id DESC
                    """),
                    "shelf_movement_headers": query_df("SELECT * FROM shelf_movement_headers ORDER BY movement_id DESC"),
                    "shelf_movement_lines": query_df("SELECT * FROM shelf_movement_lines ORDER BY line_id DESC"),
                }

                # 2) Build ZIP in-memory: schema.sql (from live DB) + CSVs + README
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:

                    # 2a) Try to get **live schema** via pg_dump --schema-only
                    schema_bytes = b""
                    try:
                        db_url = _db_url()
                        if not db_url:
                            raise RuntimeError("DATABASE_URL / POSTGRES_URL not set.")

                        if not _pg_dump_available():
                            raise FileNotFoundError("pg_dump not found")

                        import subprocess
                        cmd_schema = [
                            "pg_dump",
                            "--no-owner",
                            "--no-privileges",
                            "--format=plain",
                            "--schema-only",
                            _normalize_pg_url(db_url),
                        ]
                        p = subprocess.run(cmd_schema, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
                        if p.returncode != 0 or not p.stdout:
                            err = p.stderr.decode("utf-8", errors="ignore")
                            raise RuntimeError(f"pg_dump --schema-only failed.\n{err.strip() or 'No error text.'}")
                        schema_bytes = p.stdout
                    except Exception as e:
                        # Fall back to placeholder schema + guidance
                        schema_bytes = (
                            b"-- schema.sql not auto-included.\n"
                            b"-- Reason: " + str(e).encode("utf-8", errors="ignore") + b"\n"
                            b"-- Tip: Run a full SQL dump from a machine with pg_dump installed, or\n"
                            b"--       enable pg_dump in your deployment image.\n"
                        )

                    zf.writestr("schema.sql", schema_bytes)

                    # 2b) Add CSVs
                    for name, df in tables.items():
                        csv_bytes = df.to_csv(index=False, na_rep="").encode("utf-8-sig")
                        zf.writestr(f"data/{name}_{ts}.csv", csv_bytes)

                    # 2c) README with restore steps
                    readme = f"""# Portable Backup

    This archive contains:
    - `schema.sql` — your **live** PostgreSQL schema (dumped via pg_dump when available).
    - `data/*.csv` — table data exports.

    ## Quick restore (psql)

    1) Create an empty database.
    2) Load the schema:

    psql "$DATABASE_URL" -f schema.sql

    3) Load CSVs (example):

    psql "$DATABASE_URL" -c "\\copy users FROM 'data/users_{ts}.csv' WITH (FORMAT csv, HEADER true)"
    psql "$DATABASE_URL" -c "\\copy customers FROM 'data/customers_{ts}.csv' WITH (FORMAT csv, HEADER true)"
    psql "$DATABASE_URL" -c "\\copy objectives FROM 'data/objectives_{ts}.csv' WITH (FORMAT csv, HEADER true)"
    psql "$DATABASE_URL" -c "\\copy business_units FROM 'data/business_units_{ts}.csv' WITH (FORMAT csv, HEADER true)"
    psql "$DATABASE_URL" -c "\\copy business_lines FROM 'data/business_lines_{ts}.csv' WITH (FORMAT csv, HEADER true)"
    psql "$DATABASE_URL" -c "\\copy items FROM 'data/items_{ts}.csv' WITH (FORMAT csv, HEADER true)"
    psql "$DATABASE_URL" -c "\\copy target_audiences FROM 'data/target_audiences_{ts}.csv' WITH (FORMAT csv, HEADER true)"
    psql "$DATABASE_URL" -c "\\copy visits FROM 'data/visits_{ts}.csv' WITH (FORMAT csv, HEADER true)"
    psql "$DATABASE_URL" -c "\\copy home_visits FROM 'data/home_visits_{ts}.csv' WITH (FORMAT csv, HEADER true)"
    psql "$DATABASE_URL" -c "\\copy shelf_movement_headers FROM 'data/shelf_movement_headers_{ts}.csv' WITH (FORMAT csv, HEADER true)"
    psql "$DATABASE_URL" -c "\\copy shelf_movement_lines FROM 'data/shelf_movement_lines_{ts}.csv' WITH (FORMAT csv, HEADER true)"

    > If you get FK errors, import in dependency order (units → lines → items → customers → target_audiences → visits → home_visits → shelf_*).
    """
                    zf.writestr("README_restore.md", readme.encode("utf-8"))

                data = buf.getvalue()
                size_mb = len(data) / (1024 * 1024)
                st.success(f"Portable backup ready (~{size_mb:.2f} MB).")
                st.download_button(
                    "Download portable_backup.zip",
                    data=data,
                    file_name=f"portable_backup_{ts}.zip",
                    mime="application/zip",
                    key="dl_portable_zip",
                )
            except Exception as e:
                st.error("Portable backup failed ❌")
                st.caption(str(e))

# =============================
# Helper — generate a strong temporary password
# =============================
import secrets, string

def _gen_tmp_pw(length: int = 12) -> str:
    # at least one of each class
    alphabet = string.ascii_lowercase + string.ascii_uppercase + string.digits + "!@#$%^&*"
    while True:
        pw = ''.join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
            and any(c.isdigit() for c in pw) and any(c in "!@#$%^&*" for c in pw)):
            return pw

# =============================
# Page — Admin: Users (add/manage + reset password)
# =============================
from sqlalchemy import text  # ensure available in this scope
from passlib.hash import pbkdf2_sha256

def page_admin_users():
    st.title("👤 Admin — Users")
    set_current_page("admin_users")
    st.subheader("Add a user")

    # --- Temp Password Generator (outside the form) ---
    st.session_state.setdefault("create_user_tmp_pw", "")

    gcol1, gcol2 = st.columns([1, 4])
    if gcol1.button("🔄 Generate Temporary Password"):
        st.session_state["create_user_tmp_pw"] = _gen_tmp_pw()
    if st.session_state["create_user_tmp_pw"]:
        st.caption(f"Generated: `{st.session_state['create_user_tmp_pw']}` (you can edit before saving)")

    # --- User Creation Form ---
    with st.form("add_user", clear_on_submit=True):
        col1, col2 = st.columns(2)

        with col1:
            email = st.text_input("Email *")
            name = st.text_input("Name *")
            region = st.selectbox("Region", ["", "C/R", "W/R", "E/R"], index=0)

        with col2:
            bu_df = query_df("SELECT business_unit_id, name FROM business_units WHERE is_active IS TRUE ORDER BY name")
            bu_names = bu_df["name"].tolist()
            bu_sel = st.selectbox("Business Unit (optional)", [""] + bu_names, index=0)
            role = st.selectbox("Role", ["rep", "admin","manager"], index=0)
            pw = st.text_input("Temporary Password *", type="password",
                               value=st.session_state["create_user_tmp_pw"])

        add_btn = st.form_submit_button("Create User", type="primary")

    if add_btn:
        if not (email and name and pw):
            st.error("Email, Name, and Password are required.")
        else:
            try:
                bu_id = None
                if bu_sel:
                    bu_id = int(bu_df.loc[bu_df["name"] == bu_sel, "business_unit_id"].iloc[0])

                # Insert (PostgreSQL named parameters). Use proper booleans.
                exec_sql(
                    """
                    INSERT INTO users(email, password_hash, name, region, business_unit_id, role, is_active)
                    VALUES (:email, :pwd, :name, :region, :buid, :role, TRUE)
                    """,
                    {
                        "email": email.strip().lower(),
                        "pwd": pbkdf2_sha256.hash(pw),
                        "name": name.strip(),
                        "region": (region.strip() if region else None),
                        "buid": bu_id,
                        "role": role,
                    },
                )
                st.success("✅ User added successfully")
                st.session_state["create_user_tmp_pw"] = ""
            except Exception as e:
                st.error("Could not add user (email might already exist).")
                st.caption(str(e))

    # ---- All users (with BU) ----
    st.subheader("All users")
    df = query_df("""
        SELECT u.user_id,
               u.email,
               u.name,
               u.region,
               u.role,
               u.is_active,
               bu.name AS business_unit
        FROM users u
        LEFT JOIN business_units bu ON bu.business_unit_id = u.business_unit_id
        ORDER BY u.user_id DESC
    """)
    st.markdown(f"**Total: {len(df):,}**")
    st.dataframe(df, width="stretch", hide_index=True)
    if not df.empty:
        st.download_button(
            "Download CSV",
            df.to_csv(index=False).encode("utf-8-sig"),
            "users.csv",
            "text/csv",
            key="dl_users2"
        )

    st.divider()
    st.subheader("📝 Manage Users (Activate / Deactivate / Edit / Reset Password)")

    mdf = query_df("""
        SELECT u.user_id,
               u.email,
               u.name,
               u.region,
               u.role,
               u.is_active,
               u.business_unit_id,
               bu.name AS business_unit
        FROM users u
        LEFT JOIN business_units bu ON bu.business_unit_id = u.business_unit_id
        ORDER BY u.name, u.user_id
    """)

    if mdf.empty:
        st.info("No users to manage yet.")
        return

    def _fmt_user(r):
        status = "active" if bool(r.is_active) else "inactive"
        bu = f" · BU: {r.business_unit}" if pd.notna(r.business_unit) and str(r.business_unit).strip() else ""
        return f"{r.name or r.email} <{r.email}> ({r.role}) — {status}{bu}"

    labels = [_fmt_user(r) for r in mdf.itertuples(index=False)]
    sel = st.selectbox("Select user", [""] + labels, index=0, key="mg_user_sel")

    if not sel:
        st.info("Select a user above to manage.")
        return

    row = mdf.iloc[labels.index(sel)]
    uid = int(row["user_id"])
    is_active = bool(row["is_active"])
    status_badge = "🟢 Active" if is_active else "🔴 Inactive"
    st.caption(f"Selected: **{row['name'] or row['email']}** · {status_badge}")

    colA, colB, colC = st.columns([1, 1, 2])

    # Activate / Deactivate
    with colA:
        label = "Deactivate" if is_active else "Activate"
        if st.button(label, key=f"mg_user_toggle_{uid}"):
            current = st.session_state.get("user")
            current_uid = int(current["user_id"]) if current and "user_id" in current else None
            if label == "Deactivate" and current_uid == uid:
                st.error("You can't deactivate your own account while logged in.")
            else:
                try:
                    exec_sql(
                        "UPDATE users SET is_active = :active WHERE user_id = :uid",
                        {"active": (not is_active), "uid": uid},  # send True/False
                    )
                    st.success("Updated ✅")
                except Exception as e:
                    st.error("Could not update user status.")
                    st.caption(str(e))

    # Show current Role / BU
    with colB:
        bu_display = row["business_unit"] or "—"
        st.markdown(f"**Role:** {row['role']}  \n**Business Unit:** {bu_display}")

    # Quick Edit (Region / BU / Role)
    with colC:
        edit_box = st.popover("Edit") if hasattr(st, "popover") else st.expander("Edit", expanded=False)
        with edit_box:
            bu_df2 = query_df("SELECT business_unit_id, name FROM business_units WHERE is_active IS TRUE ORDER BY name")
            bu_labels = [""] + bu_df2["name"].tolist()

            current_bu_name = row["business_unit"] or ""
            bu_idx = bu_labels.index(current_bu_name) if current_bu_name in bu_labels else 0

            with st.form(f"mg_user_edit_{uid}"):
                new_region = st.selectbox(
                    "Region",
                    ["", "C/R", "W/R", "E/R"],
                    index=(["", "C/R", "W/R", "E/R"].index(row["region"]) if row["region"] in ["", "C/R", "W/R", "E/R"] else 0)
                )
                new_bu_label = st.selectbox("Business Unit (optional)", bu_labels, index=bu_idx)
                new_role = st.selectbox("Role", ["rep", "admin","manager"], index=(0 if row["role"] == "rep" else 1))
                save = st.form_submit_button("Save changes")

            if save:
                try:
                    new_bu_id = None
                    if new_bu_label:
                        new_bu_id = int(bu_df2.loc[bu_df2["name"] == new_bu_label, "business_unit_id"].iloc[0])
                    exec_sql(
                        "UPDATE users SET region = :region, business_unit_id = :buid, role = :role WHERE user_id = :uid",
                        {
                            "region": (new_region.strip() if new_region else None),
                            "buid": new_bu_id,
                            "role": new_role,
                            "uid": uid,
                        },
                    )
                    st.success("Saved ✅")
                except Exception as e:
                    st.error("Could not save changes.")
                    st.caption(str(e))

    st.divider()

    # --- Admin: Reset password for selected user (no forced change) ---
    st.subheader("🔐 Reset Password for Selected User")

    # Flash message area (rendered directly under the button group)
    flash_key = f"flash_reset_{uid}"
    if st.session_state.get(flash_key):
        st.success(st.session_state[flash_key])

    # Keys for the input + buffer
    tmp_input_key = f"tmp_pw_input_{uid}"
    buf_key = f"tmp_pw_buf_{uid}"
    st.session_state.setdefault(buf_key, "")

    # Handle 'Generate' BEFORE rendering the text_input,
    gen_col, _ = st.columns([1, 6])
    gen_clicked = gen_col.button("Generate", key=f"gen_tmp_pw_{uid}")
    if gen_clicked:
        gen_pw = _gen_tmp_pw()
        st.session_state[buf_key] = gen_pw
        st.session_state[tmp_input_key] = gen_pw

    # Now render the input (uses session_state if present)
    tmp_pw = st.text_input(
        "Temporary Password *",
        key=tmp_input_key,
        type="password",
        help="Share this with the user. They can change it later from User Settings."
    )

    # Action buttons row
    b1, _ = st.columns([2, 5])
    if b1.button("Set Temporary Password", type="primary", key=f"set_tmp_pw_{uid}"):
        final_tmp_pw = (st.session_state.get(tmp_input_key) or "").strip()
        if not final_tmp_pw:
            st.error("Please enter or generate a temporary password.")
        else:
            try:
                new_hash = pbkdf2_sha256.hash(final_tmp_pw)
                exec_sql(
                    "UPDATE users SET password_hash = :pwd WHERE user_id = :uid",
                    {"pwd": new_hash, "uid": uid},
                )
                st.session_state[flash_key] = (
                    f"Temporary password set ✅ (user not forced to change). Temp password: `{final_tmp_pw}`"
                )
                st.success(st.session_state[flash_key])
            except Exception as e:
                st.error("Could not reset the password.")
                st.caption(str(e))

# =============================
# Review / Cleanup Target Audiences Page
# =============================            
def page_review_target_audiences():
    """
    Admin / manager page to review visits that used 'Other' Target Audience.
    - Shows all visits where audience_id IS NULL but other_audience_* is filled.
    - Suggests closest matches from existing target_audiences using generic similarity.
    - Allows linking visit to an existing TA or creating a new TA from the 'Other' fields.
    """
    import pandas as pd
    import re
    from difflib import SequenceMatcher
    from datetime import datetime

    PAGE_NS = "review_ta"
    set_current_page(PAGE_NS)
    
    TITLE_OPTIONS = ["", "Dr.", "Mr.", "Ms.", "Mrs.", "Prof.", "Eng.", "Other"]

    success_key = f"{PAGE_NS}_last_success"
    if success_key not in st.session_state:
        st.session_state[success_key] = ""

    # ------------- Auth -------------
    u = st.session_state.get("user") or resolve_session_user()
    if not u:
        st.warning("Please sign in to continue.")
        st.stop()

    role = (u.get("role") or "").lower().strip()
    if role not in ("admin", "manager"):
        st.warning("You do not have access to this page.")
        st.stop()

    uid = int(u.get("user_id") or u.get("id"))
    display_name   = u.get("name") or u.get("email") or f"User #{uid}"
    display_region = u.get("region") or "—"
    display_role   = u.get("role") or "—"

    st.title("🎯 Review Target Audiences (Other)")
    st.caption(
        f"Logged in as **{display_name}** · Region: **{display_region}** · Role: **{display_role}**"
    )

    # Show any success message from last rerun
    last_msg = st.session_state.get(success_key) or ""
    if last_msg:
        st.success(last_msg)
        st.session_state[success_key] = ""

    # ------------- Similarity helpers (generic) -------------
    SIM_THRESHOLD = 0.78  # left here in case you want to use it later

    def normalize_name(s: str) -> str:
        if not s:
            return ""
        s = str(s).strip().lower()

        titles = ["dr.", "dr", "mr.", "mr", "mrs.", "mrs", "ms.", "ms", "prof.", "prof"]
        for t in titles:
            if s.startswith(t + " "):
                s = s[len(t) + 1:]

        s = "".join(ch if (ch.isalpha() or ch.isspace()) else " " for ch in s)
        s = " ".join(s.split())
        return s

    def string_similarity(a: str, b: str) -> float:
        a_norm, b_norm = normalize_name(a), normalize_name(b)
        if not a_norm or not b_norm:
            return 0.0

        s1 = SequenceMatcher(None, a_norm, b_norm).ratio()

        def only_cons(s: str) -> str:
            return "".join(ch for ch in s if ch not in "aeiou ")

        cons_a = only_cons(a_norm)
        cons_b = only_cons(b_norm)
        s2 = SequenceMatcher(None, cons_a, cons_b).ratio() if (cons_a and cons_b) else 0.0

        return 0.6 * s1 + 0.4 * s2

    def audience_similarity(other_row: pd.Series, ta_row: pd.Series | dict) -> float:
        name_other = (other_row.get("other_audience_name") or "").strip()
        name_ta    = (ta_row.get("name") or "").strip()
        name_score = string_similarity(name_other, name_ta)

        dept_other = (other_row.get("other_audience_department") or "").strip().lower()
        dept_ta    = (ta_row.get("department") or "").strip().lower()
        pos_other  = (other_row.get("other_audience_position") or "").strip().lower()
        pos_ta     = (ta_row.get("position") or "").strip().lower()

        dept_score = 1.0 if dept_other and dept_other == dept_ta else 0.0
        pos_score  = 1.0 if pos_other and pos_other == pos_ta else 0.0

        return 0.7 * name_score + 0.15 * dept_score + 0.15 * pos_score

    def format_ta_label(row) -> str:
        if isinstance(row, pd.Series):
            #title = (row.get("title") or "").strip()
            name  = (row.get("name") or "").strip()
            dept  = (row.get("department") or "").strip()
            pos   = (row.get("position") or "").strip()
        else:
            #title = (getattr(row, "title", "") or "").strip()
            name  = (getattr(row, "name", "") or "").strip()
            dept  = (getattr(row, "department", "") or "").strip()
            pos   = (getattr(row, "position", "") or "").strip()

        parts = []
        if name:
            #full_name = f"{title} {name}".strip() if title else name
            full_name = f"{name}".strip()
            parts.append(full_name)
        if dept:
            parts.append(dept)
        if pos:
            parts.append(pos)
        return " || ".join(parts) if parts else "(unnamed)"

    # ------------- Load unresolved "Other" visits -------------
    unresolved_df = query_df(
        """
        SELECT
            v.visit_id,
            v.customer_id,
            c.account_name               AS customer_name,
            v.submitted_at_local,
            v.other_audience_title,
            v.other_audience_name,
            v.other_audience_department,
            v.other_audience_position,
            v.other_audience_phone,
            v.other_audience_email,
            v.notes                      AS visit_notes,
            v.user_id,
            u.name                       AS rep_name,
            u.email                      AS rep_email,
            bu.name                      AS business_unit_name
        FROM visits v
        JOIN customers c 
            ON c.customer_id = v.customer_id
        JOIN users u     
            ON u.user_id = v.user_id
        LEFT JOIN business_lines bl
            ON bl.business_line_id = v.business_line_id
        LEFT JOIN business_units bu
            ON bu.business_unit_id = bl.business_unit_id
        WHERE v.audience_id IS NULL
        AND v.other_audience_name IS NOT NULL
        AND trim(v.other_audience_name) <> ''
        ORDER BY v.submitted_at_local DESC, v.visit_id DESC
        """
    )

    if unresolved_df.empty:
        st.success("✅ No visits pending review for 'Other' Target Audience.")
        return

    unresolved_df["submitted_at_local"] = pd.to_datetime(
        unresolved_df["submitted_at_local"], errors="coerce"
    ).dt.strftime("%d/%m/%Y %H:%M")

    st.markdown("### 1️⃣ Visits with 'Other' Target Audience")
    st.caption("These visits have no audience_id but contain Other Target Audience details.")

    unresolved_display = unresolved_df.rename(
        columns={
            "customer_name": "Customer",
            "submitted_at_local": "Visit Date/Time",
            "other_audience_title": "Other TA Title",
            "other_audience_name": "Other TA Name",
            "other_audience_department": "Other TA Dept",
            "other_audience_position": "Other TA Position",
            "other_audience_phone": "Other TA Phone",
            "other_audience_email": "Other TA Email",
            "visit_notes": "Notes",
            "rep_name": "Submitted By",
            "rep_email": "Email",
            "business_unit_name": "Business Unit",
        }
    )

    st.dataframe(
        unresolved_display[
            [
                "visit_id",
                "Customer",
                "Visit Date/Time",
                "Other TA Title",
                "Other TA Name",
                "Other TA Dept",
                "Other TA Position",
                "Other TA Phone",
                "Other TA Email",
                "Notes",
                "Submitted By",
                "Email",
            ]
        ],
        width='stretch',
        hide_index=True,
    )

    # ------------- Pick a visit to review -------------
    st.markdown("---")
    st.markdown("### 2️⃣ Review & Resolve One Visit")

    visit_labels = []
    visit_id_map = {}
    for _, row in unresolved_df.iterrows():
        label = (
            f"{int(row['visit_id'])} — {row['customer_name']} — "
            f"{row['other_audience_name']} ({row['submitted_at_local']})"
        )
        visit_labels.append(label)
        visit_id_map[label] = int(row["visit_id"])

    visit_select_options = [""] + visit_labels

    selected_visit_label = st.selectbox(
        "Select a visit to review",
        options=visit_select_options,
        index=0,
        key=f"{PAGE_NS}_visit_sel",
    )

    if not selected_visit_label:
        st.info("Please select a visit from the list above to start reviewing.")
        return

    selected_visit_id = visit_id_map[selected_visit_label]

    visit_row = unresolved_df.loc[unresolved_df["visit_id"] == selected_visit_id].iloc[0]

    st.info(
        f"**Visit #{selected_visit_id}** — Customer: **{visit_row['customer_name']}**  · \n"
        f"Title: **{visit_row['other_audience_title'] or '—'}**  · "
        f"Name: **{visit_row['other_audience_name']}**  · "
        f"Dept: **{visit_row['other_audience_department'] or '—'}**  · "
        f"Position: **{visit_row['other_audience_position'] or '—'}**  \n"
        f"Phone: **{visit_row['other_audience_phone'] or '—'}**  · "
        f"Email: **{visit_row['other_audience_email'] or '—'}**  \n"
        f"Submitted by: **{visit_row['rep_name']}** ({visit_row['rep_email']})"
    )

    # ------------- Load all existing TAs for that customer -------------
    ta_df = query_df(
        """
        SELECT
            audience_id,
            title,
            name,
            department,
            position,
            mobile,
            email
        FROM target_audiences
        WHERE customer_id = :cid
          AND COALESCE(is_active, TRUE) IS TRUE
        ORDER BY name
        """,
        {"cid": int(visit_row["customer_id"])},
    )

    if ta_df.empty:
        st.warning("This customer has no existing Target Audiences. You can only create a new one.")
        existing_options = []
    else:
        ta_df["similarity"] = ta_df.apply(
            lambda r: audience_similarity(
                visit_row,
                {
                    "name": r["name"],
                    "department": r["department"],
                    "position": r["position"],
                },
            ),
            axis=1,
        )
        ta_df = ta_df.sort_values(by="similarity", ascending=False)

        st.markdown("#### Suggested Matches")
        st.caption("Sorted by similarity (generic name similarity + department + position).")

        ta_display = ta_df.copy()
        ta_display["Label"] = ta_display.apply(format_ta_label, axis=1)
        ta_display["Similarity"] = ta_display["similarity"].map(lambda x: f"{x:.2f}")

        st.dataframe(
            ta_display[
                ["audience_id", "Label", "Similarity", "department", "position", "mobile", "email"]
            ].rename(
                columns={
                    "audience_id": "ID",
                    "department": "Dept",
                    "position": "Position",
                    "mobile": "Mobile",
                    "email": "Email",
                }
            ),
            width='stretch',
            hide_index=True,
        )

        existing_options = [
            f"{int(r.audience_id)} — {format_ta_label(r)}"
            for r in ta_df.itertuples(index=False)
        ]

    # ------------- Global dept/position lists for dropdowns -------------
    dept_choices_base: list[str] = []
    pos_choices_base:  list[str] = []

    dept_df = query_df(
        """
        SELECT DISTINCT department
        FROM target_audiences
        WHERE department IS NOT NULL
          AND trim(department) <> ''
        ORDER BY department
        """
    )
    if not dept_df.empty:
        dept_choices_base = dept_df["department"].astype(str).str.strip().tolist()

    pos_df = query_df(
        """
        SELECT DISTINCT position
        FROM target_audiences
        WHERE position IS NOT NULL
          AND trim(position) <> ''
        ORDER BY position
        """
    )
    if not pos_df.empty:
        pos_choices_base = pos_df["position"].astype(str).str.strip().tolist()

    # ------------- Actions: Link existing / Create new -------------
    col_link, col_new = st.columns(2)

    # --- Link to existing TA ---
    with col_link:
        st.markdown("#### 🔗 Link to Existing Target Audience")

        existing_sel = st.selectbox(
            "Existing Target Audience",
            options=[""] + existing_options,
            index=0,
            key=f"{PAGE_NS}_existing_ta_sel",
            help="Pick an existing TA for this customer, then click 'Link to Selected'.",
        )

        if st.button("✅ Link to Selected", key=f"{PAGE_NS}_link_btn"):
            if not existing_sel:
                st.error("Please select an existing Target Audience first.")
            else:
                sel_id_str = existing_sel.split("—", 1)[0].strip()
                try:
                    audience_id = int(sel_id_str)
                except ValueError:
                    st.error("Could not parse selected Target Audience ID.")
                    st.stop()

                try:
                    with engine.begin() as conn:
                        conn.execute(
                            text(
                                """
                                UPDATE visits
                                SET audience_id = :aid
                                WHERE visit_id = :vid
                                """
                            ),
                            {"aid": audience_id, "vid": selected_visit_id},
                        )
                    st.session_state[success_key] = (
                        f"Linked visit #{selected_visit_id} to existing Target Audience ID {audience_id} ✅"
                    )
                    st.rerun()
                except Exception as e:
                    st.error("Failed to link visit to existing Target Audience.")
                    st.caption(str(e))

    # --- Create new TA ---
    with col_new:
        st.markdown("#### 🆕 Create New Target Audience")

        st.caption(
            "This will create a new Target Audience using the details below "
            "(you can adjust them first) and link this visit to it. "
            "The original 'Other' fields in the visit will remain stored."
        )

        name_key        = f"{PAGE_NS}_new_ta_name_{selected_visit_id}"
        title_sel_key   = f"{PAGE_NS}_new_ta_title_sel_{selected_visit_id}"
        mobile_key      = f"{PAGE_NS}_new_ta_mobile_{selected_visit_id}"
        email_key       = f"{PAGE_NS}_new_ta_email_{selected_visit_id}"
        dept_sel_key    = f"{PAGE_NS}_new_ta_dept_sel_{selected_visit_id}"
        dept_custom_key = f"{PAGE_NS}_new_ta_dept_custom_{selected_visit_id}"
        pos_sel_key     = f"{PAGE_NS}_new_ta_pos_sel_{selected_visit_id}"
        pos_custom_key  = f"{PAGE_NS}_new_ta_pos_custom_{selected_visit_id}"
        confirm_key     = f"{PAGE_NS}_confirm_new_{selected_visit_id}"

        # Prefill from visit (title/name/dept/pos/phone/email)
        raw_title  = (visit_row.get("other_audience_title") or "").strip()
        raw_name   = (visit_row.get("other_audience_name") or "")
        raw_dept   = (visit_row.get("other_audience_department") or "").strip()
        raw_pos    = (visit_row.get("other_audience_position") or "").strip()
        raw_mobile = (visit_row.get("other_audience_phone") or "").strip()
        raw_email  = (visit_row.get("other_audience_email") or "").strip()

        # Title (optional) — TITLE_OPTIONS defined globally
        if raw_title and raw_title in TITLE_OPTIONS:
            title_index = TITLE_OPTIONS.index(raw_title)
        else:
            title_index = 0

        selected_title = st.selectbox(
            "Title (optional)",
            TITLE_OPTIONS,
            index=title_index,
            key=title_sel_key,
        )

        # Name (required)
        new_name = st.text_input(
            "Target Audience Name *",
            value=raw_name.upper(),  # show as ALL CAPS
            key=name_key,
        )

        # Department
        dept_opts = [""] + dept_choices_base + ["Other"]
        if raw_dept and raw_dept in dept_choices_base:
            dept_index = 1 + dept_choices_base.index(raw_dept)
        elif raw_dept:
            dept_index = len(dept_opts) - 1
        else:
            dept_index = 0

        selected_dept = st.selectbox(
            "Department *",
            dept_opts,
            index=dept_index,
            key=dept_sel_key,
        )

        dept_custom = None
        if selected_dept == "Other":
            dept_custom = st.text_input(
                "Custom Department *",
                value=raw_dept,
                key=dept_custom_key,
            )

        # Position
        pos_opts = [""] + pos_choices_base + ["Other"]
        if raw_pos and raw_pos in pos_choices_base:
            pos_index = 1 + pos_choices_base.index(raw_pos)
        elif raw_pos:
            pos_index = len(pos_opts) - 1
        else:
            pos_index = 0

        selected_pos = st.selectbox(
            "Position *",
            pos_opts,
            index=pos_index,
            key=pos_sel_key,
        )

        pos_custom = None
        if selected_pos == "Other":
            pos_custom = st.text_input(
                "Custom Position *",
                value=raw_pos,
                key=pos_custom_key,
            )

        # Mobile (optional)
        new_mobile = st.text_input(
            "Mobile # (optional)",
            value=raw_mobile,
            key=mobile_key,
            help="Optional – KSA mobile like 05XXXXXXXX.",
        )

        # Email (optional)
        new_email = st.text_input(
            "Email (optional)",
            value=raw_email,
            key=email_key,
            help="Optional – must be a valid email address.",
        )

        confirm_new = st.checkbox(
            "I confirm this is a **new** Target Audience (not already in the list).",
            key=confirm_key,
        )

        if st.button("➕ Create New & Link", key=f"{PAGE_NS}_create_btn"):
            if not confirm_new:
                st.error("Please confirm that this is a new Target Audience.")
                return

            name = (new_name or "").strip().upper()

            if not selected_dept:
                st.error("Please select a **Department** or choose **Other** and type a value.")
                return
            if selected_dept == "Other":
                dept_to_save = (dept_custom or "").strip()
                if not dept_to_save:
                    st.error("Please enter a **Custom Department**.")
                    return
            else:
                dept_to_save = selected_dept

            if not selected_pos:
                st.error("Please select a **Position** or choose **Other** and type a value.")
                return
            if selected_pos == "Other":
                pos_to_save = (pos_custom or "").strip()
                if not pos_to_save:
                    st.error("Please enter a **Custom Position**.")
                    return
            else:
                pos_to_save = selected_pos

            if not name:
                st.error("Cannot create a new Target Audience without a name.")
                return

            # Optional mobile validation (KSA)
            mobile_to_save = (new_mobile or "").strip()
            if mobile_to_save:
                if not re.fullmatch(r"(?:\+966|00966|0)?5\d{8}", mobile_to_save):
                    st.error(
                        "**Mobile #** looks invalid (expected KSA mobile like 05XXXXXXXX)."
                    )
                    return

            # Optional email validation
            email_to_save = (new_email or "").strip()
            if email_to_save:
                if not re.fullmatch(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_to_save):
                    st.error("**Email** looks invalid.")
                    return

            title_to_save = selected_title or None

            try:
                with engine.begin() as conn:
                    # Insert new TA with title + mobile + email
                    res = conn.execute(
                        text(
                            """
                            INSERT INTO target_audiences
                                (customer_id, title, name, department, position, mobile, email, is_active)
                            VALUES
                                (:cid, :title, :name, :dept, :pos, :mobile, :email, TRUE)
                            RETURNING audience_id
                            """
                        ),
                        {
                            "cid":    int(visit_row["customer_id"]),
                            "title":  title_to_save,
                            "name":   name,
                            "dept":   dept_to_save,
                            "pos":    pos_to_save,
                            "mobile": mobile_to_save or None,
                            "email":  email_to_save or None,
                        },
                    )
                    new_aid = res.scalar_one()

                    # Link visit to this new TA
                    conn.execute(
                        text(
                            """
                            UPDATE visits
                            SET audience_id = :aid
                            WHERE visit_id  = :vid
                            """
                        ),
                        {"aid": new_aid, "vid": selected_visit_id},
                    )

                st.session_state[success_key] = (
                    f"Created new Target Audience (ID {new_aid}) and linked visit #{selected_visit_id} ✅"
                )
                st.rerun()

            except Exception as e:
                st.error("Failed to create new Target Audience and link visit.")
                st.caption(str(e))

# =============================
# Footer
# =============================
def get_almadar_logo_base64() -> str:
    """
    Load Al Madar logo from /static and return as base64 string.
    Adjust the path if app_v11.py is inside a subfolder.
    """
    logo_path = Path(__file__).parent / "static" / "Almadar-Logo-01.png"
    try:
        with open(logo_path, "rb") as f:
            data = f.read()
        return base64.b64encode(data).decode("utf-8")
    except Exception as e:
        print("Logo load error:", e)
        return ""

def show_footer():
    logo_b64 = get_almadar_logo_base64()

    if logo_b64:
        logo_html = f'<img src="data:image/png;base64,{logo_b64}" style="height:45px;opacity:.85;margin-bottom:6px;" />'
    else:
        logo_html = '<strong>Al Madar Medical Co.</strong>'  # fallback text

    st.markdown(
        f"""
        <hr style="margin-top:2rem;margin-bottom:1rem;opacity:0.25;">
        <div style="text-align:center; color:#6c757d;">
            {logo_html}<br>
            <span style="font-size:0.9rem;">
                © 2025 <strong>Al Madar Medical Co.</strong><br>
                Core System © <strong>Muaz Sulaiman</strong><br>
                <span style="font-size:0.8rem;">Version 11 • All rights reserved.</span>
            </span>
        </div>
        """,
        unsafe_allow_html=True
    )
    
# =============================
# MAIN
# =============================
user = st.session_state.get("user")

if not user:
    login_block()
    show_footer()
else:
    # ✅ Apply role-based layout once per request
    apply_role_based_layout()
    
    logout_button()
    page = sidebar_nav()

    if page == "Submit Visit":
        page_submit_visit()
    elif page == "My Submissions":
        page_my_submissions()
    elif page == "User Settings":
        page_user_settings()
    elif page == "Project Creation":
        page_create_project()
    elif page == "Projects View":
        page_project_view()          # you'll define this
    elif page == "Project Management":
        page_project_management()    # you'll define this
    elif page == "Admin: Import Lookups":
        page_admin_import()
    elif page == "Admin: Data Browser":
        page_admin_data()
    elif page == "Admin: Users":
        page_admin_users()
    elif page == "Review Target Audiences":
        page_review_target_audiences()

    show_footer()