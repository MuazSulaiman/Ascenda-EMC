# app_pages/admin_change_requests.py
import datetime
import pytz
import pandas as pd
import streamlit as st
from sqlalchemy import text

from auth import resolve_session_user
from config import TIMEZONE
from db import engine
from db_ops import query_df, exec_sql
from ui import section_header, status_badge, compare_row
from widgets import set_current_page, customer_quick_find_module, customer_cascading_selectors
from app_pages.change_request_helpers import (
    _norm, _safe_int, _add_detail,
    _load_bu_options, _bu_id_from_name,
    _load_category_options, _load_bl_options, _bl_id_from_name,
    _load_product_options, _product_id_from_label,
    _audience_label_for_id, _load_audience_options, _resolve_audience_id_from_label,
    _infer_bu_cat_bl, _objective_id_from_name,
    _fmt_field_label, _resolve_field_display_value,
    _load_other_dept_options, _load_other_position_options,
)

_TITLE_OPTIONS = ["", "Dr.", "Mr.", "Ms.", "Mrs.", "Prof.", "Eng.", "Other"]

_TZ = pytz.timezone(TIMEZONE)

PAGE_NS = "admin_change_req"
_FA_NS  = f"{PAGE_NS}_fa"

ALLOWED_VISIT_FIELDS = {
    "visits.customer_id", "visits.audience_id", "visits.business_line_id",
    "visits.product_id", "visits.objective_id", "visits.notes",
    "visits.evaluation", "visits.project_id", "visits.other_customer_name",
    "visits.other_audience_title", "visits.other_audience_name",
    "visits.other_audience_department", "visits.other_audience_position",
    "visits.other_audience_phone", "visits.other_audience_email",
}

FORCE_EXTRA_FIELDS = {
    "visits.latitude", "visits.longitude", "visits.accuracy_m",
    "visits.submitted_at_local", "visits.submitted_at_utc",
}


def _fmt_field(field: str) -> str:
    """Strip 'visits.' prefix for display."""
    return field.split(".", 1)[-1] if "." in field else field


def _apply_changes(request_id: int, visit_id: int, admin_uid: int):
    """
    Apply all request_change_details for request_id to the visits table.
    Returns (success: bool, error_msg: str | None).
    """
    detail_rows = query_df(
        "SELECT field, new_value FROM request_change_details WHERE request_id = :rid",
        {"rid": request_id},
    )

    if detail_rows.empty:
        return False, "No change details found for this request."

    # Whitelist check before opening transaction
    # Fields are stored as "visits.columnname" — must match ALLOWED_VISIT_FIELDS exactly.
    for _, r in detail_rows.iterrows():
        if r["field"] not in ALLOWED_VISIT_FIELDS:
            return False, f"Field not in allowed list: {r['field']}"

    NULLABLE_VISIT_FIELDS = {"visits.notes", "visits.product_id", "visits.audience_id", "visits.project_id"}
    for _, r in detail_rows.iterrows():
        if r["field"] not in NULLABLE_VISIT_FIELDS and (r["new_value"] is None or r["new_value"] == ""):
            return False, f"Field '{r['field']}' cannot be set to an empty value."

    try:
        with engine.begin() as conn:
            for _, r in detail_rows.iterrows():
                col = r["field"].split(".", 1)[-1]
                conn.execute(
                    text(f"UPDATE visits SET {col} = :val WHERE visit_id = :vid"),
                    {"val": r["new_value"], "vid": visit_id},
                )
            conn.execute(
                text(
                    """
                    UPDATE request_changes
                    SET status = 'APPROVED', applied_at = NOW(), resolve_date = NOW(), changed_by = :admin_uid
                    WHERE request_id = :rid
                    """
                ),
                {"admin_uid": admin_uid, "rid": request_id},
            )
        return True, None
    except Exception as e:
        # Record error in a separate connection (main transaction rolled back)
        try:
            exec_sql(
                "UPDATE request_changes SET apply_error = :err WHERE request_id = :rid",
                {"err": str(e), "rid": request_id},
            )
        except Exception:
            pass
        return False, str(e)


def _load_pending() -> pd.DataFrame:
    return query_df(
        """
        SELECT
          rc.request_id,
          rc.visit_id,
          u.name            AS rep_name,
          rc.request_date,
          rc.request_note,
          COUNT(rcd.detail_id) AS fields_changed
        FROM request_changes rc
        JOIN users u ON u.user_id = rc.requested_by
        JOIN visits v ON v.visit_id = rc.visit_id
        LEFT JOIN request_change_details rcd ON rcd.request_id = rc.request_id
        WHERE rc.status = 'IN_REVIEW'
          AND COALESCE(v.is_deleted, FALSE) IS FALSE
        GROUP BY rc.request_id, rc.visit_id, u.name, rc.request_date, rc.request_note
        ORDER BY rc.request_date ASC
        """
    )


def _load_visit_context(visit_id: int) -> dict:
    df = query_df(
        """
        SELECT
          v.visit_id,
          c.account_name AS customer_name,
          v.submitted_at_local,
          u.name         AS rep_name,
          bl.name        AS business_line
        FROM visits v
        JOIN customers c    ON c.customer_id    = v.customer_id
        JOIN users u        ON u.user_id        = v.user_id
        LEFT JOIN business_lines bl ON bl.business_line_id = v.business_line_id
        WHERE v.visit_id = :vid
        """,
        {"vid": visit_id},
    )
    return df.iloc[0].to_dict() if not df.empty else {}


def _load_diff(request_id: int) -> pd.DataFrame:
    return query_df(
        "SELECT field, old_value, new_value FROM request_change_details WHERE request_id = :rid ORDER BY field",
        {"rid": request_id},
    )


