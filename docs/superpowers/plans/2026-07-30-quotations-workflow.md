# Quotations Workflow — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the sales quotation request → manager approval → Odoo-handoff process from manual Excel/paper into a new "Quotations" area of Ascenda: rep submits → sales manager approves/rejects/requests-edit (loop) → once approved, a sales coordinator manually re-keys it into Odoo and marks it Done. No PDF generation, no live Odoo API — those stay out of scope by design.

**Architecture:** Three new Streamlit pages (`quotation_request.py` rep-facing, `quotation_review.py` manager-facing, `quotation_handoff.py` coordinator-facing) sharing one DB/helper module (`quotation_helpers.py`), modeled structurally on the existing change-request approval pattern (`change_request.py` / `admin_change_requests.py` / `change_request_helpers.py`). Schema added idempotently via `migrations/quotations_schema.sql` and wired into `db.py::_run_migrations()`, mirroring the `targets_schema.sql` precedent.

**Tech Stack:** Python 3.11+, Streamlit ≥1.37, SQLAlchemy 2.x (psycopg v3), PostgreSQL, pandas, pytest, `Decimal` for all money math.

**Spec:** `docs/superpowers/specs/2026-07-30-quotations-workflow-design.md` — this is the source of truth for exact schema, function signatures, and UX detail. Every task below tells you which section(s) to read. When this plan and the spec conflict, the spec wins (this plan is a task breakdown of the spec, not a replacement for it).

**Global Constraints (bind every task):**
- Every DB write must go through `engine.begin()` (never raw `conn.execute` outside a transaction) and every guarded state transition must follow the `UPDATE ... WHERE id = :id AND status = '<expected>'` + `rowcount == 0` → "already resolved elsewhere" idiom from `admin_change_requests.py::_apply_changes`/`_reject_request`. Never check-then-update in two separate statements.
- All money math (`compute_line_total`, `compute_header_totals`, and anywhere totals are computed) uses Python `Decimal`, quantized to 2dp with `ROUND_HALF_UP`. Never `float()` a money value.
- Any nullable DataFrame-sourced value used in a conditional or SQL parameter goes through `_norm()`/`_sql_val()` (import from `app_pages/change_request_helpers.py`, do not reimplement).
- Status values are plain `TEXT` matched against Python string literals (`'IN_REVIEW'`, `'EDIT_REQUESTED'`, etc.) — no Postgres ENUM type anywhere in this feature.
- No `manufacturer` field anywhere — not in schema, not in any form, not in any constant list.
- Every manager/coordinator transition function calls `_require_role()` as its first line, re-querying the actor's role from the DB — never trust `st.session_state["user"]["role"]` inside a transition function.
- Do not modify `admin_import.py`, `admin_targets.py`, `admin_change_requests.py`, `change_request.py`, `app_settings.py`. Only import from `change_request_helpers.py`, never modify it.
- Commit after each task with a descriptive message; run the full test suite (`python -m pytest tests/ -v`) before every commit to confirm no regressions.

---

## File Map

| Action | Path | Responsibility |
|--------|------|---------------|
| Create | `migrations/quotations_schema.sql` | Idempotent DDL for all new tables, indexes, role-CHECK migration |
| Create | `app_pages/quotation_helpers.py` | Totals math, DB helpers, guarded transition functions, shared render helpers |
| Create | `app_pages/quotation_request.py` | Rep-facing: New Quotation / My Quotations |
| Create | `app_pages/quotation_review.py` | Sales-manager-facing: Pending queue, Approved queue |
| Create | `app_pages/quotation_handoff.py` | Sales-coordinator-facing: Approved queue, Mark Done |
| Create | `tests/test_quotation_totals.py` | Pure-function tests for totals math |
| Create | `tests/test_quotation_workflow_race.py` | Concurrency/guard integration tests against the live DB |
| Modify | `db.py` | Load `quotations_schema.sql` in `_run_migrations()`; run `users.role` CHECK migration |
| Modify | `init_db_v11.py` | Append new schema block; add `'sales coordinator'` to `users.role` CHECK |
| Modify | `app_pages/admin_users.py` | Add `"sales coordinator"` to the 2 role-dropdown lists |
| Modify | `ui.py` | `sidebar_nav()`: wire 3 new page names into role-conditional lists |
| Modify | `app_v11.py` | Import 3 new page functions; add to `PAGE_MAP` and `PAGE_ROLES` |

---

## Task 1: Schema Migration + Role Change

**Read first:** spec sections "Schema (final, as amended)" (all subsections) and "Role change — add `'sales coordinator'`".

**Files:**
- Create: `migrations/quotations_schema.sql`
- Modify: `db.py`, `init_db_v11.py`, `app_pages/admin_users.py`

- [ ] **Step 1: Write `migrations/quotations_schema.sql`**

