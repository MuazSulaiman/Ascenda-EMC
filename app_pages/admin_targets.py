# app_pages/admin_targets.py
import streamlit as st

from ui import section_header, status_badge
from widgets import set_current_page
from app_pages.admin_targets_db import (
    calc_productivity, derive_breakdown_level,
    get_all_years, get_year, create_year, update_year,
    transition_year_status, get_year_allocated_totals,
    get_reps_for_year, get_rep, upsert_rep, remove_rep,
    get_rep_breakdown_count, get_breakdown_rows, get_breakdown_totals,
    add_breakdown_row, delete_breakdown_row, check_duplicate_breakdown,
    get_contextual_gaps, get_non_admin_users, get_customers,
    get_business_units, get_product_categories,
    get_business_lines, get_articles,
)

_TABS = ["Overview", "Year Setup", "Rep Allocation", "Rep Breakdown"]

_PAGE_CSS = """
<style>
.tgt-panel {
    background: var(--color-surface); border: 1px solid var(--color-border);
    border-radius: 14px; padding: 1.375rem 1.5rem 1.125rem;
    box-shadow: 0 1px 3px rgba(15,23,42,0.05); margin-bottom: 1.25rem;
}
.tgt-panel-title {
    font-size: 0.9375rem; font-weight: 700; color: var(--color-text);
    margin: 0 0 0.5rem;
}
.tgt-pills { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 0.875rem; }
.tgt-pill {
    background: var(--color-surface-2); border: 1px solid var(--color-border);
    border-radius: 20px; padding: 3px 12px; font-size: 0.8125rem;
    color: var(--color-text-muted); line-height: 1.6;
}
.tgt-pill strong { color: var(--color-text); }
.tgt-context {
    background: var(--color-surface-2); border: 1px solid var(--color-border);
    border-radius: 10px; padding: 0.75rem 1rem; margin-bottom: 1rem;
    display: flex; gap: 1rem; flex-wrap: wrap; align-items: center;
}
.tgt-context-item { font-size: 0.8375rem; color: var(--color-text-muted); }
.tgt-context-item strong { color: var(--color-text); }
.tgt-sub-divider { height: 1px; background: var(--color-border); margin: 1.125rem 0; }
</style>
"""

_STATUS_VARIANT = {"DRAFT": "neutral", "ACTIVE": "success", "LOCKED": "info"}


def _fmt_num(v) -> str:
    try:
        return f"{float(v):,.2f}"
    except Exception:
        return str(v)


def _fmt_int(v) -> str:
    try:
        return f"{int(v):,}"
    except Exception:
        return str(v)


def _nav(tab: str, **kwargs):
    st.session_state["targets/active_tab"] = tab
    for k, v in kwargs.items():
        st.session_state[f"targets/{k}"] = v
    st.rerun()


def page_admin_targets():
    u = st.session_state.get("user")
    if not u or (u.get("role") or "").lower().strip() != "admin":
        st.error("Access denied.")
        st.stop()

    st.markdown(_PAGE_CSS, unsafe_allow_html=True)
    section_header("Admin — Targets", "Create and manage yearly sales and visit targets")
    set_current_page("admin_targets")

    active_tab = st.session_state.get("targets/active_tab", "Overview")

    cols = st.columns(len(_TABS))
    for i, tab in enumerate(_TABS):
        label = f"**{tab}**" if tab == active_tab else tab
        if cols[i].button(label, key=f"tgt_nav_{tab}", use_container_width=True):
            st.session_state["targets/active_tab"] = tab
            st.rerun()

    st.markdown('<div class="tgt-sub-divider"></div>', unsafe_allow_html=True)

    if active_tab == "Overview":
        _page_overview(u)
    elif active_tab == "Year Setup":
        _page_year_setup(u)
    elif active_tab == "Rep Allocation":
        _page_rep_allocation(u)
    elif active_tab == "Rep Breakdown":
        _page_rep_breakdown(u)


