# pages/review_customers.py
import json
import pandas as pd
import streamlit as st
from sqlalchemy import text

from auth import resolve_session_user
from config import TIMEZONE
from db import engine
from db_ops import query_df, exec_sql
from utils import _utcnow_iso
from widgets import set_current_page
from ui import section_header, status_badge


@st.cache_data(ttl=300)
def _cached_sector_choices() -> list:
    df = query_df(
        """
        SELECT DISTINCT sector FROM customers
        WHERE sector IS NOT NULL AND trim(sector) <> ''
        ORDER BY sector
        """
    )
    return [str(r.sector).strip() for r in df.itertuples(index=False) if str(r.sector).strip()] if not df.empty else []


@st.cache_data(ttl=300)
def _cached_region_choices() -> list:
    df = query_df(
        """
        SELECT DISTINCT region FROM customers
        WHERE region IS NOT NULL AND trim(region) <> ''
        ORDER BY region
        """
    )
    return [str(r.region).strip() for r in df.itertuples(index=False) if str(r.region).strip()] if not df.empty else []


def page_review_other_customers():
    """
    Admin / manager page to review visits where selected customer is the placeholder "Other" customer (customer_id=807).
    - Shows ALL visits where v.customer_id = 807 (no other_customer_name filter).
    - Uses a resolved name for matching:
        resolved_other_name = other_customer_name if present else notes (legacy)
    - Suggests closest matches from ALL active customers (excluding 807) using name similarity only.
    - Allows linking visit to an existing customer (updates visits.customer_id),
      or creating a new customer then linking the visit.
    - Once linked, the visit disappears from this page because customer_id != 807.
    """
    import html as _html
    import pandas as pd
    from difflib import SequenceMatcher

    PAGE_NS = "review_other_cust"
    set_current_page(PAGE_NS)

    OTHER_CUSTOMER_ID = 807

    success_key = f"{PAGE_NS}_last_success"
    st.session_state.setdefault(success_key, "")

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

    section_header("Review Other Customers", "Manage visits where the customer was recorded as 'Other'")

    last_msg = st.session_state.get(success_key) or ""
    if last_msg:
        st.success(last_msg)
        st.session_state[success_key] = ""

    # ------------- Similarity helpers (NAME ONLY) -------------
    def normalize_customer(s: str) -> str:
        if not s:
            return ""
        s = str(s).strip().lower()
        s = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in s)
        s = " ".join(s.split())

        # light stopwords (tune if needed)
        stop = {
            "medical", "center", "centre", "complex", "company", "co", "ltd", "est", "est.",
            "trading", "hospital", "clinic", "pharmacy", "specialized", "specialised"
        }
        tokens = [t for t in s.split() if t not in stop]
        return " ".join(tokens)

    def string_similarity(a: str, b: str) -> float:
        a_norm, b_norm = normalize_customer(a), normalize_customer(b)
        if not a_norm or not b_norm:
            return 0.0
        return SequenceMatcher(None, a_norm, b_norm).ratio()

    # ------------- Load unresolved visits (ONLY customer_id=807) -------------
    unresolved_df = query_df(
        """
        SELECT
            v.visit_id,
            v.customer_id,
            c.account_name              AS selected_customer_name,
            v.other_customer_name,
            v.submitted_at_local,
            v.notes                     AS visit_notes,
            v.user_id,
            u.name                      AS rep_name,
            u.email                     AS rep_email,
            c.region,
            c.city,
            c.sector,
            bu.name                     AS business_unit_name
        FROM visits v
        JOIN customers c ON c.customer_id = v.customer_id
        JOIN users u     ON u.user_id     = v.user_id
        LEFT JOIN business_units bu ON bu.business_unit_id = u.business_unit_id
        WHERE v.customer_id = :other_id
        ORDER BY v.submitted_at_local DESC, v.visit_id DESC
        """,
        {"other_id": int(OTHER_CUSTOMER_ID)},
    )

    if unresolved_df.empty:
        st.success(f"✅ No visits pending review for Other Customer (customer_id={OTHER_CUSTOMER_ID}).")
        return

    unresolved_df["submitted_at_local"] = pd.to_datetime(
        unresolved_df["submitted_at_local"], errors="coerce"
    ).dt.strftime("%d/%m/%Y %H:%M")

    # Prefer structured other_customer_name, otherwise fallback to notes (legacy)
    def _resolved_other_name(row) -> str:
        ocn = row.get("other_customer_name")
        if ocn is not None and str(ocn).strip():
            return str(ocn).strip()
        return str(row.get("visit_notes") or "").strip()

    unresolved_df["resolved_other_name"] = unresolved_df.apply(_resolved_other_name, axis=1)

    _pending_n = len(unresolved_df)
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:0.6rem;margin:1rem 0 0.2rem;">'
        f'<span style="font-size:1rem;font-weight:700;color:var(--color-text);">Visits Pending Review</span>'
        f'<span style="background:var(--color-primary-subtle);color:var(--color-primary);border-radius:6px;padding:1px 8px;'
        f'font-size:0.78rem;font-weight:700;">{_pending_n} pending</span>'
        f'</div>'
        f'<p style="font-size:0.82rem;color:var(--color-text-subtle);margin:0 0 0.75rem;">'
        f'These visits currently have customer_id = {OTHER_CUSTOMER_ID}. '
        f'Once you link them, they disappear automatically.</p>',
        unsafe_allow_html=True,
    )

    display_df = unresolved_df.rename(
        columns={
            "selected_customer_name": "Selected Customer",
            "submitted_at_local": "Visit Date/Time",
            "visit_notes": "Notes",
            "rep_name": "Submitted By",
            "rep_email": "Email",
            "business_unit_name": "Business Unit",
        }
    )

    # show resolved name column
    display_df["Provided Customer Name (Other/Notes)"] = unresolved_df["resolved_other_name"]

    st.dataframe(
        display_df[
            [
                "visit_id",
                "Selected Customer",
                "Provided Customer Name (Other/Notes)",
                "Visit Date/Time",
                "region",
                "city",
                "sector",
                "Notes",
                "Submitted By",
                "Email",
                "Business Unit",
            ]
        ],
        width="stretch",
        hide_index=True,
    )

    # ------------- Pick a visit to review -------------
    st.markdown('<hr style="border:none;border-top:1px solid var(--color-border);margin:1.5rem 0 1rem;">', unsafe_allow_html=True)
    st.markdown(
        '<p style="font-size:1rem;font-weight:700;color:var(--color-text);margin:0 0 0.75rem;">'
        'Review &amp; Resolve One Visit</p>',
        unsafe_allow_html=True,
    )

    visit_labels = []
    visit_id_map = {}
    for _, row in unresolved_df.iterrows():
        label = f"{int(row['visit_id'])} — {row['resolved_other_name'] or '(no name provided)'} — {row['submitted_at_local']}"
        visit_labels.append(label)
        visit_id_map[label] = int(row["visit_id"])

    preselect_id = st.session_state.pop("_admin_preselect_id", None)
    if preselect_id is not None:
        for lbl, vid in visit_id_map.items():
            if vid == preselect_id:
                st.session_state[f"{PAGE_NS}_visit_sel"] = lbl
                break

    selected_label = st.selectbox(
        "Select a visit to review",
        options=[""] + visit_labels,
        index=0,
        key=f"{PAGE_NS}_visit_sel",
    )

    if not selected_label:
        st.info("Please select a visit from the list above to start reviewing.")
        return

    selected_visit_id = visit_id_map[selected_label]
    visit_row = unresolved_df.loc[unresolved_df["visit_id"] == selected_visit_id].iloc[0]

    other_name = (visit_row.get("resolved_other_name") or "").strip()

    _esc = _html.escape
    st.markdown(
        f'<div style="background:var(--color-surface);border:1px solid var(--color-border);border-radius:12px;'
        f'padding:1rem 1.25rem;margin:.5rem 0 1rem;box-shadow:0 1px 2px rgba(15,23,42,.04);">'
        f'<div style="font-size:.7rem;font-weight:700;color:var(--color-primary);'
        f'text-transform:uppercase;letter-spacing:.08em;margin-bottom:.65rem;">'
        f'Visit #{selected_visit_id}</div>'
        f'<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(175px,1fr));gap:.45rem 1.5rem;">'
        f'<div><div style="font-size:.7rem;color:var(--color-text-subtle);">Provided Name</div>'
        f'<div style="font-size:.875rem;font-weight:600;color:var(--color-text);">{_esc(other_name or "—")}</div></div>'
        f'<div><div style="font-size:.7rem;color:var(--color-text-subtle);">Submitted By</div>'
        f'<div style="font-size:.875rem;color:var(--color-text);">{_esc(str(visit_row["rep_name"]))} '
        f'({_esc(str(visit_row["rep_email"]))})</div></div>'
        f'<div><div style="font-size:.7rem;color:var(--color-text-subtle);">Location</div>'
        f'<div style="font-size:.875rem;color:var(--color-text);">'
        f'{_esc(str(visit_row.get("region") or "—"))} / '
        f'{_esc(str(visit_row.get("city") or "—"))} / '
        f'{_esc(str(visit_row.get("sector") or "—"))}</div></div>'
        f'<div><div style="font-size:.7rem;color:var(--color-text-subtle);">Business Unit</div>'
        f'<div style="font-size:.875rem;color:var(--color-text);">{_esc(str(visit_row.get("business_unit_name") or "—"))}</div></div>'
        f'</div></div>',
        unsafe_allow_html=True,
    )

    # ------------- Load ALL candidate customers (NO region/city/sector filter) -------------
    candidates_df = query_df(
        """
        SELECT customer_id, account_name, sector, region, city, is_active, latitude, longitude, party_id, account_id
        FROM customers
        WHERE COALESCE(is_active, TRUE) IS TRUE
          AND customer_id <> :other_id
        ORDER BY account_name
        """,
        {"other_id": int(OTHER_CUSTOMER_ID)},
    )

    # ------------- Suggested matches (NAME ONLY) -------------
    st.markdown(
        '<p style="font-size:.875rem;font-weight:700;color:var(--color-text);margin:.75rem 0 .1rem;">'
        'Suggested Matches</p>'
        '<p style="font-size:.78rem;color:var(--color-text-subtle);margin:0 0 .5rem;">'
        'Top 15 matches sorted by name similarity (account_name vs provided name).</p>',
        unsafe_allow_html=True,
    )

    existing_options = []
    if candidates_df.empty:
        st.warning("No customers available to match against.")
    else:
        candidates_df["similarity"] = candidates_df.apply(
            lambda r: string_similarity(other_name, r["account_name"]),
            axis=1,
        )
        candidates_df = candidates_df.sort_values(by="similarity", ascending=False)

        top_df = candidates_df.head(15).copy()
        top_df["Similarity"] = top_df["similarity"].map(lambda x: f"{x*100:.0f}%")

        st.dataframe(
            top_df[["customer_id", "account_name", "Similarity", "region", "city", "sector", "account_id", "party_id"]],
            width="stretch",
            hide_index=True,
        )

        for r in top_df.itertuples(index=False):
            existing_options.append(
                f"{int(r.customer_id)} — {r.account_name} — {float(r.similarity)*100:.0f}%"
            )

    # ------------- Actions -------------
    col_link, col_new = st.columns(2)

    # --- Link to existing customer ---
    with col_link:
        st.markdown(
            '<p style="font-size:.875rem;font-weight:700;color:var(--color-text);margin:0 0 .5rem;">'
            '🔗 Link to Existing Customer</p>',
            unsafe_allow_html=True,
        )

        existing_sel = st.selectbox(
            "Existing Customer",
            options=[""] + existing_options,
            index=0,
            key=f"{PAGE_NS}_existing_customer_sel",
            help="Pick an existing customer, then click 'Link to Selected'.",
        )

        link_confirm_key = f"{PAGE_NS}_confirm_link_{selected_visit_id}"
        confirm_link = st.checkbox(
            "I confirm this is the correct match.",
            key=link_confirm_key,
        )

        if st.button("✅ Link to Selected", key=f"{PAGE_NS}_link_btn"):
            if not existing_sel:
                st.error("Please select an existing customer first.")
            elif not confirm_link:
                st.error("Please tick the confirmation checkbox before linking.")
            else:
                sel_id_str = existing_sel.split("—", 1)[0].strip()
                try:
                    new_customer_id = int(sel_id_str)
                except ValueError:
                    st.error("Could not parse selected Customer ID.")
                    return

                try:
                    with engine.begin() as conn:
                        conn.execute(
                            text("""
                                UPDATE visits
                                SET customer_id = :cid
                                WHERE visit_id = :vid
                            """),
                            {"cid": new_customer_id, "vid": selected_visit_id},
                        )

                    st.session_state[success_key] = (
                        f"Linked visit #{selected_visit_id} to Customer ID {new_customer_id} ✅"
                    )
                    st.rerun()
                except Exception as e:
                    st.error("Failed to link visit to customer.")
                    st.caption(str(e))

    # --- Create new customer and link (WITH dropdowns for Sector/Region/City) ---
    with col_new:
        st.markdown(
            '<p style="font-size:.875rem;font-weight:700;color:var(--color-text);margin:0 0 .5rem;">'
            '🆕 Create New Customer &amp; Link</p>',
            unsafe_allow_html=True,
        )

        st.caption(
            "Creates a new customer record and links this visit to it. "
            "After linking, this visit will no longer appear here."
        )

        # ---------------------------------------------------------------
        # Common dropdown data for sectors / regions (from existing data)
        # ---------------------------------------------------------------
        sector_values  = _cached_sector_choices()
        sector_options = [""] + sector_values + ["OTHER"]

        region_values  = _cached_region_choices()
        region_options = [""] + region_values + ["OTHER"]

        # -------------------------
        # Keys (visit-scoped)
        # -------------------------
        name_key         = f"{PAGE_NS}_new_name_{selected_visit_id}"
        sector_opt_key   = f"{PAGE_NS}_new_sector_opt_{selected_visit_id}"
        sector_other_key = f"{PAGE_NS}_new_sector_other_{selected_visit_id}"
        region_opt_key   = f"{PAGE_NS}_new_region_opt_{selected_visit_id}"
        region_other_key = f"{PAGE_NS}_new_region_other_{selected_visit_id}"
        city_opt_key     = f"{PAGE_NS}_new_city_opt_{selected_visit_id}"
        city_other_key   = f"{PAGE_NS}_new_city_other_{selected_visit_id}"

        acctid_key       = f"{PAGE_NS}_new_account_id_{selected_visit_id}"
        partyid_key      = f"{PAGE_NS}_new_party_id_{selected_visit_id}"
        lat_key          = f"{PAGE_NS}_new_lat_{selected_visit_id}"
        lon_key          = f"{PAGE_NS}_new_lon_{selected_visit_id}"

        confirm_key      = f"{PAGE_NS}_confirm_new_{selected_visit_id}"

        # -------------------------
        # Init defaults once
        # -------------------------
        st.session_state.setdefault(name_key, (other_name or ""))
        st.session_state.setdefault(sector_opt_key, "")
        st.session_state.setdefault(sector_other_key, "")
        st.session_state.setdefault(region_opt_key, "")
        st.session_state.setdefault(region_other_key, "")
        st.session_state.setdefault(city_opt_key, "")
        st.session_state.setdefault(city_other_key, "")

        st.session_state.setdefault(acctid_key, "")
        st.session_state.setdefault(partyid_key, "")
        st.session_state.setdefault(lat_key, "")
        st.session_state.setdefault(lon_key, "")

        # Optional: prefill sector/region/city from visit if they exist in customers table values
        # (won't affect similarity; just helps data quality)
        visit_sector = (visit_row.get("sector") or "").strip()
        visit_region = (visit_row.get("region") or "").strip()
        visit_city   = (visit_row.get("city") or "").strip()

        if st.session_state.get(sector_opt_key, "") == "" and visit_sector:
            st.session_state[sector_opt_key] = visit_sector if visit_sector in sector_options else "OTHER"
            if st.session_state[sector_opt_key] == "OTHER":
                st.session_state[sector_other_key] = visit_sector

        if st.session_state.get(region_opt_key, "") == "" and visit_region:
            st.session_state[region_opt_key] = visit_region if visit_region in region_options else "OTHER"
            if st.session_state[region_opt_key] == "OTHER":
                st.session_state[region_other_key] = visit_region

        # -------------------------
        # Account Name (required)
        # -------------------------
        new_name = st.text_input("Customer Name *", key=name_key)

        # -------------------------
        # Sector dropdown (+ OTHER)
        # -------------------------
        if st.session_state[sector_opt_key] not in sector_options:
            st.session_state[sector_opt_key] = ""
        sec_idx = sector_options.index(st.session_state[sector_opt_key])

        sector_sel = st.selectbox(
            "Sector *",
            sector_options,
            index=sec_idx,
            key=sector_opt_key,
        )
        if sector_sel == "OTHER":
            sector_other = st.text_input("Other sector *", key=sector_other_key)
        else:
            sector_other = st.session_state.get(sector_other_key, "")

        # -------------------------
        # Region dropdown (+ OTHER)
        # -------------------------
        if st.session_state[region_opt_key] not in region_options:
            st.session_state[region_opt_key] = ""
        reg_idx = region_options.index(st.session_state[region_opt_key])

        region_sel = st.selectbox(
            "Region *",
            region_options,
            index=reg_idx,
            key=region_opt_key,
        )
        if region_sel == "OTHER":
            region_other = st.text_input("Other region *", key=region_other_key)
        else:
            region_other = st.session_state.get(region_other_key, "")

        # -------------------------
        # City depends on Region (same logic as admin import)
        # -------------------------
        if region_sel not in ("", "OTHER"):
            city_df = query_df(
                """
                SELECT DISTINCT city
                FROM customers
                WHERE region = :r
                  AND city IS NOT NULL AND trim(city) <> ''
                ORDER BY city
                """,
                {"r": region_sel},
            )
            city_values = [str(r.city).strip() for r in city_df.itertuples(index=False) if str(r.city).strip()]
            city_options = [""] + city_values + ["OTHER"]
        else:
            city_options = ["", "OTHER"]

        # prefill city if empty
        if st.session_state.get(city_opt_key, "") == "" and visit_city:
            if visit_city in city_options:
                st.session_state[city_opt_key] = visit_city
            else:
                st.session_state[city_opt_key] = "OTHER"
                st.session_state[city_other_key] = visit_city

        if st.session_state[city_opt_key] not in city_options:
            st.session_state[city_opt_key] = ""
        city_idx = city_options.index(st.session_state[city_opt_key])

        city_sel = st.selectbox(
            "City *",
            city_options,
            index=city_idx,
            key=city_opt_key,
        )
        if city_sel == "OTHER":
            city_other = st.text_input("Other city *", key=city_other_key)
        else:
            city_other = st.session_state.get(city_other_key, "")

        # -------------------------
        # Optional fields (as in your schema)
        # -------------------------
        new_account_id = st.text_input("Account ID (optional)", key=acctid_key)
        new_party_id   = st.text_input("Party ID (optional)", key=partyid_key)
        new_lat        = st.text_input("Latitude (optional)", key=lat_key)
        new_lon        = st.text_input("Longitude (optional)", key=lon_key)

        confirm_new = st.checkbox(
            "I confirm this is a **new** customer (not already in the list).",
            key=confirm_key,
        )

        def _parse_float_or_none(x: str):
            x = (x or "").strip()
            if not x:
                return None
            try:
                return float(x)
            except ValueError:
                return "INVALID"

        if st.button("➕ Create New & Link", key=f"{PAGE_NS}_create_btn"):
            if not confirm_new:
                st.error("Please confirm that this is a new customer.")
                return

            # ---- Required name ----
            name_to_save = (new_name or "").strip()
            if not name_to_save:
                st.error("Customer Name is required.")
                return

            # ---- Resolve sector ----
            if sector_sel == "":
                st.error("Sector is required.")
                return
            if sector_sel == "OTHER":
                sector_to_save = (sector_other or "").strip()
                if not sector_to_save:
                    st.error("Please enter Other sector.")
                    return
            else:
                sector_to_save = sector_sel

            # ---- Resolve region ----
            if region_sel == "":
                st.error("Region is required.")
                return
            if region_sel == "OTHER":
                region_to_save = (region_other or "").strip()
                if not region_to_save:
                    st.error("Please enter Other region.")
                    return
            else:
                region_to_save = region_sel

            # ---- Resolve city ----
            if city_sel == "":
                st.error("City is required.")
                return
            if city_sel == "OTHER":
                city_to_save = (city_other or "").strip()
                if not city_to_save:
                    st.error("Please enter Other city.")
                    return
            else:
                city_to_save = city_sel

            # ---- Optional lat/lon parse ----
            lat_val = _parse_float_or_none(new_lat)
            lon_val = _parse_float_or_none(new_lon)
            if lat_val == "INVALID":
                st.error("Latitude must be a number (or leave it empty).")
                return
            if lon_val == "INVALID":
                st.error("Longitude must be a number (or leave it empty).")
                return

            account_id_val = (new_account_id or "").strip() or None
            party_id_val   = (new_party_id or "").strip() or None

            try:
                with engine.begin() as conn:
                    # Insert new customer (full schema fields)
                    res = conn.execute(
                        text(
                            """
                            INSERT INTO customers
                                (account_name, sector, region, city, is_active, latitude, longitude, party_id, account_id)
                            VALUES
                                (:name, :sector, :region, :city, TRUE, :lat, :lon, :party_id, :account_id)
                            RETURNING customer_id
                            """
                        ),
                        {
                            "name": name_to_save,
                            "sector": sector_to_save,
                            "region": region_to_save,
                            "city": city_to_save,
                            "lat": lat_val,
                            "lon": lon_val,
                            "party_id": party_id_val,
                            "account_id": account_id_val,
                        },
                    )
                    new_cid = int(res.scalar_one())

                    # Link visit to this customer
                    conn.execute(
                        text(
                            """
                            UPDATE visits
                            SET customer_id = :cid
                            WHERE visit_id  = :vid
                            """
                        ),
                        {"cid": new_cid, "vid": selected_visit_id},
                    )

                st.session_state[success_key] = (
                    f"Created new customer (ID {new_cid}) and linked visit #{selected_visit_id} ✅"
                )
                st.rerun()

            except Exception as e:
                st.error("Failed to create new customer and link visit.")
                st.caption(str(e))

# ============================================================
# Change Request Page (User) — Ascenda
# - Separate from Submit Visit flow (no shared helpers required)
# - Creates change requests ONLY (no modifications to visits/home/shelf tables)
# - Home Visit logic is TEXT-driven:
#     Target Audience label startswith "Home Visit" => Home Visit required
# - Shelf Movement logic is TEXT-driven:
#     Objective name contains "shelf movement" => Shelf Movement required
# - Customer + Location + Project are VIEW-ONLY
# ============================================================