Create the file with, in this exact order: `quotation_requests`, `quotation_lines`, `quotation_revisions`, `quotation_revision_lines`, `quotation_status_events`, `quotation_number_counters`, then all indexes, then the `users.role` CHECK migration `DO $$ ... $$` block. Use the exact column definitions from the spec's "Schema (final, as amended)" section verbatim — do not add, rename, or drop any column, and do not add a `manufacturer` column anywhere. Use `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` throughout (idempotent, safe to re-run every startup, matching `migrations/targets_schema.sql`'s header comment convention). Start the file with:
```sql
-- migrations/quotations_schema.sql
-- Idempotent — safe to re-run on every app startup.
```

For the `users.role` CHECK migration, use exactly the `DO $$ ... $$` block from the spec's "Role change" section (the `pg_constraint`/`pg_get_constraintdef` lookup that only replaces the constraint if `'sales coordinator'` isn't already present).

- [ ] **Step 2: Wire the migration into `db.py`**

In `db.py`, inside `_run_migrations()`, after the existing `targets_schema.sql` loading block, add:
```python
    # Quotations Workflow — schema migration
    _quotations_migration_path = os.path.join(os.path.dirname(__file__), "migrations", "quotations_schema.sql")
    if os.path.exists(_quotations_migration_path):
        with open(_quotations_migration_path, "r") as _f:
            _quotations_sql = _f.read()
        with engine.begin() as conn:
            conn.execute(text(_quotations_sql))
```
`db.py` does not currently `import os` — add it at the top alongside the existing imports if not already present (check first; `targets_schema.sql`'s loader already uses `os.path`, so it may already be imported — verify with `grep -n "^import os" db.py` before adding a duplicate).

- [ ] **Step 3: Add new tables to `init_db_v11.py`'s `SCHEMA_SQL`**

Inside the `SCHEMA_SQL` triple-quoted string, after the target-management tables block and before the final `-- Indexes` section, add the same six `CREATE TABLE IF NOT EXISTS` statements (identical DDL to the migration file — this is the fresh-install path). Add the corresponding indexes to the existing `-- Indexes` block at the end of `SCHEMA_SQL`.

Also update the `users` table's `role` CHECK constraint in `init_db_v11.py` (the `CREATE TABLE IF NOT EXISTS users (...)` block, currently `role TEXT NOT NULL CHECK (role IN ('admin', 'rep', 'maintenance', 'supervisor', 'sales manager', 'biomedical manager'))`) to add `'sales coordinator'` to that list. Do not touch `role_objectives`'s CHECK list.

- [ ] **Step 4: Update `app_pages/admin_users.py` role dropdowns**

At line ~241 (Add User form) and line ~445 (`role_opts` in the Edit Role/Region/BU popover), both currently `["", "rep", "admin", "sales manager", "biomedical manager", "maintenance"]` — append `"sales coordinator"` to both lists, preserving every existing entry including `"maintenance"`.

- [ ] **Step 5: Verify the migration runs cleanly**

```bash
python -c "from db import engine; print('Migration OK')"
```
Expected: `Migration OK` with no exceptions. Then verify against the live DB (e.g. via `psql` or a quick Python `SELECT` through `db_ops.query_df`) that all six new tables exist, the new indexes exist, and `\d users` (or an equivalent `information_schema` query) shows `'sales coordinator'` in the role CHECK constraint. Run the migration a second time (re-run the same command) to confirm it's a true no-op the second time (no errors, no duplicate constraint attempts).

- [ ] **Step 6: Run full test suite and commit**

```bash
python -m pytest tests/ -v
```
Expected: same pass count as the pre-task baseline (39 passed, 1 skipped) — this task adds no tests of its own, it must not break any existing ones.

```bash
git add migrations/quotations_schema.sql db.py init_db_v11.py app_pages/admin_users.py
git commit -m "feat: add quotations schema migration and sales coordinator role"
```

---

## Task 2: `quotation_helpers.py` — Totals Math (TDD)

**Read first:** spec section "New files → `app_pages/quotation_helpers.py`" (the `compute_line_total`/`compute_header_totals` bullet only — ignore the DB-helper bullets, those are Task 3).

**Files:**
- Create: `tests/test_quotation_totals.py`
- Create: `app_pages/quotation_helpers.py` (totals functions only — DB helpers added in Task 3)

- [ ] **Step 1: Write failing tests**

Create `tests/test_quotation_totals.py`:
```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from decimal import Decimal
from app_pages.quotation_helpers import compute_line_total, compute_header_totals


def test_line_total_no_discount():
    assert compute_line_total(Decimal("2"), Decimal("100.00"), Decimal("0")) == Decimal("200.00")

def test_line_total_with_discount():
    assert compute_line_total(Decimal("3"), Decimal("50.00"), Decimal("10")) == Decimal("135.00")

def test_line_total_100_pct_discount():
    assert compute_line_total(Decimal("5"), Decimal("40.00"), Decimal("100")) == Decimal("0.00")

def test_line_total_fractional_quantity():
    assert compute_line_total(Decimal("1.5"), Decimal("10.00"), Decimal("0")) == Decimal("15.00")

def test_line_total_rounds_half_up():
    # 1 x 10.005 with 0% discount -> 10.005 rounds to 10.01 under ROUND_HALF_UP
    assert compute_line_total(Decimal("1"), Decimal("10.005"), Decimal("0")) == Decimal("10.01")


def test_header_totals_single_line():
    lines = [{"quantity": Decimal("2"), "unit_price": Decimal("100.00"), "discount_pct": Decimal("0")}]
    totals = compute_header_totals(lines, Decimal("15.00"))
    assert totals["subtotal"] == Decimal("200.00")
    assert totals["vat_amount"] == Decimal("30.00")
    assert totals["grand_total"] == Decimal("230.00")

def test_header_totals_multiple_lines_with_discounts():
    lines = [
        {"quantity": Decimal("2"), "unit_price": Decimal("100.00"), "discount_pct": Decimal("10")},
        {"quantity": Decimal("1"), "unit_price": Decimal("50.00"), "discount_pct": Decimal("0")},
    ]
    totals = compute_header_totals(lines, Decimal("15.00"))
    # line1 = 2*100*0.9 = 180.00, line2 = 50.00, subtotal = 230.00
    assert totals["subtotal"] == Decimal("230.00")
    assert totals["vat_amount"] == Decimal("34.50")
    assert totals["grand_total"] == Decimal("264.50")

def test_header_totals_zero_vat():
    lines = [{"quantity": Decimal("1"), "unit_price": Decimal("100.00"), "discount_pct": Decimal("0")}]
    totals = compute_header_totals(lines, Decimal("0"))
    assert totals["vat_amount"] == Decimal("0.00")
    assert totals["grand_total"] == Decimal("100.00")

def test_header_totals_empty_lines():
    totals = compute_header_totals([], Decimal("15.00"))
    assert totals["subtotal"] == Decimal("0.00")
    assert totals["vat_amount"] == Decimal("0.00")
    assert totals["grand_total"] == Decimal("0.00")

def test_header_totals_14_lines_no_float_drift():
    # 14 lines chosen so a naive float accumulation would visibly drift
    # from the correct Decimal result; asserts exact equality, not approx.
    lines = [
        {"quantity": Decimal("1"), "unit_price": Decimal("0.10"), "discount_pct": Decimal("0")}
        for _ in range(14)
    ]
    totals = compute_header_totals(lines, Decimal("15.00"))
    assert totals["subtotal"] == Decimal("1.40")
    assert totals["vat_amount"] == Decimal("0.21")
    assert totals["grand_total"] == Decimal("1.61")
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_quotation_totals.py -v
```
Expected: `ImportError`/`ModuleNotFoundError` — `app_pages/quotation_helpers.py` does not exist yet.

- [ ] **Step 3: Create `quotation_helpers.py` with the totals functions**

Create `app_pages/quotation_helpers.py`:
```python
# app_pages/quotation_helpers.py
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping


TWO_PLACES = Decimal("0.01")


def _q(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def compute_line_total(quantity: Decimal, unit_price: Decimal, discount_pct: Decimal) -> Decimal:
    """qty x price x (1 - discount%), quantized to 2dp with ROUND_HALF_UP."""
    gross = Decimal(quantity) * Decimal(unit_price)
    net = gross * (Decimal("1") - Decimal(discount_pct) / Decimal("100"))
    return _q(net)


def compute_header_totals(lines: Iterable[Mapping], vat_rate: Decimal) -> dict:
    """Sum per-line totals into subtotal/vat_amount/grand_total, all Decimal, 2dp."""
    subtotal = Decimal("0.00")
    for line in lines:
        subtotal += compute_line_total(
            Decimal(line["quantity"]), Decimal(line["unit_price"]), Decimal(line["discount_pct"])
        )
    subtotal = _q(subtotal)
    vat_amount = _q(subtotal * Decimal(vat_rate) / Decimal("100"))
    grand_total = _q(subtotal + vat_amount)
    return {"subtotal": subtotal, "vat_amount": vat_amount, "grand_total": grand_total}
```
Note: `compute_line_total` quantizes each line individually before summing (matches the Excel template's per-line rounding behavior) — this is why Step 1's multi-line test expects `180.00 + 50.00`, not a single quantization at the end.

- [ ] **Step 4: Run tests — verify they pass**

```bash
python -m pytest tests/test_quotation_totals.py -v
```
Expected: 9 tests PASSED.

- [ ] **Step 5: Run full suite and commit**

```bash
python -m pytest tests/ -v
```
Expected: 39 + 9 = 48 passed, 1 skipped.
```bash
git add app_pages/quotation_helpers.py tests/test_quotation_totals.py
git commit -m "feat: add Decimal-based quotation totals math with tests"
```

---

## Task 3: `quotation_helpers.py` — DB Helpers + Guarded Transition Functions

**Read first:** spec section "New files → `app_pages/quotation_helpers.py`" in full, plus "Guard/race-condition table" and "Duplicate Odoo reference — hard block". This is the highest-risk task in the plan — read `app_pages/admin_change_requests.py`'s `_apply_changes` and `_reject_request` functions and `change_request_helpers.py`'s `_norm`/`_sql_val` before writing any code, and follow their transaction idiom exactly.

**Files:**
- Modify: `app_pages/quotation_helpers.py` (add everything below the totals functions from Task 2)

- [ ] **Step 1: Add imports and shared constants**

At the top of `app_pages/quotation_helpers.py`, add (below the existing `decimal` imports):
```python
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text

from db import engine
from db_ops import query_df, query_scalar, exec_sql
from app_pages.change_request_helpers import _norm, _sql_val
```
Add the dropdown-option constants exactly as named in the spec:
```python
REQUEST_SOURCE_OPTIONS = ["Purchasing Dept.", "Procurement Dept.", "Sales Dept.", "Direct Request"]
VALIDITY_OPTIONS = [30, 60, 90, 180, 365]
DELIVERY_OPTIONS = ["Immediate", "15 Days", "30 Days", "45 Days", "60 Days", "90 Days", "120 Days"]
PAYMENT_TERMS_OPTIONS = [
    "Cash in Advance", "Cash on Delivery", "15 Days", "30 Days",
    "60 Days", "90 Days", "120 Days", "As per Agreed Policy",
]
```
These lists must exactly match the CHECK constraint values in `migrations/quotations_schema.sql` from Task 1 — cross-check before moving on.

- [ ] **Step 2: `_require_role` and `_odoo_reference_exists`**

```python
def _require_role(actor_uid: int, allowed_roles: set[str]) -> str:
    """Re-query the actor's current role from the DB. Raises ValueError if not allowed."""
    role = query_scalar("SELECT role FROM users WHERE user_id = :uid", {"uid": actor_uid})
    role = _norm(role)
    if role not in allowed_roles:
        raise ValueError(f"Role '{role}' is not permitted to perform this action.")
    return role


def _odoo_reference_exists(ref: str) -> bool:
    ref = _norm(ref)
    if not ref:
        return False
    row = query_scalar(
        "SELECT 1 FROM quotation_requests WHERE odoo_reference = :ref", {"ref": ref}
    )
    return row is not None
```

- [ ] **Step 3: `_next_quotation_number`**

```python
def _next_quotation_number(conn, year: int) -> str:
    result = conn.execute(
        text("""
            INSERT INTO quotation_number_counters (year, last_seq) VALUES (:yr, 1)
            ON CONFLICT (year) DO UPDATE SET last_seq = quotation_number_counters.last_seq + 1
            RETURNING last_seq
        """),
        {"yr": year},
    )
    seq = result.scalar_one()
    return f"QT-{year}-{seq:06d}"
```

- [ ] **Step 4: Read helpers**

```python
def _load_quotation_header(quotation_id: int) -> dict | None:
    df = query_df("SELECT * FROM quotation_requests WHERE quotation_id = :id", {"id": quotation_id})
    return df.iloc[0].to_dict() if not df.empty else None


def _load_quotation_lines(quotation_id: int) -> pd.DataFrame:
    return query_df(
        "SELECT * FROM quotation_lines WHERE quotation_id = :id ORDER BY line_no",
        {"id": quotation_id},
    )


def _load_status_events(quotation_id: int) -> pd.DataFrame:
    return query_df(
        "SELECT * FROM quotation_status_events WHERE quotation_id = :id ORDER BY at_utc",
        {"id": quotation_id},
    )


def _load_revisions(quotation_id: int) -> pd.DataFrame:
    return query_df(
        "SELECT * FROM quotation_revisions WHERE quotation_id = :id ORDER BY revision_no",
        {"id": quotation_id},
    )


def _load_revision_lines(revision_id: int) -> pd.DataFrame:
    return query_df(
        "SELECT * FROM quotation_revision_lines WHERE revision_id = :id ORDER BY line_no",
        {"id": revision_id},
    )
```

- [ ] **Step 5: `submit_quotation`**

Single `engine.begin()` transaction. Header fields come in as a dict with keys matching `quotation_requests` columns (`customer_id`, `request_source`, `quotation_date`, `vat_rate`, `remarks`, `validity_days`, `delivery_terms`, `payment_terms`); `lines` is a list of dicts with `product_id`, `quantity`, `unit_price`, `discount_pct` (1-indexed `line_no` assigned by position). Steps inside the transaction: resolve `year = header["quotation_date"].year` (or current year if not present), call `_next_quotation_number(conn, year)`, `INSERT INTO quotation_requests` with `status='IN_REVIEW'`, `version=0`, `submitted_by=actor_uid`, `rep_user_id=actor_uid`, `RETURNING quotation_id`; loop-insert `quotation_lines` with `line_no` 1..N; compute `compute_header_totals(lines, header["vat_rate"])`; `INSERT INTO quotation_revisions` with `revision_no=1` and the computed totals, `RETURNING revision_id`; loop-insert `quotation_revision_lines` (snapshot `article_number_snapshot`/`description_snapshot` by joining `items` for each `product_id` inside the same transaction); `INSERT INTO quotation_status_events` with `event_type='SUBMITTED'`, `from_status=NULL`, `to_status='IN_REVIEW'`, `revision_id=<the new revision_id>`. Return `(quotation_id, quotation_number)` on success. No status guard needed (pure insert).

- [ ] **Step 6: `resubmit_quotation`**

Signature: `resubmit_quotation(quotation_id, header, lines, actor_uid, expected_version) -> tuple[bool, str | None]`. Single `engine.begin()` transaction:
```python
result = conn.execute(
    text("""
        UPDATE quotation_requests
        SET version = version + 1, status = 'IN_REVIEW'
        WHERE quotation_id = :qid AND status = 'EDIT_REQUESTED'
          AND version = :expected_version AND rep_user_id = :actor_uid
        RETURNING version
    """),
    {"qid": quotation_id, "expected_version": expected_version, "actor_uid": actor_uid},
)
row = result.fetchone()
if row is None:
    raise ValueError("This quotation was already changed elsewhere. Refresh and try again.")
new_version = row[0]
```
Then, in the same transaction: `DELETE FROM quotation_lines WHERE quotation_id = :qid` then re-insert the new `lines` (same shape as Step 5); update the mutable header fields (`customer_id`, `request_source`, `quotation_date`, `vat_rate`, `remarks`, `validity_days`, `delivery_terms`, `payment_terms`) via a second `UPDATE quotation_requests SET ... WHERE quotation_id = :qid`; compute totals; `INSERT INTO quotation_revisions` with `revision_no = new_version + 1`; insert revision lines; `INSERT INTO quotation_status_events` with `event_type='RESUBMITTED'`, `from_status='EDIT_REQUESTED'`, `to_status='IN_REVIEW'`, `revision_id=<new revision_id>`. Wrap the whole function body in try/except around the transaction, returning `(True, None)` on success and `(False, str(e))` on the `ValueError` raised above (mirroring `_apply_changes`'s pattern of raising inside `engine.begin()` and letting the caller catch it — check the exact pattern in `admin_change_requests.py` and match it, don't invent a different error-propagation style).

- [ ] **Step 7: Manager/coordinator transition functions**

Implement `manager_approve`, `manager_reject`, `manager_request_edit`, `manager_return_for_revision`, `withdraw_quotation`, `coordinator_mark_done` exactly per the spec's bullet list and the Guard/race-condition table. Every manager/coordinator function's first line inside the function body (before opening `engine.begin()`) is a call to `_require_role(...)`; the `admin` self-review exemption is implemented by building the guard SQL conditionally — when `_require_role` resolves the actor's role to `"admin"`, omit the `rep_user_id != :actor_uid` clause from the guard; for any other allowed role, include it. All follow the same `engine.begin()` + guarded `UPDATE ... RETURNING` (or check `rowcount`) + status-event insert shape as Steps 5-6. Return `(True, None)` / `(False, error_message)` tuples throughout, matching `_apply_changes`/`_reject_request`'s calling convention so page code can do `ok, err = fn(...)`.

For `coordinator_mark_done`: normalize `odoo_reference` via `_norm()` before binding (empty string → `None`); still perform the DB-level guarded UPDATE even though the caller is expected to have already called `_odoo_reference_exists()` — wrap the `INSERT`/`UPDATE` in a `try/except` catching the unique-index violation (`sqlalchemy.exc.IntegrityError`) as the final backstop for the true concurrent-race case, converting it to `(False, "That Odoo reference was just used by another quotation.")`.

- [ ] **Step 8: `render_revision_diff` and `render_quotation_detail`**

`render_revision_diff(revision_a: dict, revision_b: dict) -> None` — renders header-field-by-field and line-by-line comparison using `ui.py::compare_row`, in an HTML `<table>` the same way `admin_change_requests.py::_render_diff_table` does (import `compare_row` from `ui.py`; do not reimplement the diff-row HTML). `render_quotation_detail(quotation_id: int) -> None` — a Streamlit-rendering function combining header, lines, totals, and status-event timeline, called identically from all three pages so the read-only view isn't copy-pasted three times.

- [ ] **Step 9: `_status_badge_variant`**

```python
def _status_badge_variant(status: str) -> str:
    return {
        "IN_REVIEW": "info",
        "EDIT_REQUESTED": "warning",
        "REJECTED": "danger",
        "APPROVED": "success",
        "DONE": "success",
        "WITHDRAWN": "neutral",
    }.get(status, "neutral")
```
(Match whatever variant-name vocabulary `ui.py`'s existing status-badge component actually uses — check its call sites in `admin_change_requests.py` before finalizing these strings; use the same variant names, don't invent new ones.)

- [ ] **Step 10: Manual smoke test**

```bash
python -c "
from app_pages.quotation_helpers import compute_header_totals, REQUEST_SOURCE_OPTIONS
from decimal import Decimal
print(REQUEST_SOURCE_OPTIONS)
print(compute_header_totals([], Decimal('15')))
"
```
Expected: no import errors, prints the options list and a zero-totals dict. This is a smoke test only — the real correctness check for the transition functions is Task 8's race-condition test suite; do not write ad-hoc DB-touching scripts here.

- [ ] **Step 11: Run full suite and commit**

```bash
python -m pytest tests/ -v
```
Expected: 48 passed, 1 skipped (unchanged from Task 2 — this task adds no new tests, Task 8 does).
```bash
git add app_pages/quotation_helpers.py
git commit -m "feat: add quotation DB helpers and guarded state-transition functions"
```

---

## Task 4: `quotation_request.py` — Rep-Facing Page

**Read first:** spec section "New files → `app_pages/quotation_request.py`". Also read `app_pages/change_request.py`'s tab structure and `admin_change_requests.py`'s `_render_force_tab` delete-confirmation checkbox pattern before writing the withdraw flow.

**Files:**
- Create: `app_pages/quotation_request.py`

- [ ] **Step 1: Page scaffold + role gate**

```python
# app_pages/quotation_request.py
import streamlit as st

from ui import section_header
from widgets import customer_quick_find_module, customer_cascading_selectors
from app_pages.quotation_helpers import (
    REQUEST_SOURCE_OPTIONS, VALIDITY_OPTIONS, DELIVERY_OPTIONS, PAYMENT_TERMS_OPTIONS,
    compute_header_totals, submit_quotation, resubmit_quotation, withdraw_quotation,
    render_quotation_detail, render_revision_diff,
    _load_revisions,
)


def page_quotation_request():
    u = st.session_state.get("user")
    role = (u.get("role") or "").lower().strip() if u else ""
    if role not in ("rep", "sales manager", "biomedical manager", "admin"):
        st.error("Access denied.")
        st.stop()

    section_header("Quotations", "Submit and track your quotation requests")

    active_tab = st.radio(
        "Quotations Section", ["New Quotation", "My Quotations"],
        key="quotation_request_active_tab", horizontal=True, label_visibility="collapsed",
    )
    if active_tab == "New Quotation":
        _render_new_quotation_tab(u)
    else:
        _render_my_quotations_tab(u)
```
Use `st.radio(horizontal=True)`, not `st.tabs()` — confirmed rerun-reset gotcha, matches `admin_change_requests.py`'s documented workaround.

- [ ] **Step 2: "New Quotation" tab**

Implement `_render_new_quotation_tab(u)`: customer picker via `customer_quick_find_module`/`customer_cascading_selectors` (reuse verbatim, do not reimplement customer lookup); form fields for `request_source` (`st.selectbox(REQUEST_SOURCE_OPTIONS)`), `quotation_date` (`st.date_input`, default today), `vat_rate` (`st.number_input`, default `15.00`), `remarks` (`st.text_area`, optional), `validity_days`/`delivery_terms`/`payment_terms` (selectboxes from the imported option constants, optional); a dynamic line-items editor for up to 14 rows (product picker via the existing BU→Category→Business Line→Product cascading loaders already used elsewhere in this codebase — locate and reuse them, do not write new product-lookup SQL), each row collecting `product_id`, `quantity`, `unit_price`, `discount_pct`; a live totals preview computed via `compute_header_totals` as rows are edited; a Submit button that calls `submit_quotation(header, lines, actor_uid=u["user_id"])` inside a `try/except`, showing `st.success` with the assigned quotation number on success or `st.error` on failure.

- [ ] **Step 3: "My Quotations" tab**

Implement `_render_my_quotations_tab(u)`: query `quotation_requests WHERE rep_user_id = :uid` (unfiltered if `role == "admin"`), grouped/sectioned by status. For each quotation, render via `render_quotation_detail(quotation_id)` (status-event timeline included). Rows with `status == 'EDIT_REQUESTED'`: show `manager_comment`, plus an **Edit & Resubmit** button that prefills the New-Quotation-style form from the latest revision (`_load_revisions` + its lines) and on submit calls `resubmit_quotation(quotation_id, header, lines, actor_uid=u["user_id"], expected_version=<current version>)`; plus a **Withdraw Request** button. Rows with `status == 'IN_REVIEW'`: only **Withdraw Request**. Withdraw flow: mandatory `st.text_area` reason, a `st.warning` + `st.checkbox` confirmation gate before the actual submit button is enabled (mirror `admin_change_requests.py::_render_force_tab`'s "Delete Visit" confirm pattern exactly — required-text + checkbox both gating the button's `disabled=` state), calling `withdraw_quotation(quotation_id, rep_uid=u["user_id"], reason=reason)`. Rows with `status == 'WITHDRAWN'` render fully read-only (no action buttons). When a quotation has 2+ revisions (`len(_load_revisions(quotation_id)) >= 2`), automatically render `render_revision_diff` comparing the latest two.

- [ ] **Step 4: Manual verification**

Start the app (`streamlit run app_v11.py` or however this project is normally run locally — check `README.md`/existing dev workflow if unsure) and log in as a `rep` test user. Confirm the page is reachable only once wired into navigation (Task 7) — for now, verify there are no Python import/syntax errors:
```bash
python -c "import app_pages.quotation_request"
```
Expected: no errors.

- [ ] **Step 5: Run full suite and commit**

```bash
python -m pytest tests/ -v
```
Expected: 48 passed, 1 skipped (unchanged).
```bash
git add app_pages/quotation_request.py
git commit -m "feat: add rep-facing Quotations page (submit, resubmit, withdraw)"
```

---

## Task 5: `quotation_review.py` — Sales-Manager-Facing Page

**Read first:** spec section "New files → `app_pages/quotation_review.py`".

**Files:**
- Create: `app_pages/quotation_review.py`

- [ ] **Step 1: Page scaffold + role gate**

Role-gated to `sales manager`, `admin` only (mirror Task 4's gate pattern with this role set). Section header "Review Quotations".

- [ ] **Step 2: Pending queue**

Query `quotation_requests WHERE status = 'IN_REVIEW'` plus, for non-admin viewers, `AND rep_user_id != :manager_uid`. For each row: `render_quotation_detail(quotation_id)`, plus **Approve** / **Reject** (required reason `st.text_area`) / **Request Edit** (required comment `st.text_area`) buttons, each calling the matching `quotation_helpers` function and following the `ok, err = fn(...); st.rerun() if ok else st.error(err)` idiom already used in `admin_change_requests.py` (locate and match that exact call-site pattern, don't invent a different one). Show `render_revision_diff` when `_load_revisions(quotation_id)` has 2+ rows.

- [ ] **Step 3: Approved queue**

Query `quotation_requests WHERE status = 'APPROVED'` plus, for non-admin viewers, `AND rep_user_id != :manager_uid`. Read-only `render_quotation_detail` per row, plus **Return to Rep for Revision** (mandatory reason, same confirm-then-submit checkbox UX as Task 4's withdraw flow) calling `manager_return_for_revision`.

- [ ] **Step 4: Manual verification**

```bash
python -c "import app_pages.quotation_review"
```
Expected: no errors.

- [ ] **Step 5: Run full suite and commit**

```bash
python -m pytest tests/ -v
```
Expected: 48 passed, 1 skipped.
```bash
git add app_pages/quotation_review.py
git commit -m "feat: add sales-manager Review Quotations page"
```

---

## Task 6: `quotation_handoff.py` — Sales-Coordinator-Facing Page

**Read first:** spec section "New files → `app_pages/quotation_handoff.py`" and "Duplicate Odoo reference — hard block".

**Files:**
- Create: `app_pages/quotation_handoff.py`

- [ ] **Step 1: Page scaffold + role gate**

Filename and page function must be exactly `app_pages/quotation_handoff.py` / `page_quotation_handoff` (not `quotation_coordinator.py`). Role-gated to `sales coordinator`, `admin`. Section header "Quotation Handoff (Odoo)".

- [ ] **Step 2: Approved queue + Mark Done**

Query `quotation_requests WHERE status = 'APPROVED'`. For each row: `render_quotation_detail(quotation_id)`, an optional Odoo reference `st.text_input` and optional note `st.text_area`. Call `_odoo_reference_exists(ref)` on every rerun of that field (i.e. read the current widget value each render, not just on submit) — if it returns `True`, disable the **Mark Done** button (`disabled=True`) and show `st.error` naming the conflicting quotation (query `quotation_number` for the matching row so the error is specific, not generic). On click, call `coordinator_mark_done(quotation_id, coordinator_uid=u["user_id"], odoo_reference=ref, note=note)`.

- [ ] **Step 3: Manual verification**

```bash
python -c "import app_pages.quotation_handoff"
```
Expected: no errors.

- [ ] **Step 4: Run full suite and commit**

```bash
python -m pytest tests/ -v
```
Expected: 48 passed, 1 skipped.
```bash
git add app_pages/quotation_handoff.py
git commit -m "feat: add sales-coordinator Quotation Handoff page"
```

---

## Task 7: Navigation Wiring

**Read first:** spec section "Navigation wiring".

**Files:**
- Modify: `ui.py`, `app_v11.py`

- [ ] **Step 1: `ui.py::sidebar_nav()`**

In `field_pages` (currently built starting `["Submit Visit", "Check-In", "My Visits"]` then conditionally appending `"My Change Requests"` for roles including `rep`/`sales manager`/`biomedical manager`/`admin` — locate this exact block), add a second conditional append for `"Quotations"` using the same role set (`rep`, `sales manager`, `biomedical manager`, `admin`).

`review_pages` today is built as a single admin-only assignment: `if role == "admin": review_pages = ["Review Target Audiences", "Review Other Customers", "Review Change Requests"]`. Restructure this to a role-conditional append pattern (matching `field_pages`'s style) so `sales manager` and `sales coordinator` can also get entries without being `admin`:
```python
review_pages: list = []
if role == "admin":
    review_pages = ["Review Target Audiences", "Review Other Customers", "Review Change Requests"]
if role in ("sales manager", "admin"):
    review_pages.append("Review Quotations")
if role in ("sales coordinator", "admin"):
    review_pages.append("Quotation Handoff (Odoo)")
```
Preserve every existing entry and ordering for the `admin`-only pages — only add the two new conditional appends.

- [ ] **Step 2: `app_v11.py` — `PAGE_MAP` and `PAGE_ROLES`**

Import the three new page functions at the top of `app_v11.py` alongside the existing `app_pages` imports:
```python
from app_pages.quotation_request import page_quotation_request
from app_pages.quotation_review import page_quotation_review
from app_pages.quotation_handoff import page_quotation_handoff
```
In `PAGE_MAP`, add:
```python
"Quotations":                page_quotation_request,
"Review Quotations":         page_quotation_review,
"Quotation Handoff (Odoo)":  page_quotation_handoff,
```
In `PAGE_ROLES`, add:
```python
"Quotations":                ["rep", "sales manager", "biomedical manager", "admin"],
"Review Quotations":         ["sales manager", "admin"],
"Quotation Handoff (Odoo)":  ["sales coordinator", "admin"],
```

- [ ] **Step 3: Manual verification**

Start the app locally and log in as each of `rep`, `sales manager`, `sales coordinator`, `admin` test users (create them via Admin: Users if they don't exist — remember `"sales coordinator"` is now a selectable role per Task 1). Confirm: `rep` sees "Quotations" but not "Review Quotations"/"Quotation Handoff (Odoo)"; `sales manager` sees "Quotations" and "Review Quotations" but not "Quotation Handoff (Odoo)"; `sales coordinator` sees "Quotation Handoff (Odoo)" only (not "Quotations" or "Review Quotations" — coordinators don't submit or review); `admin` sees all three plus everything else. Also confirm each page renders without a Python traceback (the pages themselves are still functionally thin at this point — full functional testing is Task 8's live-DB test suite plus the plan's final manual walkthrough).

- [ ] **Step 4: Run full suite and commit**

```bash
python -m pytest tests/ -v
```
Expected: 48 passed, 1 skipped.
```bash
git add ui.py app_v11.py
git commit -m "feat: wire Quotations pages into navigation and role routing"
```

---

## Task 8: Race-Condition & Guard Test Suite

**Read first:** spec section "Verification → Automated tests", and read `tests/test_admin_change_requests_race.py` in full before writing anything — this task's fixture structure must mirror it exactly (self-cleaning, marked test rows, live-DB, no mocking).

**Files:**
- Create: `tests/test_quotation_workflow_race.py`

- [ ] **Step 1: Fixture scaffold**

Mirror `tests/test_admin_change_requests_race.py`'s structure: a `TEST_MARKER` constant, pytest fixtures that INSERT throwaway test users (a rep, two distinct sales managers, a coordinator, an admin — reuse existing test users if the fixture pattern in `test_targets_db.py`'s `admin_user_id`/`any_user_id` fixtures already provides suitable ones, otherwise create marked ones) and a throwaway customer/item if needed, `yield` the ids, then delete everything created (`quotation_status_events`, `quotation_revision_lines`, `quotation_revisions`, `quotation_lines`, `quotation_requests`, then any users/customers/items created solely for this test file) in teardown. A helper fixture/function to submit a baseline test quotation (1 line item) via `submit_quotation` so each test starts from a real `IN_REVIEW` row rather than hand-crafting SQL.

- [ ] **Step 2: Write all test cases from the spec**

Implement every test named in the spec's "Automated tests" list under `tests/test_quotation_workflow_race.py`:
`test_approve_after_reject_is_noop`, `test_reject_twice_is_noop`, `test_mark_done_before_approve_is_noop`, `test_resubmit_stale_version_is_noop`, `test_withdraw_while_in_review`, `test_withdraw_while_edit_requested`, `test_withdraw_after_approve_is_blocked`, `test_withdraw_by_another_rep_is_blocked`, `test_manager_action_after_withdrawal_is_noop`, `test_resubmit_after_withdrawal_is_blocked`, `test_manager_cannot_approve_own_submission`, `test_return_for_revision_from_approved`, `test_old_revisions_remain_unchanged`, `test_failed_resubmit_does_not_partially_update`, `test_duplicate_odoo_reference_is_blocked`. Each test's exact scenario is described in the spec — implement it as described, asserting on both the return tuple (`ok, err`) and, where the spec calls for it, on the DB state directly (e.g. `test_old_revisions_remain_unchanged` must re-query revision #1's rows after the resubmit and assert byte-identical values).

- [ ] **Step 3: Run the new suite in isolation first**

```bash
python -m pytest tests/test_quotation_workflow_race.py -v
```
Expected: all 15 tests PASSED. Debug any failures against the live DB directly before moving on — these tests exercise real concurrency guards, so a failure here is a real bug in Task 3's transition functions, not a test problem, unless you find the test itself is asserting the wrong thing per the spec.

- [ ] **Step 4: Run full suite and commit**

```bash
python -m pytest tests/ -v
```
Expected: 48 + 15 = 63 passed, 1 skipped.
```bash
git add tests/test_quotation_workflow_race.py
git commit -m "test: add quotation workflow race-condition and guard test suite"
```

---

## Final Manual Walkthrough (not a coding task — run after Task 8, before final review)

Follow the spec's "Verification → Manual walkthrough" paragraph exactly, end to end, using real test accounts through the actual running app (not just the automated tests). This is the only step that exercises the full UI flow (form prefill on Edit & Resubmit, live totals preview, disabled-button states, sidebar visibility per role) that the automated suite does not cover. Report any UX gap found; do not silently patch around it — raise it the same way any other review finding would be raised.
