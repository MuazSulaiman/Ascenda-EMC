# app_pages/admin_targets.py
import streamlit as st
import pandas as pd

from ui import section_header, status_badge
from widgets import set_current_page
from app_pages.admin_targets_db import (
    get_all_years, get_year, create_year, update_year, transition_year_status,
    get_reps_for_year, add_rep_to_year, remove_rep, get_rep_breakdown_count,
    get_non_admin_users,
    get_breakdown_rows, get_breakdown_totals, check_duplicate_breakdown,
    add_breakdown_row, delete_breakdown_row,
    get_customers, get_business_units, get_product_categories,
    get_business_lines, get_articles,
)

_STATUS_DOT     = {"ACTIVE": "🟢", "LOCKED": "🔵", "DRAFT": "⚪"}
_STATUS_VARIANT = {"DRAFT": "neutral", "ACTIVE": "success", "LOCKED": "info"}

_CSS = """
<style>
.tgt-section-label {
    font-size: 0.75rem; font-weight: 700; color: var(--color-text-muted);
    text-transform: uppercase; letter-spacing: 0.06em; margin: 0.9rem 0 0.2rem;
}
.tgt-context {
    background: var(--color-surface-2); border: 1px solid var(--color-border);
    border-radius: 10px; padding: 0.75rem 1rem; margin-bottom: 0.875rem;
}
.tgt-context-row { display: flex; gap: 1.5rem; flex-wrap: wrap; }
.tgt-context-item { font-size: 0.8375rem; color: var(--color-text-muted); }
.tgt-context-item strong { color: var(--color-text); }
.tgt-divider { height: 1px; background: var(--color-border); margin: 0.75rem 0; }
.tgt-rep-meta { font-size: 0.8125rem; color: var(--color-text-muted); margin-bottom: 0.5rem; }
.tgt-warn {
    background: #fef9c3; border: 1px solid #fde047; border-radius: 8px;
    padding: 0.5rem 0.75rem; font-size: 0.85rem; margin-bottom: 0.75rem;
    color: #713f12;
}
</style>
"""


def _fmt(v) -> str:
    try:
        return f"{float(v):,.0f}"
    except Exception:
        return str(v)


def page_admin_targets():
    u = st.session_state.get("user")
    if not u or (u.get("role") or "").lower().strip() != "admin":
        st.error("Access denied.")
        st.stop()

    st.markdown(_CSS, unsafe_allow_html=True)
    section_header("Admin — Targets", "Manage yearly sales and visit targets")
    set_current_page("admin_targets")

    years_df = get_all_years()

    # Auto-select most recent active (or any) year on first visit
    if "tgt/year" not in st.session_state:
        if not years_df.empty:
            active = years_df[years_df["status"] == "ACTIVE"]
            first = active.iloc[0] if not active.empty else years_df.iloc[0]
            st.session_state["tgt/year"] = int(first["year"])
        else:
            st.session_state["tgt/year"] = None

    selected_year   = st.session_state.get("tgt/year")
    selected_rep_id = st.session_state.get("tgt/rep_id")
    reps_df = get_reps_for_year(selected_year) if selected_year else pd.DataFrame()

    left_col, right_col = st.columns([1, 2.3])

    with left_col:
        _render_left_panel(years_df, reps_df, selected_year, selected_rep_id)

    with right_col:
        _render_right_panel(u, years_df, reps_df, selected_year, selected_rep_id)


