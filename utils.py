# utils.py
import base64
import os
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

import requests
import streamlit as st
from dateutil import tz

from config import TIMEZONE, PBI_PUSH_URL


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
