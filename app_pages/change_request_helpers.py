# app_pages/change_request_helpers.py
# Shared DB helpers used by both change_request.py and admin_change_requests.py.
import pandas as pd

from db_ops import query_df


def _norm(x) -> str:
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


def _add_detail(details: list[dict], field: str, old, new):
    if old is None and new is None:
        return
    if str(old) == str(new):
        return
    details.append({
        "field": field,
        "old_value": None if old is None else str(old),
        "new_value": None if new is None else str(new),
    })


def _load_bu_options() -> list[str]:
    df = query_df("SELECT name FROM business_units WHERE is_active IS TRUE ORDER BY name")
    return ([""] + df["name"].astype(str).tolist()) if not df.empty else [""]


def _bu_id_from_name(bu_name: str):
    bu_name = _norm(bu_name)
    if not bu_name:
        return None
    df = query_df(
        "SELECT business_unit_id FROM business_units WHERE is_active IS TRUE AND trim(name) = :n LIMIT 1",
        {"n": bu_name},
    )
    return int(df.iloc[0]["business_unit_id"]) if not df.empty else None


def _load_category_options(bu_id) -> list[str]:
    if not bu_id:
        return [""]
    df = query_df(
        """
        SELECT DISTINCT category FROM business_lines
        WHERE is_active IS TRUE AND business_unit_id = :bid
          AND category IS NOT NULL AND trim(category) <> ''
        ORDER BY category
        """,
        {"bid": int(bu_id)},
    )
    return ([""] + df["category"].astype(str).tolist()) if not df.empty else [""]


def _load_bl_options(bu_id, category: str) -> list[str]:
    category = _norm(category)
    if not bu_id or not category:
        return [""]
    df = query_df(
        """
        SELECT name FROM business_lines
        WHERE is_active IS TRUE AND business_unit_id = :bid AND category = :cat
        ORDER BY name
        """,
        {"bid": int(bu_id), "cat": category},
    )
    return ([""] + df["name"].astype(str).tolist()) if not df.empty else [""]


def _bl_id_from_name(bu_id, category: str, bl_name: str):
    category = _norm(category)
    bl_name = _norm(bl_name)
    if not (bu_id and category and bl_name):
        return None
    df = query_df(
        """
        SELECT business_line_id FROM business_lines
        WHERE is_active IS TRUE AND business_unit_id = :bid
          AND category = :cat AND trim(name) = :nm LIMIT 1
        """,
        {"bid": int(bu_id), "cat": category, "nm": bl_name},
    )
    return int(df.iloc[0]["business_line_id"]) if not df.empty else None


def _load_product_options(bl_id) -> list[str]:
    if not bl_id:
        return [""]
    df = query_df(
        """
        SELECT product_id, article_number, description FROM items
        WHERE is_active IS TRUE AND business_line_id = :blid
        ORDER BY COALESCE(article_number, product_id)
        """,
        {"blid": int(bl_id)},
    )
    labels = [""]
    for _, r in df.iterrows():
        art = r["article_number"] if pd.notna(r["article_number"]) else r["product_id"]
        desc = str(r["description"]).strip() if pd.notna(r["description"]) else ""
        labels.append(f"{art} — {desc}" if desc else str(art))
    return labels


def _product_id_from_label(bl_id, label: str):
    label = _norm(label)
    if not label:
        return None
    df = query_df(
        "SELECT product_id, article_number, description FROM items WHERE is_active IS TRUE AND business_line_id = :blid",
        {"blid": int(bl_id)},
    )
    for _, r in df.iterrows():
        art = r["article_number"] if pd.notna(r["article_number"]) else r["product_id"]
        desc = str(r["description"]).strip() if pd.notna(r["description"]) else ""
        lbl = f"{art} — {desc}" if desc else str(art)
        if _norm(lbl) == label:
            return str(r["product_id"])
    return None


def _fmt_audience(row) -> str:
    title = (str(row["title"]).strip() + " ") if pd.notna(row["title"]) and str(row["title"]).strip() else ""
    name = str(row["name"]).strip() if pd.notna(row["name"]) else ""
    parts = [(title + name).strip()]
    if pd.notna(row["department"]) and str(row["department"]).strip():
        parts.append(str(row["department"]).strip())
    if pd.notna(row["position"]) and str(row["position"]).strip():
        parts.append(str(row["position"]).strip())
    return " || ".join(p for p in parts if p)


def _audience_label_for_id(audience_id: int) -> str:
    df = query_df(
        "SELECT title, name, department, position FROM target_audiences WHERE audience_id = :aid",
        {"aid": int(audience_id)},
    )
    if df.empty:
        return ""
    return _fmt_audience(df.iloc[0])


def _load_audience_options(customer_id: int, include_other: bool = False) -> list[str]:
    df = query_df(
        """
        SELECT title, name, department, position
        FROM target_audiences
        WHERE is_active IS TRUE AND customer_id = :cid ORDER BY name
        """,
        {"cid": int(customer_id)},
    )
    labels = [""] + [_fmt_audience(r) for _, r in df.iterrows()]
    if include_other:
        labels.append("Other")
    return labels


def _resolve_audience_id_from_label(customer_id: int, label: str):
    label = _norm(label)
    if not label or label == "Other":
        return None
    df = query_df(
        """
        SELECT audience_id, title, name, department, position
        FROM target_audiences WHERE is_active IS TRUE AND customer_id = :cid
        """,
        {"cid": int(customer_id)},
    )
    if df.empty:
        return None
    for _, r in df.iterrows():
        if _norm(_fmt_audience(r)) == label:
            return int(r["audience_id"])
    return None


