# widgets.py
import math
import time
import unicodedata
from typing import Optional, Tuple

import folium
import pandas as pd
import streamlit as st
from dateutil import tz
from sqlalchemy import text
from streamlit_autorefresh import st_autorefresh
from streamlit_folium import st_folium
from streamlit_geolocation import streamlit_geolocation

try:
    from streamlit_js_eval import get_geolocation as _get_geo_js
except Exception:
    _get_geo_js = None

from db_ops import query_df
from config import ACCURACY_METERS, TIMEZONE
from utils import _local_now


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
        # Optionally: reset *this* page's geo right when you enter it
        _reset_location_state_for_page(page_ns)
    st.session_state["__current_page"] = page_ns

# =============================
# Quick Find module
# =============================

def customer_quick_find_module(
    *,
    page_ns: str,
    query_df,
    customers_table: str = "customers",
    # --- fixed widget keys (pass your existing ones so it reuses the same state) ---
    KEY_ACCT: str,
    KEY_REGION: str,
    KEY_CITY: str,
    KEY_SECTOR: str,
    KEY_CUST: str,
    KEY_CUSTID: str,
    # --- state keys for locking + request flags ---
    cid_locked_key: str,
    req_clear_customer_key: str,
    req_clear_acct_key: str,
    req_set_acct_key: str,
    acct_set_value_key: str,
    qf_msg_key: str,
    qf_msg_type_key: str,
    # --- optional UI text ---
    title: str = "##### 🔎 Quick Find (Account ID)",
    acct_placeholder: str = "e.g., C100XXX / P000XXX",
    acct_help: str = "Search by the Account ID used by the ERP.",
):
    """
    Reusable Quick-Find module:
    - Renders Account ID input + Find/Clear buttons (inside a form)
    - Finds active customer by account_id (case/space-insensitive)
    - Fills region/city/sector/customer + customer_id, locks customer widgets
    - Uses request flags to safely clear/set widget values on the next run

    Returns dict:
      {
        "locked": bool,
        "customer_id": int|None,
        "account_id_norm": str,
        "did_find": bool,
        "did_clear": bool,
      }
    """

    # -------------------------
    # helpers (message + ordering)
    # -------------------------
    def _set_qf_msg(msg: str, msg_type: str = "error"):
        st.session_state[qf_msg_key] = msg
        st.session_state[qf_msg_type_key] = msg_type

    def _clear_qf_msg():
        st.session_state[qf_msg_key] = ""
        st.session_state[qf_msg_type_key] = ""

    def _order_with_other_last(values: list) -> list:
        normal_vals, other_vals = [], []
        for v in values:
            if isinstance(v, str) and v.strip().lower() == "other":
                other_vals.append(v)
            else:
                normal_vals.append(v)
        return normal_vals + other_vals

    # -------------------------
    # apply requests BEFORE widgets
    # -------------------------
    def _apply_requests_before_widgets():
        # clear customer fields (never touch location)
        if st.session_state.get(req_clear_customer_key, False):
            st.session_state[req_clear_customer_key] = False

            st.session_state[cid_locked_key] = False
            st.session_state[KEY_REGION] = ""
            st.session_state[KEY_CITY]   = ""
            st.session_state[KEY_SECTOR] = ""
            st.session_state[KEY_CUST]   = ""
            st.session_state.pop(KEY_CUSTID, None)

        # clear account input
        if st.session_state.get(req_clear_acct_key, False):
            st.session_state[req_clear_acct_key] = False
            st.session_state[KEY_ACCT] = ""

        # set account input (e.g., uppercase) safely next run
        if st.session_state.get(req_set_acct_key, False):
            st.session_state[req_set_acct_key] = False
            st.session_state[KEY_ACCT] = (st.session_state.get(acct_set_value_key) or "")

    _apply_requests_before_widgets()

    # -------------------------
    # show persistent message
    # -------------------------
    if st.session_state.get(qf_msg_key):
        if st.session_state.get(qf_msg_type_key) == "success":
            st.success(st.session_state[qf_msg_key])
        else:
            st.error(st.session_state[qf_msg_key])

    st.markdown(title)

    # -------------------------
    # form UI
    # -------------------------
    did_find = False
    did_clear = False

    with st.form(key=f"{page_ns}/quick_find_form", clear_on_submit=False):
        q1, q2, q3 = st.columns([3, 1, 1])
        with q1:
            st.text_input(
                "Account ID",
                key=KEY_ACCT,
                placeholder=acct_placeholder,
                help=acct_help,
            )
        with q2:
            find_click = st.form_submit_button("Find", width="stretch")
        with q3:
            clear_click = st.form_submit_button("Clear", width="stretch")

    # -------------------------
    # Clear
    # -------------------------
    if clear_click:
        did_clear = True
        _clear_qf_msg()
        st.session_state[req_clear_customer_key] = True
        st.session_state[req_clear_acct_key] = True
        st.rerun()

    # -------------------------
    # Find
    # -------------------------
    account_id_norm = ((st.session_state.get(KEY_ACCT) or "").strip().upper())

    if find_click:
        # Empty
        if not account_id_norm:
            _set_qf_msg("Please enter an Account ID. Fields cleared.", "error")
            st.session_state[req_clear_customer_key] = True
            st.session_state[req_clear_acct_key] = True
            st.rerun()

        found = query_df(
            f"""
            SELECT customer_id, account_id, account_name, region, city, sector
            FROM {customers_table}
            WHERE is_active IS TRUE
              AND UPPER(TRIM(account_id)) = :aid
            LIMIT 1
            """,
            {"aid": account_id_norm},
        )

        if found.empty:
            _set_qf_msg("Account ID not found (or inactive). Fields cleared.", "error")
            st.session_state[req_clear_customer_key] = True
            st.session_state[req_clear_acct_key] = False  # keep what user typed
            st.rerun()
        else:
            did_find = True
            r = found.iloc[0]

            # Fill fields + lock
            st.session_state[KEY_REGION] = (str(r["region"]) if r["region"] is not None else "").strip()
            st.session_state[KEY_CITY]   = (str(r["city"])   if r["city"]   is not None else "").strip()
            st.session_state[KEY_SECTOR] = (str(r["sector"]) if r["sector"] is not None else "").strip()
            st.session_state[KEY_CUST]   = (str(r["account_name"]) if r["account_name"] is not None else "").strip()
            st.session_state[KEY_CUSTID] = int(r["customer_id"])
            st.session_state[cid_locked_key] = True

            # make textbox itself uppercase next run (safe)
            st.session_state[acct_set_value_key] = account_id_norm
            st.session_state[req_set_acct_key] = True

            _set_qf_msg("Customer filled successfully.", "success")
            st.rerun()

    # -------------------------
    # compute resolved outputs
    # -------------------------
    locked = bool(st.session_state.get(cid_locked_key, False))
    customer_id = int(st.session_state.get(KEY_CUSTID)) if locked and st.session_state.get(KEY_CUSTID) else None

    if locked and customer_id:
        st.caption(f"🔒 Filled by Account ID · Internal customer_id: **{customer_id}**")

    return {
        "locked": locked,
        "customer_id": customer_id,
        "account_id_norm": account_id_norm,
        "did_find": did_find,
        "did_clear": did_clear,
    }

