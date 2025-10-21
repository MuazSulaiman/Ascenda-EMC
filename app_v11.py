# app_v11.py — Sales Daily Feedback — v11 (+Business Lines, Shelf Movement)
# - Home Visit fields right after Audience (label startswith "Home Visit")
# - Business Unit → Business Line → (optional) Item
# - Shelf Movement objective shows items of the chosen Business Line with Qty Checked
# - Atomic insert: visit + optional home_visit + optional shelf_movement(header+lines)
# - Power BI push BEFORE rerun; includes shelf aggregates when present
# - Secrets fallback: env["PBI_PUSH_URL"] or st.secrets["PBI_PUSH_URL"]
# - Duplicate check uses submitted_at_utc
# - Render + PostgreSQL (psycopg v3) — no SQLite, no settings.get

import io
import os
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

import pandas as pd
import sqlalchemy as sa
from sqlalchemy import text
import streamlit as st
from passlib.hash import pbkdf2_sha256
from streamlit_geolocation import streamlit_geolocation
from dateutil import tz
from streamlit_folium import st_folium
import folium
import streamlit.components.v1 as components
import json, requests
import re
import secrets, string

# USE our centralized Postgres engine (Render): provided by db.py
# db.py must define `engine = create_engine(get_database_url(), ...)`
from db import engine

# =============================
# Config
# =============================
APP_TITLE = "Sales Daily Feedback — v11"
TIMEZONE = "Asia/Riyadh"
SESSION_TTL_MIN = 60        # 60 mins (1 hour)
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
# DB Utilities (PostgreSQL)
# =============================

from sqlalchemy import text

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

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

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

def create_session(user_id: int) -> str:
    sid = uuid.uuid4().hex
    now = _utcnow()
    exp = now + timedelta(minutes=SESSION_TTL_MIN)
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO app_sessions(session_id, user_id, created_at_utc, expires_at_utc)
                VALUES (:sid, :uid, :created, :expires)
            """),
            {
                "sid": sid,
                "uid": user_id,
                "created": now,   # SQLAlchemy/psycopg converts to TIMESTAMPTZ
                "expires": exp,
            },
        )
    return sid

def purge_expired_sessions() -> None:
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM app_sessions WHERE expires_at_utc < :now"),
            {"now": _utcnow()},
        )

def delete_session(sid: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM app_sessions WHERE session_id = :sid"), {"sid": sid})

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

def resolve_session_user() -> Optional[Dict[str, Any]]:
    # Support both sid and legacy _sid param
    sid = st.query_params.get("sid") or st.query_params.get("_sid")
    if st.query_params.get("_sid"):
        st.query_params["sid"] = st.query_params["_sid"]
        st.query_params.pop("_sid", None)
        sid = st.query_params.get("sid")

    if not sid:
        return None

    with engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT session_id, user_id, expires_at_utc
                FROM app_sessions
                WHERE session_id = :sid
            """),
            {"sid": sid},
        ).mappings().first()

    if not row:
        st.query_params.pop("sid", None)
        return None

    try:
        # psycopg returns aware datetimes already; but guard anyway
        exp = row["expires_at_utc"]
        if isinstance(exp, str):
            exp = datetime.fromisoformat(exp)
    except Exception:
        delete_session(sid)
        st.query_params.pop("sid", None)
        return None

    exp_cmp = exp if getattr(exp, "tzinfo", None) else exp.replace(tzinfo=timezone.utc)
    if _utcnow() >= exp_cmp:
        delete_session(sid)
        st.query_params.pop("sid", None)
        return None

    u = get_user_by_id(int(row["user_id"]))
    # In Postgres schema, is_active is BOOLEAN; treat None/False as inactive
    if not u or not bool(u.get("is_active", True)):
        delete_session(sid)
        st.query_params.pop("sid", None)
        return None

    # Extend session
    new_exp = _utcnow() + timedelta(minutes=SESSION_TTL_MIN)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE app_sessions SET expires_at_utc = :exp WHERE session_id = :sid"),
            {"exp": new_exp, "sid": sid},
        )

    return u

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

# ====== dependency reset callbacks ======
def _on_customer_change():
    st.session_state.pop("aud_sel", None)

def _on_bu_change():
    st.session_state.pop("bl_sel", None)
    st.session_state.pop("prod_sel", None)

def _on_line_change():
    st.session_state.pop("prod_sel", None)

# =============================
# UI — Login / Logout (blocks inactive accounts)
# =============================
def login_block():
    st.title(f"{APP_TITLE} — Login")
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
        sid = create_session(int(u["user_id"]))
        set_url_session_param(sid)

        st.session_state["_current_page"] = "Submit Visit"
        set_url_param("page", "Submit Visit")

        st.success(f"Welcome, {u.get('name') or u.get('email')}!")
        st.rerun()

def logout_button():
    if st.sidebar.button("Logout"):
        sid = st.query_params.get("sid")
        if sid:
            delete_session(sid)
        set_url_session_param(None)
        st.session_state.user = None
        st.session_state.pop("_current_page", None)
        set_url_param("page", None)
        st.rerun()

# =============================
# UI — Sidebar (page memory via URL + session)
# =============================
def sidebar_nav():
    st.sidebar.title("Navigation")

    # Show to all authenticated users
    pages = ["Submit Visit", "My Submissions", "User Settings"]

    # Admin-only pages
    if st.session_state.user and st.session_state.user.get("role") == "admin":
        pages += ["Admin: Import Lookups", "Admin: Data Browser", "Admin: Users"]

    url_page = get_url_param("page")
    sess_page = st.session_state.get("_current_page")
    if url_page in pages:
        current = url_page
    elif sess_page in pages:
        current = sess_page
    else:
        current = pages[0]

    idx = pages.index(current)

    def _on_change():
        chosen = st.session_state["_nav_choice"]
        st.session_state["_current_page"] = chosen
        set_url_param("page", chosen)

    choice = st.sidebar.radio(
        "Go to",
        pages,
        index=idx,
        key="_nav_choice",
        on_change=_on_change
    )

    if st.session_state.get("_current_page") != choice:
        st.session_state["_current_page"] = choice
    if get_url_param("page") != choice:
        set_url_param("page", choice)

    return choice

# =============================
# Location block (auto-flow, minimal UI) – required
# =============================
def get_location_block(k) -> tuple[Optional[float], Optional[float], Optional[float]]:
    with st.expander("📍 Location (auto) — required for check-in", expanded=True):
        # Hide the default locate button from Leaflet controls if any
        st.markdown("<style>.leaflet-control-locate{display:none!important}</style>", unsafe_allow_html=True)

        tried_key = k("geo_try")
        tried = st.session_state.get(tried_key, False)

        if not tried:
            c1, _ = st.columns([1, 3])
            with c1:
                if st.button("📍 Get location", key=k("btn_get_loc"), type="primary"):
                    st.session_state[tried_key] = True
                    st.rerun()
            return (None, None, None)

        # NOTE: Works reliably over HTTPS (Render is HTTPS by default).
        loc = streamlit_geolocation()  # dict or None
        lat = loc.get("latitude") if isinstance(loc, dict) else None
        lon = loc.get("longitude") if isinstance(loc, dict) else None
        acc = loc.get("accuracy") if isinstance(loc, dict) else None

        c1, _ = st.columns([1, 3])
        with c1:
            if st.button("🔁 Retry location", key=k("btn_retry_loc")):
                st.session_state.pop(tried_key, None)
                st.rerun()

        # Use None checks (0.0 is a valid coordinate)
        if lat is None or lon is None:
            st.warning(
                "We couldn't read your location.\n\n"
                "• Allow **Location** (and **Precise location** on iOS) in browser permissions.\n"
                "• If it still fails, try tapping **Retry location**."
            )
            return (None, None, None)

        # Safe numeric casts
        try:
            flat = float(lat)
            flon = float(lon)
            facc = float(acc) if acc is not None else None
        except (TypeError, ValueError):
            st.warning("Location values looked invalid. Please try again.")
            return (None, None, None)

        acc_txt = f" (~{facc:.0f} m accuracy)" if facc is not None else ""
        st.success(f"Captured location: {flat:.6f}, {flon:.6f}{acc_txt}")

        m = folium.Map(location=[flat, flon], zoom_start=16, control_scale=True)
        folium.Marker([flat, flon], tooltip="Your location").add_to(m)
        st_folium(m, height=300, key=k("geo_map"))

        return (flat, flon, facc)

# =============================
# Page — Submit Visit (sticky submit + dedupe guard)
# =============================
from sqlalchemy.exc import IntegrityError
try:
    # psycopg 3 error class (optional, for finer duplicate checks)
    from psycopg.errors import UniqueViolation
except Exception:
    UniqueViolation = None