def _render_left_panel(years_df, reps_df, selected_year, selected_rep_id):
    st.markdown('<div class="tgt-section-label">Years</div>', unsafe_allow_html=True)

    if years_df.empty:
        st.caption("No years yet.")
    else:
        for _, row in years_df.iterrows():
            year = int(row["year"])
            dot  = _STATUS_DOT.get(row["status"], "⚪")
            label = f"{dot} **{year}**" if year == selected_year else f"{dot} {year}"
            if st.button(label, key=f"lp_yr_{year}", use_container_width=True):
                st.session_state["tgt/year"]       = year
                st.session_state["tgt/rep_id"]     = None
                st.session_state["tgt/edit_year"]  = False
                st.session_state["tgt/adding_rep"] = False
                st.session_state["tgt/new_year"]   = False
                st.rerun()

    if st.button("＋ New Year", key="lp_new_yr", use_container_width=True):
        st.session_state["tgt/year"]     = None
        st.session_state["tgt/rep_id"]   = None
        st.session_state["tgt/new_year"] = True
        st.rerun()

    if selected_year and not years_df.empty:
        year_rows = years_df[years_df["year"] == selected_year]
        if year_rows.empty:
            return
        year_row = year_rows.iloc[0]

        st.markdown('<div class="tgt-divider"></div>', unsafe_allow_html=True)
        st.markdown('<div class="tgt-section-label">Reps</div>', unsafe_allow_html=True)

        if reps_df.empty:
            st.caption("No reps assigned.")
        else:
            for _, rep in reps_df.iterrows():
                rid   = int(rep["target_rep_id"])
                has_r = int(rep["breakdown_row_count"]) > 0
                dot   = "●" if has_r else "○"
                warn  = "" if has_r else " ⚠"
                is_sel = rid == selected_rep_id
                label = f"{dot} **{rep['rep_name']}**{warn}" if is_sel else f"{dot} {rep['rep_name']}{warn}"
                if st.button(label, key=f"lp_rep_{rid}", use_container_width=True):
                    st.session_state["tgt/rep_id"]     = rid
                    st.session_state["tgt/edit_year"]  = False
                    st.session_state["tgt/adding_rep"] = False
                    st.rerun()

        if year_row["status"] != "LOCKED":
            if st.button("＋ Add Rep", key="lp_add_rep", use_container_width=True):
                st.session_state["tgt/rep_id"]     = None
                st.session_state["tgt/adding_rep"] = True
                st.session_state["tgt/edit_year"]  = False
                st.rerun()


def _render_right_panel(u, years_df, reps_df, selected_year, selected_rep_id):
    # New year form
    if st.session_state.get("tgt/new_year"):
        _render_year_form(u, years_df, year=None)
        return

    if selected_year is None:
        _render_state_a()
        return

    year_rows = years_df[years_df["year"] == selected_year]
    if year_rows.empty:
        st.warning("Selected year not found.")
        return
    year_row = year_rows.iloc[0]

    # Add rep form
    if st.session_state.get("tgt/adding_rep"):
        _render_add_rep(u, selected_year, reps_df, year_row)
        return

    # Year edit form
    if st.session_state.get("tgt/edit_year"):
        _render_year_form(u, years_df, year=selected_year)
        return

    if selected_rep_id is None:
        _render_state_b(u, selected_year, year_row, reps_df)
    else:
        rep_rows = reps_df[reps_df["target_rep_id"] == selected_rep_id]
        if rep_rows.empty:
            st.warning("Selected rep not found in this year.")
            return
        _render_state_c(u, selected_year, year_row, selected_rep_id, rep_rows.iloc[0])


# ── State A: no year selected ─────────────────────────────────────────────────

def _render_state_a():
    st.info("Select a year from the left panel, or create one to get started.")
    if st.button("＋ Create First Year", key="sa_create"):
        st.session_state["tgt/new_year"] = True
        st.rerun()


# ── Year form ─────────────────────────────────────────────────────────────────

def _render_year_form(u, years_df, year):
    uid      = u["user_id"]
    existing = get_year(year) if year else None
    is_new   = existing is None

    st.markdown(f"### {'Create' if is_new else 'Edit'} Year")

    with st.form("year_form"):
        year_val = st.number_input(
            "Year", min_value=2000, max_value=2099,
            value=int(year) if year else 2025, step=1,
            disabled=not is_new,
        )
        budget_amount = st.number_input(
            "Budget Amount (SAR)", min_value=0.0, step=1000.0, format="%.2f",
            value=float(existing["budget_amount"]) if existing else 0.0,
        )
        budget_visits = st.number_input(
            "Budget Visits", min_value=0, step=1,
            value=int(existing["budget_visits"]) if existing else 0,
        )
        submitted = st.form_submit_button("Save", type="primary")

    if st.button("Cancel", key="yf_cancel"):
        st.session_state.pop("tgt/new_year",  None)
        st.session_state.pop("tgt/edit_year", None)
        if year:
            st.session_state["tgt/year"] = year
        st.rerun()

    if submitted:
        errs = []
        year_val = int(year_val)
        if is_new and get_year(year_val):
            errs.append(f"Year {year_val} already exists.")
        if budget_amount < 0:
            errs.append("Budget amount cannot be negative.")
        if budget_visits < 0:
            errs.append("Budget visits cannot be negative.")
        if errs:
            for e in errs:
                st.error(e)
        else:
            if is_new:
                create_year(year_val, budget_amount, budget_visits, uid)
                st.session_state["tgt/year"]    = year_val
                st.session_state["tgt/new_year"] = False
                st.success(f"Year {year_val} created.")
                st.rerun()
            else:
                update_year(year, budget_amount, budget_visits, uid)
                st.session_state["tgt/edit_year"] = False
                st.success(f"Year {year} updated.")
                st.rerun()