def _render_diff_table(
    diff_df: pd.DataFrame,
    before_label: str = "Original",
    after_label: str = "Requested",
):
    rows_html = "".join(
        compare_row(
            _fmt_field_label(str(r["field"])),
            _resolve_field_display_value(str(r["field"]), r["old_value"] if pd.notna(r["old_value"]) else None),
            _resolve_field_display_value(str(r["field"]), r["new_value"] if pd.notna(r["new_value"]) else None),
            changed=True,
        )
        for _, r in diff_df.iterrows()
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
                         border-bottom:1px solid #e4e8ec;">{before_label}</th>
              <th style="padding:10px 12px;text-align:left;font-weight:600;color:#57606a;
                         border-bottom:1px solid #e4e8ec;">{after_label}</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
        """,
        unsafe_allow_html=True,
    )


# ─── Force-Adjustment helpers ─────────────────────────────────────────────────

def _fa_load_all_visits() -> pd.DataFrame:
    return query_df(
        """
        SELECT v.visit_id, v.submitted_at_local, c.account_name, u.name AS rep_name
        FROM visits v
        JOIN customers c ON c.customer_id = v.customer_id
        JOIN users u ON u.user_id = v.user_id
        WHERE COALESCE(v.is_deleted, FALSE) IS FALSE
        ORDER BY v.visit_id DESC
        LIMIT 1000
        """
    )


def _fa_load_visit_snap(visit_id: int) -> dict | None:
    df = query_df(
        """
        SELECT
          v.visit_id, v.user_id,
          v.customer_id, c.account_name,
          v.audience_id,
          v.business_line_id, bl.name AS bl_name, bl.category,
          bu.name AS bu_name,
          v.product_id, i.article_number, i.description AS product_desc,
          v.objective_id, o.name AS objective_name,
          v.notes, v.evaluation,
          v.latitude, v.longitude, v.accuracy_m,
          v.submitted_at_local, v.submitted_at_utc,
          v.other_audience_title, v.other_audience_name,
          v.other_audience_department, v.other_audience_position,
          v.other_audience_phone, v.other_audience_email
        FROM visits v
        JOIN customers c ON c.customer_id = v.customer_id
        LEFT JOIN business_lines bl ON bl.business_line_id = v.business_line_id
        LEFT JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
        LEFT JOIN items i ON i.product_id = v.product_id
        LEFT JOIN objectives o ON o.objective_id = v.objective_id
        WHERE v.visit_id = :vid
        """,
        {"vid": visit_id},
    )
    return df.iloc[0].to_dict() if not df.empty else None



def _fa_load_objective_options() -> list[str]:
    df = query_df("SELECT name FROM objectives WHERE COALESCE(is_active, TRUE) IS TRUE ORDER BY name")
    return ([""] + df["name"].tolist()) if not df.empty else [""]


def _fa_product_label_for_id(product_id: str) -> str:
    df = query_df(
        "SELECT product_id, article_number, description FROM items WHERE product_id=:pid LIMIT 1",
        {"pid": product_id},
    )
    if df.empty:
        return ""
    r = df.iloc[0]
    art  = r["article_number"] if pd.notna(r["article_number"]) else r["product_id"]
    desc = str(r["description"]).strip() if pd.notna(r["description"]) else ""
    return f"{art} — {desc}" if desc else str(art)


def _apply_force_adjustment(visit_id: int, admin_uid: int, details: list[dict], note: str):
    force_allowed = ALLOWED_VISIT_FIELDS | FORCE_EXTRA_FIELDS
    for d in details:
        if d["field"] not in force_allowed:
            return False, f"Field not allowed: {d['field']}"
    try:
        with engine.begin() as conn:
            # Snapshot current DB values before any UPDATE so audit log has authoritative before-state
            cols = [d["field"].split(".", 1)[-1] for d in details]
            before_row = conn.execute(
                text(f"SELECT {', '.join(cols)} FROM visits WHERE visit_id = :vid"),
                {"vid": visit_id},
            ).mappings().one_or_none()

            for d in details:
                col = d["field"].split(".", 1)[-1]
                conn.execute(
                    text(f"UPDATE visits SET {col} = :val WHERE visit_id = :vid"),
                    {"val": d["new_value"], "vid": visit_id},
                )
            request_id = conn.execute(
                text(
                    """
                    INSERT INTO request_changes
                      (visit_id, change_source, requested_by, request_note, status,
                       request_date, applied_at, changed_by, resolve_date)
                    VALUES
                      (:vid, 'FORCE', :admin_uid, :note, 'APPROVED',
                       NOW(), NOW(), :admin_uid, NOW())
                    RETURNING request_id
                    """
                ),
                {"vid": visit_id, "admin_uid": admin_uid, "note": note},
            ).scalar_one()
            for d in details:
                col = d["field"].split(".", 1)[-1]
                db_old = None
                if before_row is not None:
                    raw = before_row[col]
                    db_old = None if raw is None else str(raw)
                conn.execute(
                    text(
                        """
                        INSERT INTO request_change_details (request_id, field, old_value, new_value)
                        VALUES (:rid, :field, :old, :new)
                        """
                    ),
                    {
                        "rid":   request_id,
                        "field": d["field"],
                        "old":   db_old,
                        "new":   d.get("new_value"),
                    },
                )
        return True, None
    except Exception as e:
        return False, str(e)


def _delete_visit(visit_id: int, admin_uid: int, note: str):
    """
    Soft-delete a visit. All five steps run in one transaction.
    Returns (success: bool, error_msg: str | None).
    """
    try:
        if not (note or "").strip():
            return False, "A deletion note is required."
        with engine.begin() as conn:
            # 1. Guard: visit must exist and not already be deleted
            row = conn.execute(
                text(
                    "SELECT visit_id FROM visits "
                    "WHERE visit_id = :vid AND COALESCE(is_deleted, FALSE) IS FALSE"
                ),
                {"vid": visit_id},
            ).fetchone()
            if not row:
                return False, "Visit not found or already deleted."

            # 2. Auto-reject any open change requests for this visit
            conn.execute(
                text(
                    """
                    UPDATE request_changes
                    SET status       = 'REJECTED',
                        reject_note  = 'Visit was deleted by admin',
                        resolve_date = NOW(),
                        changed_by   = :admin_uid
                    WHERE visit_id = :vid AND status = 'IN_REVIEW'
                    """
                ),
                {"admin_uid": admin_uid, "vid": visit_id},
            )

            # 3. Hard-delete child records
            # shelf_movement_lines cascade automatically from shelf_movement_headers
            conn.execute(text("DELETE FROM home_visits WHERE visit_id = :vid"), {"vid": visit_id})
            conn.execute(text("DELETE FROM shelf_movement_headers WHERE visit_id = :vid"), {"vid": visit_id})

            # 4. Insert deletion audit record in request_changes
            conn.execute(
                text(
                    """
                    INSERT INTO request_changes
                      (visit_id, change_source, requested_by, request_note, status,
                       request_date, applied_at, changed_by, resolve_date)
                    VALUES
                      (:vid, 'FORCE', :admin_uid, :note, 'DELETED',
                       NOW(), NOW(), :admin_uid, NOW())
                    """
                ),
                {"vid": visit_id, "admin_uid": admin_uid, "note": note},
            )

            # 5. Soft-delete the visit row
            conn.execute(
                text(
                    """
                    UPDATE visits
                    SET is_deleted = TRUE,
                        deleted_at = NOW(),
                        deleted_by = :admin_uid
                    WHERE visit_id = :vid
                    """
                ),
                {"admin_uid": admin_uid, "vid": visit_id},
            )

        return True, None
    except Exception as e:
        return False, str(e)


def _render_force_tab(admin_uid: int):
    NS = _FA_NS
    st.markdown(
        "<style>"
        "div[class*='st-key-admin-change-req-fa-del-btn'] button {"
        "  background-color:#dc2626!important;"
        "  color:#fff!important;"
        "  border-color:#dc2626!important;"
        "}"
        "</style>",
        unsafe_allow_html=True,
    )
    success_key = f"{NS}_success"

    if st.session_state.get(success_key):
        st.success(st.session_state.pop(success_key))

    del_success_key = f"{NS}_del_success"
    if st.session_state.get(del_success_key):
        st.success(st.session_state.pop(del_success_key))

    # ── Visit search ──────────────────────────────────────────────────────────
    st.markdown("### Select Visit")
    search_q = st.text_input(
        "Search by visit ID, customer, or rep:",
        key=f"{NS}_search",
        placeholder="Type to filter…",
    )

    all_visits = _fa_load_all_visits()
    if all_visits.empty:
        st.info("No visits found.")
        return

    sq = (search_q or "").strip().lower()
    if sq:
        mask = (
            all_visits["visit_id"].astype(str).str.contains(sq, na=False)
            | all_visits["account_name"].str.lower().str.contains(sq, na=False)
            | all_visits["rep_name"].str.lower().str.contains(sq, na=False)
        )
        filtered = all_visits[mask]
    else:
        filtered = all_visits.head(100)

    if filtered.empty:
        st.info("No visits match your search.")
        return

    def _vlabel(r) -> str:
        date_str = str(r["submitted_at_local"])[:10] if pd.notna(r["submitted_at_local"]) else "?"
        return f"V-{int(r['visit_id'])} — {r['account_name']} — {r['rep_name']} — {date_str}"

    visit_options = [""] + [_vlabel(r) for _, r in filtered.iterrows()]
    visit_id_map  = {_vlabel(r): int(r["visit_id"]) for _, r in filtered.iterrows()}

    prev_vid     = st.session_state.get(f"{NS}_snap_vid")
    chosen_label = st.selectbox("Select a visit:", visit_options, key=f"{NS}_visit_sel")

    if not chosen_label:
        st.info("Select a visit above to start a forced adjustment.")
        return

    visit_id = visit_id_map[chosen_label]

    # Prefill session state when visit changes
    if prev_vid != visit_id:
        snap = _fa_load_visit_snap(visit_id)
        if not snap:
            st.error("Visit not found.")
            return
        # Clear old form state
        for k in list(st.session_state.keys()):
            if k.startswith(f"{NS}_") and k not in (f"{NS}_search", f"{NS}_visit_sel"):
                del st.session_state[k]
        # Pre-fill
        st.session_state[f"{NS}_snap"]     = snap
        st.session_state[f"{NS}_snap_vid"] = visit_id
        st.session_state[f"{NS}_notes"]    = _norm(snap.get("notes"))
        st.session_state[f"{NS}_eval_sel"] = _norm(snap.get("evaluation"))
        lat = snap.get("latitude")
        lon = snap.get("longitude")
        acc = snap.get("accuracy_m")
        st.session_state[f"{NS}_lat"] = "" if lat is None else str(lat)
        st.session_state[f"{NS}_lon"] = "" if lon is None else str(lon)
        st.session_state[f"{NS}_acc"] = "" if acc is None else str(acc)
        # Date/time prefill
        raw_dt = snap.get("submitted_at_local")
        try:
            parsed = pd.to_datetime(raw_dt, errors="coerce")
            st.session_state[f"{NS}_date"] = parsed.date() if parsed and not pd.isnull(parsed) else datetime.date.today()
            t = parsed.time() if parsed and not pd.isnull(parsed) else datetime.time(0, 0)
            st.session_state[f"{NS}_hour"] = t.hour
            st.session_state[f"{NS}_minute"] = t.minute
        except Exception:
            st.session_state[f"{NS}_date"] = datetime.date.today()
            st.session_state[f"{NS}_hour"] = 0
            st.session_state[f"{NS}_minute"] = 0
        # Prefill cascading customer selectors from snap
        cust_df_snap = query_df(
            "SELECT region, city, sector FROM customers WHERE customer_id = :cid LIMIT 1",
            {"cid": int(snap["customer_id"])},
        )
        if not cust_df_snap.empty:
            cr = cust_df_snap.iloc[0]
            st.session_state[f"{NS}_region_sel"] = _norm(cr.get("region"))
            st.session_state[f"{NS}_city_sel"]   = _norm(cr.get("city"))
            st.session_state[f"{NS}_sector_sel"] = _norm(cr.get("sector"))
        st.session_state[f"{NS}_cust_sel"]   = _norm(snap.get("account_name"))
        st.session_state[f"{NS}_custid"]     = int(snap["customer_id"])
        st.session_state[f"{NS}_cid_locked"] = False
        st.session_state[f"{NS}_acct_input"] = ""
        st.session_state[f"{NS}_qf_msg"]     = ""
        st.session_state[f"{NS}_qf_msg_type"]= ""
        if snap.get("audience_id"):
            st.session_state[f"{NS}_aud_sel"] = _audience_label_for_id(int(snap["audience_id"]))
        else:
            st.session_state[f"{NS}_aud_sel"] = ""
        bl_id = _safe_int(snap.get("business_line_id"))
        if bl_id:
            infer = _infer_bu_cat_bl(bl_id)
            st.session_state[f"{NS}_bu_sel"]  = infer["bu_name"]
            st.session_state[f"{NS}_cat_sel"] = infer["category"]
            st.session_state[f"{NS}_bl_sel"]  = infer["bl_name"]
        else:
            st.session_state[f"{NS}_bu_sel"]  = ""
            st.session_state[f"{NS}_cat_sel"] = ""
            st.session_state[f"{NS}_bl_sel"]  = ""
        if snap.get("product_id"):
            st.session_state[f"{NS}_prod_sel"] = _fa_product_label_for_id(_norm(snap["product_id"]))
        else:
            st.session_state[f"{NS}_prod_sel"] = ""
        st.session_state[f"{NS}_obj_sel"] = _norm(snap.get("objective_name"))
        st.rerun()

    snap = st.session_state.get(f"{NS}_snap")
    if not snap:
        return

    # ── Visit summary ─────────────────────────────────────────────────────────
    rep_df   = query_df("SELECT name FROM users WHERE user_id=:uid", {"uid": int(snap["user_id"])})
    rep_name = rep_df.iloc[0]["name"] if not rep_df.empty else "—"
    date_str = str(snap.get("submitted_at_local") or "")[:10] or "—"
    st.info(
        f"**V-{visit_id}** · Rep: {rep_name} · "
        f"Customer: {_norm(snap.get('account_name'))} · Date: {date_str}"
    )
    st.markdown("---")

    # ── Delete Visit ──────────────────────────────────────────────────────────
    with st.expander("🗑️ Delete Visit", expanded=False):
        st.warning(
            "⚠️ **This action permanently hides the visit** and deletes any "
            "associated home visit and shelf movement records. It cannot be undone."
        )
        del_reason = st.text_area(
            "Deletion reason (required) *",
            key=f"{NS}_del_reason",
            placeholder="Explain why this visit is being deleted.",
        )
        del_confirm = st.checkbox(
            "I confirm I want to delete this visit",
            key=f"{NS}_del_confirm",
        )
        del_enabled = bool((del_reason or "").strip()) and del_confirm

        st.markdown(
            "<style>"
            "div:has(#del-visit-btn-anchor) + div button {"
            "  background-color: #c0392b !important;"
            "  color: white !important;"
            "  border: 1px solid #c0392b !important;"
            "}"
            "div:has(#del-visit-btn-anchor) + div button:hover:not(:disabled) {"
            "  background-color: #a93226 !important;"
            "  border-color: #a93226 !important;"
            "}"
            "div:has(#del-visit-btn-anchor) + div button:disabled {"
            "  background-color: #e8b4b0 !important;"
            "  border-color: #e8b4b0 !important;"
            "  color: rgba(255,255,255,0.6) !important;"
            "}"
            "</style>"
            '<div id="del-visit-btn-anchor"></div>',
            unsafe_allow_html=True,
        )

        if st.button(
            "🗑️ Delete Visit",
            key=f"{NS}_del_btn",
            type="secondary",
            disabled=not del_enabled,
        ):
            ok, err = _delete_visit(visit_id, admin_uid, (del_reason or "").strip())
            if ok:
                st.session_state[del_success_key] = f"Visit V-{visit_id} has been deleted."
                # Clear visit selection state
                for k in list(st.session_state.keys()):
                    if k.startswith(f"{NS}_") and k not in (del_success_key, f"{NS}_search"):
                        del st.session_state[k]
                st.rerun()
            else:
                st.error(f"Delete failed: {err}")

    # ── Customer (Quick Find + cascading Region/City/Sector/Customer) ────────
    st.markdown("#### Customer")
    customer_quick_find_module(
        page_ns=NS,
        query_df=query_df,
        KEY_ACCT=f"{NS}_acct_input",
        KEY_REGION=f"{NS}_region_sel",
        KEY_CITY=f"{NS}_city_sel",
        KEY_SECTOR=f"{NS}_sector_sel",
        KEY_CUST=f"{NS}_cust_sel",
        KEY_CUSTID=f"{NS}_custid",
        cid_locked_key=f"{NS}_cid_locked",
        req_clear_customer_key=f"{NS}_req_clear_cust",
        req_clear_acct_key=f"{NS}_req_clear_acct",
        req_set_acct_key=f"{NS}_req_set_acct",
        acct_set_value_key=f"{NS}_acct_set_val",
        qf_msg_key=f"{NS}_qf_msg",
        qf_msg_type_key=f"{NS}_qf_msg_type",
    )
    new_customer_id = customer_cascading_selectors(
        query_df=query_df,
        KEY_REGION=f"{NS}_region_sel",
        KEY_CITY=f"{NS}_city_sel",
        KEY_SECTOR=f"{NS}_sector_sel",
        KEY_CUST=f"{NS}_cust_sel",
        KEY_CUSTID=f"{NS}_custid",
        cid_locked_key=f"{NS}_cid_locked",
        qf_msg_key=f"{NS}_qf_msg",
        qf_msg_type_key=f"{NS}_qf_msg_type",
    )

    # ── Target Audience ───────────────────────────────────────────────────────
    st.markdown("#### Target Audience")
    effective_cust_id = new_customer_id or _safe_int(snap.get("customer_id"))
    aud_options = _load_audience_options(effective_cust_id, include_other=True) if effective_cust_id else [""]
    aud_sel_val = st.session_state.get(f"{NS}_aud_sel", "")
    if aud_sel_val not in aud_options:
        st.session_state[f"{NS}_aud_sel"] = ""
    aud_choice = st.selectbox("Target Audience", aud_options, key=f"{NS}_aud_sel")
    is_other_aud = (aud_choice == "Other")
    new_audience_id = (
        _resolve_audience_id_from_label(effective_cust_id, aud_choice)
        if (effective_cust_id and aud_choice and not is_other_aud) else None
    )

    if is_other_aud:
        st.markdown("##### New Target Audience Details")
        ota_title_opts = _TITLE_OPTIONS
        ota_title_val  = st.session_state.get(f"{NS}_ota_title", "")
        if ota_title_val not in ota_title_opts:
            st.session_state[f"{NS}_ota_title"] = ""
        st.selectbox("Title (optional)", ota_title_opts, key=f"{NS}_ota_title")
        st.text_input("Name *", key=f"{NS}_ota_name")
        dept_opts = _load_other_dept_options()
        dept_val  = st.session_state.get(f"{NS}_ota_dept", "")
        if dept_val not in dept_opts:
            st.session_state[f"{NS}_ota_dept"] = ""
        st.selectbox("Department *", dept_opts, key=f"{NS}_ota_dept")
        pos_opts = _load_other_position_options()
        pos_val  = st.session_state.get(f"{NS}_ota_pos", "")
        if pos_val not in pos_opts:
            st.session_state[f"{NS}_ota_pos"] = ""
        st.selectbox("Position *", pos_opts, key=f"{NS}_ota_pos")
        st.text_input("Phone # (optional)", key=f"{NS}_ota_phone")
        st.text_input("Email (optional)", key=f"{NS}_ota_email")

    # ── Product & Business ────────────────────────────────────────────────────
    st.markdown("#### Product & Business")
    bu_options = _load_bu_options()
    bu_choice  = st.selectbox("Business Unit *", bu_options, key=f"{NS}_bu_sel")
    bu_id      = _bu_id_from_name(bu_choice) if bu_choice else None

    cat_options = _load_category_options(bu_id)
    cat_sel_val = st.session_state.get(f"{NS}_cat_sel", "")
    if cat_sel_val not in cat_options:
        st.session_state[f"{NS}_cat_sel"] = ""
    cat_choice = st.selectbox("Category *", cat_options, key=f"{NS}_cat_sel", disabled=not bu_id)

    bl_options = _load_bl_options(bu_id, cat_choice)
    bl_sel_val = st.session_state.get(f"{NS}_bl_sel", "")
    if bl_sel_val not in bl_options:
        st.session_state[f"{NS}_bl_sel"] = ""
    bl_choice  = st.selectbox("Business Line *", bl_options, key=f"{NS}_bl_sel", disabled=not cat_choice)
    new_bl_id  = _bl_id_from_name(bu_id, cat_choice, bl_choice) if (bu_id and cat_choice and bl_choice) else None

    prod_options = _load_product_options(new_bl_id)
    prod_sel_val = st.session_state.get(f"{NS}_prod_sel", "")
    if prod_sel_val not in prod_options:
        st.session_state[f"{NS}_prod_sel"] = ""
    prod_choice    = st.selectbox("Product (optional)", prod_options, key=f"{NS}_prod_sel", disabled=not new_bl_id)
    new_product_id = _product_id_from_label(new_bl_id, prod_choice) if (new_bl_id and prod_choice) else None

    # ── Date & Time (locked for rep, editable here) ───────────────────────────
    st.markdown("#### Date & Time")
    col_date, col_hour, col_min = st.columns([2, 1, 1])
    with col_date:
        new_date = st.date_input("Visit Date *", key=f"{NS}_date")
    with col_hour:
        new_hour = st.selectbox("Hour *", list(range(24)), key=f"{NS}_hour",
                                format_func=lambda h: f"{h:02d}")
    with col_min:
        new_minute = st.selectbox("Minute *", list(range(60)), key=f"{NS}_minute",
                                  format_func=lambda m: f"{m:02d}")
    new_time = datetime.time(new_hour, new_minute)

    # ── Visit Details ─────────────────────────────────────────────────────────
    st.markdown("#### Visit Details")
    obj_options = _fa_load_objective_options()
    obj_sel_val = st.session_state.get(f"{NS}_obj_sel", "")
    if obj_sel_val not in obj_options:
        st.session_state[f"{NS}_obj_sel"] = ""
    obj_choice     = st.selectbox("Business Objective *", obj_options, key=f"{NS}_obj_sel")
    new_objective_id = _objective_id_from_name(obj_choice) if obj_choice else None

    notes = st.text_area("Notes (optional)", key=f"{NS}_notes")

    eval_options = ["", "Positive", "Negative", "Neutral"]
    eval_sel_val = st.session_state.get(f"{NS}_eval_sel", "")
    if eval_sel_val not in eval_options:
        st.session_state[f"{NS}_eval_sel"] = ""
    eval_choice = st.selectbox("Evaluation *", eval_options, key=f"{NS}_eval_sel")

    # ── Location (locked for rep, editable here) ──────────────────────────────
    st.markdown("#### Location")
    col_lat, col_lon, col_acc = st.columns(3)
    with col_lat:
        lat_val = st.text_input("Latitude *", key=f"{NS}_lat")
    with col_lon:
        lon_val = st.text_input("Longitude *", key=f"{NS}_lon")
    with col_acc:
        acc_val = st.text_input("Accuracy (m)", key=f"{NS}_acc")

    # ── Build change details ──────────────────────────────────────────────────
    # Compute new datetime strings
    new_local_dt = datetime.datetime.combine(new_date, new_time)
    new_local_str = new_local_dt.strftime("%Y-%m-%d %H:%M:%S")
    new_utc_str = (
        _TZ.localize(new_local_dt).astimezone(pytz.utc).strftime("%Y-%m-%d %H:%M:%S+00")
    )
    old_local_str = str(snap.get("submitted_at_local") or "")[:16] + ":00"
    old_utc_str   = str(snap.get("submitted_at_utc")   or "")[:16] + ":00+00"

    details: list[dict] = []
    _add_detail(details, "visits.customer_id",      snap.get("customer_id"),      new_customer_id)
    _add_detail(details, "visits.audience_id",      snap.get("audience_id"),      new_audience_id)
    _add_detail(details, "visits.business_line_id", snap.get("business_line_id"), new_bl_id)
    _add_detail(details, "visits.product_id",       snap.get("product_id"),       new_product_id)
    _add_detail(details, "visits.objective_id",     snap.get("objective_id"),     new_objective_id)
    _add_detail(details, "visits.notes",            snap.get("notes"),            notes.strip() or None)
    _add_detail(details, "visits.evaluation",       snap.get("evaluation"),       eval_choice or None)
    _add_detail(details, "visits.submitted_at_local", old_local_str, new_local_str)
    _add_detail(details, "visits.submitted_at_utc",   old_utc_str,   new_utc_str)
    _add_detail(details, "visits.latitude",         snap.get("latitude"),         lat_val.strip() or None)
    _add_detail(details, "visits.longitude",        snap.get("longitude"),        lon_val.strip() or None)
    _add_detail(details, "visits.accuracy_m",       snap.get("accuracy_m"),       acc_val.strip() or None)
    if is_other_aud:
        _add_detail(details, "visits.other_audience_title",      snap.get("other_audience_title"),      (st.session_state.get(f"{NS}_ota_title") or None))
        _add_detail(details, "visits.other_audience_name",       snap.get("other_audience_name"),       (st.session_state.get(f"{NS}_ota_name") or None))
        _add_detail(details, "visits.other_audience_department", snap.get("other_audience_department"), (st.session_state.get(f"{NS}_ota_dept") or None))
        _add_detail(details, "visits.other_audience_position",   snap.get("other_audience_position"),   (st.session_state.get(f"{NS}_ota_pos") or None))
        _add_detail(details, "visits.other_audience_phone",      snap.get("other_audience_phone"),      (st.session_state.get(f"{NS}_ota_phone") or None))
        _add_detail(details, "visits.other_audience_email",      snap.get("other_audience_email"),      (st.session_state.get(f"{NS}_ota_email") or None))

    # ── Live preview ──────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### Changes Preview")
    if not details:
        st.info("No changes detected.")
    else:
        rows_html = "".join(
            compare_row(
                _fmt_field_label(d["field"]),
                _resolve_field_display_value(d["field"], d.get("old_value")),
                _resolve_field_display_value(d["field"], d.get("new_value")),
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
                             border-bottom:1px solid #e4e8ec;">New Value</th>
                </tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>
            """,
            unsafe_allow_html=True,
        )

    # ── Admin note + submit ───────────────────────────────────────────────────
    st.markdown("---")
    admin_note = st.text_area(
        "Admin note (required) *",
        key=f"{NS}_admin_note",
        placeholder="Reason for this forced adjustment.",
    )

    if st.button(
        "⚡ Apply Force Adjustment",
        type="primary",
        disabled=not details,
        key=f"{NS}_submit",
    ):
        errors = []
        if not (admin_note or "").strip():
            errors.append("Admin note is required.")
        if not new_customer_id:
            errors.append("Please select a customer.")
        if not new_bl_id:
            errors.append("Please select a business line.")
        if not new_objective_id:
            errors.append("Please select an objective.")
        if not eval_choice:
            errors.append("Please select an evaluation.")

        # Lat / Lon / Accuracy validation (lat & lon are required)
        _lat_s = lat_val.strip()
        _lon_s = lon_val.strip()
        _acc_s = acc_val.strip()
        if not _lat_s:
            errors.append("Latitude is required.")
        else:
            try:
                _lat_f = float(_lat_s)
                if not (-90 <= _lat_f <= 90):
                    errors.append("Latitude must be between -90 and 90.")
            except ValueError:
                errors.append("Latitude must be a valid number.")
        if not _lon_s:
            errors.append("Longitude is required.")
        else:
            try:
                _lon_f = float(_lon_s)
                if not (-180 <= _lon_f <= 180):
                    errors.append("Longitude must be between -180 and 180.")
            except ValueError:
                errors.append("Longitude must be a valid number.")
        if _acc_s:
            try:
                _acc_f = float(_acc_s)
                if _acc_f < 0:
                    errors.append("Accuracy must be a non-negative number.")
            except ValueError:
                errors.append("Accuracy must be a valid number.")

        # Other audience validation
        if is_other_aud:
            if not (st.session_state.get(f"{NS}_ota_name") or "").strip():
                errors.append("For Other Audience, Name is required.")
            if not (st.session_state.get(f"{NS}_ota_dept") or "").strip():
                errors.append("For Other Audience, Department is required.")
            if not (st.session_state.get(f"{NS}_ota_pos") or "").strip():
                errors.append("For Other Audience, Position is required.")

        for err in errors:
            st.error(err)
        if not errors:
            ok, err_msg = _apply_force_adjustment(visit_id, admin_uid, details, admin_note.strip())
            if ok:
                st.session_state[success_key] = f"Force adjustment applied to V-{visit_id}."
                for k in list(st.session_state.keys()):
                    if k.startswith(f"{NS}_") and k != success_key:
                        del st.session_state[k]
                st.rerun()
            else:
                st.error(f"Failed to apply: {err_msg}")