def _page_overview(u):
    uid = u["user_id"]
    df = get_all_years()

    total = len(df)
    active = len(df[df["status"] == "ACTIVE"]) if not df.empty else 0
    drafts = len(df[df["status"] == "DRAFT"]) if not df.empty else 0
    total_reps = int(df["rep_count"].sum()) if not df.empty else 0

    st.markdown(
        f'<div class="tgt-pills">'
        f'<div class="tgt-pill">Total Years: <strong>{total}</strong></div>'
        f'<div class="tgt-pill">Active: <strong>{active}</strong></div>'
        f'<div class="tgt-pill">Draft: <strong>{drafts}</strong></div>'
        f'<div class="tgt-pill">Total Reps Assigned: <strong>{total_reps}</strong></div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    c1, _ = st.columns([1, 4])
    if c1.button("+ Create New Target Year", type="primary"):
        _nav("Year Setup", edit_year=None)

    if df.empty:
        st.info("No target years created yet. Click '+ Create New Target Year' to start.")
        return

    st.markdown('<div class="tgt-sub-divider"></div>', unsafe_allow_html=True)

    for _, row in df.iterrows():
        year = int(row["year"])
        prod = calc_productivity(row["target_amount"], row["target_visits"])
        prod_str = f"{prod:,.2f}" if prod is not None else "N/A"
        locked = row["status"] == "LOCKED"

        with st.expander(
            f"{year}  ·  SAR {_fmt_num(row['target_amount'])}  ·  "
            f"{_fmt_int(row['target_visits'])} visits  ·  "
            f"Productivity: {prod_str}",
            expanded=False,
        ):
            sc1, sc2, sc3, sc4 = st.columns(4)
            sc1.markdown(status_badge(row["status"], _STATUS_VARIANT.get(row["status"], "neutral")), unsafe_allow_html=True)
            sc2.metric("Reps", _fmt_int(row["rep_count"]))
            sc3.metric("Allocated", f"SAR {_fmt_num(row['allocated_amount'])}")
            sc4.metric("Remaining", f"SAR {_fmt_num(row['remaining_amount'])}")

            bc1, bc2, bc3, bc4 = st.columns(4)
            if bc1.button("View Reps", key=f"ov_view_{year}"):
                _nav("Rep Allocation", selected_year=year)
            if not locked:
                if bc2.button("Edit Year", key=f"ov_edit_{year}"):
                    _nav("Year Setup", edit_year=year)
            if row["status"] == "DRAFT":
                if bc3.button("Activate", key=f"ov_act_{year}"):
                    transition_year_status(year, "ACTIVE", uid)
                    st.success(f"Year {year} activated.")
                    st.rerun()
            if row["status"] == "ACTIVE":
                if bc4.button("Lock", key=f"ov_lock_{year}"):
                    st.session_state[f"tgt_lock_confirm_{year}"] = True

            if st.session_state.get(f"tgt_lock_confirm_{year}"):
                st.warning(f"Locking year {year} is permanent. Type **{year}** to confirm.")
                confirm_input = st.text_input("Type year to confirm lock", key=f"tgt_lock_input_{year}")
                if st.button("Confirm Lock", type="primary", key=f"tgt_lock_btn_{year}"):
                    if confirm_input.strip() == str(year):
                        transition_year_status(year, "LOCKED", uid)
                        st.session_state.pop(f"tgt_lock_confirm_{year}", None)
                        st.success(f"Year {year} is now locked.")
                        st.rerun()
                    else:
                        st.error("Year number does not match. Lock cancelled.")


def _page_year_setup(u):
    uid = u["user_id"]
    edit_year = st.session_state.get("targets/edit_year")
    existing = get_year(edit_year) if edit_year else None
    is_locked = existing and existing.get("status") == "LOCKED"

    st.markdown(
        f'<div class="tgt-panel-title">{"Edit" if existing else "Create"} Target Year</div>',
        unsafe_allow_html=True,
    )

    if is_locked:
        st.error("This year is LOCKED and cannot be modified.")
        if st.button("Back to Overview"):
            _nav("Overview")
        return

    with st.form("year_setup_form"):
        year_val = st.number_input(
            "Year *", min_value=2000, max_value=2099,
            value=int(edit_year) if edit_year else 2025, step=1,
        )
        amount_val = st.number_input(
            "Overall Target Amount (SAR) *",
            min_value=0.0, step=1000.0, format="%.2f",
            value=float(existing["target_amount"]) if existing else 0.0,
            help="Enter as a plain number, e.g. 420000",
        )
        visits_val = st.number_input(
            "Overall Target Visits *",
            min_value=0, step=1,
            value=int(existing["target_visits"]) if existing else 0,
            help="Enter as a plain number, e.g. 300",
        )
        submitted = st.form_submit_button("Save Target", type="primary")

    prod = calc_productivity(amount_val, visits_val)
    prod_str = f"SAR {prod:,.2f} / visit" if prod is not None else "N/A (zero visits)"
    st.markdown(
        f'<div class="tgt-pills"><div class="tgt-pill">Productivity: <strong>{prod_str}</strong></div></div>',
        unsafe_allow_html=True,
    )

    if st.button("Cancel", key="year_cancel"):
        _nav("Overview")

    if submitted:
        year_val = int(year_val)
        errors = []
        if not (2000 <= year_val <= 2099):
            errors.append("Year must be between 2000 and 2099.")
        if amount_val < 0:
            errors.append("Target amount cannot be negative.")
        if visits_val < 0:
            errors.append("Target visits cannot be negative.")
        if not existing and get_year(year_val):
            errors.append(f"A target for year {year_val} already exists.")
        if existing:
            alloc = get_year_allocated_totals(edit_year)
            if amount_val < alloc["amount"]:
                errors.append(
                    f"Cannot reduce year target below currently allocated rep total "
                    f"(SAR {alloc['amount']:,.2f})."
                )
            if visits_val < alloc["visits"]:
                errors.append(
                    f"Cannot reduce visit target below currently allocated rep total "
                    f"({alloc['visits']:,} visits)."
                )

        if errors:
            for e in errors:
                st.error(e)
        else:
            try:
                if existing:
                    update_year(edit_year, amount_val, visits_val, uid)
                    st.success(f"Year {edit_year} updated.")
                else:
                    create_year(year_val, amount_val, visits_val, uid)
                    st.session_state["targets/edit_year"] = year_val
                    st.success(f"Year {year_val} created. You can now assign reps.")
                    _nav("Rep Allocation", selected_year=year_val)
            except Exception as e:
                st.error("Could not save. The year may already exist.")
                st.caption(str(e))


def _page_rep_allocation(u):
    uid = u["user_id"]

    all_years_df = get_all_years()
    if all_years_df.empty:
        st.info("No target years exist yet. Create one first.")
        if st.button("Create Year"):
            _nav("Year Setup")
        return

    year_options = [int(y) for y in all_years_df["year"].tolist()]
    saved_year = st.session_state.get("targets/selected_year")
    default_idx = year_options.index(saved_year) if saved_year in year_options else 0
    selected_year = st.selectbox("Year", year_options, index=default_idx, key="tgt_ra_year")
    st.session_state["targets/selected_year"] = selected_year

    year_row = all_years_df[all_years_df["year"] == selected_year].iloc[0]
    is_locked = year_row["status"] == "LOCKED"

    st.markdown(
        f'<div class="tgt-context">'
        f'<div class="tgt-context-item">Year: <strong>{selected_year}</strong></div>'
        f'<div class="tgt-context-item">Total: <strong>SAR {_fmt_num(year_row["target_amount"])}</strong></div>'
        f'<div class="tgt-context-item">Visits: <strong>{_fmt_int(year_row["target_visits"])}</strong></div>'
        f'<div class="tgt-context-item">Allocated: <strong>SAR {_fmt_num(year_row["allocated_amount"])}</strong></div>'
        f'<div class="tgt-context-item">Remaining: <strong>SAR {_fmt_num(year_row["remaining_amount"])}</strong></div>'
        f'<div class="tgt-context-item">Status: </div>'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        status_badge(year_row["status"], _STATUS_VARIANT.get(year_row["status"], "neutral")),
        unsafe_allow_html=True,
    )

    if is_locked:
        st.warning("This year is LOCKED. Rep allocations cannot be modified.")
    else:
        st.markdown('<div class="tgt-sub-divider"></div>', unsafe_allow_html=True)
        st.markdown("**Assign a Rep**")
        users_df = get_non_admin_users()
        reps_df = get_reps_for_year(selected_year)
        assigned_ids = set(reps_df["user_id"].tolist()) if not reps_df.empty else set()
        available = users_df[~users_df["user_id"].isin(assigned_ids)]

        user_labels = [f"{r.name} ({r.role})" for r in available.itertuples(index=False)]
        user_map = {f"{r.name} ({r.role})": int(r.user_id) for r in available.itertuples(index=False)}

        with st.form("add_rep_form", clear_on_submit=True):
            fc1, fc2, fc3 = st.columns(3)
            sel_label = fc1.selectbox("User *", [""] + user_labels)
            rep_amount = fc2.number_input("Target Amount (SAR) *", min_value=0.0,
                                          step=1000.0, format="%.2f",
                                          help="Enter as a plain number, e.g. 420000")
            rep_visits = fc3.number_input("Target Visits *", min_value=0, step=1)
            add_btn = st.form_submit_button("Add Rep", type="primary")

        if sel_label:
            rep_prod = calc_productivity(rep_amount, rep_visits)
            rep_prod_str = f"SAR {rep_prod:,.2f} / visit" if rep_prod is not None else "N/A"
            st.markdown(
                f'<div class="tgt-pills"><div class="tgt-pill">Productivity: <strong>{rep_prod_str}</strong></div></div>',
                unsafe_allow_html=True,
            )

        if add_btn:
            errors = []
            if not sel_label:
                errors.append("Please select a user.")
            if rep_amount < 0:
                errors.append("Amount cannot be negative.")
            if rep_visits < 0:
                errors.append("Visits cannot be negative.")

            if errors:
                for e in errors:
                    st.error(e)
            else:
                new_total_amount = float(year_row["allocated_amount"]) + rep_amount
                new_total_visits = int(year_row["allocated_visits"]) + rep_visits
                over_amount = new_total_amount > float(year_row["target_amount"])
                over_visits = new_total_visits > int(year_row["target_visits"])

                if (over_amount or over_visits) and not st.session_state.get("tgt_ra_over_confirm"):
                    warn_parts = []
                    if over_amount:
                        warn_parts.append(f"amount (new total: SAR {new_total_amount:,.2f} vs year target SAR {float(year_row['target_amount']):,.2f})")
                    if over_visits:
                        warn_parts.append(f"visits (new total: {new_total_visits:,} vs year target {int(year_row['target_visits']):,})")
                    st.warning(f"This allocation exceeds the year target for: {', '.join(warn_parts)}.")
                    st.checkbox(
                        "I understand this exceeds the year target and wish to proceed.",
                        key="tgt_ra_over_confirm",
                    )
                else:
                    try:
                        target_user_id = user_map[sel_label]
                        upsert_rep(selected_year, target_user_id, rep_amount, rep_visits, uid)
                        st.session_state.pop("tgt_ra_over_confirm", None)
                        st.success("Rep added successfully.")
                        st.rerun()
                    except Exception as e:
                        st.error("Could not add rep.")
                        st.caption(str(e))

    st.markdown('<div class="tgt-sub-divider"></div>', unsafe_allow_html=True)
    st.markdown("**Assigned Reps**")
    reps_df = get_reps_for_year(selected_year)

    if reps_df.empty:
        st.info("No reps assigned yet.")
        return

    for _, rep in reps_df.iterrows():
        rep_id = int(rep["target_rep_id"])
        rep_prod = calc_productivity(rep["target_amount"], rep["target_visits"])
        rep_prod_str = f"SAR {rep_prod:,.2f}" if rep_prod is not None else "N/A"
        bd_count = int(rep["breakdown_row_count"])

        with st.expander(
            f"{rep['rep_name']}  ·  {rep['rep_role']}  ·  SAR {_fmt_num(rep['target_amount'])}  ·  {_fmt_int(rep['target_visits'])} visits",
            expanded=False,
        ):
            mc1, mc2, mc3 = st.columns(3)
            mc1.metric("Productivity", rep_prod_str)
            mc2.metric("Breakdown Rows", bd_count)
            mc3.metric("Unallocated", f"SAR {_fmt_num(rep['unallocated_amount'])}")

            ac1, ac2, ac3 = st.columns(3)
            if ac1.button("Edit Target", key=f"ra_edit_{rep_id}"):
                st.session_state[f"tgt_ra_edit_{rep_id}"] = True

            if ac2.button("→ Breakdown", key=f"ra_bd_{rep_id}"):
                _nav("Rep Breakdown", selected_year=selected_year, selected_rep_id=rep_id)

            if not is_locked:
                if ac3.button("Remove", key=f"ra_remove_{rep_id}"):
                    st.session_state[f"tgt_ra_remove_{rep_id}"] = True

            if st.session_state.get(f"tgt_ra_edit_{rep_id}"):
                with st.form(f"edit_rep_{rep_id}"):
                    new_amount = st.number_input(
                        "New Target Amount", min_value=0.0, step=1000.0,
                        value=float(rep["target_amount"]), format="%.2f",
                    )
                    new_visits = st.number_input(
                        "New Target Visits", min_value=0, step=1,
                        value=int(rep["target_visits"]),
                    )
                    save_edit = st.form_submit_button("Save", type="primary")
                if save_edit:
                    bd_totals = get_breakdown_totals(rep_id)
                    errs = []
                    if new_amount < bd_totals["amount"]:
                        errs.append(f"Cannot reduce below breakdown total (SAR {bd_totals['amount']:,.2f}).")
                    if new_visits < bd_totals["visits"]:
                        errs.append(f"Cannot reduce below breakdown visits total ({bd_totals['visits']:,}).")
                    if errs:
                        for e in errs:
                            st.error(e)
                    else:
                        upsert_rep(selected_year, int(rep["user_id"]),
                                   new_amount, new_visits, uid)
                        st.session_state.pop(f"tgt_ra_edit_{rep_id}", None)
                        st.success("Rep target updated.")
                        st.rerun()

            if st.session_state.get(f"tgt_ra_remove_{rep_id}"):
                bd_cnt = get_rep_breakdown_count(rep_id)
                if bd_cnt > 0:
                    st.error(
                        f"{rep['rep_name']} has {bd_cnt} breakdown row(s). "
                        "Go to Rep Breakdown and clear all rows first, then return here to remove."
                    )
                    if st.button("Go to Breakdown", key=f"ra_goto_bd_{rep_id}"):
                        _nav("Rep Breakdown", selected_year=selected_year, selected_rep_id=rep_id)
                    st.session_state.pop(f"tgt_ra_remove_{rep_id}", None)
                else:
                    st.warning(f"Remove {rep['rep_name']} from year {selected_year}? This cannot be undone.")
                    if st.button("Confirm Remove", type="primary", key=f"ra_confirm_remove_{rep_id}"):
                        remove_rep(rep_id)
                        st.session_state.pop(f"tgt_ra_remove_{rep_id}", None)
                        st.success("Rep removed.")
                        st.rerun()


def _page_rep_breakdown(u):
    uid = u["user_id"]

    all_years_df = get_all_years()
    if all_years_df.empty:
        st.info("No target years exist yet.")
        return

    year_options = [int(y) for y in all_years_df["year"].tolist()]
    saved_year = st.session_state.get("targets/selected_year")
    default_year_idx = year_options.index(saved_year) if saved_year in year_options else 0

    sc1, sc2 = st.columns(2)
    selected_year = sc1.selectbox("Year", year_options, index=default_year_idx, key="tgt_bd_year")
    st.session_state["targets/selected_year"] = selected_year

    reps_df = get_reps_for_year(selected_year)
    if reps_df.empty:
        st.info("No reps assigned to this year. Go to Rep Allocation first.")
        return

    rep_options = {f"{r.rep_name} ({r.rep_role})": int(r.target_rep_id)
                   for r in reps_df.itertuples(index=False)}
    saved_rep_id = st.session_state.get("targets/selected_rep_id")
    rep_labels = list(rep_options.keys())
    saved_rep_label = next((lbl for lbl, rid in rep_options.items() if rid == saved_rep_id), None)
    default_rep_idx = rep_labels.index(saved_rep_label) if saved_rep_label else 0
    selected_rep_label = sc2.selectbox("Rep", rep_labels, index=default_rep_idx, key="tgt_bd_rep")
    selected_rep_id = rep_options[selected_rep_label]
    st.session_state["targets/selected_rep_id"] = selected_rep_id

    rep_row = reps_df[reps_df["target_rep_id"] == selected_rep_id].iloc[0]
    is_locked = all_years_df[all_years_df["year"] == selected_year].iloc[0]["status"] == "LOCKED"
    bd_totals = get_breakdown_totals(selected_rep_id)

    remaining_amount = float(rep_row["target_amount"]) - bd_totals["amount"]
    remaining_visits = int(rep_row["target_visits"]) - bd_totals["visits"]
    pct = min(bd_totals["amount"] / float(rep_row["target_amount"]) * 100, 100) if float(rep_row["target_amount"]) > 0 else 0
    bar_color = "#ef4444" if pct > 100 else ("#f59e0b" if pct > 90 else "#2563eb")

    st.markdown(
        f'<div class="tgt-context">'
        f'<div class="tgt-context-item">Rep Target: <strong>SAR {_fmt_num(rep_row["target_amount"])}</strong></div>'
        f'<div class="tgt-context-item">Visits: <strong>{_fmt_int(rep_row["target_visits"])}</strong></div>'
        f'<div class="tgt-context-item">Breakdown Total: <strong>SAR {_fmt_num(bd_totals["amount"])}</strong></div>'
        f'<div class="tgt-context-item">Remaining: <strong>SAR {_fmt_num(remaining_amount)}</strong></div>'
        f'</div>'
        f'<div style="background:var(--color-surface-2);border-radius:6px;height:8px;margin-bottom:1rem;overflow:hidden;">'
        f'<div style="width:{pct:.1f}%;height:100%;background:{bar_color};border-radius:6px;"></div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    gaps = get_contextual_gaps(selected_rep_id)
    if not gaps.empty:
        for _, g in gaps.iterrows():
            st.warning(
                f"{g['account_name']}: SAR {float(g['sub_amount']):,.2f} of "
                f"SAR {float(g['target_amount']):,.2f} allocated at sub-level — "
                f"SAR {float(g['gap_amount']):,.2f} unallocated."
            )

    if is_locked:
        st.warning("This year is LOCKED. Breakdown rows cannot be modified.")
    else:
        st.markdown('<div class="tgt-sub-divider"></div>', unsafe_allow_html=True)
        st.markdown("**Add Breakdown Row**")

        _bk = f"bd_{selected_rep_id}"
        st.session_state.setdefault(f"{_bk}_bu_id", None)
        st.session_state.setdefault(f"{_bk}_pc_id", None)
        st.session_state.setdefault(f"{_bk}_bl_id", None)

        customers_df = get_customers()
        cust_map = {"": None, **{r.account_name: int(r.customer_id) for r in customers_df.itertuples(index=False)}}

        bus_df = get_business_units()
        bu_map = {"": None, **{r.name: int(r.business_unit_id) for r in bus_df.itertuples(index=False)}}

        r1c1, r1c2 = st.columns(2)
        sel_cust = r1c1.selectbox("Customer", list(cust_map.keys()), key=f"{_bk}_cust")
        sel_bu   = r1c2.selectbox("Business Unit", list(bu_map.keys()), key=f"{_bk}_bu")

        bu_id = bu_map.get(sel_bu)
        pcs_df = get_product_categories(bu_id)
        pc_map = {"": None, **{r.name: int(r.product_category_id) for r in pcs_df.itertuples(index=False)}}

        r2c1, r2c2 = st.columns(2)
        sel_pc = r2c1.selectbox("Product Category", list(pc_map.keys()), key=f"{_bk}_pc")
        pc_id = pc_map.get(sel_pc)

        bls_df = get_business_lines(pc_id)
        bl_map = {"": None, **{r.name: int(r.business_line_id) for r in bls_df.itertuples(index=False)}}
        sel_bl = r2c2.selectbox("Business Line", list(bl_map.keys()), key=f"{_bk}_bl")
        bl_id = bl_map.get(sel_bl)

        arts_df = get_articles(bl_id)
        art_map = {"": None, **{
            f"{r.article_number} — {r.description or ''}".strip(" —"): r.product_id
            for r in arts_df.itertuples(index=False)
        }}
        sel_art = st.selectbox("Article #", list(art_map.keys()), key=f"{_bk}_art")
        article_id = art_map.get(sel_art)

        cust_id = cust_map.get(sel_cust)
        auto_level = derive_breakdown_level(article_id, bl_id, pc_id, bu_id, cust_id)

        st.markdown(f'<div class="tgt-pills"><div class="tgt-pill">Auto Level: <strong>{auto_level}</strong></div></div>', unsafe_allow_html=True)
        use_override = st.checkbox("Override breakdown level", key=f"{_bk}_override")
        level_options = ["rep", "customer", "business_unit", "product_category", "business_line", "article"]
        if use_override:
            final_level = st.selectbox("Breakdown Level", level_options,
                                       index=level_options.index(auto_level),
                                       key=f"{_bk}_level_override")
            st.warning(f"Auto-detected level is '{auto_level}'. You selected '{final_level}' — make sure this is intentional.")
        else:
            final_level = auto_level

        a1, a2 = st.columns(2)
        row_amount = a1.number_input("Target Amount (SAR) *", min_value=0.0, step=1000.0,
                                     format="%.2f", key=f"{_bk}_amount",
                                     help="Enter as a plain number, e.g. 50000")
        row_visits = a2.number_input("Target Visits *", min_value=0, step=1, key=f"{_bk}_visits")

        row_prod = calc_productivity(row_amount, row_visits)
        row_prod_str = f"SAR {row_prod:,.2f} / visit" if row_prod is not None else "N/A"
        st.markdown(
            f'<div class="tgt-pills"><div class="tgt-pill">Productivity: <strong>{row_prod_str}</strong></div></div>',
            unsafe_allow_html=True,
        )

        if st.button("+ Add Row", type="primary", key=f"{_bk}_add"):
            dims = {
                "customer_id": cust_id, "business_unit_id": bu_id,
                "product_category_id": pc_id, "business_line_id": bl_id,
                "article_id": article_id,
            }
            errs = []

            if row_amount == 0 and row_visits == 0:
                st.warning("This row has zero amount and zero visits. Tick below to proceed.")
                st.checkbox("Add row with zero values anyway", key=f"{_bk}_zero_confirm")
                if not st.session_state.get(f"{_bk}_zero_confirm"):
                    errs.append("Zero-value row not confirmed.")

            if check_duplicate_breakdown(selected_rep_id, dims):
                errs.append("An identical breakdown row already exists for this rep.")

            new_total_amount = bd_totals["amount"] + row_amount
            new_total_visits = bd_totals["visits"] + row_visits
            over_a = new_total_amount > float(rep_row["target_amount"])
            over_v = new_total_visits > int(rep_row["target_visits"])
            if (over_a or over_v) and not st.session_state.get(f"{_bk}_over_confirm"):
                warn_parts = []
                if over_a:
                    warn_parts.append(f"amount ({new_total_amount:,.2f} > {float(rep_row['target_amount']):,.2f})")
                if over_v:
                    warn_parts.append(f"visits ({new_total_visits:,} > {int(rep_row['target_visits']):,})")
                st.warning(f"Breakdown total exceeds rep target for: {', '.join(warn_parts)}.")
                st.checkbox("I understand this exceeds the rep target and wish to proceed.",
                            key=f"{_bk}_over_confirm")
                errs.append("Over-allocation not confirmed.")

            if not errs:
                try:
                    new_row = {
                        "target_rep_id": selected_rep_id,
                        "year": selected_year,
                        "user_id": int(rep_row["user_id"]),
                        "breakdown_level": final_level,
                        **dims,
                        "target_amount": row_amount,
                        "target_visits": row_visits,
                    }
                    add_breakdown_row(new_row, uid)
                    for key in [f"{_bk}_over_confirm", f"{_bk}_zero_confirm"]:
                        st.session_state.pop(key, None)
                    st.success("Breakdown row added.")
                    st.rerun()
                except Exception as e:
                    st.error("Could not add row.")
                    st.caption(str(e))

    st.markdown('<div class="tgt-sub-divider"></div>', unsafe_allow_html=True)
    st.markdown("**Existing Breakdown Rows**")
    rows_df = get_breakdown_rows(selected_rep_id)

    if rows_df.empty:
        st.info("No breakdown rows yet. Use the form above to add rows.")
        return

    display_cols = [
        "customer_name", "business_unit_name", "product_category_name",
        "business_line_name", "article_number", "breakdown_level",
        "target_amount", "target_visits",
    ]
    existing_cols = [c for c in display_cols if c in rows_df.columns]
    display_df = rows_df[existing_cols + ["id"]].copy()
    display_df = display_df.fillna("—")

    for _, drow in display_df.iterrows():
        bd_id = int(drow["id"])
        col_vals = " · ".join(
            str(drow[c]) for c in existing_cols
            if str(drow[c]) not in ("—", "nan", "")
        )
        with st.expander(col_vals, expanded=False):
            dc1, dc2, dc3 = st.columns(3)
            prod_d = calc_productivity(drow.get("target_amount", 0),
                                       drow.get("target_visits", 0))
            dc1.metric("Amount", f"SAR {_fmt_num(drow.get('target_amount', 0))}")
            dc2.metric("Visits", _fmt_int(drow.get("target_visits", 0)))
            dc3.metric("Productivity", f"SAR {prod_d:,.2f}" if prod_d else "N/A")

            if not is_locked:
                if st.button("Delete Row", key=f"bd_del_{bd_id}"):
                    st.session_state[f"bd_del_confirm_{bd_id}"] = True
                if st.session_state.get(f"bd_del_confirm_{bd_id}"):
                    st.warning("Delete this breakdown row? This cannot be undone.")
                    if st.button("Confirm Delete", type="primary", key=f"bd_del_ok_{bd_id}"):
                        delete_breakdown_row(bd_id)
                        st.session_state.pop(f"bd_del_confirm_{bd_id}", None)
                        st.success("Row deleted.")
                        st.rerun()