# ── State B: year overview ────────────────────────────────────────────────────

def _render_state_b(u, selected_year, year_row, reps_df):
    uid       = u["user_id"]
    is_locked = year_row["status"] == "LOCKED"
    budget    = float(year_row["budget_amount"])
    planned   = float(year_row["planned_amount"])
    pct       = round(planned / budget * 100, 1) if budget > 0 else 0.0
    bar_color = "#ef4444" if pct > 100 else "#2563eb"
    bar_width = min(pct, 100)

    # Header
    hc1, hc2 = st.columns([3, 1])
    hc1.markdown(f"### {selected_year}")
    with hc2:
        st.markdown(
            status_badge(year_row["status"], _STATUS_VARIANT.get(year_row["status"], "neutral")),
            unsafe_allow_html=True,
        )

    # Budget context bar
    st.markdown(
        f'<div class="tgt-context">'
        f'<div class="tgt-context-row">'
        f'<div class="tgt-context-item">Budget: <strong>SAR {_fmt(budget)}</strong>'
        f' &nbsp;·&nbsp; <strong>{_fmt(year_row["budget_visits"])}</strong> visits</div>'
        f'<div class="tgt-context-item">Planned: <strong>SAR {_fmt(planned)}</strong>'
        f' &nbsp;·&nbsp; <strong>{pct}%</strong></div>'
        f'<div class="tgt-context-item">Reps: <strong>{int(year_row["rep_count"])}</strong></div>'
        f'</div>'
        f'<div style="background:var(--color-border);border-radius:6px;height:6px;'
        f'margin-top:0.5rem;overflow:hidden;">'
        f'<div style="width:{bar_width}%;height:100%;background:{bar_color};border-radius:6px;"></div>'
        f'</div></div>',
        unsafe_allow_html=True,
    )

    # Action buttons
    if not is_locked:
        bc1, bc2, bc3 = st.columns(3)
        if bc1.button("Edit Year", key="sb_edit"):
            st.session_state["tgt/edit_year"] = True
            st.rerun()
        if year_row["status"] == "DRAFT":
            if bc2.button("Activate", key="sb_activate", type="primary"):
                if transition_year_status(selected_year, "ACTIVE", uid, "DRAFT"):
                    st.success(f"Year {selected_year} is now ACTIVE.")
                    st.rerun()
                else:
                    st.error("Status changed by another session — please refresh.")
        if year_row["status"] == "ACTIVE":
            if bc3.button("Lock Year", key="sb_lock"):
                st.session_state["tgt/lock_confirm"] = True
                st.rerun()

    # Lock confirmation
    if st.session_state.get("tgt/lock_confirm"):
        st.warning(f"Locking **{selected_year}** is permanent. Type **{selected_year}** to confirm.")
        confirm_val = st.text_input("Type year to confirm", key="sb_lock_input")
        lc1, lc2 = st.columns(2)
        if lc1.button("Confirm Lock", type="primary", key="sb_lock_ok"):
            if confirm_val.strip() == str(selected_year):
                if transition_year_status(selected_year, "LOCKED", uid, "ACTIVE"):
                    st.session_state.pop("tgt/lock_confirm", None)
                    st.success(f"Year {selected_year} is now LOCKED.")
                    st.rerun()
                else:
                    st.error("Status changed by another session.")
            else:
                st.error("Year number does not match.")
        if lc2.button("Cancel", key="sb_lock_cancel"):
            st.session_state.pop("tgt/lock_confirm", None)
            st.rerun()

    # Rep summary table
    st.markdown('<div class="tgt-divider"></div>', unsafe_allow_html=True)
    if reps_df.empty:
        st.info("No reps assigned yet. Use '＋ Add Rep' in the left panel.")
        return

    st.markdown("**Reps**")
    header = st.columns([3, 2, 2, 1])
    header[0].caption("Name / Role")
    header[1].caption("Planned Amount")
    header[2].caption("Rows / Visits")
    header[3].caption("% Budget")

    for _, rep in reps_df.iterrows():
        rc = st.columns([3, 2, 2, 1])
        warn = " ⚠" if int(rep["breakdown_row_count"]) == 0 else ""
        rc[0].write(f"**{rep['rep_name']}**{warn}  \n{rep['rep_role']}")
        rc[1].write(f"SAR {_fmt(rep['planned_amount'])}")
        rc[2].write(f"{int(rep['breakdown_row_count'])} rows · {_fmt(rep['planned_visits'])} visits")
        pct_rep = float(rep.get("pct_of_year_budget") or 0)
        rc[3].write(f"{pct_rep:.1f}%")

    st.markdown('<div class="tgt-divider"></div>', unsafe_allow_html=True)
    st.markdown(
        f"**Total planned:** SAR {_fmt(planned)} &nbsp;·&nbsp; "
        f"{_fmt(year_row['planned_visits'])} visits &nbsp;·&nbsp; {pct}% of budget"
    )