def page_admin_change_requests():
    set_current_page(PAGE_NS)

    u = st.session_state.get("user") or resolve_session_user()
    if not u:
        st.warning("Please sign in to continue.")
        return

    role = (u.get("role") or "").lower().strip()
    if role != "admin":
        st.warning("You do not have access to this page.")
        return

    uid_raw = u.get("user_id") or u.get("id")
    if not uid_raw:
        st.error("Session user ID could not be resolved.")
        return
    admin_uid = int(uid_raw)

    section_header("Review Change Requests", "Force adjustments, approve or reject rep requests")

    tab_force, tab_review, tab_history = st.tabs(["⚡ Force Adjust", "🔍 Review Pending", "📋 All Requests"])

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 0 — Force Adjustment
    # ──────────────────────────────────────────────────────────────────────────
    with tab_force:
        _render_force_tab(admin_uid)

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 1 — Review Pending
    # ──────────────────────────────────────────────────────────────────────────
    with tab_review:
        success_key = f"{PAGE_NS}_review_success"
        if st.session_state.get(success_key):
            st.success(st.session_state.pop(success_key))

        pending_df = _load_pending()
        count = len(pending_df)

        if count == 0:
            st.info("No pending change requests.")
        else:
            st.markdown(
                status_badge(f"{count} request{'s' if count != 1 else ''} pending review", "warning"),
                unsafe_allow_html=True,
            )
            st.markdown("")

            def _label(row) -> str:
                date_str = pd.to_datetime(row["request_date"]).strftime("%b %d") if pd.notna(row["request_date"]) else "?"
                n = int(row["fields_changed"])
                return f"Request #{int(row['request_id'])} — Visit #{int(row['visit_id'])} — {row['rep_name']} — {date_str} ({n} field{'s' if n != 1 else ''})"

            options = {_label(row): row for _, row in pending_df.iterrows()}
            preselect_id = st.session_state.pop("_admin_preselect_id", None)
            if preselect_id is not None:
                for lbl in options:
                    if f"Request #{preselect_id}" in lbl:
                        st.session_state[f"{PAGE_NS}_sel"] = lbl
                        break
            chosen_label = st.selectbox(
                "Select a request to review:",
                list(options.keys()),
                key=f"{PAGE_NS}_sel",
            )
            sel = options[chosen_label]
            request_id = int(sel["request_id"])
            visit_id   = int(sel["visit_id"])

            ctx = _load_visit_context(visit_id)
            if ctx:
                date_str = pd.to_datetime(ctx.get("submitted_at_local")).strftime("%d/%m/%Y") if pd.notna(ctx.get("submitted_at_local")) else "—"
                st.info(
                    f"**Visit #{visit_id}**  \n"
                    f"Customer: {ctx.get('customer_name', '—')}  \n"
                    f"Rep: {ctx.get('rep_name', '—')}  \n"
                    f"Date: {date_str}  \n"
                    f"Business Line: {ctx.get('business_line', '—')}"
                )

            if pd.notna(sel.get("request_note")) and sel.get("request_note"):
                st.info(f"**Rep note:** \"{sel['request_note']}\"")

            diff_df = _load_diff(request_id)
            if not diff_df.empty:
                _render_diff_table(diff_df)
            else:
                st.warning("No field details found for this request.")

            st.markdown("---")
            col_approve, col_reject = st.columns(2)

            with col_approve:
                st.markdown("**Approve**")
                if st.button("✅ Approve & Apply Changes", type="primary", key=f"{PAGE_NS}_approve_{request_id}"):
                    ok, err = _apply_changes(request_id, visit_id, admin_uid)
                    if ok:
                        st.session_state[success_key] = f"Request #{request_id} approved — changes applied to Visit #{visit_id}."
                        st.rerun()
                    else:
                        st.error(f"Apply failed: {err}")

            with col_reject:
                st.markdown("**Reject**")
                reject_note = st.text_area(
                    "Rejection reason (required)",
                    key=f"{PAGE_NS}_reject_note_{request_id}",
                    placeholder="Explain why the request is rejected.",
                )
                if st.button("❌ Reject Request", type="secondary", key=f"{PAGE_NS}_reject_{request_id}"):
                    if not reject_note or not reject_note.strip():
                        st.error("Rejection reason is required.")
                    else:
                        exec_sql(
                            """
                            UPDATE request_changes
                            SET status = 'REJECTED',
                                reject_note = :note,
                                resolve_date = NOW(),
                                changed_by = :admin_uid
                            WHERE request_id = :rid
                            """,
                            {"note": reject_note.strip(), "admin_uid": admin_uid, "rid": request_id},
                        )
                        st.session_state[success_key] = f"Request #{request_id} rejected."
                        st.rerun()

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 2 — All Requests (History)
    # ──────────────────────────────────────────────────────────────────────────
    with tab_history:
        _render_history_tab()


