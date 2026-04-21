# pages/change_request.py
import json
import re
from datetime import datetime, timezone
from typing import Optional

import folium
import pandas as pd
import streamlit as st
from sqlalchemy import text
from streamlit_folium import st_folium

from auth import resolve_session_user
from config import TIMEZONE
from db_ops import query_df, exec_sql
from db import engine
from utils import _utcnow_iso, _local_now_str, _utcnow
from widgets import get_location_block, _reset_geo_on_user_or_page_change, set_current_page
from ui import section_header, status_badge, compare_row

def page_change_request():
    import re
    import json
    import pandas as pd
    import folium
    from streamlit_folium import st_folium
    from sqlalchemy import text
    import streamlit as st

    section_header("Visit Change Requests", "Submit and review field visit correction requests")

    # ------------------------------------------------------------
    # Resolve logged-in user safely
    # ------------------------------------------------------------
    u = st.session_state.get("user") or resolve_session_user()
    if not u:
        st.warning("Please sign in to continue.")
        return

    uid = int(u.get("user_id") or u.get("id"))
    role = (u.get("role") or "").lower().strip()

    display_name = u.get("name") or u.get("email") or f"User #{uid}"
    display_region = u.get("region") or "—"
    display_role = u.get("role") or "—"
    PAGE_NS = "change_request"
    TITLE_OPTIONS = ["", "Dr.", "Mr.", "Ms.", "Mrs.", "Prof.", "Eng.", "Other"]

    # ============================================================
    # Helpers
    # ============================================================
    def _norm(x):
        if x is None:
            return ""
        try:
            if pd.isna(x):
                return ""
        except Exception:
            pass
        return str(x).strip()

    def _safe_int(x):
        try:
            s = str(x).strip()
            return int(s) if s else None
        except Exception:
            return None

    def _json_dumps(obj) -> str:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)

    def _is_home_visit_label(aud_label):
        return bool(aud_label and aud_label.strip().lower().startswith("home visit"))

    def _is_shelf_objective(obj_name):
        return bool(obj_name and ("shelf movement" in obj_name.strip().lower()))

    def _add_detail(details: list[dict], field: str, old, new):
        if old is None and new is None:
            return
        if str(old) == str(new):
            return
        details.append(
            {
                "field": field,
                "old_value": (None if old is None else str(old)),
                "new_value": (None if new is None else str(new)),
            }
        )

    def _prefill_selectbox(label: str, options: list[str], key: str, prefill_value: str, **kwargs):
        opts = [_norm(o) for o in (options or [""])]
        pv = _norm(prefill_value)

        if not opts:
            opts = [""]

        if opts[0] != "":
            opts = [""] + [o for o in opts if o != ""]

        if pv and pv not in opts:
            opts = [""] + [pv] + [o for o in opts[1:] if o != pv]

        idx = opts.index(pv) if pv and pv in opts else 0
        return st.selectbox(label, opts, index=idx, key=key, **kwargs)

    # ============================================================
    # DB helpers
    # ============================================================
    def _has_open_request_for_visit(visit_id: int) -> bool:
        df = query_df(
            """
            SELECT request_id
            FROM request_changes
            WHERE visit_id = :vid AND status = 'IN_REVIEW'
            LIMIT 1
            """,
            {"vid": int(visit_id)},
        )
        return not df.empty

    def _load_visit_snapshot(visit_id: int) -> dict | None:
        vdf = query_df(
            """
            SELECT
              visit_id, user_id, submitted_at_utc, submitted_at_local,
              latitude, longitude, accuracy_m,
              customer_id, audience_id,
              business_line_id, product_id, objective_id,
              notes, evaluation, project_id,
              other_audience_name, other_audience_department, other_audience_position,
              other_audience_title, other_audience_phone, other_audience_email,
              other_customer_name
            FROM visits
            WHERE visit_id = :vid
            """,
            {"vid": int(visit_id)},
        )
        if vdf.empty:
            return None

        visit = vdf.iloc[0].to_dict()

        hvdf = query_df(
            """
            SELECT home_visit_id, patient_name, patient_phone, serial_no
            FROM home_visits
            WHERE visit_id = :vid
            """,
            {"vid": int(visit_id)},
        )
        home_visit = hvdf.iloc[0].to_dict() if not hvdf.empty else None

        mhdf = query_df(
            """
            SELECT movement_id
            FROM shelf_movement_headers
            WHERE visit_id = :vid
            """,
            {"vid": int(visit_id)},
        )
        movement_id = int(mhdf.iloc[0]["movement_id"]) if not mhdf.empty else None

        movement_lines = []
        if movement_id:
            mldf = query_df(
                """
                SELECT product_id, qty_checked
                FROM shelf_movement_lines
                WHERE movement_id = :mid
                """,
                {"mid": movement_id},
            )
            if not mldf.empty:
                movement_lines = mldf.to_dict(orient="records")

        return {
            "visit": visit,
            "home_visit": home_visit,
            "movement_id": movement_id,
            "movement_lines": movement_lines,
        }

    def _get_customer_locked_fields(customer_id: int) -> dict:
        df = query_df(
            """
            SELECT account_name, region, city, sector
            FROM customers
            WHERE customer_id = :cid
            """,
            {"cid": int(customer_id)},
        )
        if df.empty:
            return {"account_name": "", "region": "", "city": "", "sector": ""}
        return df.iloc[0].to_dict()

    def _audience_label_for_id(audience_id: int) -> str:
        df = query_df(
            """
            SELECT title, name, department, position
            FROM target_audiences
            WHERE audience_id = :aid
            """,
            {"aid": int(audience_id)},
        )
        if df.empty:
            return ""
        r = df.iloc[0]
        parts = []
        title = (str(r["title"]).strip() + " ") if pd.notna(r["title"]) and str(r["title"]).strip() else ""
        name = str(r["name"]).strip() if pd.notna(r["name"]) else ""
        parts.append((title + name).strip())
        if pd.notna(r["department"]) and str(r["department"]).strip():
            parts.append(str(r["department"]).strip())
        if pd.notna(r["position"]) and str(r["position"]).strip():
            parts.append(str(r["position"]).strip())
        return " || ".join([p for p in parts if p])

    def _load_audience_options(customer_id: int) -> list[str]:
        df = query_df(
            """
            SELECT audience_id, title, name, department, position
            FROM target_audiences
            WHERE is_active IS TRUE AND customer_id = :cid
            ORDER BY name
            """,
            {"cid": int(customer_id)},
        )

        def fmt(row) -> str:
            parts = []
            title = (str(row["title"]).strip() + " ") if pd.notna(row["title"]) and str(row["title"]).strip() else ""
            name = str(row["name"]).strip() if pd.notna(row["name"]) else ""
            parts.append((title + name).strip())
            if pd.notna(row["department"]) and str(row["department"]).strip():
                parts.append(str(row["department"]).strip())
            if pd.notna(row["position"]) and str(row["position"]).strip():
                parts.append(str(row["position"]).strip())
            return " || ".join([p for p in parts if p])

        labels = [""]
        if not df.empty:
            for _, r in df.iterrows():
                labels.append(fmt(r))
        labels.append("Other")
        return labels

    def _resolve_audience_id_from_label(customer_id: int, label: str) -> int | None:
        label = _norm(label)
        if not label or label == "Other":
            return None

        df = query_df(
            """
            SELECT audience_id, title, name, department, position
            FROM target_audiences
            WHERE is_active IS TRUE AND customer_id = :cid
            """,
            {"cid": int(customer_id)},
        )
        if df.empty:
            return None

        def fmt(row) -> str:
            parts = []
            title = (str(row["title"]).strip() + " ") if pd.notna(row["title"]) and str(row["title"]).strip() else ""
            name = str(row["name"]).strip() if pd.notna(row["name"]) else ""
            parts.append((title + name).strip())
            if pd.notna(row["department"]) and str(row["department"]).strip():
                parts.append(str(row["department"]).strip())
            if pd.notna(row["position"]) and str(row["position"]).strip():
                parts.append(str(row["position"]).strip())
            return " || ".join([p for p in parts if p])

        for _, r in df.iterrows():
            if _norm(fmt(r)) == label:
                return int(r["audience_id"])
        return None

    # ============================================================
    # Other Audience dropdowns (Department -> Position dependent)
    # ============================================================
    def _load_other_dept_options() -> list[str]:
        df = query_df(
            """
            SELECT DISTINCT department
            FROM target_audiences
            WHERE COALESCE(is_active, TRUE) IS TRUE
              AND department IS NOT NULL
              AND trim(department) <> ''
            ORDER BY department
            """
        )
        return [""] + (df["department"].astype(str).tolist() if not df.empty else [])

    def _load_other_position_options() -> list[str]:
        df = query_df(
            """
            SELECT DISTINCT position
            FROM target_audiences
            WHERE COALESCE(is_active, TRUE) IS TRUE
            AND position IS NOT NULL
            AND trim(position) <> ''
            ORDER BY position
            """
        )
        return [""] + (df["position"].astype(str).tolist() if not df.empty else [])

    # ============================================================
    # BU / Category / BL / Product
    # ============================================================
    def _load_bu_options() -> list[str]:
        df = query_df(
            """
            SELECT name
            FROM business_units
            WHERE is_active IS TRUE
            ORDER BY name
            """
        )
        return [""] + (df["name"].astype(str).tolist() if not df.empty else [])

    def _bu_id_from_name(bu_name: str) -> int | None:
        bu_name = _norm(bu_name)
        if not bu_name:
            return None
        df = query_df(
            """
            SELECT business_unit_id
            FROM business_units
            WHERE is_active IS TRUE AND trim(name) = :n
            LIMIT 1
            """,
            {"n": bu_name},
        )
        return int(df.iloc[0]["business_unit_id"]) if not df.empty else None

    def _load_category_options(bu_id: int | None) -> list[str]:
        if not bu_id:
            return [""]
        df = query_df(
            """
            SELECT DISTINCT category
            FROM business_lines
            WHERE is_active IS TRUE
              AND business_unit_id = :bid
              AND category IS NOT NULL
              AND trim(category) <> ''
            ORDER BY category
            """,
            {"bid": int(bu_id)},
        )
        return [""] + (df["category"].astype(str).tolist() if not df.empty else [])

    def _load_bl_options(bu_id: int | None, category: str | None) -> list[str]:
        category = _norm(category)
        if not bu_id or not category:
            return [""]
        df = query_df(
            """
            SELECT name
            FROM business_lines
            WHERE is_active IS TRUE
              AND business_unit_id = :bid
              AND category = :cat
            ORDER BY name
            """,
            {"bid": int(bu_id), "cat": category},
        )
        return [""] + (df["name"].astype(str).tolist() if not df.empty else [])

    def _bl_id_from_name(bu_id: int, category: str, bl_name: str) -> int | None:
        category = _norm(category)
        bl_name = _norm(bl_name)
        if not (bu_id and category and bl_name):
            return None
        df = query_df(
            """
            SELECT business_line_id
            FROM business_lines
            WHERE is_active IS TRUE
              AND business_unit_id = :bid
              AND category = :cat
              AND trim(name) = :nm
            LIMIT 1
            """,
            {"bid": int(bu_id), "cat": category, "nm": bl_name},
        )
        return int(df.iloc[0]["business_line_id"]) if not df.empty else None

    def _infer_bu_cat_bl_from_blid(business_line_id: int) -> dict:
        df = query_df(
            """
            SELECT
              bl.business_line_id,
              bl.name AS business_line_name,
              bl.category,
              bu.business_unit_id,
              bu.name AS business_unit_name
            FROM business_lines bl
            JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
            WHERE bl.business_line_id = :blid
            LIMIT 1
            """,
            {"blid": int(business_line_id)},
        )
        if df.empty:
            return {"bu_name": "", "bu_id": None, "category": "", "bl_name": ""}
        r = df.iloc[0].to_dict()
        return {
            "bu_name": _norm(r.get("business_unit_name")),
            "bu_id": int(r.get("business_unit_id")) if r.get("business_unit_id") is not None else None,
            "category": _norm(r.get("category")),
            "bl_name": _norm(r.get("business_line_name")),
        }

    def _load_product_options(business_line_id: int | None) -> list[str]:
        if not business_line_id:
            return [""]
        df = query_df(
            """
            SELECT product_id, article_number, description
            FROM items
            WHERE is_active IS TRUE
              AND business_line_id = :blid
            ORDER BY COALESCE(article_number, product_id)
            """,
            {"blid": int(business_line_id)},
        )
        labels = [""]
        if not df.empty:
            for _, r in df.iterrows():
                article = r["article_number"] if pd.notna(r["article_number"]) else r["product_id"]
                desc = str(r["description"]).strip() if pd.notna(r["description"]) else ""
                labels.append(f"{article} — {desc}" if desc else f"{article}")
        return labels

    def _product_id_from_label(business_line_id: int, label: str) -> str | None:
        label = _norm(label)
        if not label:
            return None
        df = query_df(
            """
            SELECT product_id, article_number, description
            FROM items
            WHERE is_active IS TRUE
              AND business_line_id = :blid
            """,
            {"blid": int(business_line_id)},
        )
        if df.empty:
            return None
        for _, r in df.iterrows():
            article = r["article_number"] if pd.notna(r["article_number"]) else r["product_id"]
            desc = str(r["description"]).strip() if pd.notna(r["description"]) else ""
            lbl = f"{article} — {desc}" if desc else f"{article}"
            if _norm(lbl) == label:
                return str(r["product_id"])
        return None

    # ============================================================
    # Objectives (Role-based)
    # ============================================================
    def _objective_name_for_id(objective_id: int) -> str:
        df = query_df(
            """
            SELECT name
            FROM objectives
            WHERE objective_id = :oid
            LIMIT 1
            """,
            {"oid": int(objective_id)},
        )
        return _norm(df.iloc[0]["name"]) if not df.empty else ""

    def _load_objective_options_for_role(role_name: str) -> list[str]:
        if role_name in {"admin", "manager"}:
            df = query_df(
                """
                SELECT name
                FROM objectives
                WHERE COALESCE(is_active, TRUE) IS TRUE
                ORDER BY name
                """
            )
        else:
            df = query_df(
                """
                SELECT o.name
                FROM objectives o
                JOIN role_objectives ro ON ro.objective_id = o.objective_id
                WHERE COALESCE(o.is_active, TRUE) IS TRUE
                  AND COALESCE(ro.is_active, TRUE) IS TRUE
                  AND ro.role = :role
                ORDER BY o.name
                """,
                {"role": role_name},
            )
        return [""] + (df["name"].astype(str).tolist() if not df.empty else [])

    def _objective_id_from_name(obj_name: str) -> int | None:
        obj_name = _norm(obj_name)
        if not obj_name:
            return None
        df = query_df(
            """
            SELECT objective_id
            FROM objectives
            WHERE trim(name) = :n
            LIMIT 1
            """,
            {"n": obj_name},
        )
        return int(df.iloc[0]["objective_id"]) if not df.empty else None

    # ============================================================
    # Location view
    # ============================================================
    def _render_location_view(lat, lon, acc):
        st.markdown("### 1️⃣ Visit Location")
        st.text_input("Latitude", value=str(lat or ""), disabled=True, key=f"{PAGE_NS}/lat_view")
        st.text_input("Longitude", value=str(lon or ""), disabled=True, key=f"{PAGE_NS}/lon_view")
        st.text_input("Accuracy (m)", value=str(acc or ""), disabled=True, key=f"{PAGE_NS}/acc_view")

        if lat is None or lon is None:
            st.info("No location captured for this visit.")
            return

        try:
            m = folium.Map(location=[float(lat), float(lon)], zoom_start=15, control_scale=True)
            folium.Marker([float(lat), float(lon)], tooltip="Visit Location").add_to(m)
            st_folium(m, width="100%", height=280)
        except Exception:
            st.info("Map preview unavailable (lat/lon invalid).")

    # ============================================================
    # Insert request + details
    # ============================================================
    def _insert_request_and_details(visit_id: int, requested_by: int, note: str, details: list[dict]) -> int:
        sql_req = text(
            """
            INSERT INTO request_changes
              (visit_id, change_source, requested_by, request_note, status, request_date)
            VALUES
              (:visit_id, 'REQUEST', :requested_by, :request_note, 'IN_REVIEW', NOW())
            RETURNING request_id
            """
        )
        sql_det = text(
            """
            INSERT INTO request_change_details
              (request_id, field, old_value, new_value)
            VALUES
              (:request_id, :field, :old_value, :new_value)
            """
        )

        with engine.begin() as conn:
            chk = conn.execute(
                text(
                    """
                    SELECT 1
                    FROM request_changes
                    WHERE visit_id = :vid AND status = 'IN_REVIEW'
                    LIMIT 1
                    """
                ),
                {"vid": int(visit_id)},
            ).fetchone()
            if chk:
                raise ValueError("There is already an IN_REVIEW request for this visit.")

            request_id = conn.execute(
                sql_req,
                {"visit_id": int(visit_id), "requested_by": int(requested_by), "request_note": str(note)},
            ).scalar_one()

            for d in details:
                conn.execute(
                    sql_det,
                    {
                        "request_id": int(request_id),
                        "field": str(d["field"]),
                        "old_value": d.get("old_value"),
                        "new_value": d.get("new_value"),
                    },
                )

        return int(request_id)

    # ============================================================
    # Cascading reset callbacks (guarded by prefill phase)
    # ============================================================
    def _in_prefill_phase() -> bool:
        return bool(st.session_state.get(f"{PAGE_NS}/prefill_phase", False))

    def _clear_from_bu():
        if _in_prefill_phase():
            return
        st.session_state[f"{PAGE_NS}/cat_sel"] = ""
        st.session_state[f"{PAGE_NS}/bl_sel"] = ""
        st.session_state[f"{PAGE_NS}/prod_sel"] = ""
        st.session_state.pop(f"{PAGE_NS}/sm_editor", None)

    def _clear_from_cat():
        if _in_prefill_phase():
            return
        st.session_state[f"{PAGE_NS}/bl_sel"] = ""
        st.session_state[f"{PAGE_NS}/prod_sel"] = ""
        st.session_state.pop(f"{PAGE_NS}/sm_editor", None)

    def _clear_from_bl():
        if _in_prefill_phase():
            return
        st.session_state[f"{PAGE_NS}/prod_sel"] = ""

    # ============================================================
    # Keys to clear when searching a new visit
    # ============================================================
    PREFILL_KEYS = [
        f"{PAGE_NS}/snap",
        f"{PAGE_NS}/prefilled_vid",
        f"{PAGE_NS}/pending_vid",
        f"{PAGE_NS}/prefill_phase",
        f"{PAGE_NS}/active_visit_id",

        f"{PAGE_NS}/aud_sel",
        f"{PAGE_NS}/bu_sel", f"{PAGE_NS}/cat_sel", f"{PAGE_NS}/bl_sel", f"{PAGE_NS}/prod_sel",
        f"{PAGE_NS}/obj_sel", f"{PAGE_NS}/eval_sel",
        f"{PAGE_NS}/notes",

        f"{PAGE_NS}/ota_title", f"{PAGE_NS}/ota_name", f"{PAGE_NS}/ota_dept", f"{PAGE_NS}/ota_pos",
        f"{PAGE_NS}/ota_phone", f"{PAGE_NS}/ota_email",

        f"{PAGE_NS}/hv_name", f"{PAGE_NS}/hv_phone", f"{PAGE_NS}/hv_serial",

        f"{PAGE_NS}/sm_editor",
        f"{PAGE_NS}/req_note",
    ]

    # ============================================================
    # Tabs
    # ============================================================
    tab_new, tab_view = st.tabs(["➕ New Request", "📄 View Requests"])

    # ============================================================
    # TAB 1 — New Request
    # ============================================================
    with tab_new:
        st.markdown(
            '<div style="margin:.25rem 0 1rem 0;">'
            'Fields marked with <span style="color:#d00000;font-weight:700">*</span> are required.'
            "</div>",
            unsafe_allow_html=True,
        )

        st.markdown("### Select Visit")
        vcol1, vcol2 = st.columns([2, 1])
        with vcol1:
            visit_id_str = st.text_input(
                "Input Visit ID you are wishing to request a change for:",
                key=f"{PAGE_NS}/visit_id_in",
            )
        with vcol2:
            search_click = st.button("Search", type="primary", key=f"{PAGE_NS}/search_btn")

        if search_click:
            vid = _safe_int(visit_id_str)
            if not vid:
                st.error("Enter a valid Visit ID.")
                st.stop()

            # clear previous state
            for k in PREFILL_KEYS:
                st.session_state.pop(k, None)

            st.session_state[f"{PAGE_NS}/pending_vid"] = int(vid)
            st.rerun()

        pending_vid = st.session_state.pop(f"{PAGE_NS}/pending_vid", None)
        if pending_vid:
            snap = _load_visit_snapshot(int(pending_vid))
            if not snap:
                st.error("Visit ID not found.")
                st.stop()

            if int(snap["visit"]["user_id"]) != uid and role not in {"admin", "manager"}:
                st.error("You can only request changes for your own visits.")
                st.stop()

            if _has_open_request_for_visit(int(pending_vid)):
                st.error("This visit already has an IN_REVIEW request.")
                st.stop()

            v = snap["visit"]

            # ---- PRE-FILL PHASE ON ----
            st.session_state[f"{PAGE_NS}/prefill_phase"] = True

            # Audience label
            aud_label = ""
            if v.get("audience_id"):
                aud_label = _audience_label_for_id(int(v["audience_id"]))
            elif _norm(v.get("other_audience_name")):
                aud_label = "Other"
            st.session_state[f"{PAGE_NS}/aud_sel"] = aud_label

            # Infer BU/Category/BL from BLID
            blid = _safe_int(v.get("business_line_id"))
            infer = _infer_bu_cat_bl_from_blid(blid) if blid else {"bu_name": "", "category": "", "bl_name": ""}
            st.session_state[f"{PAGE_NS}/bu_sel"] = _norm(infer.get("bu_name"))
            st.session_state[f"{PAGE_NS}/cat_sel"] = _norm(infer.get("category"))
            st.session_state[f"{PAGE_NS}/bl_sel"] = _norm(infer.get("bl_name"))

            # Product label lookup (bind pid as text)
            prod_lbl = ""
            pid_txt = _norm(v.get("product_id"))
            if pid_txt:
                dfp = query_df(
                    """
                    SELECT product_id, article_number, description
                    FROM items
                    WHERE product_id = :pid
                    LIMIT 1
                    """,
                    {"pid": pid_txt},
                )
                if not dfp.empty:
                    r = dfp.iloc[0]
                    article = r["article_number"] if pd.notna(r["article_number"]) else r["product_id"]
                    desc = str(r["description"]).strip() if pd.notna(r["description"]) else ""
                    prod_lbl = f"{article} — {desc}" if desc else f"{article}"
            st.session_state[f"{PAGE_NS}/prod_sel"] = _norm(prod_lbl)

            # Objective + eval
            obj_lbl = ""
            oid = _safe_int(v.get("objective_id"))
            if oid:
                obj_lbl = _objective_name_for_id(int(oid))
            st.session_state[f"{PAGE_NS}/obj_sel"] = _norm(obj_lbl)
            st.session_state[f"{PAGE_NS}/eval_sel"] = _norm(v.get("evaluation"))

            # Notes
            st.session_state[f"{PAGE_NS}/active_visit_id"] = int(pending_vid)
            st.session_state[f"{PAGE_NS}/notes"] = _norm(v.get("notes"))

            # Other TA fields
            st.session_state[f"{PAGE_NS}/ota_title"] = _norm(v.get("other_audience_title"))
            st.session_state[f"{PAGE_NS}/ota_name"] = _norm(v.get("other_audience_name"))
            st.session_state[f"{PAGE_NS}/ota_dept"] = _norm(v.get("other_audience_department"))
            st.session_state[f"{PAGE_NS}/ota_pos"] = _norm(v.get("other_audience_position"))
            st.session_state[f"{PAGE_NS}/ota_phone"] = _norm(v.get("other_audience_phone"))
            st.session_state[f"{PAGE_NS}/ota_email"] = _norm(v.get("other_audience_email"))

            # Home visit fields
            hv0 = snap["home_visit"] or {}
            st.session_state[f"{PAGE_NS}/hv_name"] = _norm(hv0.get("patient_name"))
            st.session_state[f"{PAGE_NS}/hv_phone"] = _norm(hv0.get("patient_phone"))
            st.session_state[f"{PAGE_NS}/hv_serial"] = _norm(hv0.get("serial_no"))

            st.session_state[f"{PAGE_NS}/snap"] = snap
            st.session_state[f"{PAGE_NS}/prefilled_vid"] = int(pending_vid)

            st.rerun()

        snap = st.session_state.get(f"{PAGE_NS}/snap")
        if not snap:
            st.info("Search for a Visit ID to start.")
            st.stop()

        v = snap["visit"]

        # keep notes synced if visit changes
        current_vid = int(v.get("visit_id") or 0)
        if st.session_state.get(f"{PAGE_NS}/active_visit_id") != current_vid:
            st.session_state[f"{PAGE_NS}/active_visit_id"] = current_vid
            st.session_state[f"{PAGE_NS}/notes"] = _norm(v.get("notes"))

        # =====================================================
        # SECTION 1 — Visit Location (VIEW ONLY)
        # =====================================================
        _render_location_view(v.get("latitude"), v.get("longitude"), v.get("accuracy_m"))

        # =====================================================
        # SECTION 3 — Customer & Target Audience
        # =====================================================
        st.markdown("### 3️⃣ Customer & Target Audience")
        customer_id = int(v.get("customer_id") or 0)
        cust = _get_customer_locked_fields(customer_id)

        st.text_input("Region *", value=_norm(cust.get("region")), disabled=True, key=f"{PAGE_NS}/region_view")
        st.text_input("City *", value=_norm(cust.get("city")), disabled=True, key=f"{PAGE_NS}/city_view")
        st.text_input("Sector *", value=_norm(cust.get("sector")), disabled=True, key=f"{PAGE_NS}/sector_view")
        st.text_input("Customer *", value=_norm(cust.get("account_name")), disabled=True, key=f"{PAGE_NS}/cust_view")

        aud_labels = _load_audience_options(customer_id)
        _prefill_selectbox(
            "Target Audience *",
            aud_labels,
            key=f"{PAGE_NS}/aud_sel",
            prefill_value=st.session_state.get(f"{PAGE_NS}/aud_sel", ""),
        )

        aud_choice = st.session_state.get(f"{PAGE_NS}/aud_sel", "")
        is_other_aud = (aud_choice == "Other")
        is_home_visit = _is_home_visit_label(aud_choice)

        if is_other_aud:
            st.markdown("##### ➕ New Target Audience Details")
            _prefill_selectbox(
                "Title (optional)",
                TITLE_OPTIONS,
                key=f"{PAGE_NS}/ota_title",
                prefill_value=st.session_state.get(f"{PAGE_NS}/ota_title", ""),
            )
            st.text_input("Target Audience Name *", key=f"{PAGE_NS}/ota_name")

            dept_opts = _load_other_dept_options()
            _prefill_selectbox(
                "Department *",
                dept_opts,
                key=f"{PAGE_NS}/ota_dept",
                prefill_value=st.session_state.get(f"{PAGE_NS}/ota_dept", ""),
            )

            pos_opts = _load_other_position_options()
            _prefill_selectbox(
                "Position *",
                pos_opts,
                key=f"{PAGE_NS}/ota_pos",
                prefill_value=st.session_state.get(f"{PAGE_NS}/ota_pos", ""),
            )

            st.text_input("Phone # (optional)", key=f"{PAGE_NS}/ota_phone")
            st.text_input("Email (optional)", key=f"{PAGE_NS}/ota_email")

        if is_home_visit:
            st.markdown("##### 🏠 Home Visit Details")
            st.text_input("Patient Name *", key=f"{PAGE_NS}/hv_name")
            st.text_input("Patient Phone # *", key=f"{PAGE_NS}/hv_phone")
            st.text_input("Device Serial # *", key=f"{PAGE_NS}/hv_serial")

        # =====================================================
        # SECTION 4 — Product Details (RESTORED)
        # =====================================================
        st.markdown("### 4️⃣ Product Details")

        bu_names = _load_bu_options()
        st.selectbox(
            "Business Unit *",
            bu_names,
            index=bu_names.index(st.session_state.get(f"{PAGE_NS}/bu_sel", "")) if st.session_state.get(f"{PAGE_NS}/bu_sel", "") in bu_names else 0,
            key=f"{PAGE_NS}/bu_sel",
            on_change=_clear_from_bu,
        )
        bu_choice = st.session_state.get(f"{PAGE_NS}/bu_sel", "")
        bu_id = _bu_id_from_name(bu_choice) if bu_choice else None

        cat_names = _load_category_options(bu_id)
        st.selectbox(
            "Category *",
            cat_names,
            index=cat_names.index(st.session_state.get(f"{PAGE_NS}/cat_sel", "")) if st.session_state.get(f"{PAGE_NS}/cat_sel", "") in cat_names else 0,
            key=f"{PAGE_NS}/cat_sel",
            disabled=(bu_id is None),
            on_change=_clear_from_cat,
        )
        cat_choice = st.session_state.get(f"{PAGE_NS}/cat_sel", "")

        bl_names = _load_bl_options(bu_id, cat_choice)
        st.selectbox(
            "Business Line *",
            bl_names,
            index=bl_names.index(st.session_state.get(f"{PAGE_NS}/bl_sel", "")) if st.session_state.get(f"{PAGE_NS}/bl_sel", "") in bl_names else 0,
            key=f"{PAGE_NS}/bl_sel",
            disabled=(bu_id is None) or (not cat_choice),
            on_change=_clear_from_bl,
        )
        bl_choice = st.session_state.get(f"{PAGE_NS}/bl_sel", "")

        new_business_line_id = (
            _bl_id_from_name(bu_id, cat_choice, bl_choice) if (bu_id and cat_choice and bl_choice) else None
        )

        prod_labels = _load_product_options(new_business_line_id)
        st.selectbox(
            "Article Number/Product (optional)",
            prod_labels,
            index=prod_labels.index(st.session_state.get(f"{PAGE_NS}/prod_sel", "")) if st.session_state.get(f"{PAGE_NS}/prod_sel", "") in prod_labels else 0,
            key=f"{PAGE_NS}/prod_sel",
            disabled=(new_business_line_id is None),
        )
        prod_choice = st.session_state.get(f"{PAGE_NS}/prod_sel", "")

        # =====================================================
        # SECTION 5 — Objective, Notes, Evaluation
        # =====================================================
        st.markdown("### 5️⃣ Visit Details & Outcome")

        obj_names = _load_objective_options_for_role(role)
        _prefill_selectbox(
            "Business Objective *",
            obj_names,
            key=f"{PAGE_NS}/obj_sel",
            prefill_value=st.session_state.get(f"{PAGE_NS}/obj_sel", ""),
        )
        obj_choice = st.session_state.get(f"{PAGE_NS}/obj_sel", "")
        is_shelf = _is_shelf_objective(obj_choice)

        # Notes
        prefilled_notes = st.session_state.get(f"{PAGE_NS}/notes", "")
        notes = st.text_area("Notes (optional)", value=prefilled_notes, key=f"{PAGE_NS}/notes")

        _prefill_selectbox(
            "Evaluation *",
            ["", "Positive", "Negative", "Neutral"],
            key=f"{PAGE_NS}/eval_sel",
            prefill_value=st.session_state.get(f"{PAGE_NS}/eval_sel", ""),
        )
        evaluation_choice = st.session_state.get(f"{PAGE_NS}/eval_sel", "")

        # turn off prefill phase after first render
        if st.session_state.get(f"{PAGE_NS}/prefill_phase"):
            st.session_state[f"{PAGE_NS}/prefill_phase"] = False

        # =====================================================
        # LIVE Changes Preview
        # =====================================================
        details: list[dict] = []

        new_audience_id = _resolve_audience_id_from_label(customer_id, aud_choice)
        new_objective_id = _objective_id_from_name(obj_choice)

        new_product_id = None
        if (not is_shelf) and new_business_line_id and prod_choice:
            new_product_id = _product_id_from_label(new_business_line_id, prod_choice)

        _add_detail(details, "visits.audience_id", v.get("audience_id"), new_audience_id)
        _add_detail(details, "visits.business_line_id", v.get("business_line_id"), new_business_line_id)
        _add_detail(details, "visits.product_id", v.get("product_id"), (None if is_shelf else new_product_id))
        _add_detail(details, "visits.objective_id", v.get("objective_id"), new_objective_id)
        _add_detail(details, "visits.notes", v.get("notes"), (notes.strip() if notes else None))
        _add_detail(details, "visits.evaluation", v.get("evaluation"), (evaluation_choice or None))

        # include other-audience fields in preview if Other
        if is_other_aud:
            _add_detail(details, "visits.other_audience_title", v.get("other_audience_title"), (st.session_state.get(f"{PAGE_NS}/ota_title") or None))
            _add_detail(details, "visits.other_audience_name", v.get("other_audience_name"), (st.session_state.get(f"{PAGE_NS}/ota_name") or None))
            _add_detail(details, "visits.other_audience_department", v.get("other_audience_department"), (st.session_state.get(f"{PAGE_NS}/ota_dept") or None))
            _add_detail(details, "visits.other_audience_position", v.get("other_audience_position"), (st.session_state.get(f"{PAGE_NS}/ota_pos") or None))
            _add_detail(details, "visits.other_audience_phone", v.get("other_audience_phone"), (st.session_state.get(f"{PAGE_NS}/ota_phone") or None))
            _add_detail(details, "visits.other_audience_email", v.get("other_audience_email"), (st.session_state.get(f"{PAGE_NS}/ota_email") or None))

        section_header("Live Changes Preview")
        if not details:
            st.info("No changes yet.")
        else:
            rows_html = "".join(
                compare_row(
                    d["field"],
                    str(d.get("old_value") or "—"),
                    str(d.get("new_value") or "—"),
                    changed=True,
                )
                for d in details
            )
            st.markdown(
                f"""
                <table style="width:100%;border-collapse:collapse;border:1px solid #e4e8ec;
                              border-radius:10px;overflow:hidden;font-size:0.875rem;">
                  <thead>
                    <tr style="background:#f6f8fa;">
                      <th style="padding:10px 12px;text-align:left;font-weight:600;color:#57606a;
                                 border-bottom:1px solid #e4e8ec;width:30%;">Field</th>
                      <th style="padding:10px 12px;text-align:left;font-weight:600;color:#57606a;
                                 border-bottom:1px solid #e4e8ec;">Original</th>
                      <th style="padding:10px 12px;text-align:left;font-weight:600;color:#57606a;
                                 border-bottom:1px solid #e4e8ec;">Requested</th>
                    </tr>
                  </thead>
                  <tbody>{rows_html}</tbody>
                </table>
                """,
                unsafe_allow_html=True,
            )

        # =====================================================
        # Request note + Submit
        # =====================================================
        st.markdown("### Change Request Note")
        st.text_area(
            "Change Request Note (required) *",
            key=f"{PAGE_NS}/req_note",
            placeholder="Explain what you changed and why.",
        )
        req_note = st.session_state.get(f"{PAGE_NS}/req_note", "")

        submit_click = st.button(
            "Submit Change Request",
            type="primary",
            disabled=(len(details) == 0),
            key=f"{PAGE_NS}/submit_btn",
        )
        if submit_click:
            if not req_note or not req_note.strip():
                st.error("Change Request Note is required.")
                st.stop()

            try:
                req_id = _insert_request_and_details(
                    visit_id=int(v["visit_id"]),
                    requested_by=uid,
                    note=req_note.strip(),
                    details=details,
                )
                st.success(f"Request submitted ✅ (Request ID: {req_id})")

                # clear loaded state
                for k in PREFILL_KEYS:
                    st.session_state.pop(k, None)

                st.rerun()
            except Exception as e:
                st.error("Could not submit your request.")
                st.caption(str(e))

    # ============================================================
    # TAB 2 — View Requests
    # ============================================================
    with tab_view:
        section_header("My Change Requests")

        df = query_df(
            """
            SELECT
              request_id,
              request_date,
              visit_id,
              change_source,
              status,
              resolve_date,
              reject_note
            FROM request_changes
            WHERE requested_by = :uid
            ORDER BY request_date DESC
            """,
            {"uid": int(uid)},
        )

        if df.empty:
            st.info("No change requests submitted yet.")
        else:
            # Fetch field summaries for all requests
            req_ids = df["request_id"].astype(int).tolist()
            placeholders = ", ".join([f":id{i}" for i in range(len(req_ids))])
            det_params = {f"id{i}": int(rid) for i, rid in enumerate(req_ids)}
            det = query_df(
                f"SELECT request_id, field FROM request_change_details "
                f"WHERE request_id IN ({placeholders})",
                det_params,
            )
            summary = {}
            if not det.empty:
                summary = (
                    det.groupby("request_id")["field"]
                    .apply(lambda s: ", ".join(sorted(set(str(x).split(".")[-1] for x in s))))
                    .to_dict()
                )

            _BADGE_MAP = {
                "IN_REVIEW": "warning",
                "APPROVED":  "success",
                "REJECTED":  "danger",
                "WITHDRAWN": "neutral",
            }

            withdraw_success_key = f"{PAGE_NS}_withdraw_success"
            if st.session_state.get(withdraw_success_key):
                st.success(st.session_state.pop(withdraw_success_key))

            for _, row in df.iterrows():
                rid = int(row["request_id"])
                status_val = str(row["status"])
                badge_html = status_badge(status_val, _BADGE_MAP.get(status_val, "neutral"))
                fields_changed = summary.get(rid, "—")
                req_date = pd.to_datetime(row["request_date"]).strftime("%d/%m/%Y %H:%M") if pd.notna(row["request_date"]) else "—"

                with st.expander(f"Request #{rid} — Visit #{int(row['visit_id'])} — {status_val} — {req_date}"):
                    col_a, col_b = st.columns([3, 1])
                    with col_a:
                        st.markdown(f"**Status:** {badge_html}", unsafe_allow_html=True)
                        st.caption(f"Fields changed: {fields_changed}")
                        if status_val == "REJECTED" and pd.notna(row["reject_note"]) and row["reject_note"]:
                            st.warning(f"Rejection reason: {row['reject_note']}")
                    with col_b:
                        if status_val == "IN_REVIEW":
                            if st.button("Withdraw", key=f"{PAGE_NS}/withdraw_{rid}", type="secondary"):
                                with engine.begin() as conn:
                                    result = conn.execute(
                                        text("""
                                            UPDATE request_changes
                                            SET status = 'WITHDRAWN', resolve_date = NOW()
                                            WHERE request_id = :rid
                                              AND requested_by = :uid
                                              AND status = 'IN_REVIEW'
                                        """),
                                        {"rid": rid, "uid": int(uid)},
                                    )
                                if result.rowcount == 0:
                                    st.warning("Could not withdraw — request may already be resolved.")
                                    st.rerun()
                                else:
                                    st.session_state[withdraw_success_key] = f"Request #{rid} withdrawn."
                                    st.rerun()

# =============================
# Footer
# =============================