# ── Add rep form ──────────────────────────────────────────────────────────────

def _render_add_rep(u, selected_year, reps_df, year_row):
    uid = u["user_id"]
    st.markdown("### Add Rep to Year")

    users_df    = get_non_admin_users()
    assigned_ids = set(reps_df["user_id"].tolist()) if not reps_df.empty else set()
    available   = users_df[~users_df["user_id"].isin(assigned_ids)]

    if available.empty:
        st.info("All active non-admin users are already assigned to this year.")
        if st.button("← Back", key="ar_back"):
            st.session_state["tgt/adding_rep"] = False
            st.rerun()
        return

    user_labels = [f"{r.name} ({r.role})" for r in available.itertuples(index=False)]
    user_map    = {f"{r.name} ({r.role})": int(r.user_id) for r in available.itertuples(index=False)}

    sel_label = st.selectbox("Select User", [""] + user_labels, key="ar_user")
    ac1, ac2  = st.columns(2)

    if ac1.button("Add to Year", type="primary", key="ar_add", disabled=not sel_label):
        try:
            new_rep_id = add_rep_to_year(selected_year, user_map[sel_label], uid)
            st.session_state["tgt/adding_rep"] = False
            st.session_state["tgt/rep_id"]     = new_rep_id
            st.success("Rep added.")
            st.rerun()
        except Exception as e:
            st.error("Could not add rep.")
            st.caption(str(e))

    if ac2.button("Cancel", key="ar_cancel"):
        st.session_state["tgt/adding_rep"] = False
        st.rerun()


# ── State C: rep breakdown editor ─────────────────────────────────────────────