_STATUS_LABEL = {
    "APPROVED":  "Approved",
    "REJECTED":  "Rejected",
    "IN_REVIEW": "In Review",
    "WITHDRAWN": "Withdrawn",
    "DELETED":   "Deleted",
}


def _visit_status_summary(visit_rows: pd.DataFrame) -> str:
    counts = visit_rows["status"].value_counts()
    parts = []
    for status in ["IN_REVIEW", "APPROVED", "REJECTED", "WITHDRAWN", "DELETED"]:
        n = counts.get(status, 0)
        if n:
            label = _STATUS_LABEL.get(status, status)
            parts.append(f"{n} {label}")
    return " · ".join(parts) if parts else ""


def _render_visit_groups(df: pd.DataFrame) -> None:
    for visit_id, group in df.groupby("visit_id", sort=False):
        first = group.iloc[0]
        customer  = str(first.get("customer_name") or "—")
        rep       = str(first.get("rep_name") or "—")
        visit_dt  = first.get("visit_date")
        visit_date_str = (
            pd.to_datetime(visit_dt, errors="coerce").strftime("%d %b %Y")
            if pd.notna(visit_dt) else "—"
        )
        summary = _visit_status_summary(group)

        expander_label = (
            f"V-{visit_id}  ·  {customer}  ·  {rep}  ·  {visit_date_str}"
            + (f"        {summary}" if summary else "")
        )

        with st.expander(expander_label):
            _render_request_timeline(group)