def customer_cascading_selectors(
    *,
    query_df,
    # fixed keys
    KEY_REGION: str,
    KEY_CITY: str,
    KEY_SECTOR: str,
    KEY_CUST: str,
    KEY_CUSTID: str,
    # state keys
    cid_locked_key: str,
    qf_msg_key: str,
    qf_msg_type_key: str,
    # db table
    customers_table: str = "customers",
):
    """
    Reusable Region/City/Sector/Customer cascading selectors.
    Respects the lock state set by Quick-Find.
    Returns resolved customer_id (locked uses KEY_CUSTID, otherwise resolves from selected customer name).
    """

    def _clear_qf_msg():
        st.session_state[qf_msg_key] = ""
        st.session_state[qf_msg_type_key] = ""

    def _order_with_other_last(values: list) -> list:
        normal_vals, other_vals = [], []
        for v in values:
            if isinstance(v, str) and v.strip().lower() == "other":
                other_vals.append(v)
            else:
                normal_vals.append(v)
        return normal_vals + other_vals

    locked = bool(st.session_state.get(cid_locked_key, False))

    def _on_region_change():
        _clear_qf_msg()
        st.session_state[cid_locked_key] = False
        st.session_state[KEY_CITY] = ""
        st.session_state[KEY_SECTOR] = ""
        st.session_state[KEY_CUST] = ""
        st.session_state.pop(KEY_CUSTID, None)

    def _on_city_change():
        _clear_qf_msg()
        st.session_state[cid_locked_key] = False
        st.session_state[KEY_SECTOR] = ""
        st.session_state[KEY_CUST] = ""
        st.session_state.pop(KEY_CUSTID, None)

    def _on_sector_change():
        _clear_qf_msg()
        st.session_state[cid_locked_key] = False
        st.session_state[KEY_CUST] = ""
        st.session_state.pop(KEY_CUSTID, None)

    # Region options
    reg_df = query_df(
        f"""
        SELECT DISTINCT region
        FROM {customers_table}
        WHERE is_active IS TRUE
          AND region IS NOT NULL AND trim(region) <> ''
        ORDER BY region
        """
    )
    region_opts = [""] + _order_with_other_last(reg_df["region"].tolist())

    region_choice = st.selectbox(
        "Region *",
        region_opts,
        index=0,
        key=KEY_REGION,
        disabled=locked,
        on_change=_on_region_change if not locked else None,
        help=("Filled from Account ID. Click Clear to change." if locked else None),
    )

    # City options
    city_opts = [""]
    if region_choice:
        city_df = query_df(
            f"""
            SELECT DISTINCT city
            FROM {customers_table}
            WHERE is_active IS TRUE
              AND region = :r
              AND city IS NOT NULL AND trim(city) <> ''
            ORDER BY city
            """,
            {"r": region_choice},
        )
        city_opts = [""] + _order_with_other_last(city_df["city"].tolist())

    city_choice = st.selectbox(
        "City *",
        city_opts,
        index=0,
        key=KEY_CITY,
        disabled=locked or (not bool(region_choice)),
        on_change=_on_city_change if (not locked and region_choice) else None,
        help=("Filled from Account ID. Click Clear to change." if locked else ("Select a Region first" if not region_choice else None)),
    )

    # Sector options
    sector_opts = [""]
    if region_choice and city_choice:
        sec_df = query_df(
            f"""
            SELECT DISTINCT sector
            FROM {customers_table}
            WHERE is_active IS TRUE
              AND region = :r
              AND city   = :c
              AND sector IS NOT NULL AND trim(sector) <> ''
            ORDER BY sector
            """,
            {"r": region_choice, "c": city_choice},
        )
        sector_opts = [""] + _order_with_other_last(sec_df["sector"].tolist())

    sector_choice = st.selectbox(
        "Sector *",
        sector_opts,
        index=0,
        key=KEY_SECTOR,
        disabled=locked or (not bool(region_choice and city_choice)),
        on_change=_on_sector_change if (not locked and region_choice and city_choice) else None,
        help=("Filled from Account ID. Click Clear to change." if locked else ("Select a City first" if not (region_choice and city_choice) else None)),
    )

    # Customer options
    cust_df = pd.DataFrame(columns=["customer_id", "account_name"])
    cust_names = [""]

    if region_choice and city_choice and sector_choice:
        cust_df = query_df(
            f"""
            SELECT customer_id, account_name
            FROM {customers_table}
            WHERE is_active IS TRUE
              AND region = :r
              AND city   = :c
              AND sector = :s
            ORDER BY account_name
            """,
            {"r": region_choice, "c": city_choice, "s": sector_choice},
        )
        cust_names = [""] + _order_with_other_last(cust_df["account_name"].tolist())

    cust_choice = st.selectbox(
        "Customer *",
        cust_names,
        index=0,
        key=KEY_CUST,
        disabled=locked or (not bool(region_choice and city_choice and sector_choice)),
        help=("Filled from Account ID. Click Clear to change." if locked else ("Select Sector first" if not (region_choice and city_choice and sector_choice) else None)),
    )

    # Resolve customer_id
    if locked and st.session_state.get(KEY_CUSTID):
        return int(st.session_state.get(KEY_CUSTID))

    if cust_choice and not cust_df.empty:
        match = cust_df.loc[cust_df["account_name"] == cust_choice, "customer_id"]
        return int(match.iloc[0]) if not match.empty else None

    return None


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
    MAX_ACC_M = ACCURACY_METERS  # max allowed accuracy in meters

    with st.expander("📍 Location (auto) — required for check-in", expanded=True):
        tried_key   = k("geo_try")
        start_key   = k("geo_start_ts")
        refresh_key = k("geo_autorefresh")

        st.session_state.setdefault(tried_key, False)

        # Screen 1 — just the "Get location" button
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
                return (None, None, None)

            # Timeout reached → show the warning (only now)
            st.warning(
                "We couldn't read your location.\n\n"
                "• Allow **Location** (and **Precise location** on iOS) in browser permissions.\n"
                "• Make sure you're on **HTTPS** and device location is **ON**.\n"
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

        # ✅ Accuracy gate (BLOCK if > 300m)
        if facc is not None and facc > MAX_ACC_M:
            shown_acc = f"{facc:.0f}m" if facc is not None else "unknown"
            st.error(
                f"Location accuracy is **{shown_acc}**, which is above the allowed limit (**≤ {MAX_ACC_M:.0f}m**).\n\n"
                "Please enable **Precise location**, move outdoors, or wait a few seconds then try again."
                )
            if st.button("🔁 Capture again", key=k("btn_retry_low_accuracy")):
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