def _render_state_c(u, selected_year, year_row, selected_rep_id, rep_row):
    uid       = u["user_id"]
    is_locked = year_row["status"] == "LOCKED"

    planned        = float(rep_row["planned_amount"])
    planned_visits = int(rep_row["planned_visits"])
    budget         = float(year_row["budget_amount"])
    pct            = round(planned / budget * 100, 1) if budget > 0 else 0.0

    # Rep header
    st.markdown(f"### {rep_row['rep_name']}")
    st.markdown(
        f'<div class="tgt-rep-meta">'
        f'{rep_row["rep_role"]} &nbsp;·&nbsp; '
        f'Planned: SAR {_fmt(planned)} &nbsp;·&nbsp; {_fmt(planned_visits)} visits '
        f'&nbsp;·&nbsp; {pct}% of year budget'
        f'</div>',
        unsafe_allow_html=True,
    )

    if is_locked:
        st.info("This year is LOCKED. Breakdown rows cannot be modified.")
    else:
        _render_add_row_form(u, selected_year, year_row, selected_rep_id, rep_row)

    st.markdown('<div class="tgt-divider"></div>', unsafe_allow_html=True)
    _render_breakdown_table(selected_rep_id, is_locked)

    # Remove rep
    if not is_locked:
        st.markdown('<div class="tgt-divider"></div>', unsafe_allow_html=True)
        if st.button("Remove Rep from Year", key=f"sc_remove_{selected_rep_id}"):
            st.session_state[f"tgt/remove_{selected_rep_id}"] = True

        if st.session_state.get(f"tgt/remove_{selected_rep_id}"):
            bd_count = get_rep_breakdown_count(selected_rep_id)
            msg = f"Remove **{rep_row['rep_name']}** from year {selected_year}?"
            if bd_count > 0:
                msg += f" This will also delete their **{bd_count} breakdown row(s)**."
            st.warning(msg)
            rc1, rc2 = st.columns(2)
            if rc1.button("Confirm Remove", type="primary", key=f"sc_confirm_{selected_rep_id}"):
                remove_rep(selected_rep_id)
                st.session_state.pop(f"tgt/remove_{selected_rep_id}", None)
                st.session_state["tgt/rep_id"] = None
                st.success("Rep removed.")
                st.rerun()
            if rc2.button("Cancel", key=f"sc_cancel_{selected_rep_id}"):
                st.session_state.pop(f"tgt/remove_{selected_rep_id}", None)
                st.rerun()


# ── Add row form ──────────────────────────────────────────────────────────────

def _derive_level(dims: dict) -> str | None:
    if dims.get("article_id"):          return "article"
    if dims.get("business_line_id"):    return "business_line"
    if dims.get("product_category_id"): return "product_category"
    if dims.get("business_unit_id"):    return "business_unit"
    if dims.get("customer_id"):         return "customer"
    return None


def _render_add_row_form(u, selected_year, year_row, selected_rep_id, rep_row):
    uid = u["user_id"]
    _bk = f"arf_{selected_rep_id}"

    st.markdown("**Add Row**")
    st.caption("Select as many levels as needed — stop at any point. Deeper levels unlock as you go.")

    dims = {
        "customer_id": None, "business_unit_id": None,
        "product_category_id": None, "business_line_id": None, "article_id": None,
    }

    # Customer (always visible)
    cust_df = get_customers()
    cust_map = {"": None, **{r.account_name: int(r.customer_id) for r in cust_df.itertuples(index=False)}}
    cust_sel = st.selectbox("Customer", list(cust_map.keys()), key=f"{_bk}_cust")
    dims["customer_id"] = cust_map.get(cust_sel)

    # Business Unit (always visible — independent of customer)
    bu_df = get_business_units()
    bu_map = {"": None, **{r.name: int(r.business_unit_id) for r in bu_df.itertuples(index=False)}}
    bu_sel = st.selectbox("Business Unit", list(bu_map.keys()), key=f"{_bk}_bu")
    dims["business_unit_id"] = bu_map.get(bu_sel)

    # Product Category (unlocks when BU is selected)
    if dims["business_unit_id"]:
        pc_df = get_product_categories(dims["business_unit_id"])
        pc_map = {"": None, **{r.name: int(r.product_category_id) for r in pc_df.itertuples(index=False)}}
        pc_sel = st.selectbox("Product Category", list(pc_map.keys()), key=f"{_bk}_pc")
        dims["product_category_id"] = pc_map.get(pc_sel)

    # Business Line (unlocks when PC is selected)
    if dims["product_category_id"]:
        bl_df = get_business_lines(dims["product_category_id"])
        bl_map = {"": None, **{r.name: int(r.business_line_id) for r in bl_df.itertuples(index=False)}}
        bl_sel = st.selectbox("Business Line", list(bl_map.keys()), key=f"{_bk}_bl")
        dims["business_line_id"] = bl_map.get(bl_sel)

    # Article (unlocks when BL is selected)
    if dims["business_line_id"]:
        art_df = get_articles(dims["business_line_id"])
        art_map = {"": None, **{
            f"{r.article_number} — {r.description or ''}".strip(" —"): r.product_id
            for r in art_df.itertuples(index=False)
        }}
        art_sel = st.selectbox("Article", list(art_map.keys()), key=f"{_bk}_art")
        dims["article_id"] = art_map.get(art_sel)

    a1, a2     = st.columns(2)
    row_amount = a1.number_input("Amount (SAR)", min_value=0.0, step=1000.0,
                                  format="%.2f", key=f"{_bk}_amount")
    row_visits = a2.number_input("Visits", min_value=0, step=1, key=f"{_bk}_visits")

    # Over-budget warning — computed unconditionally so it is always a stable widget
    totals    = get_breakdown_totals(selected_rep_id)
    new_total = totals["amount"] + row_amount
    is_over   = float(year_row["budget_amount"]) > 0 and new_total > float(year_row["budget_amount"])
    if is_over:
        st.markdown(
            f'<div class="tgt-warn">⚠ Adding this row brings year planned total to '
            f'SAR {_fmt(new_total)}, exceeding the budget of '
            f'SAR {_fmt(year_row["budget_amount"])}.</div>',
            unsafe_allow_html=True,
        )

    if st.button("＋ Add Row", type="primary", key=f"{_bk}_add"):
        derived_level = _derive_level(dims)
        errs = []
        if derived_level is None:
            errs.append("Select at least one dimension before adding.")
        elif check_duplicate_breakdown(selected_rep_id, derived_level, dims):
            errs.append("This exact combination already exists for this rep.")
        if errs:
            for e in errs:
                st.error(e)
        else:
            if row_amount == 0 and row_visits == 0:
                st.warning("Amount and visits are both zero — row added.")
            new_row = {
                "target_rep_id":   selected_rep_id,
                "year":            selected_year,
                "user_id":         int(rep_row["user_id"]),
                "breakdown_level": derived_level,
                **dims,
                "target_amount":   row_amount,
                "target_visits":   row_visits,
            }
            try:
                add_breakdown_row(new_row, uid)
                st.success("Row added.")
                st.rerun()
            except Exception as e:
                st.error("Could not add row.")
                st.caption(str(e))