def _render_request_timeline(group: pd.DataFrame) -> None:
    _BADGE_VARIANT = {
        "APPROVED":  "success",
        "REJECTED":  "danger",
        "IN_REVIEW": "warning",
        "WITHDRAWN": "neutral",
        "DELETED":   "danger",
    }

    total = len(group)
    for i, (_, row) in enumerate(group.iterrows()):
        request_id  = int(row["request_id"])
        status_val  = str(row["status"])
        req_date    = row["request_date"]
        req_date_str = (
            pd.to_datetime(req_date, errors="coerce").strftime("%d %b %Y, %H:%M")
            if pd.notna(req_date) else "—"
        )

        # ── Request header ────────────────────────────────────────────────────
        change_source = str(row.get("change_source", "")).upper()
        is_delete = status_val == "DELETED"
        is_force  = change_source == "FORCE" and not is_delete
        badge = status_badge(status_val, _BADGE_VARIANT.get(status_val, "neutral"))
        if is_delete:
            source_badge = (
                '<span style="font-size:0.75rem;font-weight:600;color:#991b1b;'
                'background:#fee2e2;border:1px solid #fca5a5;border-radius:4px;'
                'padding:1px 7px;">🗑️ Deleted</span>'
            )
        elif is_force:
            source_badge = (
                '<span style="font-size:0.75rem;font-weight:600;color:#b45309;'
                'background:#fef3c7;border:1px solid #fcd34d;border-radius:4px;'
                'padding:1px 7px;">⚡ Force</span>'
            )
        else:
            source_badge = (
                '<span style="font-size:0.75rem;font-weight:600;color:#1d4ed8;'
                'background:#eff6ff;border:1px solid #bfdbfe;border-radius:4px;'
                'padding:1px 7px;">👤 Rep</span>'
            )
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:10px;'
            f'margin-bottom:6px;">'
            f'<span style="font-size:0.875rem;font-weight:600;color:#0d1117;">'
            f'Request #{request_id}</span>'
            f'<span style="font-size:0.8rem;color:#8b949e;">{req_date_str}</span>'
            f'{source_badge}'
            f'{badge}'
            f'</div>',
            unsafe_allow_html=True,
        )

        # ── Rep note ──────────────────────────────────────────────────────────
        req_note = str(row.get("request_note") or "").strip()
        if req_note:
            st.markdown(
                f'<p style="font-size:0.85rem;color:#57606a;'
                f'font-style:italic;margin:0 0 8px 0;">"{req_note}"</p>',
                unsafe_allow_html=True,
            )

        # ── Diff table (skip for DELETE — no field changes) ───────────────────
        if not is_delete:
            diff_df = _load_diff(request_id)
            if not diff_df.empty:
                _render_diff_table(
                    diff_df,
                    before_label="Before" if is_force else "Original",
                    after_label="After" if is_force else "Requested",
                )
            else:
                st.caption("No field details recorded.")

        # ── Resolution line ───────────────────────────────────────────────────
        if status_val == "DELETED":
            applied_str = (
                pd.to_datetime(row.get("applied_at"), errors="coerce").strftime("%d %b %Y, %H:%M")
                if pd.notna(row.get("applied_at")) else "—"
            )
            resolver = str(row.get("resolved_by") or "—")
            st.error(f"Deleted by {resolver} on {applied_str}")

        elif status_val == "APPROVED":
            applied_str = (
                pd.to_datetime(row.get("applied_at"), errors="coerce").strftime("%d %b %Y, %H:%M")
                if pd.notna(row.get("applied_at")) else "—"
            )
            resolver = str(row.get("resolved_by") or "—")
            st.success(f"Approved by {resolver} on {applied_str}")

        elif status_val == "REJECTED":
            reject_note = str(row.get("reject_note") or "").strip()
            if reject_note:
                st.error(f"Rejected: {reject_note}")
            else:
                st.error("Rejected.")

        elif status_val == "IN_REVIEW":
            st.info("Pending review")

        elif status_val == "WITHDRAWN":
            resolve_str = (
                pd.to_datetime(row.get("resolve_date"), errors="coerce").strftime("%d %b %Y")
                if pd.notna(row.get("resolve_date")) else "—"
            )
            st.caption(f"Withdrawn on {resolve_str}")

        if pd.notna(row.get("apply_error")) and str(row.get("apply_error")).strip():
            st.warning(f"Apply error: {row['apply_error']}")

        # ── Divider between requests (not after last) ─────────────────────────
        if i < total - 1:
            st.markdown(
                '<hr style="border:none;border-top:1px solid #e4e8ec;margin:12px 0;">',
                unsafe_allow_html=True,
            )