def page_submit_visit():
    st.title("📝 Submit Visit (v11)")

    # ---- tiny CSS for floating submit ----
    st.markdown("""
    <style>
      .sticky-submit-wrap{position:fixed; right:16px; bottom:16px; z-index:1000;}
      @media (max-width:640px){
        .sticky-submit-wrap{left:16px; right:16px;}
        .sticky-submit-wrap button{width:100%;}
      }
    </style>
    """, unsafe_allow_html=True)

    # ---- Red asterisk legend ----
    st.markdown(
        '<div style="margin:.25rem 0 1rem 0;">'
        'Fields marked with <span style="color:#d00000;font-weight:700">*</span> are required.'
        '</div>',
        unsafe_allow_html=True,
    )

    PAGE_NS = "submit_visit"
    nonce_key        = f"_{PAGE_NS}_form_nonce"
    saved_ok_key     = f"_{PAGE_NS}_saved_ok"
    geo_nonce_key    = f"_{PAGE_NS}_geo_nonce"
    geo_captured_key = f"_{PAGE_NS}_geo_captured"
    busy_key         = f"_{PAGE_NS}_busy"
    intent_key       = f"_{PAGE_NS}_submit_intent"

    st.session_state.setdefault(nonce_key, 0)
    st.session_state.setdefault(geo_nonce_key, 0)
    st.session_state.setdefault(busy_key, False)
    st.session_state.setdefault(intent_key, False)

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

    u = st.session_state.user
    uid = int(u["user_id"] if "user_id" in u else u["id"])
    st.caption(f"Logged in as **{u['name']}** · Region: **{u.get('region') or '—'}** · Role: **{u.get('role')}**")

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

    # ---------------- Location (REQUIRED) ----------------
    lat, lon, acc = get_location_block(k)
    if lat is None or lon is None:
        st.info("📍 Location is required before you can submit.")
        return

    # =====================================================
    # Region → City → Sector → Customer (all REQUIRED)
    # =====================================================

    # ---- Region (from customers.region) ----
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

    # ---- City (depends on Region; from customers.city) ----
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
        help=None if region_choice else "Select a Region first",
        on_change=_on_city_change
    )

    # ---- Sector (depends on City; from customers.sector) ----
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
        help=None if (region_choice and city_choice) else "Select a City first",
        on_change=_on_sector_change
    )

    # ---- Customer (depends on Region+City+Sector) ----
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
        cust_names = [""] + cust_df["account_name"].tolist()
    else:
        cust_df = pd.DataFrame(columns=["customer_id","account_name"])
        cust_names = [""]

    cust_choice = st.selectbox(
        "Customer *",
        cust_names,
        index=0,
        key=k("cust_sel"),
        disabled=(not (region_choice and city_choice and sector_choice)),
        help=None if (region_choice and city_choice and sector_choice) else "Select Sector first",
    )

    customer_id = None
    if cust_choice:
        match = cust_df.loc[cust_df["account_name"] == cust_choice, "customer_id"]
        customer_id = int(match.iloc[0]) if not match.empty else None

    # ---------------- Target Audience (REQUIRED; depends on Customer) ----------------
    audience_id = None
    aud_choice_label = ""
    aud_choice_name = None

    aud_labels = [""]; aud_rows = []
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
            name  = str(row.name).strip() if pd.notna(row.name) else ""
            parts.append((title + name).strip())
            if pd.notna(row.department) and str(row.department).strip():
                parts.append(str(row.department).strip())
            if pd.notna(row.position) and str(row.position).strip():
                parts.append(str(row.position).strip())
            parts = [p for p in parts if p]
            return " — ".join(parts) if parts else name

        for r in aud_df.itertuples(index=False):
            label = _fmt_audience(r)
            aud_rows.append((label, int(r.audience_id), str(r.name).strip() if pd.notna(r.name) else ""))

        aud_labels = [""] + [x[0] for x in aud_rows]
        if len(aud_labels) == 1:
            st.warning("This customer has no Target Audiences.")

    aud_choice_label = st.selectbox(
        "Target Audience *",
        aud_labels,
        index=0,
        key=k("aud_sel"),
        disabled=(customer_id is None),
        help=None if customer_id else "Select a Customer first",
    )
    if customer_id and aud_choice_label:
        for lbl, aid, raw_name in aud_rows:
            if lbl == aud_choice_label:
                audience_id = aid
                aud_choice_name = raw_name
                break

    # -------- Home Visit block (REQUIRED only if triggered) --------
    is_home_visit = bool(aud_choice_label and aud_choice_label.strip().lower().startswith("home visit"))
    patient_name = patient_phone = serial_no = None
    if is_home_visit:
        with st.container():
            patient_name  = st.text_input("Patient Name *", key=k("pat_name"))
            patient_phone = st.text_input("Patient Phone # *", key=k("pat_phone"))
            serial_no     = st.text_input("Serial # *", key=k("serial_no"))

    # ---------------- Business Unit (REQUIRED) ----------------
    bu_df = query_df("""
        SELECT business_unit_id, name
        FROM business_units
        WHERE is_active IS TRUE
        ORDER BY name
    """)
    bu_names = [""] + bu_df["name"].tolist()
    bu_choice = st.selectbox("Business Unit *", bu_names, index=0, key=k("bu_sel"), on_change=_on_bu_change)
    bu_id = None
    if bu_choice:
        match = bu_df.loc[bu_df["name"] == bu_choice, "business_unit_id"]
        bu_id = int(match.iloc[0]) if not match.empty else None

    # ---------------- Category (REQUIRED; depends on BU) ----------------
    cat_df = pd.DataFrame()
    cat_names = [""]; cat_choice = ""
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

    cat_choice = st.selectbox(
        "Category *",
        cat_names,
        index=0,
        key=k("cat_sel"),
        disabled=(bu_id is None),
        help=None if bu_id else "Select a Business Unit first",
    )

    # ---------------- Business Line (REQUIRED; depends on BU + Category) ----------------
    bl_df = pd.DataFrame()
    bl_names = [""]; bl_choice = ""
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

    # ---------------- Article Number / Product (OPTIONAL) ----------------
    prod_labels, prod_df = [""], pd.DataFrame()
    product_id = None; prod_choice = ""

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
        prod_labels = [""] + [
            (f"{(r.article_number or r.product_id)} — {r.description}" if pd.notna(r.description) and str(r.description).strip()
             else f"{(r.article_number or r.product_id)}")
            for r in prod_df.itertuples(index=False)
        ]

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
            label = (f"{(r.article_number or r.product_id)} — {r.description}" if pd.notna(r.description) and str(r.description).strip()
                     else f"{(r.article_number or r.product_id)}")
            label_to_pid[label] = r.product_id
        product_id = label_to_pid.get(prod_choice)

    # ---------------- Objective (REQUIRED) + Evaluation (REQUIRED) + Notes (OPTIONAL) ----------------
    obj_df = query_df("""
        SELECT objective_id, name
        FROM objectives
        WHERE COALESCE(is_active, TRUE) IS TRUE
        ORDER BY name
    """)
    obj_names = [""] + obj_df["name"].tolist()
    obj_choice = st.selectbox("Business Objective *", obj_names, index=0, key=k("obj_sel"))
    objective_id = None
    if obj_choice:
        match = obj_df.loc[obj_df["name"] == obj_choice, "objective_id"]
        objective_id = int(match.iloc[0]) if not match.empty else None

    is_shelf_movement = bool(obj_choice) and ("shelf movement" in obj_choice.strip().lower())
    notes = st.text_area("Notes (optional)", key=k("notes"))

    allowed_evals = {"Positive", "Negative", "Neutral", "IDK"}
    evaluation_choice = st.selectbox(
        "Evaluation *",
        [""] + sorted(list(allowed_evals)),
        index=0,
        key=k("eval_sel"),
    )
    evaluation_val = evaluation_choice if evaluation_choice in allowed_evals else None

    # ---------------- Shelf Movement grid (when objective is Shelf Movement) ----------------
    shelf_df = pd.DataFrame(); shelf_editor = None
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
                    use_container_width=True,
                    hide_index=True,
                    num_rows="fixed",
                    column_config={
                        "product_id": st.column_config.TextColumn("Product ID", disabled=True),
                        "article_number": st.column_config.TextColumn("Article #", disabled=True),
                        "description": st.column_config.TextColumn("Description", disabled=True),
                        "qty_checked": st.column_config.NumberColumn(
                            "Qty Checked",
                            help="Leave blank if not checked. Enter 0 if none on shelf.",
                            min_value=0,
                            step=1
                        ),
                    },
                )

    # ---------------- Potential duplicate banner ----------------
    if customer_id:
        mins = recent_visit_minutes(uid, customer_id)
        if mins is not None and mins < DUP_MINUTES:
            st.info(f"You submitted for **{cust_choice}** {mins} minutes ago — potential duplicate.")

    # ---------------- Submit button ----------------
    inline_click = st.button(
        "Submit",
        type="primary",
        key=k("submit_btn_inline"),
        disabled=st.session_state[busy_key],
        help="Saves immediately. You’ll see a spinner while saving."
    )

    if (inline_click) and not st.session_state[busy_key]:
        st.session_state[intent_key] = True
        st.session_state[busy_key]   = True
        st.rerun()

    if not st.session_state[intent_key]:
        return

    # ---------------- Process submission with a global spinner ----------------
    with st.spinner("Saving your visit…"):
        errors = []

        # REQUIRED in page order (new cascade first)
        if not region_choice:
            errors.append("Please choose a **Region**.")
        if not city_choice:
            errors.append("Please choose a **City**.")
        if not sector_choice:
            errors.append("Please choose a **Sector**.")
        if not customer_id:
            errors.append("Please choose a **Customer**.")
        if not audience_id:
            errors.append("Please choose a **Target Audience** for the selected customer.")

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
                errors.append("**Shelf Movement** grid is empty. Load items by selecting Business Unit and Category.")
            else:
                edited = shelf_editor.copy()
                edited["qty_checked"] = pd.to_numeric(edited["qty_checked"], errors="coerce")
                if (edited["qty_checked"].dropna() < 0).any():
                    errors.append("Quantities in **Shelf Movement** cannot be negative.")
                filled_rows = edited[edited["qty_checked"].notna()]
                if filled_rows is not None and filled_rows.empty:
                    errors.append("Enter at least **one** quantity in the **Shelf Movement** grid (blank = not checked; 0 is allowed).")
                if filled_rows is not None and not filled_rows.empty:
                    shelf_lines_payload = [
                        {"product_id": r["product_id"], "qty_checked": float(r["qty_checked"])}
                        for _, r in filled_rows.iterrows()
                    ]

        if errors:
            for msg in errors:
                st.error(msg)
            st.session_state[busy_key] = False
            st.session_state[intent_key] = False
            return

        # ----- All validations passed → persist -----
        visit_row = {
            "user_id": uid,
            "submitted_at_utc": _utcnow(),
            "submitted_at_local": _local_now_str(),
            "latitude": lat,
            "longitude": lon,
            "accuracy_m": acc,
            "customer_id": int(customer_id),
            "audience_id": int(audience_id) if audience_id else None,
            "business_line_id": int(business_line_id),
            "product_id": (None if is_shelf_movement else product_id),
            "objective_id": int(objective_id),
            "notes": (notes.strip() if notes else None),
            "evaluation": evaluation_val,
        }

        home_payload = None
        if is_home_visit:
            home_payload = {
                "patient_name": patient_name,
                "patient_phone": patient_phone,
                "serial_no": serial_no,
            }

        try:
            visit_id = insert_visit_atomic(visit_row, home_payload, shelf_lines_payload)

            # Power BI row
            def _article_from_label(lbl: str | None) -> str:
                if not lbl: return ""
                return str(lbl).split(" — ", 1)[0].strip()

            shelf_lines_count = int(len(filled_rows)) if (is_shelf_movement and filled_rows is not None) else 0
            shelf_total_qty   = int(filled_rows["qty_checked"].sum()) if (is_shelf_movement and filled_rows is not None) else 0

            pbi_row = {
                "submitted_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "submitted_at_local": datetime.now().isoformat(),
                "user_name": str(u.get("name") or ""),
                "user_region": str(u.get("region") or ""),
                "customer_name": str(cust_choice or ""),
                "audience_name": ("Home Visit" if is_home_visit else str(aud_choice_label or "")),
                "business_unit": str(bu_choice or ""),
                "category": str(cat_choice or ""),
                "business_line": str(bl_choice or ""),
                "article_number": ("" if is_shelf_movement else _article_from_label(prod_choice if (business_line_id and prod_choice) else None)),
                "objective": str(obj_choice or ""),
                "evaluation": str(evaluation_val or ""),
                "latitude": float(lat) if lat is not None else 0.0,
                "longitude": float(lon) if lon is not None else 0.0,
                "accuracy_m": (f"{acc:.1f}" if isinstance(acc, (int, float)) else (str(acc) if acc is not None else "")),
                "notes": (notes.strip() if notes else ""),
                "shelf_lines_count": shelf_lines_count,
                "shelf_total_qty": shelf_total_qty,
            }
            if is_home_visit:
                pbi_row.update({
                    "patient_name": patient_name.strip(),
                    "patient_phone": patient_phone.strip(),
                    "serial_no": serial_no.strip().upper(),
                })

            ok, err = push_visit_to_pbi(pbi_row)
            if not ok:
                st.warning(f"Saved, but Power BI push failed → {err}")
            else:
                st.toast("Pushed to Power BI ✅", icon="✅")

            # reset
            st.session_state[nonce_key] += 1
            st.session_state[geo_nonce_key] += 1
            st.session_state.pop(geo_captured_key, None)
            st.session_state[saved_ok_key] = True
            st.session_state[intent_key] = False
            st.session_state[busy_key] = False
            st.rerun()

        except IntegrityError as e:
            emsg = str(e).lower()
            if (UniqueViolation and isinstance(e.orig, UniqueViolation)) or ("duplicate key value violates unique constraint" in emsg) or ("unique constraint" in emsg and "home_visits_serial_no" in emsg):
                st.error("Serial # already exists. Please verify and try again.")
            else:
                st.error("Could not save your submission.")
                st.caption(str(e))
            st.session_state[intent_key] = False
            st.session_state[busy_key] = False
        except Exception as e:
            st.error("Could not save your submission.")
            st.caption(str(e))
            st.session_state[intent_key] = False
            st.session_state[busy_key] = False