# ── Breakdown table ───────────────────────────────────────────────────────────

def _render_breakdown_table(selected_rep_id, is_locked):
    rows_df = get_breakdown_rows(selected_rep_id)

    if rows_df.empty:
        st.info("No breakdown rows yet.")
        return

    st.markdown("**Breakdown Rows**")

    # Column headers
    h = st.columns([2, 3, 2, 2, 1])
    h[0].caption("Level")
    h[1].caption("Dimension")
    h[2].caption("Amount (SAR)")
    h[3].caption("Visits")
    h[4].caption("")

    total_amount = 0.0
    total_visits = 0

    for _, row in rows_df.iterrows():
        bd_id = int(row["id"])
        parts = [
            p for p in [
                row.get("customer_name"),
                row.get("business_unit_name"),
                row.get("product_category_name"),
                row.get("business_line_name"),
                row.get("article_number"),
            ] if p
        ]
        dim = " › ".join(parts) if parts else "—"
        amt = float(row.get("target_amount", 0))
        vis = int(row.get("target_visits", 0))
        total_amount += amt
        total_visits += vis

        if st.session_state.get(f"tgt/del_{bd_id}"):
            dc = st.columns([5, 2, 2])
            dc[0].warning(f"Delete row: **{row.get('breakdown_level')}** / {dim}?")
            if dc[1].button("Confirm", type="primary", key=f"del_ok_{bd_id}"):
                delete_breakdown_row(bd_id)
                st.session_state.pop(f"tgt/del_{bd_id}", None)
                st.success("Row deleted.")
                st.rerun()
            if dc[2].button("Cancel", key=f"del_no_{bd_id}"):
                st.session_state.pop(f"tgt/del_{bd_id}", None)
                st.rerun()
        else:
            rc = st.columns([2, 3, 2, 2, 1])
            rc[0].write(row.get("breakdown_level", "—"))
            rc[1].write(dim)
            rc[2].write(f"{_fmt(amt)}")
            rc[3].write(f"{vis}")
            if not is_locked:
                if rc[4].button("×", key=f"del_{bd_id}"):
                    st.session_state[f"tgt/del_{bd_id}"] = True
                    st.rerun()

    st.markdown('<div class="tgt-divider"></div>', unsafe_allow_html=True)
    st.markdown(f"**Rep total: SAR {_fmt(total_amount)} &nbsp;·&nbsp; {total_visits} visits**")