def _render_history_tab():
    all_df = query_df(
        """
        SELECT
          rc.request_id,
          rc.visit_id,
          rep.name             AS rep_name,
          rc.request_date,
          rc.status,
          COUNT(rcd.detail_id) AS fields_changed,
          resolver.name        AS resolved_by,
          rc.resolve_date,
          rc.applied_at,
          rc.apply_error,
          rc.request_note,
          rc.reject_note,
          rc.change_source,
          c.account_name       AS customer_name,
          v.submitted_at_local AS visit_date
        FROM request_changes rc
        JOIN users rep ON rep.user_id = rc.requested_by
        LEFT JOIN users resolver ON resolver.user_id = rc.changed_by
        LEFT JOIN request_change_details rcd ON rcd.request_id = rc.request_id
        JOIN visits v ON v.visit_id = rc.visit_id
        JOIN customers c ON c.customer_id = v.customer_id
        GROUP BY rc.request_id, rc.visit_id, rep.name, rc.request_date, rc.status,
                 rc.applied_at, rc.apply_error, rc.request_note, rc.reject_note,
                 rc.resolve_date, resolver.name, rc.change_source, c.account_name, v.submitted_at_local
        ORDER BY rc.request_date DESC
        """
    )

    if all_df.empty:
        st.info("No change requests found.")
        return

    # ── Status filter ─────────────────────────────────────────────────────────
    status_opts = ["All"] + sorted(all_df["status"].unique().tolist())
    status_filter = st.selectbox("Filter by status:", status_opts, key=f"{PAGE_NS}_hist_filter")

    if status_filter != "All":
        # Keep only visits that have at least one request with this status
        visit_ids_with_status = all_df[all_df["status"] == status_filter]["visit_id"].unique()
        all_df = all_df[all_df["visit_id"].isin(visit_ids_with_status)]

    if all_df.empty:
        st.info(f"No requests with status: {status_filter}")
        return

    # ── Group by visit ────────────────────────────────────────────────────────
    # Sort so most-recently-active visit comes first; within each visit,
    # requests are oldest→newest for the timeline.
    all_df["request_date"] = pd.to_datetime(all_df["request_date"], errors="coerce")
    all_df["visit_date"]   = pd.to_datetime(all_df["visit_date"],   errors="coerce")

    latest_per_visit = (
        all_df.groupby("visit_id")["request_date"].max().rename("latest_req_date")
    )
    all_df = all_df.join(latest_per_visit, on="visit_id")
    all_df = all_df.sort_values(
        ["latest_req_date", "visit_id", "request_date"],
        ascending=[False, False, True],
    )

    # ── Pagination (by visit group) ───────────────────────────────────────────
    PAGE_SIZE = 10
    visit_ids_ordered = list(dict.fromkeys(all_df["visit_id"].tolist()))
    total_visits = len(visit_ids_ordered)

    filter_key = status_filter
    if st.session_state.get("_hist_filter_key") != filter_key:
        st.session_state["_hist_filter_key"] = filter_key
        st.session_state["_hist_page"] = 0

    current_page = st.session_state.get("_hist_page", 0)
    total_pages  = max(1, (total_visits + PAGE_SIZE - 1) // PAGE_SIZE)
    current_page = min(current_page, total_pages - 1)

    start_idx = current_page * PAGE_SIZE
    end_idx   = min(start_idx + PAGE_SIZE, total_visits)
    page_visit_ids = visit_ids_ordered[start_idx:end_idx]

    st.markdown(
        f'<p style="font-size:0.8rem;color:#8b949e;margin:6px 0 8px;">'
        f'Showing visits {start_idx + 1}–{end_idx} of {total_visits:,}</p>',
        unsafe_allow_html=True,
    )

    page_df = all_df[all_df["visit_id"].isin(page_visit_ids)]
    _render_visit_groups(page_df)

    if total_pages > 1:
        col_prev, col_info, col_next = st.columns([1, 2, 1])
        with col_prev:
            if st.button("← Prev", key="hist_prev", disabled=(current_page == 0),
                         use_container_width=True):
                st.session_state["_hist_page"] = current_page - 1
                st.rerun()
        with col_info:
            st.markdown(
                f'<p style="text-align:center;font-size:0.85rem;color:#57606a;'
                f'padding-top:0.4rem;">Page {current_page + 1} of {total_pages}</p>',
                unsafe_allow_html=True,
            )
        with col_next:
            if st.button("Next →", key="hist_next", disabled=(current_page >= total_pages - 1),
                         use_container_width=True):
                st.session_state["_hist_page"] = current_page + 1
                st.rerun()
