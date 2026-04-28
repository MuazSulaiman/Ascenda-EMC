# pages/admin_users.py
import os
import pandas as pd
import streamlit as st
from passlib.hash import pbkdf2_sha256

from db_ops import query_df, exec_sql
from utils import _gen_tmp_pw
from widgets import set_current_page
from ui import section_header, status_badge as _status_badge, html_table

# ── SVG icon helpers ──────────────────────────────────────────────────────────
_ICON_ADD_USER = (
    '<svg width="16" height="16" fill="none" stroke="#2563EB" stroke-width="2" '
    'viewBox="0 0 24 24"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/>'
    '<circle cx="9" cy="7" r="4"/><line x1="19" y1="8" x2="19" y2="14"/>'
    '<line x1="22" y1="11" x2="16" y2="11"/></svg>'
)
_ICON_USERS = (
    '<svg width="16" height="16" fill="none" stroke="#2563EB" stroke-width="2" '
    'viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>'
    '<circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/>'
    '<path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>'
)
_ICON_EDIT = (
    '<svg width="15" height="15" fill="none" stroke="#57606a" stroke-width="2" '
    'viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>'
    '<path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>'
)
_ICON_LOCK = (
    '<svg width="15" height="15" fill="none" stroke="#57606a" stroke-width="2" '
    'viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2"/>'
    '<path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>'
)
_ICON_LICENSE = (
    '<svg width="16" height="16" fill="none" stroke="#2563EB" stroke-width="2" '
    'viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/>'
    '<path d="M8 12h8M8 8h8M8 16h5"/></svg>'
)

_PAGE_CSS = """
<style>
/* ── Section panel ────────────────────────────────────────── */
.au-panel {
    background: var(--color-surface);
    border: 1px solid var(--color-border);
    border-radius: 14px;
    padding: 1.375rem 1.5rem 1.125rem;
    box-shadow: 0 1px 3px rgba(15,23,42,0.05);
    margin-bottom: 1.25rem;
}
.au-panel-title {
    display: flex; align-items: center; gap: 8px;
    font-size: 0.9375rem; font-weight: 700; color: var(--color-text);
    margin: 0 0 0.25rem;
}
.au-panel-subtitle {
    font-size: 0.8125rem; color: var(--color-text-muted); margin: 0 0 1rem;
}
.au-panel-icon {
    width: 30px; height: 30px; border-radius: 8px; background: var(--color-primary-subtle);
    display: flex; align-items: center; justify-content: center; flex-shrink: 0;
}
/* ── Stat pills ────────────────────────────────────────────── */
.au-pills { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 0.875rem; }
.au-pill {
    background: var(--color-surface-2); border: 1px solid var(--color-border); border-radius: 20px;
    padding: 3px 12px; font-size: 0.8125rem; color: var(--color-text-muted); line-height: 1.6;
}
.au-pill strong { color: var(--color-text); }
/* ── User info card ────────────────────────────────────────── */
.au-user-card {
    display: flex; align-items: center; gap: 12px;
    background: var(--color-surface-2); border: 1px solid var(--color-border); border-radius: 10px;
    padding: 0.75rem 1rem; margin-bottom: 0.875rem;
}
.au-avatar {
    width: 42px; height: 42px; border-radius: 50%; background: #2563EB;
    display: flex; align-items: center; justify-content: center;
    font-size: 0.9rem; font-weight: 700; color: #fff !important; flex-shrink: 0;
    letter-spacing: 0.02em;
}
.au-user-name { font-size: 0.9375rem; font-weight: 600; color: var(--color-text); }
.au-user-meta { font-size: 0.8rem; color: var(--color-text-muted); margin-top: 2px; }
/* ── Generated-password hint ──────────────────────────────── */
.au-pw-hint {
    font-size: 0.8125rem; color: var(--color-text);
    background: var(--color-surface-2); border-left: 3px solid var(--color-primary);
    border-radius: 0 6px 6px 0; padding: 6px 10px; margin-bottom: 0.5rem;
}
.au-pw-hint code {
    background: var(--color-primary-subtle); padding: 1px 6px; border-radius: 4px;
    font-size: 0.8rem; font-family: ui-monospace, monospace;
}
/* ── Sub-section divider ──────────────────────────────────── */
.au-sub-divider { height: 1px; background: var(--color-border); margin: 1.125rem 0; }
.au-sub-title {
    display: flex; align-items: center; gap: 7px;
    font-size: 0.9rem; font-weight: 600; color: var(--color-text);
    margin-bottom: 0.75rem;
}
/* ── License usage bar ─────────────────────────────────────── */
.au-license-bar-wrap {
    background: var(--color-surface-2); border-radius: 6px; height: 10px;
    margin: 0.5rem 0 0.25rem; overflow: hidden;
}
.au-license-bar-fill {
    height: 100%; border-radius: 6px;
    transition: width 0.3s ease;
}
.au-license-label {
    font-size: 0.8rem; color: var(--color-text-muted); margin-top: 2px;
}
</style>
"""