def _infer_bu_cat_bl(bl_id: int) -> dict:
    df = query_df(
        """
        SELECT bl.name AS bl_name, bl.category, bu.business_unit_id, bu.name AS bu_name
        FROM business_lines bl
        JOIN business_units bu ON bu.business_unit_id = bl.business_unit_id
        WHERE bl.business_line_id = :blid LIMIT 1
        """,
        {"blid": int(bl_id)},
    )
    if df.empty:
        return {"bu_name": "", "bu_id": None, "category": "", "bl_name": ""}
    r = df.iloc[0].to_dict()
    return {
        "bu_name": _norm(r.get("bu_name")),
        "bu_id": int(r["business_unit_id"]) if r.get("business_unit_id") is not None else None,
        "category": _norm(r.get("category")),
        "bl_name": _norm(r.get("bl_name")),
    }


def _load_other_dept_options() -> list[str]:
    df = query_df(
        """
        SELECT DISTINCT department FROM target_audiences
        WHERE COALESCE(is_active, TRUE) IS TRUE
          AND department IS NOT NULL AND trim(department) <> ''
        ORDER BY department
        """
    )
    return [""] + (df["department"].astype(str).tolist() if not df.empty else [])


def _load_other_position_options() -> list[str]:
    df = query_df(
        """
        SELECT DISTINCT position FROM target_audiences
        WHERE COALESCE(is_active, TRUE) IS TRUE
          AND position IS NOT NULL AND trim(position) <> ''
        ORDER BY position
        """
    )
    return [""] + (df["position"].astype(str).tolist() if not df.empty else [])


def _objective_id_from_name(obj_name: str):
    obj_name = _norm(obj_name)
    if not obj_name:
        return None
    df = query_df(
        "SELECT objective_id FROM objectives WHERE trim(name) = :n LIMIT 1",
        {"n": obj_name},
    )
    return int(df.iloc[0]["objective_id"]) if not df.empty else None


# ── Display helpers ────────────────────────────────────────────────────────────

_FIELD_LABELS = {
    "visits.customer_id":               "Customer",
    "visits.audience_id":               "Target Audience",
    "visits.business_line_id":          "Business Line",
    "visits.product_id":                "Product",
    "visits.objective_id":              "Objective",
    "visits.notes":                     "Notes",
    "visits.evaluation":                "Evaluation",
    "visits.other_audience_title":      "Audience Title",
    "visits.other_audience_name":       "Audience Name",
    "visits.other_audience_department": "Audience Department",
    "visits.other_audience_position":   "Audience Position",
    "visits.other_audience_phone":      "Audience Phone",
    "visits.other_audience_email":      "Audience Email",
    "home_visits.patient_name":         "Patient Name",
    "home_visits.patient_phone":        "Patient Phone",
    "home_visits.serial_no":            "Device Serial #",
    "_display.category":                "Product Category",
    "_display.business_unit":           "Business Unit",
}


def _fmt_field_label(field: str) -> str:
    return _FIELD_LABELS.get(field, field.split(".")[-1].replace("_", " ").title())


def _product_label_for_id(product_id) -> str:
    if not product_id:
        return ""
    pid = str(product_id).strip()
    if not pid:
        return ""
    df = query_df(
        "SELECT article_number, description FROM items WHERE product_id = :pid LIMIT 1",
        {"pid": pid},
    )
    if df.empty:
        return pid
    r = df.iloc[0]
    art = r["article_number"] if pd.notna(r["article_number"]) else pid
    desc = str(r["description"]).strip() if pd.notna(r["description"]) else ""
    return f"{art} — {desc}" if desc else str(art)


def _objective_name_for_id_safe(objective_id) -> str:
    if not objective_id:
        return ""
    try:
        oid = int(str(objective_id).strip())
    except (ValueError, TypeError):
        return str(objective_id)
    df = query_df(
        "SELECT name FROM objectives WHERE objective_id = :oid LIMIT 1",
        {"oid": oid},
    )
    return str(df.iloc[0]["name"]) if not df.empty else str(objective_id)


def _resolve_field_display_value(field: str, raw_value) -> str:
    """Convert a stored raw ID/value to a human-readable string for diff tables."""
    if raw_value is None:
        return "—"
    try:
        if pd.isna(raw_value):
            return "—"
    except Exception:
        pass
    val = str(raw_value).strip()
    if not val:
        return "—"

    if field == "visits.customer_id":
        try:
            df = query_df(
                "SELECT account_name FROM customers WHERE customer_id = :cid LIMIT 1",
                {"cid": int(val)},
            )
            return str(df.iloc[0]["account_name"]) if not df.empty else val
        except Exception:
            return val
    elif field == "visits.audience_id":
        try:
            label = _audience_label_for_id(int(val))
            return label or val
        except Exception:
            return val
    elif field == "visits.business_line_id":
        try:
            info = _infer_bu_cat_bl(int(val))
            return info.get("bl_name") or val
        except Exception:
            return val
    elif field == "visits.product_id":
        return _product_label_for_id(val) or val
    elif field == "visits.objective_id":
        return _objective_name_for_id_safe(val)
    return val
