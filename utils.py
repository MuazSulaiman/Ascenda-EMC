# utils.py
import base64
import os
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd
import requests
import streamlit as st
from dateutil import tz

from config import TIMEZONE, PBI_PUSH_URL


def safe_str(v) -> str:
    """Coerce a DB/dataframe value to a stripped string, treating None and
    NaN (pandas' representation of SQL NULL, e.g. from itertuples/.iloc on a
    nullable column) as empty. Plain truthy/`is not None` checks misfire on
    NaN because NaN is truthy and is not None, so it slips through and gets
    stringified as the literal text "nan"."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def _get_secret(name: str, default: str = "") -> str:
    val = os.environ.get(name)
    if val:
        return val
    try:
        return st.secrets[name]
    except Exception:
        return default


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _client_ip() -> Optional[str]:
    return st.session_state.get("client_ip") if "client_ip" in st.session_state else None


def _local_now() -> datetime:
    return datetime.now(tz.gettz(TIMEZONE))


def _local_now_str() -> str:
    return _local_now().strftime("%Y-%m-%d %H:%M:%S")


def to_local(dt) -> Optional[datetime]:
    """Convert a stored UTC timestamp (aware, or naive-and-assumed-UTC) to
    the org's local timezone (config.TIMEZONE) for display — never use this
    for storage or comparison, only for what a user reads on screen. Accepts
    a datetime, a pandas Timestamp, or None/NaT; returns None for anything
    that isn't a real timestamp."""
    if dt is None or pd.isna(dt):
        return None
    if isinstance(dt, pd.Timestamp):
        dt = dt.to_pydatetime()
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz.gettz(TIMEZONE))


def to_local_str(dt, fmt: str = "%d %b %Y, %H:%M") -> str:
    """Format a stored UTC timestamp in the org's local timezone. Returns
    '—' for None/NaT, matching this codebase's existing display convention
    for missing values."""
    local_dt = to_local(dt)
    return local_dt.strftime(fmt) if local_dt is not None else "—"


def refresh_default_date(key: str) -> None:
    """Keep a `key=`-bound st.date_input's default pinned to "today" (the
    org's local date, not the server's), without ever clobbering a date the
    user deliberately picked. Call this once, right before instantiating
    the widget — then give the widget only `key=`, no `value=`.

    Needed because this app persists the Streamlit session id in
    localStorage (see ui.py), so the same browser tab/device can carry one
    session across a midnight rollover, or even across several days.
    st.date_input's `value=` argument only seeds st.session_state[key] on
    that key's very first render — after that, "today" freezes at whatever
    day the widget first appeared for that session and never updates
    again, which is what made a form default silently show yesterday's (or
    older) date. This only resets the stored value when it still equals
    the date we last auto-seeded it with; once a user changes it away from
    that, their choice is left alone."""
    today = _local_now().date()
    seeded_key = f"{key}__seeded_on"
    if key not in st.session_state:
        st.session_state[key] = today
        st.session_state[seeded_key] = today
    elif (
        st.session_state.get(seeded_key) != today
        and st.session_state.get(key) == st.session_state.get(seeded_key)
    ):
        st.session_state[key] = today
        st.session_state[seeded_key] = today


def push_visit_to_pbi(row: dict) -> Tuple[bool, Optional[str]]:
    """
    Push a single visit to your Power BI streaming/push dataset.
    Returns (ok, err_msg).
    """
    if not PBI_PUSH_URL:
        return False, "Missing PBI_PUSH_URL (env var or secrets)."
    try:
        r = requests.post(PBI_PUSH_URL, json={"rows": [row]}, timeout=8)
        if r.status_code in (200, 202):
            return True, None
        return False, f"{r.status_code} {r.text}"
    except Exception as e:
        return False, str(e)


def _gen_tmp_pw(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    core = "".join(secrets.choice(alphabet) for _ in range(length - 1))
    return core + secrets.choice("!@#$%^&*")


def _img_b64(p: Path) -> str:
    return base64.b64encode(p.read_bytes()).decode("utf-8")