def _panel_header(title: str, subtitle: str, icon_svg: str) -> str:
    sub = f'<p class="au-panel-subtitle">{subtitle}</p>' if subtitle else ""
    return (
        f'<div class="au-panel">'
        f'<div class="au-panel-title">'
        f'<div class="au-panel-icon">{icon_svg}</div>{title}'
        f'</div>{sub}'
    )


def _get_max_users() -> int | None:
    """Return the configured seat limit from MAX_USERS env var, or None if unlimited."""
    raw = os.environ.get("MAX_USERS", "").strip()
    try:
        v = int(raw)
        return v if v > 0 else None
    except ValueError:
        return None


def _render_license_panel(total_users: int, max_users: int | None) -> None:
    if max_users is None:
        return
    used     = total_users
    pct      = min(used / max_users, 1.0) * 100
    free     = max(max_users - used, 0)
    bar_color = "#ef4444" if pct >= 100 else ("#f59e0b" if pct >= 80 else "#2563EB")
    st.markdown(
        _panel_header("Account Licence", "Seats provisioned for this deployment.", _ICON_LICENSE)
        + f'<div class="au-pills">'
        f'<div class="au-pill"><strong>{used}</strong> used</div>'
        f'<div class="au-pill"><strong>{free}</strong> available</div>'
        f'<div class="au-pill"><strong>{max_users}</strong> total seats</div>'
        f'</div>'
        f'<div class="au-license-bar-wrap">'
        f'<div class="au-license-bar-fill" style="width:{pct:.1f}%;background:{bar_color};"></div>'
        f'</div>'
        f'<div class="au-license-label">{pct:.0f}% of seats used</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def page_admin_users():
    st.markdown(_PAGE_CSS, unsafe_allow_html=True)
    section_header("Admin — Users", "Create, manage, and deactivate user accounts")
    set_current_page("admin_users")

    # ── Fetch live user count once (used by licence panel + guard) ──
    _count_row = query_df("SELECT COUNT(*) AS n FROM users")
    _total_users = int(_count_row["n"].iloc[0]) if not _count_row.empty else 0
    _max_users   = _get_max_users()

    _render_license_panel(_total_users, _max_users)

    # ═══════════════════════════════════════════════════════════════
    # SECTION 1 — Add a User
    # ═══════════════════════════════════════════════════════════════
    st.markdown(
        _panel_header("Add a User", "Fill in the details to create a new account.", _ICON_ADD_USER),
        unsafe_allow_html=True,
    )

    st.session_state.setdefault("create_user_tmp_pw", "")

    gen_col, _ = st.columns([1, 5])
    if gen_col.button("Generate Password", key="gen_pw_top"):
        st.session_state["create_user_tmp_pw"] = _gen_tmp_pw()

    if st.session_state["create_user_tmp_pw"]:
        st.markdown(
            f'<div class="au-pw-hint">Generated: '
            f'<code>{st.session_state["create_user_tmp_pw"]}</code>'
            f' — copy or edit before saving.</div>',
            unsafe_allow_html=True,
        )

    with st.form("add_user", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            email  = st.text_input("Email *",  placeholder="user@company.com")
            name   = st.text_input("Name *",   placeholder="Full name")
            region = st.selectbox("Region", ["", "C/R", "W/R", "E/R"], index=0)
        with col2:
            bu_df    = query_df("SELECT business_unit_id, name FROM business_units WHERE is_active IS TRUE ORDER BY name")
            bu_names = bu_df["name"].tolist()
            bu_sel   = st.selectbox("Business Unit (optional)", [""] + bu_names, index=0)
            role     = st.selectbox("Role", ["", "rep", "admin", "sales manager", "biomedical manager", "maintenance"], index=0)
            pw       = st.text_input("Temporary Password *", type="password",
                                     value=st.session_state["create_user_tmp_pw"])
        add_btn = st.form_submit_button("Create User", type="primary")

    st.markdown("</div>", unsafe_allow_html=True)  # close au-panel

    if add_btn:
        if not (email and name and pw):
            st.error("Email, Name, and Password are required.")
        elif len(pw) < 8:
            st.error("Password must be at least 8 characters.")
        elif _max_users is not None and _total_users >= _max_users:
            st.error(
                f"Seat limit reached — this deployment is licensed for {_max_users} account(s). "
                "Contact your administrator to increase the limit."
            )
        else:
            try:
                bu_id = None
                if bu_sel:
                    bu_id = int(bu_df.loc[bu_df["name"] == bu_sel, "business_unit_id"].iloc[0])
                exec_sql(
                    """
                    INSERT INTO users(email, password_hash, name, region, business_unit_id, role, is_active)
                    VALUES (:email, :pwd, :name, :region, :buid, :role, TRUE)
                    """,
                    {
                        "email":  email.strip().lower(),
                        "pwd":    pbkdf2_sha256.hash(pw),
                        "name":   name.strip(),
                        "region": (region.strip() if region else None),
                        "buid":   bu_id,
                        "role":   role,
                    },
                )
                st.success("User created successfully.")
                st.session_state["create_user_tmp_pw"] = ""
            except Exception as e:
                st.error("Could not add user — the email address may already exist.")
                st.caption(str(e))

    # ═══════════════════════════════════════════════════════════════
    # SECTION 2 — All Users
    # ═══════════════════════════════════════════════════════════════
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

    active_count   = int(df["is_active"].sum()) if not df.empty else 0
    inactive_count = len(df) - active_count

    st.markdown(
        _panel_header("All Users", "", _ICON_USERS)
        + f'<div class="au-pills">'
        f'<div class="au-pill"><strong>{len(df):,}</strong> total</div>'
        f'<div class="au-pill"><strong>{active_count}</strong> active</div>'
        f'<div class="au-pill"><strong>{inactive_count}</strong> inactive</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.markdown(html_table(df), unsafe_allow_html=True)
    if not df.empty:
        st.download_button(
            "Download CSV",
            df.to_csv(index=False).encode("utf-8-sig"),
            "users.csv",
            "text/csv",
            key="dl_users2",
        )
    st.markdown("</div>", unsafe_allow_html=True)  # close au-panel

    # ═══════════════════════════════════════════════════════════════
    # SECTION 3 — Manage Users
    # ═══════════════════════════════════════════════════════════════
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

    st.markdown(
        _panel_header(
            "Manage Users",
            "Select a user to update their role, region, status, or password.",
            _ICON_EDIT,
        ),
        unsafe_allow_html=True,
    )

    if mdf.empty:
        st.info("No users to manage yet.")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    def _fmt_user(r):
        status = "active" if bool(r.is_active) else "inactive"
        bu = f" · {r.business_unit}" if pd.notna(r.business_unit) and str(r.business_unit).strip() else ""
        return f"{r.name or r.email} ({r.role or '—'}) — {status}{bu}"

    labels = [_fmt_user(r) for r in mdf.itertuples(index=False)]
    sel = st.selectbox("Select user", [""] + labels, index=0, key="mg_user_sel",
                       label_visibility="collapsed", placeholder="Choose a user to manage…")

    if not sel:
        st.markdown(
            '<p style="font-size:0.875rem;color:var(--color-text-subtle);margin:0.25rem 0 0;">Select a user above to view and edit their account.</p>',
            unsafe_allow_html=True,
        )
        st.markdown("</div>", unsafe_allow_html=True)
        return

    row       = mdf.iloc[labels.index(sel)]
    uid       = int(row["user_id"])
    is_active = bool(row["is_active"])

    # ── User identity card ──────────────────────────────────────────────────
    _name     = row["name"] or row["email"] or "User"
    _initials = "".join(w[0].upper() for w in _name.split()[:2])
    _bu       = row["business_unit"] or "—"
    _region   = row["region"] or "—"
    badge_variant = "success" if is_active else "neutral"
    status_text   = "Active" if is_active else "Inactive"

    st.markdown(
        f'<div class="au-user-card">'
        f'<div class="au-avatar" style="color:#fff !important;">{_initials}</div>'
        f'<div style="flex:1;min-width:0;">'
        f'<div class="au-user-name">{_name}</div>'
        f'<div class="au-user-meta">{row["email"]}</div>'
        f'</div>'
        f'<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">'
        f'{_status_badge(status_text, badge_variant)}'
        f'{_status_badge(row["role"] or "—", "info")}'
        f'</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # Meta pills
    st.markdown(
        f'<div class="au-pills">'
        f'<div class="au-pill">Region: <strong>{_region}</strong></div>'
        f'<div class="au-pill">BU: <strong>{_bu}</strong></div>'
        f'<div class="au-pill">ID: <strong>#{uid}</strong></div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Actions row: Activate/Deactivate + Edit ─────────────────────────────
    colA, colB = st.columns([1, 2])

    with colA:
        toggle_label = "Deactivate User" if is_active else "Activate User"
        if st.button(toggle_label, key=f"mg_user_toggle_{uid}"):
            current     = st.session_state.get("user")
            current_uid = int(current["user_id"]) if current and "user_id" in current else None
            if toggle_label == "Deactivate User" and current_uid == uid:
                st.error("You cannot deactivate your own account while logged in.")
            else:
                try:
                    exec_sql(
                        "UPDATE users SET is_active = :active WHERE user_id = :uid",
                        {"active": not is_active, "uid": uid},
                    )
                    st.success("Account status updated.")
                except Exception as e:
                    st.error("Could not update user status.")
                    st.caption(str(e))

    with colB:
        edit_box = (
            st.popover("Edit Role / Region / BU")
            if hasattr(st, "popover")
            else st.expander("Edit Role / Region / BU", expanded=False)
        )
        with edit_box:
            bu_df2      = query_df("SELECT business_unit_id, name FROM business_units WHERE is_active IS TRUE ORDER BY name")
            bu_labels   = [""] + bu_df2["name"].tolist()
            current_bu  = row["business_unit"] or ""
            bu_idx      = bu_labels.index(current_bu) if current_bu in bu_labels else 0
            role_opts   = ["", "rep", "admin", "sales manager", "biomedical manager", "maintenance"]
            current_role = (row.get("role") or "").strip().lower()
            role_idx    = role_opts.index(current_role) if current_role in role_opts else 0

            with st.form(f"mg_user_edit_{uid}"):
                new_region   = st.selectbox(
                    "Region", ["", "C/R", "W/R", "E/R"],
                    index=(["", "C/R", "W/R", "E/R"].index(row["region"])
                           if row["region"] in ["", "C/R", "W/R", "E/R"] else 0),
                )
                new_bu_label = st.selectbox("Business Unit (optional)", bu_labels, index=bu_idx)
                new_role     = st.selectbox("Role", role_opts, index=role_idx)
                save         = st.form_submit_button("Save Changes", type="primary")

            if save:
                try:
                    new_bu_id = None
                    if new_bu_label:
                        new_bu_id = int(bu_df2.loc[bu_df2["name"] == new_bu_label, "business_unit_id"].iloc[0])
                    exec_sql(
                        "UPDATE users SET region = :region, business_unit_id = :buid, role = :role WHERE user_id = :uid",
                        {
                            "region": (new_region.strip() if new_region else None),
                            "buid":   new_bu_id,
                            "role":   new_role,
                            "uid":    uid,
                        },
                    )
                    st.success("Changes saved.")
                except Exception as e:
                    st.error("Could not save changes.")
                    st.caption(str(e))

    # ── Reset Password sub-section ──────────────────────────────────────────
    st.markdown(
        '<div class="au-sub-divider"></div>'
        f'<div class="au-sub-title">{_ICON_LOCK} Reset Password</div>',
        unsafe_allow_html=True,
    )

    flash_key     = f"flash_reset_{uid}"
    tmp_input_key = f"tmp_pw_input_{uid}"
    buf_key       = f"tmp_pw_buf_{uid}"
    st.session_state.setdefault(buf_key, "")

    if st.session_state.get(flash_key):
        st.success(st.session_state[flash_key])

    gen_col2, _ = st.columns([1, 5])
    if gen_col2.button("Generate", key=f"gen_tmp_pw_{uid}"):
        gen_pw = _gen_tmp_pw()
        st.session_state[buf_key]       = gen_pw
        st.session_state[tmp_input_key] = gen_pw

    st.text_input(
        "New Temporary Password",
        key=tmp_input_key,
        type="password",
        placeholder="Enter or generate a temporary password",
        help="Share this with the user. They can change it later from User Settings.",
    )

    pw_col, _ = st.columns([2, 5])
    if pw_col.button("Set Temporary Password", type="primary", key=f"set_tmp_pw_{uid}"):
        final_pw = (st.session_state.get(tmp_input_key) or "").strip()
        if not final_pw:
            st.error("Please enter or generate a temporary password.")
        elif len(final_pw) < 8:
            st.error("Password must be at least 8 characters.")
        else:
            try:
                exec_sql(
                    "UPDATE users SET password_hash = :pwd WHERE user_id = :uid",
                    {"pwd": pbkdf2_sha256.hash(final_pw), "uid": uid},
                )
                st.session_state[flash_key] = (
                    f"Temporary password set. Share with the user: `{final_pw}`"
                )
                st.success(st.session_state[flash_key])
            except Exception as e:
                st.error("Could not reset the password.")
                st.caption(str(e))

    st.markdown("</div>", unsafe_allow_html=True)  # close au-panel