# =============================
# Page — My Submissions
# =============================
def page_my_submissions():
    st.title("📄 My Submissions")
    u = st.session_state.user
    uid = int(u.get("user_id")) if u.get("user_id") is not None else int(u["id"])

    sql = """
        SELECT v.visit_id,
               v.submitted_at_local,
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
        st.markdown(f"**Total: {len(df):,}**")
        st.dataframe(df, use_container_width=True, hide_index=True)
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
    from passlib.hash import pbkdf2_sha256
    import re

    st.title("👤 User Settings")

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
# Page — Admin: Import Lookups (PostgreSQL + live progress)
# =============================
import time, re, unicodedata
import pandas as pd
import sqlalchemy as sa
from sqlalchemy import text

def page_admin_import():
    st.title("🛠️ Admin — Import Lookups (Excel/CSV)")
    st.caption("Files should have exact column names shown below. Existing rows are kept; duplicates are skipped.")

    # -----------------------
    # Progress UI helpers
    # -----------------------
    def _mk_status(title: str):
        """Return (status_ctx, progress_widget, line_widget, has_status) with a safe fallback."""
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

    def _update_progress(pb, ln, i, total, inserted=0, updated=0, skipped=0, every_toast=0, label_prefix=""):
        frac = max(0.0, min(1.0, (i / float(total))) ) if total else 0.0
        pb.progress(frac)
        ln.write(f"{label_prefix} {i}/{total} · Inserted: {inserted} · Updated: {updated} · Skipped: {skipped}")
        if every_toast and i % every_toast == 0:
            st.toast(f"{label_prefix} {i}/{total}")

    def _finish_status(sts_or_spinner, has_status: bool, final_text: str, ok: bool=True):
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
        if hasattr(st, "popover"):
            return st.popover(label)
        st.markdown(f"**{label}**")
        return st.container()

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

    # =====================
    # 1) Customers
    # =====================
    st.subheader("1) Customers")
    st.write("Columns: **account_name**, sector, region, city")

    with popout("➕ Add Customer"):
        with st.form("add_customer_form", clear_on_submit=True):
            acc = st.text_input("Account Name *")
            sector = st.text_input("Sector")
            region = st.text_input("Region")
            city = st.text_input("City")
            submit_cust = st.form_submit_button("Save Customer", type="primary")
        if submit_cust:
            if not acc.strip():
                st.error("Account Name is required.")
            else:
                try:
                    with engine.begin() as conn:
                        res = conn.execute(
                            text("""
                                INSERT INTO customers(account_name, sector, region, city)
                                SELECT :acc, :sector, :region, :city
                                WHERE NOT EXISTS (
                                  SELECT 1 FROM customers WHERE lower(account_name) = lower(:acc)
                                )
                            """),
                            {
                                "acc": acc.strip(),
                                "sector": (sector.strip() or None),
                                "region": (region.strip() or None),
                                "city": (city.strip() or None),
                            },
                        )
                    if (res.rowcount or 0) > 0:
                        st.success("Customer added ✅")
                    else:
                        st.info("A customer with that name already exists — nothing added.")
                except Exception as e:
                    st.error("Could not add customer.")
                    st.caption(str(e))

    f1 = st.file_uploader("Upload Customers", type=["xlsx", "csv"], key="cust")
    if f1 is not None:
        df = pd.read_excel(f1) if f1.name.endswith(".xlsx") else pd.read_csv(f1)
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
                        acc_v = str(getattr(r, "account_name", "")).strip()
                        if not acc_v:
                            skipped += 1
                            if i % 200 == 0 or i == total:
                                _update_progress(pb, ln, i, total, inserted, 0, skipped, label_prefix="Customers")
                            continue
                        sector_v = str(getattr(r, "sector")).strip() if hasattr(r, "sector") and pd.notna(getattr(r, "sector")) else None
                        region_v = str(getattr(r, "region")).strip() if hasattr(r, "region") and pd.notna(getattr(r, "region")) else None
                        city_v   = str(getattr(r, "city")).strip()   if hasattr(r, "city")   and pd.notna(getattr(r, "city"))   else None

                        res = conn.execute(
                            text("""
                                INSERT INTO customers(account_name, sector, region, city)
                                SELECT :acc, :sector, :region, :city
                                WHERE NOT EXISTS (
                                  SELECT 1 FROM customers WHERE lower(account_name) = lower(:acc)
                                )
                            """),
                            {"acc": acc_v, "sector": sector_v, "region": region_v, "city": city_v},
                        )
                        if (res.rowcount or 0) > 0:
                            inserted += 1
                        else:
                            skipped += 1

                        if i % 200 == 0 or i == total:
                            _update_progress(pb, ln, i, total, inserted, 0, skipped, label_prefix="Customers")
                            time.sleep(0.001)

                _finish_status(sts, has_status, f"Customers import ✅ Inserted: {inserted} | Updated: 0 | Skipped: {skipped}", ok=True)
            except Exception as e:
                _finish_status(sts, has_status, "Customers import failed ❌", ok=False)
                st.caption(str(e))

    st.divider()

    # =====================
    # 2) Target Audiences
    # =====================
    st.subheader("2) Target Audiences")
    st.write("Columns: **customer_name**, title, name, department, position, potentiality, loyalty, mobile, landline, external_number, email")

    cust_df_for_aud = query_df("SELECT customer_id, account_name FROM customers ORDER BY account_name")
    cust_name_opts = [""] + cust_df_for_aud["account_name"].tolist()
    cust_name_to_id = {r.account_name: int(r.customer_id) for r in cust_df_for_aud.itertuples(index=False)}

    with popout("➕ Add Target Audience"):
        with st.form("add_audience_form", clear_on_submit=True):
            sel_cust_name = st.selectbox("Customer *", cust_name_opts, index=0)
            title = st.text_input("Title")
            aud_name = st.text_input("Name *")
            dept = st.text_input("Department")
            pos = st.text_input("Position")
            pot = st.text_input("Potentiality")
            loy = st.text_input("Loyalty")
            mobile = st.text_input("Mobile")
            land = st.text_input("Landline")
            extn = st.text_input("External Number")
            email = st.text_input("Email")
            submit_aud = st.form_submit_button("Save Target Audience", type="primary")

        if submit_aud:
            if not (sel_cust_name and aud_name.strip()):
                st.error("Customer and Name are required.")
            else:
                try:
                    cid = cust_name_to_id.get(sel_cust_name)
                    if not cid:
                        st.error("Selected customer was not found.")
                    else:
                        with engine.begin() as conn:
                            res = conn.execute(
                                text("""
                                    INSERT INTO target_audiences(
                                      customer_id, title, name, department, position, potentiality, loyalty,
                                      mobile, landline, external_number, email, is_active
                                    ) VALUES (:cid, :title, :name, :dept, :pos, :pot, :loy, :mob, :land, :extn, :email, TRUE)
                                    ON CONFLICT (customer_id, name) DO NOTHING
                                """),
                                {
                                    "cid": cid,
                                    "title": (title.strip() or None),
                                    "name": aud_name.strip(),
                                    "dept": (dept.strip() or None),
                                    "pos": (pos.strip() or None),
                                    "pot": (pot.strip() or None),
                                    "loy": (loy.strip() or None),
                                    "mob": (mobile.strip() or None),
                                    "land": (land.strip() or None),
                                    "extn": (extn.strip() or None),
                                    "email": (email.strip() or None),
                                },
                            )
                        if (res.rowcount or 0) > 0:
                            st.success("Target audience added ✅")
                        else:
                            st.info("This customer + name already exists — nothing added.")
                except Exception as e:
                    st.error("Could not add target audience.")
                    st.caption(str(e))

    f2 = st.file_uploader("Upload Target Audiences", type=["xlsx", "csv"], key="aud")
    if f2 is not None:
        df = _read_df_upload(f2)
        needed = {"customer_name", "name"}
        if not needed.issubset(df.columns):
            st.error("Missing required columns: customer_name, name")
        else:
            total = len(df)
            inserted = 0
            updated = 0
            skipped = 0
            sts, pb, ln, has_status = _mk_status("Importing Target Audiences…")
            try:
                with engine.begin() as conn:
                    cdf = pd.read_sql_query(text("SELECT customer_id, account_name FROM customers"), conn)
                    cmap = {str(r.account_name).strip().lower(): int(r.customer_id) for r in cdf.itertuples(index=False)}

                    for i, r in enumerate(df.itertuples(index=False), start=1):
                        cname = str(getattr(r, "customer_name", "")).strip()
                        aname = str(getattr(r, "name", "")).strip()
                        if not (cname and aname):
                            skipped += 1
                            if i % 200 == 0 or i == total:
                                _update_progress(pb, ln, i, total, inserted, updated, skipped, label_prefix="Audiences")
                            continue
                        cid = cmap.get(cname.lower())
                        if not cid:
                            skipped += 1
                            if i % 200 == 0 or i == total:
                                _update_progress(pb, ln, i, total, inserted, updated, skipped, label_prefix="Audiences")
                            continue

                        title_v = (str(getattr(r, "title")).strip() if hasattr(r, "title") and pd.notna(getattr(r, "title")) else None)
                        dept_v  = (str(getattr(r, "department")).strip() if hasattr(r, "department") and pd.notna(getattr(r, "department")) else None)
                        pos_v   = (str(getattr(r, "position")).strip() if hasattr(r, "position") and pd.notna(getattr(r, "position")) else None)
                        pot_v   = (str(getattr(r, "potentiality")).strip() if hasattr(r, "potentiality") and pd.notna(getattr(r, "potentiality")) else None)
                        loy_v   = (str(getattr(r, "loyalty")).strip() if hasattr(r, "loyalty") and pd.notna(getattr(r, "loyalty")) else None)
                        mob_v   = (str(getattr(r, "mobile")).strip() if hasattr(r, "mobile") and pd.notna(getattr(r, "mobile")) else None)
                        land_v  = (str(getattr(r, "landline")).strip() if hasattr(r, "landline") and pd.notna(getattr(r, "landline")) else None)
                        extn_v  = (str(getattr(r, "external_number")).strip() if hasattr(r, "external_number") and pd.notna(getattr(r, "external_number")) else None)
                        email_v = (str(getattr(r, "email")).strip() if hasattr(r, "email") and pd.notna(getattr(r, "email")) else None)

                        # Upsert, count exactly
                        existed = conn.execute(
                            text("SELECT 1 FROM target_audiences WHERE customer_id=:cid AND lower(name)=lower(:n)"),
                            {"cid": cid, "n": aname}
                        ).fetchone() is not None

                        conn.execute(
                            text("""
                                INSERT INTO target_audiences(
                                  customer_id, title, name, department, position, potentiality, loyalty,
                                  mobile, landline, external_number, email, is_active
                                )
                                VALUES (:cid, :title, :name, :dept, :pos, :pot, :loy, :mob, :land, :extn, :email, TRUE)
                                ON CONFLICT (customer_id, name) DO UPDATE
                                  SET title=:title,
                                      department=:dept,
                                      position=:pos,
                                      potentiality=:pot,
                                      loyalty=:loy,
                                      mobile=:mob,
                                      landline=:land,
                                      external_number=:extn,
                                      email=:email,
                                      is_active=TRUE
                            """),
                            {
                                "cid": cid, "title": title_v, "name": aname, "dept": dept_v, "pos": pos_v,
                                "pot": pot_v, "loy": loy_v, "mob": mob_v, "land": land_v, "extn": extn_v, "email": email_v,
                            },
                        )

                        if existed:
                            updated += 1
                        else:
                            inserted += 1

                        if i % 200 == 0 or i == total:
                            _update_progress(pb, ln, i, total, inserted, updated, skipped, label_prefix="Audiences")
                            time.sleep(0.001)

                _finish_status(sts, has_status, f"Target audiences import ✅ Inserted: {inserted} | Updated: {updated} | Skipped: {skipped}", ok=True)
            except Exception as e:
                _finish_status(sts, has_status, "Target audiences import failed ❌", ok=False)
                st.caption(str(e))

    st.divider()

    # =====================
    # 3) Business Units
    # =====================
    st.subheader("3) Business Units")
    st.write("Columns: **name**")

    with popout("➕ Add Business Unit"):
        with st.form("add_bu_form", clear_on_submit=True):
            bu_name = st.text_input("Business Unit Name *")
            submit_bu = st.form_submit_button("Save Business Unit", type="primary")
        if submit_bu:
            if not bu_name.strip():
                st.error("Business Unit name is required.")
            else:
                try:
                    with engine.begin() as conn:
                        res = conn.execute(
                            text("""
                                INSERT INTO business_units(name, is_active)
                                VALUES (:name, TRUE)
                                ON CONFLICT (name) DO NOTHING
                            """),
                            {"name": bu_name.strip()},
                        )
                    if (res.rowcount or 0) > 0:
                        st.success("Business Unit added ✅")
                    else:
                        st.info("That Business Unit already exists — nothing added.")
                except Exception as e:
                    st.error("Could not add Business Unit.")
                    st.caption(str(e))

    fbu = st.file_uploader("Upload Business Units", type=["xlsx", "csv"], key="bus")
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
                        nm = str(getattr(r, "name", "")).strip()
                        if not nm:
                            skipped += 1
                        else:
                            res = conn.execute(
                                text("""
                                    INSERT INTO business_units(name, is_active)
                                    VALUES (:name, TRUE)
                                    ON CONFLICT (name) DO NOTHING
                                """),
                                {"name": nm},
                            )
                            if (res.rowcount or 0) > 0:
                                inserted += 1
                            else:
                                skipped += 1

                        if i % 200 == 0 or i == total:
                            _update_progress(pb, ln, i, total, inserted, 0, skipped, label_prefix="Business Units")
                            time.sleep(0.001)

                _finish_status(sts, has_status, f"Business Units import ✅ Inserted: {inserted} | Skipped: {skipped}", ok=True)
            except Exception as e:
                _finish_status(sts, has_status, "Business Units import failed ❌", ok=False)
                st.caption(str(e))

    st.divider()

    # =====================
    # 4) Business Lines
    # =====================
    st.subheader("4) Business Lines")
    st.write("Columns: **business_unit**, **name**, **category**  (optional: supplier, product_group)")

    bu_df_for_bl = query_df("SELECT business_unit_id, name FROM business_units WHERE is_active IS TRUE ORDER BY name")
    bu_name_opts = [""] + bu_df_for_bl["name"].tolist()
    bu_name_to_id = {r.name: int(r.business_unit_id) for r in bu_df_for_bl.itertuples(index=False)}

    with popout("➕ Add Business Line"):
        with st.form("add_bl_form", clear_on_submit=True):
            bu_sel = st.selectbox("Business Unit *", bu_name_opts, index=0)
            bl_name = st.text_input("Business Line Name *")
            supplier = st.text_input("Supplier")
            category = st.text_input("Category *")
            prod_group = st.text_input("Product Group")
            submit_bl = st.form_submit_button("Save Business Line", type="primary")
        if submit_bl:
            if not (bu_sel and bl_name.strip() and category.strip()):
                st.error("Business Unit, Business Line Name, and Category are required.")
            else:
                try:
                    bu_id = bu_name_to_id.get(bu_sel)
                    if not bu_id:
                        st.error("Selected Business Unit not found.")
                    else:
                        with engine.begin() as conn:
                            res = conn.execute(
                                text("""
                                    INSERT INTO business_lines(
                                      business_unit_id, name, supplier, category, product_group, is_active
                                    )
                                    VALUES (:bid, :name, :supplier, :category, :pg, TRUE)
                                    ON CONFLICT (business_unit_id, name) DO NOTHING
                                """),
                                {
                                    "bid": bu_id,
                                    "name": bl_name.strip(),
                                    "supplier": (supplier.strip() or None),
                                    "category": category.strip(),
                                    "pg": (prod_group.strip() or None),
                                },
                            )
                        if (res.rowcount or 0) > 0:
                            st.success("Business Line added ✅")
                        else:
                            st.info("That Business Unit + Line already exists — nothing added.")
                except Exception as e:
                    st.error("Could not add Business Line.")
                    st.caption(str(e))

    fbl = st.file_uploader("Upload Business Lines", type=["xlsx", "csv"], key="blines")
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
                        bu_name_raw = (str(getattr(r, "business_unit")) if hasattr(r, "business_unit") and pd.notna(getattr(r, "business_unit")) else "").strip()
                        bl_name_raw = (str(getattr(r, "name")) if hasattr(r, "name") and pd.notna(getattr(r, "name")) else "").strip()
                        cat_raw     = (str(getattr(r, "category")) if hasattr(r, "category") and pd.notna(getattr(r, "category")) else "").strip()

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

                        supplier_v = (str(getattr(r, "supplier")).strip() if hasattr(r, "supplier") and pd.notna(getattr(r, "supplier")) else None)
                        prod_group_v = (str(getattr(r, "product_group")).strip() if hasattr(r, "product_group") and pd.notna(getattr(r, "product_group")) else None)

                        res = conn.execute(
                            text("""
                                INSERT INTO business_lines(business_unit_id, name, supplier, category, product_group, is_active)
                                VALUES (:bid, :name, :supplier, :category, :pg, TRUE)
                                ON CONFLICT (business_unit_id, name) DO NOTHING
                            """),
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

                _finish_status(sts, has_status, f"Business Lines import ✅ Inserted: {inserted} | Skipped: {skipped}", ok=True)
            except Exception as e:
                _finish_status(sts, has_status, "Business Lines import failed ❌", ok=False)
                st.caption(str(e))

    st.divider()

    # =====================
    # 5) Items (Products)
    # =====================
    st.subheader("5) Items (Products)")
    st.write("Columns: **product_id**, article_number, description, **business_unit**, **business_line**")

    # Keep state for in-form BU/BL
    st.session_state.setdefault("ai_bu_id", None)
    st.session_state.setdefault("ai_bl_id", None)

    with popout("➕ Add Item"):
        # One bordered box that contains everything (BU, BL, and fields)
        with st.container(border=True):
            # ---------- State ----------
            st.session_state.setdefault("ai_bu_id", None)
            st.session_state.setdefault("ai_bl_id", None)

            def _on_bu_change():
                # Clear BL when BU changes, then rerun to refresh BL list
                st.session_state["ai_bl_id"] = None
                st.rerun()

            # ---------- Business Unit (dependent parent) ----------
            bu_df = query_df("""
                SELECT business_unit_id, name
                FROM business_units
                WHERE COALESCE(is_active, TRUE) IS TRUE
                ORDER BY name
            """)

            if bu_df.empty:
                st.warning("No active Business Units found. Add one first.")
                bu_labels, bu_ids = [], []
            else:
                bu_labels = [""] + bu_df["name"].tolist()  # <-- add blank at start
                bu_ids    = [None] + bu_df["business_unit_id"].astype(int).tolist()

            bu_index = (
                bu_ids.index(st.session_state["ai_bu_id"])
                if st.session_state["ai_bu_id"] in bu_ids
                else 0
            )

            bu_idx = st.selectbox(
                "Business Unit *",
                options=list(range(len(bu_labels))),
                index=bu_index if bu_labels else 0,
                format_func=lambda i: bu_labels[i] if bu_labels else "",
                key="ai_bu_idx",
                on_change=_on_bu_change,
            )
            selected_bu_id = bu_ids[bu_idx] if bu_labels else None
            st.session_state["ai_bu_id"] = selected_bu_id

            # ---------- Business Line (child; filtered by BU) ----------
            if selected_bu_id:
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

            bl_labels = [""] + bl_df["name"].tolist()  # <-- add blank at start
            bl_ids    = [None] + bl_df["business_line_id"].astype(int).tolist() if not bl_df.empty else [None]

            bl_index = (
                bl_ids.index(st.session_state["ai_bl_id"])
                if st.session_state["ai_bl_id"] in bl_ids
                else 0
            )

            bl_widget_key = f"ai_bl_idx__bu_{selected_bu_id or 'none'}"
            bl_idx = st.selectbox(
                "Business Line *",
                options=list(range(len(bl_labels))),
                index=bl_index if bl_labels else 0,
                format_func=lambda i: bl_labels[i] if bl_labels else "",
                key=bl_widget_key,
                help="Choose a Business Unit first to load its lines.",
            )
            selected_bl_id = bl_ids[bl_idx] if bl_labels else None
            st.session_state["ai_bl_id"] = selected_bl_id

            # ---------- Item fields ----------
            product_id = st.text_input("Product ID * (must be unique)", key="ai_pid")
            article    = st.text_input("Article Number *", key="ai_article")
            desc       = st.text_input("Description", key="ai_desc")

            # Submit button (acts like form submit)
            submitted = st.button("Save Item", type="primary", key="ai_save_item")

            # ---------- Handle submit ----------
            if submitted:
                if not product_id.strip():
                    st.error("Product ID is required.")
                elif not article.strip():
                    st.error("Article Number is required.")
                elif not selected_bu_id or not selected_bl_id:
                    st.error("Business Unit and Business Line are required.")
                else:
                    try:
                        with engine.begin() as conn:
                            res = conn.execute(
                                text("""
                                    INSERT INTO items(
                                    product_id, article_number, description, business_line_id, is_active
                                    ) VALUES (:pid, :article, :desc, :blid, TRUE)
                                    ON CONFLICT (product_id) DO NOTHING
                                """),
                                {
                                    "pid": product_id.strip(),
                                    "article": article.strip(),
                                    "desc": (desc.strip() or None),
                                    "blid": int(selected_bl_id),
                                },
                            )
                        if (res.rowcount or 0) > 0:
                            st.success("Item added ✅")
                            st.session_state["ai_article"] = ""
                            st.session_state["ai_desc"] = ""
                            st.session_state["ai_pid"] = ""
                            st.session_state["ai_bl_id"] = None
                            st.rerun()
                        else:
                            st.error("That Product ID already exists.")
                    except Exception as e:
                        st.error("Could not add item.")
                        st.caption(str(e))

                    
    # ---------------------
    # Bulk upload
    # ---------------------
    # Build a resolver map: BU name -> list[(BL name, BL id)]
    _bl_map_df = query_df("""
        SELECT bu.name AS bu_name, bl.name AS bl_name, bl.business_line_id AS bl_id
        FROM business_lines bl
        JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
        WHERE COALESCE(bu.is_active, TRUE) IS TRUE
        AND COALESCE(bl.is_active, TRUE) IS TRUE
        ORDER BY bu.name, bl.name
    """)
    bu_to_bls = {}
    for r in _bl_map_df.itertuples(index=False):
        bu_to_bls.setdefault(str(r.bu_name).strip(), []).append((str(r.bl_name).strip(), int(r.bl_id)))

    f3 = st.file_uploader("Upload Items", type=["xlsx", "csv"], key="items")
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
                    existing = set(pd.read_sql_query(text("SELECT product_id FROM items"), conn)["product_id"].astype(str).tolist())

                    for i, r in enumerate(df.itertuples(index=False), start=1):
                        pid_raw = getattr(r, "product_id", None)
                        pid = (str(pid_raw).strip() if pd.notna(pid_raw) else "")
                        bu_name_raw = (str(getattr(r, "business_unit", "")).strip() if hasattr(r, "business_unit") else "")
                        bl_name_raw = (str(getattr(r, "business_line", "")).strip() if hasattr(r, "business_line") else "")
                        if not (pid and bu_name_raw and bl_name_raw):
                            skipped += 1
                            if i % 200 == 0 or i == total:
                                _update_progress(pb, ln, i, total, inserted, updated, skipped, label_prefix="Items")
                            continue

                        # Resolve BL id from the in-memory mapping
                        bl_id = None
                        if bu_name_raw in bu_to_bls:
                            for name, _id in bu_to_bls[bu_name_raw]:
                                if name == bl_name_raw:
                                    bl_id = _id
                                    break

                        if not bl_id:
                            skipped += 1
                            if i % 200 == 0 or i == total:
                                _update_progress(pb, ln, i, total, inserted, updated, skipped, label_prefix="Items")
                            continue

                        article_v = (str(getattr(r, "article_number")).strip() if hasattr(r, "article_number") and pd.notna(getattr(r, "article_number")) else None)
                        desc_v    = (str(getattr(r, "description")).strip()    if hasattr(r, "description")    and pd.notna(getattr(r, "description"))    else None)

                        conn.execute(
                            text("""
                                INSERT INTO items(product_id, article_number, description, business_line_id, is_active)
                                VALUES (:pid, :article, :desc, :blid, TRUE)
                                ON CONFLICT (product_id) DO UPDATE
                                SET article_number   = EXCLUDED.article_number,
                                    description      = EXCLUDED.description,
                                    business_line_id = EXCLUDED.business_line_id,
                                    is_active        = TRUE
                            """),
                            {"pid": pid, "article": article_v, "desc": desc_v, "blid": int(bl_id)},
                        )

                        if pid in existing:
                            updated += 1
                        else:
                            inserted += 1

                        if i % 200 == 0 or i == total:
                            _update_progress(pb, ln, i, total, inserted, updated, skipped, label_prefix="Items")
                            time.sleep(0.001)

                _finish_status(sts, has_status, f"Items import ✅ Inserted: {inserted} | Updated: {updated} | Skipped: {skipped}", ok=True)
            except Exception as e:
                _finish_status(sts, has_status, "Items import failed ❌", ok=False)
                st.caption(str(e))

    st.divider()

    # =====================
    # 6) Objectives
    # =====================
    st.subheader("6) Objectives")
    st.write("Columns: **name**")

    with popout("➕ Add Objective"):
        with st.form("add_objective_form", clear_on_submit=True):
            obj_name = st.text_input("Objective Name *")
            submit_obj = st.form_submit_button("Save Objective", type="primary")
        if submit_obj:
            if not obj_name.strip():
                st.error("Objective name is required.")
            else:
                try:
                    with engine.begin() as conn:
                        res = conn.execute(
                            text("INSERT INTO objectives(name, is_active) VALUES(:n, TRUE) ON CONFLICT (name) DO NOTHING"),
                            {"n": obj_name.strip()},
                        )
                    if (res.rowcount or 0) > 0:
                        st.success("Objective added ✅")
                    else:
                        st.info("That objective already exists — nothing added.")
                except Exception as e:
                    st.error("Could not add objective.")
                    st.caption(str(e))

    fobj = st.file_uploader("Upload Objectives", type=["xlsx", "csv"], key="objs")
    if fobj is not None:
        df = _read_df_upload(fobj)
        if "name" not in df.columns:
            st.error("Missing required column: name")
        else:
            total = len(df)
            inserted = 0
            skipped = 0
            sts, pb, ln, has_status = _mk_status("Importing Objectives…")
            try:
                with engine.begin() as conn:
                    for i, r in enumerate(df.itertuples(index=False), start=1):
                        nm = str(getattr(r, "name", "")).strip()
                        if not nm:
                            skipped += 1
                        else:
                            res = conn.execute(
                                text("INSERT INTO objectives(name, is_active) VALUES(:n, TRUE) ON CONFLICT (name) DO NOTHING"),
                                {"n": nm},
                            )
                            if (res.rowcount or 0) > 0:
                                inserted += 1
                            else:
                                skipped += 1

                        if i % 200 == 0 or i == total:
                            _update_progress(pb, ln, i, total, inserted, 0, skipped, label_prefix="Objectives")
                            time.sleep(0.001)

                _finish_status(sts, has_status, f"Objectives import ✅ Inserted: {inserted} | Skipped: {skipped}", ok=True)
            except Exception as e:
                _finish_status(sts, has_status, "Objectives import failed ❌", ok=False)
                st.caption(str(e))

    # ============================
    # Manage (Edit / Activate / Delete)
    # ============================
    st.divider()
    st.subheader("📝 Manage (Edit / Activate / Delete)")

    tabs = st.tabs(["Customers", "Target Audiences", "Business Units", "Business Lines", "Items", "Objectives"])

    # ---- Customers ----
    with tabs[0]:
        cdf = query_df("SELECT customer_id, account_name, sector, region, city, is_active FROM customers ORDER BY account_name")
        if cdf.empty:
            st.info("No customers yet.")
        else:
            display = [
                _parts_join(r.account_name, r.region, r.city) + f" ({'active' if bool(r.is_active) else 'inactive'})"
                for r in cdf.itertuples(index=False)
            ]
            choice = st.selectbox("Select customer", display, index=0, key="mg_cust_sel")
            row = cdf.iloc[display.index(choice)]
            cid = int(row["customer_id"])

            colA, colB, colC = st.columns(3)
            with colA:
                v_cnt = _refcount("SELECT COUNT(*) FROM visits WHERE customer_id=:cid", {"cid": cid})
                a_cnt = _refcount("SELECT COUNT(*) FROM target_audiences WHERE customer_id=:cid", {"cid": cid})
                st.caption(f"Refs → Visits: **{v_cnt}** · Audiences: **{a_cnt}**")

            with colB:
                new_active = not bool(row["is_active"])
                label = "Deactivate" if bool(row["is_active"]) else "Activate"
                if st.button(label, key="mg_cust_toggle"):
                    exec_sql("UPDATE customers SET is_active=:b WHERE customer_id=:id", {"b": new_active, "id": cid})
                    st.success("Updated ✅")

            with colC:
                edit_box = st.popover("Edit") if hasattr(st, "popover") else st.expander("Edit", expanded=False)
                with edit_box:
                    with st.form("mg_cust_edit"):
                        acc = st.text_input("Account Name *", value=row["account_name"] or "")
                        sector = st.text_input("Sector", value=row["sector"] or "")
                        region = st.text_input("Region", value=row["region"] or "")
                        city = st.text_input("City", value=row["city"] or "")
                        save = st.form_submit_button("Save changes")
                    if save:
                        acc_clean = acc.strip()
                        if not acc_clean:
                            st.error("Account Name is required.")
                        else:
                            dup = query_df(
                                "SELECT 1 FROM customers WHERE lower(account_name)=lower(:n) AND customer_id<>:id",
                                {"n": acc_clean, "id": cid},
                            )
                            if not dup.empty:
                                st.error("Account Name already exists.")
                            else:
                                exec_sql(
                                    "UPDATE customers SET account_name=:acc, sector=:s, region=:r, city=:c WHERE customer_id=:id",
                                    {"acc": acc_clean, "s": sector.strip() or None, "r": region.strip() or None, "c": city.strip() or None, "id": cid},
                                )
                                st.success("Saved ✅")

            dz_keybase = "mg_cust_conf"
            conf_key = f"{dz_keybase}_{st.session_state['danger_nonce']}"
            danger = st.popover("Danger Zone") if hasattr(st, "popover") else st.expander("Danger Zone", expanded=False)
            with danger:
                st.write("Delete permanently (only if not referenced).")
                confirm = st.checkbox("I understand this cannot be undone.", key=conf_key)
                if st.button("Delete Customer", type="primary", disabled=not confirm, key="mg_cust_del"):
                    st.session_state["danger_nonce"] += 1
                    if v_cnt > 0 or a_cnt > 0:
                        st.session_state["flash_admin"] = ("error", "Cannot delete: it is referenced by visits and/or target audiences. Deactivate instead.")
                        st.rerun()
                    try:
                        exec_sql("DELETE FROM customers WHERE customer_id=:id", {"id": cid})
                        st.session_state["flash_admin"] = ("success", "Customer deleted ✅")
                    except Exception as e:
                        st.session_state["flash_admin"] = ("error", f"Delete failed: {e}")
                    st.rerun()

    # ---- Target Audiences ----
    with tabs[1]:
        adf = query_df("""
            SELECT ta.audience_id, ta.customer_id, c.account_name AS customer,
                   ta.title, ta.name, ta.department, ta.position,
                   ta.potentiality, ta.loyalty, ta.mobile, ta.landline, ta.external_number, ta.email,
                   ta.is_active
            FROM target_audiences ta
            JOIN customers c ON c.customer_id = ta.customer_id
            ORDER BY c.account_name, ta.name
        """)
        if adf.empty:
            st.info("No target audiences yet.")
        else:
            def _fmt_ta(r):
                title_name = f"{(str(r.title).strip() + ' ') if pd.notna(r.title) and str(r.title).strip() else ''}{str(r.name).strip() if pd.notna(r.name) else ''}".strip()
                parts = [str(r.customer).strip() if pd.notna(r.customer) else "", title_name]
                if pd.notna(r.department) and str(r.department).strip():
                    parts.append(str(r.department).strip())
                if pd.notna(r.position) and str(r.position).strip():
                    parts.append(str(r.position).strip())
                return " - ".join([p for p in parts if p])

            display = [f"{_fmt_ta(r)}  ({'active' if r.is_active else 'inactive'})" for r in adf.itertuples(index=False)]
            choice = st.selectbox("Select audience", display, index=0, key="mg_aud_sel")
            row = adf.iloc[display.index(choice)]
            aid = int(row["audience_id"])

            colA, colB, colC = st.columns(3)
            with colA:
                v_cnt = _refcount("SELECT COUNT(*) FROM visits WHERE audience_id=:aid", {"aid": aid})
                st.caption(f"Refs → Visits: **{v_cnt}**")

            with colB:
                new_active = not bool(row["is_active"])
                label = "Deactivate" if bool(row["is_active"]) else "Activate"
                if st.button(label, key="mg_aud_toggle"):
                    exec_sql("UPDATE target_audiences SET is_active=:b WHERE audience_id=:id", {"b": new_active, "id": aid})
                    st.success("Updated ✅")

            with colC:
                edit_box = st.popover("Edit") if hasattr(st, "popover") else st.expander("Edit", expanded=False)
                with edit_box:
                    cust_choices = query_df("SELECT customer_id, account_name FROM customers ORDER BY account_name")
                    cust_labels = [f"{r.account_name}" for r in cust_choices.itertuples(index=False)]
                    cust_idx = 0
                    for i, r in enumerate(cust_choices.itertuples(index=False)):
                        if int(r.customer_id) == int(row["customer_id"]):
                            cust_idx = i
                            break

                    with st.form("mg_aud_edit"):
                        cust_label_sel = st.selectbox("Customer *", cust_labels, index=cust_idx, key="mg_aud_cust_sel")
                        new_cust_id = int(cust_choices.iloc[cust_labels.index(cust_label_sel)]["customer_id"])
                        title = st.text_input("Title", value=row["title"] or "")
                        name = st.text_input("Name *", value=row["name"] or "")
                        dept = st.text_input("Department", value=row["department"] or "")
                        pos = st.text_input("Position", value=row["position"] or "")
                        pot = st.text_input("Potentiality", value=row["potentiality"] or "")
                        loy = st.text_input("Loyalty", value=row["loyalty"] or "")
                        mob = st.text_input("Mobile", value=row["mobile"] or "")
                        land = st.text_input("Landline", value=row["landline"] or "")
                        extn = st.text_input("External Number", value=row["external_number"] or "")
                        email = st.text_input("Email", value=row["email"] or "")
                        save = st.form_submit_button("Save changes")
                    if save:
                        nm = name.strip()
                        if not nm:
                            st.error("Name is required.")
                        else:
                            dup = query_df(
                                """
                                SELECT 1 FROM target_audiences
                                WHERE customer_id=:cid AND lower(name)=lower(:nm) AND audience_id<>:aid
                                """,
                                {"cid": new_cust_id, "nm": nm, "aid": aid},
                            )
                            if not dup.empty:
                                st.error("An audience with the same name already exists for that customer.")
                            else:
                                exec_sql(
                                    """
                                    UPDATE target_audiences
                                    SET customer_id=:cid, title=:title, name=:name, department=:dept, position=:pos,
                                        potentiality=:pot, loyalty=:loy, mobile=:mob, landline=:land, external_number=:extn, email=:email
                                    WHERE audience_id=:aid
                                    """,
                                    {
                                        "cid": new_cust_id, "title": title.strip() or None, "name": nm,
                                        "dept": dept.strip() or None, "pos": pos.strip() or None,
                                        "pot": pot.strip() or None, "loy": loy.strip() or None,
                                        "mob": mob.strip() or None, "land": land.strip() or None,
                                        "extn": extn.strip() or None, "email": email.strip() or None,
                                        "aid": aid,
                                    },
                                )
                                st.success("Saved ✅")

            dz_keybase = "mg_aud_conf"
            conf_key = f"{dz_keybase}_{st.session_state['danger_nonce']}"
            danger = st.popover("Danger Zone") if hasattr(st, "popover") else st.expander("Danger Zone", expanded=False)
            with danger:
                st.write("Delete permanently (only if not referenced).")
                confirm = st.checkbox("I understand this cannot be undone.", key=conf_key)
                if st.button("Delete Audience", type="primary", disabled=not confirm, key="mg_aud_del"):
                    st.session_state["danger_nonce"] += 1
                    if v_cnt > 0:
                        st.session_state["flash_admin"] = ("error", "Cannot delete: it is referenced by visits. Deactivate instead.")
                        st.rerun()
                    try:
                        exec_sql("DELETE FROM target_audiences WHERE audience_id=:id", {"id": aid})
                        st.session_state["flash_admin"] = ("success", "Audience deleted ✅")
                    except Exception as e:
                        st.session_state["flash_admin"] = ("error", f"Delete failed: {e}")
                    st.rerun()

    # ---- Business Units (Manage)
    with tabs[2]:
        bdf = query_df("SELECT business_unit_id, name, is_active FROM business_units ORDER BY name")
        if bdf.empty:
            st.info("No business units yet.")
        else:
            display = [f"{r.name}  ({'active' if r.is_active else 'inactive'})" for r in bdf.itertuples(index=False)]
            choice = st.selectbox("Select business unit", display, index=0, key="mg_bu_sel")
            row = bdf.iloc[display.index(choice)]
            buid = int(row["business_unit_id"])

            colA, colB, colC = st.columns(3)
            with colA:
                u_cnt = _refcount("SELECT COUNT(*) FROM users WHERE business_unit_id=:id", {"id": buid})
                bl_cnt = _refcount("SELECT COUNT(*) FROM business_lines WHERE business_unit_id=:id", {"id": buid})
                st.caption(f"Refs → Users: **{u_cnt}** · Business Lines: **{bl_cnt}**")

            with colB:
                new_active = not bool(row["is_active"])
                label = "Deactivate" if bool(row["is_active"]) else "Activate"
                if st.button(label, key="mg_bu_toggle"):
                    exec_sql("UPDATE business_units SET is_active=:b WHERE business_unit_id=:id", {"b": new_active, "id": buid})
                    st.success("Updated ✅")

            with colC:
                edit_box = st.popover("Edit") if hasattr(st, "popover") else st.expander("Edit", expanded=False)
                with edit_box:
                    with st.form("mg_bu_edit"):
                        nm = st.text_input("Business Unit Name *", value=row["name"] or "")
                        save = st.form_submit_button("Save changes")
                    if save:
                        nm_clean = nm.strip()
                        if not nm_clean:
                            st.error("Name is required.")
                        else:
                            dup = query_df(
                                "SELECT 1 FROM business_units WHERE lower(name)=lower(:n) AND business_unit_id<>:id",
                                {"n": nm_clean, "id": buid},
                            )
                            if not dup.empty:
                                st.error("A business unit with that name already exists.")
                            else:
                                exec_sql("UPDATE business_units SET name=:n WHERE business_unit_id=:id", {"n": nm_clean, "id": buid})
                                st.success("Saved ✅")

            dz_keybase = "mg_bu_conf"
            conf_key = f"{dz_keybase}_{st.session_state['danger_nonce']}"
            danger = st.popover("Danger Zone") if hasattr(st, "popover") else st.expander("Danger Zone", expanded=False)
            with danger:
                st.write("Delete permanently (only if not referenced by users/lines).")
                confirm = st.checkbox("I understand this cannot be undone.", key=conf_key)
                if st.button("Delete Business Unit", type="primary", disabled=not confirm, key="mg_bu_del"):
                    st.session_state["danger_nonce"] += 1
                    if u_cnt > 0 or bl_cnt > 0:
                        st.session_state["flash_admin"] = ("error", "Cannot delete: it is referenced by users and/or business lines. Deactivate instead.")
                        st.rerun()
                    try:
                        exec_sql("DELETE FROM business_units WHERE business_unit_id=:id", {"id": buid})
                        st.session_state["flash_admin"] = ("success", "Business Unit deleted ✅")
                    except Exception as e:
                        st.session_state["flash_admin"] = ("error", f"Delete failed: {e}")
                    st.rerun()

    # ---- Business Lines (Manage)
    with tabs[3]:
        bll = query_df("""
            SELECT bl.business_line_id, bl.name, bl.supplier, bl.category, bl.product_group, bl.is_active,
                   bl.business_unit_id, bu.name AS business_unit
            FROM business_lines bl
            JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
            ORDER BY bu.name, bl.name
        """)
        if bll.empty:
            st.info("No business lines yet.")
        else:
            def _fmt_bl(r):
                return " - ".join([p for p in [str(r.business_unit), str(r.name), str(r.category or ""), str(r.product_group or "")] if p and p != "None"])
            display = [f"{_fmt_bl(r)}  ({'active' if r.is_active else 'inactive'})" for r in bll.itertuples(index=False)]
            choice = st.selectbox("Select business line", display, index=0, key="mg_bl_sel")
            row = bll.iloc[display.index(choice)]
            blid = int(row["business_line_id"])

            colA, colB, colC = st.columns(3)
            with colA:
                i_cnt = _refcount("SELECT COUNT(*) FROM items WHERE business_line_id=:id", {"id": blid})
                v_cnt = _refcount("SELECT COUNT(*) FROM visits WHERE business_line_id=:id", {"id": blid})
                st.caption(f"Refs → Items: **{i_cnt}** · Visits: **{v_cnt}**")

            with colB:
                new_active = not bool(row["is_active"])
                label = "Deactivate" if bool(row["is_active"]) else "Activate"
                if st.button(label, key="mg_bl_toggle"):
                    exec_sql("UPDATE business_lines SET is_active=:b WHERE business_line_id=:id", {"b": new_active, "id": blid})
                    st.success("Updated ✅")

            with colC:
                bu_df = query_df("SELECT business_unit_id, name FROM business_units WHERE is_active IS TRUE ORDER BY name")
                bu_labels = bu_df["name"].tolist()
                bu_idx = 0
                if pd.notna(row["business_unit_id"]):
                    for i, r in enumerate(bu_df.itertuples(index=False)):
                        if int(r.business_unit_id) == int(row["business_unit_id"]):
                            bu_idx = i
                            break

                edit_box = st.popover("Edit") if hasattr(st, "popover") else st.expander("Edit", expanded=False)
                with edit_box:
                    with st.form("mg_bl_edit"):
                        bu_label = st.selectbox("Business Unit *", bu_labels, index=bu_idx if bu_labels else 0)
                        nm = st.text_input("Business Line Name *", value=row["name"] or "")
                        supplier = st.text_input("Supplier", value=row["supplier"] or "")
                        category = st.text_input("Category *", value=row["category"] or "")
                        prod_group = st.text_input("Product Group", value=row["product_group"] or "")
                        save = st.form_submit_button("Save changes")
                    if save:
                        nm_clean = nm.strip()
                        cat_clean = category.strip()
                        if not nm_clean or not cat_clean:
                            st.error("Business Line Name and Category are required.")
                        else:
                            new_bu_id = int(bu_df.loc[bu_df["name"] == bu_label, "business_unit_id"].iloc[0]) if not bu_df.empty else None
                            dup = query_df(
                                """
                                SELECT 1 FROM business_lines
                                WHERE business_unit_id=:bid AND lower(name)=lower(:nm) AND business_line_id<>:id
                                """,
                                {"bid": new_bu_id, "nm": nm_clean, "id": blid},
                            )
                            if not dup.empty:
                                st.error("A business line with that name already exists in the selected Business Unit.")
                            else:
                                exec_sql(
                                    """
                                    UPDATE business_lines
                                    SET business_unit_id=:bid, name=:name, supplier=:supplier, category=:cat, product_group=:pg
                                    WHERE business_line_id=:id
                                    """,
                                    {
                                        "bid": new_bu_id, "name": nm_clean, "supplier": (supplier.strip() or None),
                                        "cat": cat_clean, "pg": (prod_group.strip() or None), "id": blid
                                    },
                                )
                                st.success("Saved ✅")

            dz_keybase = "mg_bl_conf"
            conf_key = f"{dz_keybase}_{st.session_state['danger_nonce']}"
            danger = st.popover("Danger Zone") if hasattr(st, "popover") else st.expander("Danger Zone", expanded=False)
            with danger:
                st.write("Delete permanently (only if not referenced by items/visits).")
                confirm = st.checkbox("I understand this cannot be undone.", key=conf_key)
                if st.button("Delete Business Line", type="primary", disabled=not confirm, key="mg_bl_del"):
                    st.session_state["danger_nonce"] += 1
                    if i_cnt > 0 or v_cnt > 0:
                        st.session_state["flash_admin"] = ("error", "Cannot delete: referenced by items and/or visits. Deactivate instead.")
                        st.rerun()
                    try:
                        exec_sql("DELETE FROM business_lines WHERE business_line_id=:id", {"id": blid})
                        st.session_state["flash_admin"] = ("success", "Business Line deleted ✅")
                    except Exception as e:
                        st.session_state["flash_admin"] = ("error", f"Delete failed: {e}")
                    st.rerun()

    # ---- Items (Manage)
    with tabs[4]:
        idf = query_df("""
            SELECT i.product_id,
                   i.article_number,
                   i.description,
                   i.is_active,
                   bl.business_line_id,
                   bl.name AS business_line,
                   bu.name AS business_unit
            FROM items i
            JOIN business_lines bl   ON bl.business_line_id = i.business_line_id
            JOIN business_units bu   ON bu.business_unit_id = bl.business_unit_id
            ORDER BY COALESCE(i.article_number, i.product_id)
        """)
        if idf.empty:
            st.info("No items yet.")
        else:
            def _fmt_item(r):
                art = (str(r.article_number).strip() if pd.notna(r.article_number) and str(r.article_number).strip() else "")
                bl = (str(r.business_line).strip() if pd.notna(r.business_line) and str(r.business_line).strip() else "")
                bu = (str(r.business_unit).strip() if pd.notna(r.business_unit) and str(r.business_unit).strip() else "")
                desc = (str(r.description).strip() if pd.notna(r.description) and str(r.description).strip() else "")
                return " - ".join([p for p in [art, bu, bl, desc] if p]) or str(r.product_id)

            display = [f"{_fmt_item(r)}  ({'active' if r.is_active else 'inactive'})" for r in idf.itertuples(index=False)]
            choice = st.selectbox("Select item", display, index=0, key="mg_item_sel")
            row = idf.iloc[display.index(choice)]
            pid = str(row["product_id"])

            colA, colB, colC = st.columns(3)
            with colA:
                v_cnt = _refcount("SELECT COUNT(*) FROM visits WHERE product_id=:pid", {"pid": pid})
                st.caption(f"Refs → Visits: **{v_cnt}**")

            with colB:
                new_active = not bool(row["is_active"])
                label = "Deactivate" if bool(row["is_active"]) else "Activate"
                if st.button(label, key="mg_item_toggle"):
                    exec_sql("UPDATE items SET is_active=:b WHERE product_id=:pid", {"b": new_active, "pid": pid})
                    st.success("Updated ✅")

            with colC:
                edit_box = st.popover("Edit") if hasattr(st, "popover") else st.expander("Edit", expanded=False)
                with edit_box:
                    bu_df = query_df("SELECT business_unit_id, name FROM business_units WHERE is_active IS TRUE ORDER BY name")
                    bu_labels = bu_df["name"].tolist()
                    bu_idx = 0
                    for i, r in enumerate(bu_df.itertuples(index=False)):
                        if str(r.name) == str(row["business_unit"]):
                            bu_idx = i
                            break

                    with st.form("mg_item_edit"):
                        bu_label = st.selectbox("Business Unit *", bu_labels, index=bu_idx if bu_labels else 0)
                        sel_bu_id = int(bu_df.loc[bu_df["name"] == bu_label, "business_unit_id"].iloc[0]) if not bu_df.empty else None
                        bl_df = query_df(
                            "SELECT business_line_id, name FROM business_lines WHERE is_active IS TRUE AND business_unit_id=:bid ORDER BY name",
                            {"bid": sel_bu_id}
                        ) if sel_bu_id else pd.DataFrame()
                        bl_labels = bl_df["name"].tolist() if not bl_df.empty else []
                        bl_idx = 0
                        for i, r in enumerate(bl_df.itertuples(index=False)):
                            if int(r.business_line_id) == int(row["business_line_id"]):
                                bl_idx = i
                                break

                        art = st.text_input("Article Number (unique)", value=row["article_number"] or "")
                        desc = st.text_input("Description", value=row["description"] or "")
                        bl_label = st.selectbox("Business Line *", bl_labels, index=bl_idx if bl_labels else 0)
                        save = st.form_submit_button("Save changes")

                    if save:
                        new_bl_id = int(bl_df.loc[bl_df["name"] == bl_label, "business_line_id"].iloc[0]) if bl_labels else None

                        if not new_bl_id:
                            st.error("Business Line is required.")
                        else:
                            if art.strip():
                                dup = query_df("SELECT 1 FROM items WHERE lower(article_number)=lower(:a) AND product_id<>:pid",
                                               {"a": art.strip(), "pid": pid})
                                if not dup.empty:
                                    st.error("Article Number already exists.")
                                else:
                                    exec_sql(
                                        """
                                        UPDATE items
                                        SET article_number=:a, description=:d, business_line_id=:bl
                                        WHERE product_id=:pid
                                        """,
                                        {"a": art.strip(), "d": (desc.strip() or None), "bl": new_bl_id, "pid": pid},
                                    )
                                    st.success("Saved ✅")
                            else:
                                exec_sql(
                                    """
                                    UPDATE items
                                    SET article_number=NULL, description=:d, business_line_id=:bl
                                    WHERE product_id=:pid
                                    """,
                                    {"d": (desc.strip() or None), "bl": new_bl_id, "pid": pid},
                                )
                                st.success("Saved ✅")

            dz_keybase = "mg_item_conf"
            conf_key = f"{dz_keybase}_{st.session_state['danger_nonce']}"
            danger = st.popover("Danger Zone") if hasattr(st, "popover") else st.expander("Danger Zone", expanded=False)
            with danger:
                st.write("Delete permanently (only if not referenced).")
                confirm = st.checkbox("I understand this cannot be undone.", key=conf_key)
                if st.button("Delete Item", type="primary", disabled=not confirm, key="mg_item_del"):
                    st.session_state["danger_nonce"] += 1
                    if v_cnt > 0:
                        st.session_state["flash_admin"] = ("error", "Cannot delete: it is referenced by visits. Deactivate instead.")
                        st.rerun()
                    try:
                        exec_sql("DELETE FROM items WHERE product_id=:pid", {"pid": pid})
                        st.session_state["flash_admin"] = ("success", "Item deleted ✅")
                    except Exception as e:
                        st.session_state["flash_admin"] = ("error", f"Delete failed: {e}")
                    st.rerun()

    # ---- Objectives (Manage)
    with tabs[5]:
        odf = query_df("SELECT objective_id, name, COALESCE(is_active, TRUE) AS is_active FROM objectives ORDER BY name")
        if odf.empty:
            st.info("No objectives yet.")
        else:
            display = [f"{r.name}  ({'active' if bool(r.is_active) else 'inactive'})" for r in odf.itertuples(index=False)]
            choice = st.selectbox("Select objective", display, index=0, key="mg_obj_sel")
            row = odf.iloc[display.index(choice)]
            oid = int(row["objective_id"])
            active_now = bool(row["is_active"])

            colA, colB, colC = st.columns([1,1,2])
            with colA:
                v_cnt = _refcount("SELECT COUNT(*) FROM visits WHERE objective_id=:id", {"id": oid})
                st.caption(f"Refs → Visits: **{v_cnt}**")

            with colB:
                new_active = not active_now
                label = "Deactivate" if active_now else "Activate"
                if st.button(label, key=f"mg_obj_toggle_{oid}"):
                    try:
                        exec_sql("UPDATE objectives SET is_active=:b WHERE objective_id=:id", {"b": new_active, "id": oid})
                        st.success("Updated ✅")
                    except Exception as e:
                        st.error("Could not update objective status.")
                        st.caption(str(e))

            with colC:
                edit_box = st.popover("Edit") if hasattr(st, "popover") else st.expander("Edit", expanded=False)
                with edit_box:
                    with st.form("mg_obj_edit"):
                        nm = st.text_input("Objective name *", value=row["name"] or "")
                        save = st.form_submit_button("Save changes")
                    if save:
                        nm_clean = nm.strip()
                        if not nm_clean:
                            st.error("Name is required.")
                        else:
                            dup = query_df("SELECT 1 FROM objectives WHERE lower(name)=lower(:n) AND objective_id<>:id",
                                           {"n": nm_clean, "id": oid})
                            if not dup.empty:
                                st.error("Objective already exists.")
                            else:
                                exec_sql("UPDATE objectives SET name=:n WHERE objective_id=:id", {"n": nm_clean, "id": oid})
                                st.success("Saved ✅")

            dz_keybase = "mg_obj_conf"
            conf_key = f"{dz_keybase}_{st.session_state['danger_nonce']}"
            danger = st.popover("Danger Zone") if hasattr(st, "popover") else st.expander("Danger Zone", expanded=False)
            with danger:
                st.write("Delete permanently (only if not referenced).")
                confirm = st.checkbox("I understand this cannot be undone.", key=conf_key)
                if st.button("Delete Objective", type="primary", disabled=not confirm, key="mg_obj_del"):
                    st.session_state["danger_nonce"] += 1
                    if v_cnt > 0:
                        st.session_state["flash_admin"] = ("error", "Cannot delete: it is referenced by visits.")
                        st.rerun()
                    try:
                        exec_sql("DELETE FROM objectives WHERE objective_id=:id", {"id": oid})
                        st.session_state["flash_admin"] = ("success", "Objective deleted ✅")
                    except Exception as e:
                        st.session_state["flash_admin"] = ("error", f"Delete failed: {e}")
                    st.rerun()

# =============================
# Page — Admin: Data Browser
# =============================
from sqlalchemy import text  # local import for clarity with query_df/exec_sql

def page_admin_data():
    st.title("📊 Admin — Data Browser")
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
        df = query_df("SELECT * FROM target_audiences ORDER BY audience_id DESC")
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

    # ---------- Full database backup options ----------
    st.divider()
    col_sql, col_zip = st.columns(2)

    # Helper to get DATABASE_URL (Render provides it as DATABASE_URL or POSTGRES_URL / POSTGRES_CONNECTION_STRING)
    def _db_url():
        for k in ("DATABASE_URL", "POSTGRES_URL", "POSTGRES_CONNECTION_STRING"):
            v = os.environ.get(k)
            if v:
                return v
        return None

    with col_sql:
        if st.button("Download full DB (.sql via pg_dump)", key="export_pg_dump"):
            try:
                db_url = _db_url()
                if not db_url:
                    raise RuntimeError("DATABASE_URL is not set in environment.")

                # Try to run pg_dump. We ask for plain SQL, no owner/privs so it imports cleanly.
                # NOTE: If your password is in the URL, pg_dump will use it directly.
                # If your URL has 'postgres://', pg_dump accepts it, but some builds prefer 'postgresql://'.
                cmd = ["pg_dump", "--no-owner", "--no-privileges", "--format=plain", db_url]

                # Capture to memory (could be large; fine for moderate DB sizes)
                import subprocess
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

                if proc.returncode != 0 or not proc.stdout:
                    err = proc.stderr.decode("utf-8", errors="ignore")
                    raise RuntimeError(f"pg_dump failed.\n{err.strip() or 'No error text.'}")

                ts = datetime.now().strftime("%Y-%m-%d_%H%M")
                st.success("Full SQL dump ready.")
                st.download_button(
                    label="Download database.sql",
                    data=proc.stdout,  # bytes
                    file_name=f"database_backup_{ts}.sql",
                    mime="application/sql",
                    key="dl_pg_dump_sql",
                )
            except FileNotFoundError:
                st.error("`pg_dump` is not available in this environment.")
                st.info("Use the **portable backup** option on the right, or add `pg_dump` to your image.")
            except Exception as e:
                st.error("Full SQL dump failed ❌")
                st.caption(str(e))

    with col_zip:
        if st.button("Download portable backup (schema+CSVs .zip)", key="export_portable_zip"):
            try:
                ts = datetime.now().strftime("%Y-%m-%d_%H%M")

                # 1) Prepare all dataframes (reuse same queries you already use for CSV export)
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

                # 2) Build the ZIP: schema.sql + CSVs + README
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                    # Include your schema SQL for easy rebuild
                    try:
                        # SCHEMA_SQL should be imported/defined in your app (the one you shared)
                        from app_v11 import SCHEMA_SQL  # adjust import if needed
                        schema_sql = SCHEMA_SQL.strip().encode("utf-8")
                    except Exception:
                        # Fallback stub if not importable (still useful together with CSVs)
                        schema_sql = b"-- schema.sql not auto-included; add your schema here.\n"

                    zf.writestr("schema.sql", schema_sql)

                    # Add CSVs
                    for name, df in tables.items():
                        csv_bytes = df.to_csv(index=False, na_rep="").encode("utf-8-sig")
                        zf.writestr(f"data/{name}_{ts}.csv", csv_bytes)

                    # Add README with restore instructions
                    readme = f"""# Portable Backup

    This archive contains:
    - `schema.sql` — your Postgres schema (run once to create empty tables).
    - `data/*.csv` — table data exports.

    ## Quick restore (psql):

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

    > Tip: If you have foreign key errors, follow dependency order (units → lines → items → customers → target_audiences → visits → home_visits → shelf_*).
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
# Helpers (place near your imports)
# =============================


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
            role = st.selectbox("Role", ["rep", "admin"], index=0)
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
                new_role = st.selectbox("Role", ["rep", "admin"], index=(0 if row["role"] == "rep" else 1))
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
# Footer
# =============================
def show_footer():
    st.markdown("""
    <hr style="margin-top:2rem;margin-bottom:1rem;opacity:0.25;">
    <div style="text-align:center; color:#6c757d;">
        <img src="https://www.physioassist.com/wp-content/uploads/2023/06/Almadar-Logo-01.png"
             style="height:45px;opacity:.85;margin-bottom:6px;"><br>
        <span style="font-size:0.9rem;">
            © 2025 <strong>Al Madar Medical Co.</strong><br>
            Core System © <strong>Muaz Sulaiman</strong><br>
            <span style="font-size:0.8rem;">Version 11 • All rights reserved.</span>
        </span>
    </div>
    """, unsafe_allow_html=True)
    
# =============================
# MAIN
# =============================
if not st.session_state.user:
    login_block()
else:
    logout_button()
    page = sidebar_nav()
    if page == "Submit Visit":
        page_submit_visit()
    elif page == "My Submissions":
        page_my_submissions()
    elif page == "User Settings":
        page_user_settings()
    elif page == "Admin: Import Lookups":
        page_admin_import()
    elif page == "Admin: Data Browser":
        page_admin_data()
    elif page == "Admin: Users":
        page_admin_users()

    show_footer()