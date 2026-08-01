# tests/test_quotation_workflow_race.py
"""
Integration tests for the concurrency/authorization guards on the quotation
workflow's guarded state-transition functions in app_pages/quotation_helpers.py.

Requires DATABASE_URL env var. Runs against the live DB, no mocking. Mirrors
tests/test_admin_change_requests_race.py's structure: a TEST_MARKER constant,
self-cleaning fixtures (yield + teardown), and direct DB-state assertions
alongside the (ok, err) return-tuple contract.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uuid
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import text

from app_pages.quotation_helpers import (
    submit_quotation,
    resubmit_quotation,
    manager_approve,
    manager_reject,
    manager_request_edit,
    manager_return_for_revision,
    withdraw_quotation,
    coordinator_mark_done,
    _load_quotation_header,
    _load_quotation_lines,
    _load_revisions,
    _load_revision_lines,
)
from db import engine
from db_ops import query_scalar, exec_sql

TEST_MARKER = "PYTEST_QUOTATION_RACE_TMP"
BAD_PRODUCT_ID = "PYTEST_NONEXISTENT_PRODUCT_XYZ"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures: reuse existing DB users/customers/items where possible (mirrors
# test_targets_db.py's admin_user_id/any_user_id pattern); create marked,
# self-cleaning throwaway users only for roles this dev DB has none of
# (sales manager x2 distinct accounts, sales coordinator).
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def admin_user_id():
    uid = query_scalar("SELECT user_id FROM users WHERE role = 'admin' LIMIT 1")
    assert uid is not None, "Need at least one admin user in DB"
    return int(uid)


@pytest.fixture
def rep_user_id():
    uid = query_scalar("SELECT user_id FROM users WHERE role = 'rep' ORDER BY user_id LIMIT 1")
    assert uid is not None, "Need at least one rep user in DB"
    return int(uid)


@pytest.fixture
def other_rep_user_id():
    uid = query_scalar("SELECT user_id FROM users WHERE role = 'rep' ORDER BY user_id OFFSET 1 LIMIT 1")
    assert uid is not None, "Need at least two rep users in DB"
    return int(uid)


@pytest.fixture
def any_customer_id():
    cid = query_scalar("SELECT customer_id FROM customers ORDER BY customer_id LIMIT 1")
    assert cid is not None, "Need at least one customer in DB"
    return int(cid)


@pytest.fixture
def any_product_id():
    pid = query_scalar("SELECT product_id FROM items ORDER BY product_id LIMIT 1")
    assert pid is not None, "Need at least one item in DB"
    return pid


@pytest.fixture
def other_product_id(any_product_id):
    pid = query_scalar(
        "SELECT product_id FROM items WHERE product_id != :pid ORDER BY product_id LIMIT 1",
        {"pid": any_product_id},
    )
    assert pid is not None, "Need at least two items in DB"
    return pid


def _delete_test_user(uid: int):
    """
    Delete a throwaway test user created by this file. Defensively removes any
    quotation_requests rows still referencing this user (as rep/manager/
    coordinator/submitter/withdrawer) first, since quotation_requests' FKs to
    users have no ON DELETE CASCADE — a plain `DELETE FROM users` would 23503
    if a `qids`-fixture cleanup hasn't already run (fixture teardown order
    between independent fixtures is not guaranteed). Safe no-op otherwise.

    Also removes any notifications rows referencing this user (as recipient
    or actor) — Task 3 wired the quotation transition functions to insert
    notification rows via notify_users/notify_role, and notifications.
    recipient_user_id/actor_user_id also have no ON DELETE CASCADE to users.
    """
    exec_sql(
        """
        DELETE FROM quotation_requests
        WHERE rep_user_id = :uid OR manager_user_id = :uid
           OR coordinator_user_id = :uid OR submitted_by = :uid OR withdrawn_by = :uid
        """,
        {"uid": uid},
    )
    exec_sql(
        "DELETE FROM notifications WHERE recipient_user_id = :uid OR actor_user_id = :uid",
        {"uid": uid},
    )
    exec_sql("DELETE FROM users WHERE user_id = :uid", {"uid": uid})


def _create_test_user(role: str, label: str) -> int:
    email = f"pytest.{label}.{uuid.uuid4().hex[:10]}@example.test"
    with engine.begin() as conn:
        uid = conn.execute(
            text("""
                INSERT INTO users (email, password_hash, name, role, is_active)
                VALUES (:email, 'pytest-not-a-real-hash', :name, :role, TRUE)
                RETURNING user_id
            """),
            {"email": email, "name": f"{TEST_MARKER} {label}", "role": role},
        ).scalar_one()
    return int(uid)


@pytest.fixture
def sales_manager_a_id():
    uid = _create_test_user("sales manager", "mgr_a")
    yield uid
    _delete_test_user(uid)


@pytest.fixture
def sales_manager_b_id():
    uid = _create_test_user("sales manager", "mgr_b")
    yield uid
    _delete_test_user(uid)


@pytest.fixture
def coordinator_user_id():
    uid = _create_test_user("sales coordinator", "coord")
    yield uid
    _delete_test_user(uid)


@pytest.fixture
def qids():
    """List of quotation_id created during a test; each is deleted in teardown
    regardless of test outcome. quotation_lines / quotation_revisions /
    quotation_revision_lines / quotation_status_events all reference
    quotation_requests.quotation_id with ON DELETE CASCADE, so deleting the
    header row cascades to every child row for that quotation.

    notifications has no FK to quotation_requests (it only carries
    quotation_id inside the JSONB link_params column), so it isn't reached
    by that cascade — Task 3's notify_users/notify_role calls leave rows
    behind here that must be cleaned up explicitly, or they'd persist in
    the live dev DB (and reference real, non-test rep/manager/admin users,
    unlike the throwaway users _delete_test_user cleans up)."""
    ids: list[int] = []
    yield ids
    for qid in ids:
        exec_sql(
            "DELETE FROM notifications WHERE (link_params->>'quotation_id')::int = :qid",
            {"qid": qid},
        )
        exec_sql("DELETE FROM quotation_requests WHERE quotation_id = :qid", {"qid": qid})


def _base_header(customer_id: int, vat_rate=15, **overrides) -> dict:
    header = {
        "customer_id": customer_id,
        "quotation_date": date.today(),
        "vat_rate": vat_rate,
        "remarks": TEST_MARKER,
        "validity_days": 30,
        "delivery_terms": "Immediate",
        "payment_terms": "Cash on Delivery",
    }
    header.update(overrides)
    return header


def _base_lines(product_id: str, qty="1", price="100.00", discount="0") -> list[dict]:
    return [{"product_id": product_id, "quantity": qty, "unit_price": price, "discount_pct": discount}]


def _submit(qids_list, rep_uid, customer_id, product_id, **header_overrides):
    header = _base_header(customer_id, **header_overrides)
    qid, qnum = submit_quotation(header, _base_lines(product_id), rep_uid)
    qids_list.append(qid)
    return qid, qnum


@pytest.fixture
def baseline_quotation(qids, rep_user_id, any_customer_id, any_product_id):
    """A real IN_REVIEW quotation with one line item, submitted via submit_quotation."""
    qid, _ = _submit(qids, rep_user_id, any_customer_id, any_product_id)
    return qid


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_approve_after_reject_is_noop(baseline_quotation, sales_manager_a_id):
    qid = baseline_quotation
    ok, err = manager_reject(qid, sales_manager_a_id, "not a fit for this customer")
    assert ok, err

    ok2, err2 = manager_approve(qid, sales_manager_a_id)
    assert ok2 is False
    assert err2

    header = _load_quotation_header(qid)
    assert header["status"] == "REJECTED"


def test_reject_twice_is_noop(baseline_quotation, sales_manager_a_id):
    qid = baseline_quotation
    assert manager_reject(qid, sales_manager_a_id, "first reason")[0] is True
    ok2, err2 = manager_reject(qid, sales_manager_a_id, "second reason attempt")
    assert ok2 is False
    assert err2

    header = _load_quotation_header(qid)
    assert header["status"] == "REJECTED"
    assert header["manager_comment"] == "first reason"


def test_mark_done_before_approve_is_noop(baseline_quotation, coordinator_user_id):
    qid = baseline_quotation
    ok, err = coordinator_mark_done(qid, coordinator_user_id, None, None)
    assert ok is False
    assert err

    header = _load_quotation_header(qid)
    assert header["status"] == "IN_REVIEW"
    assert pd.isna(header.get("coordinator_done_at"))


def test_resubmit_stale_version_is_noop(
    baseline_quotation, rep_user_id, sales_manager_a_id, any_customer_id, any_product_id, other_product_id
):
    qid = baseline_quotation

    ok, err = manager_request_edit(qid, sales_manager_a_id, "please revise pricing")
    assert ok, err
    header = _load_quotation_header(qid)
    assert header["status"] == "EDIT_REQUESTED"
    stale_version = int(header["version"])  # 0 — captured before the first resubmit

    # First resubmit: legitimately uses the (still-current) version and bumps it.
    ok2, err2 = resubmit_quotation(
        qid, _base_header(any_customer_id), _base_lines(other_product_id), rep_user_id, stale_version
    )
    assert ok2, err2
    header2 = _load_quotation_header(qid)
    assert int(header2["version"]) == stale_version + 1
    assert header2["status"] == "IN_REVIEW"

    # Bring status back to EDIT_REQUESTED so "status alone" would satisfy the guard.
    ok3, err3 = manager_request_edit(qid, sales_manager_a_id, "revise again")
    assert ok3, err3
    header3 = _load_quotation_header(qid)
    assert header3["status"] == "EDIT_REQUESTED"
    assert int(header3["version"]) == stale_version + 1

    # Second resubmit reuses the stale (pre-bump) version — must fail even
    # though status='EDIT_REQUESTED' matches the guard.
    ok4, err4 = resubmit_quotation(
        qid, _base_header(any_customer_id), _base_lines(any_product_id), rep_user_id, stale_version
    )
    assert ok4 is False
    assert err4

    header4 = _load_quotation_header(qid)
    assert header4["status"] == "EDIT_REQUESTED"
    assert int(header4["version"]) == stale_version + 1


def test_withdraw_while_in_review(baseline_quotation, rep_user_id):
    qid = baseline_quotation
    ok, err = withdraw_quotation(qid, rep_user_id, "customer no longer interested")
    assert ok, err

    header = _load_quotation_header(qid)
    assert header["status"] == "WITHDRAWN"
    assert int(header["withdrawn_by"]) == rep_user_id
    assert not pd.isna(header.get("withdrawn_at"))


def test_withdraw_while_edit_requested(baseline_quotation, rep_user_id, sales_manager_a_id):
    qid = baseline_quotation
    ok, err = manager_request_edit(qid, sales_manager_a_id, "please fix quantities")
    assert ok, err

    ok2, err2 = withdraw_quotation(qid, rep_user_id, "no longer needed")
    assert ok2, err2

    header = _load_quotation_header(qid)
    assert header["status"] == "WITHDRAWN"


def test_withdraw_after_approve_is_blocked(baseline_quotation, rep_user_id, sales_manager_a_id):
    qid = baseline_quotation
    ok, err = manager_approve(qid, sales_manager_a_id)
    assert ok, err

    ok2, err2 = withdraw_quotation(qid, rep_user_id, "trying to withdraw too late")
    assert ok2 is False
    assert err2

    header = _load_quotation_header(qid)
    assert header["status"] == "APPROVED"


def test_withdraw_by_another_rep_is_blocked(baseline_quotation, other_rep_user_id):
    qid = baseline_quotation
    ok, err = withdraw_quotation(qid, other_rep_user_id, "not my quotation")
    assert ok is False
    assert err

    header = _load_quotation_header(qid)
    assert header["status"] == "IN_REVIEW"


def test_manager_action_after_withdrawal_is_noop(baseline_quotation, rep_user_id, sales_manager_a_id):
    qid = baseline_quotation
    ok, err = withdraw_quotation(qid, rep_user_id, "customer cancelled")
    assert ok, err

    for fn, args in [
        (manager_approve, (qid, sales_manager_a_id)),
        (manager_reject, (qid, sales_manager_a_id, "too late")),
        (manager_request_edit, (qid, sales_manager_a_id, "too late")),
    ]:
        ok2, err2 = fn(*args)
        assert ok2 is False, f"{fn.__name__} should be a no-op after withdrawal"
        assert err2

    header = _load_quotation_header(qid)
    assert header["status"] == "WITHDRAWN"


def test_resubmit_after_withdrawal_is_blocked(
    baseline_quotation, rep_user_id, sales_manager_a_id, any_customer_id, any_product_id
):
    qid = baseline_quotation
    ok, err = manager_request_edit(qid, sales_manager_a_id, "please revise")
    assert ok, err
    header = _load_quotation_header(qid)
    version = int(header["version"])

    ok2, err2 = withdraw_quotation(qid, rep_user_id, "no longer needed")
    assert ok2, err2

    ok3, err3 = resubmit_quotation(
        qid, _base_header(any_customer_id), _base_lines(any_product_id), rep_user_id, version
    )
    assert ok3 is False
    assert err3

    header2 = _load_quotation_header(qid)
    assert header2["status"] == "WITHDRAWN"


def test_manager_cannot_approve_own_submission(
    qids, sales_manager_a_id, sales_manager_b_id, any_customer_id, any_product_id
):
    qid, _ = _submit(qids, sales_manager_a_id, any_customer_id, any_product_id)

    for fn, args in [
        (manager_approve, (qid, sales_manager_a_id)),
        (manager_reject, (qid, sales_manager_a_id, "self reject")),
        (manager_request_edit, (qid, sales_manager_a_id, "self edit request")),
    ]:
        ok, err = fn(*args)
        assert ok is False, f"{fn.__name__} must be blocked for a self-submitted quotation"
        assert err

    header = _load_quotation_header(qid)
    assert header["status"] == "IN_REVIEW"

    # A genuinely distinct sales-manager account can act on it.
    ok2, err2 = manager_approve(qid, sales_manager_b_id)
    assert ok2, err2

    header2 = _load_quotation_header(qid)
    assert header2["status"] == "APPROVED"
    assert int(header2["manager_user_id"]) == sales_manager_b_id


def test_return_for_revision_from_approved(baseline_quotation, sales_manager_a_id):
    qid = baseline_quotation
    ok, err = manager_approve(qid, sales_manager_a_id)
    assert ok, err

    ok2, err2 = manager_return_for_revision(qid, sales_manager_a_id, "please adjust pricing")
    assert ok2, err2

    header = _load_quotation_header(qid)
    assert header["status"] == "EDIT_REQUESTED"

    still_in_approved_queue = query_scalar(
        "SELECT 1 FROM quotation_requests WHERE quotation_id = :qid AND status = 'APPROVED'", {"qid": qid}
    )
    assert still_in_approved_queue is None


def test_old_revisions_remain_unchanged(
    baseline_quotation, rep_user_id, sales_manager_a_id, any_customer_id, any_product_id, other_product_id
):
    qid = baseline_quotation

    revisions_before = _load_revisions(qid)
    assert len(revisions_before) == 1
    rev1_id = int(revisions_before.iloc[0]["revision_id"])
    rev1_header_before = revisions_before.iloc[0].to_dict()
    rev1_lines_before = _load_revision_lines(rev1_id).to_dict("records")
    assert len(rev1_lines_before) == 1

    ok, err = manager_request_edit(qid, sales_manager_a_id, "please change quantity and discount")
    assert ok, err
    header = _load_quotation_header(qid)

    ok2, err2 = resubmit_quotation(
        qid,
        _base_header(any_customer_id, vat_rate=20),
        _base_lines(other_product_id, qty="5", price="250.00", discount="10"),
        rep_user_id,
        int(header["version"]),
    )
    assert ok2, err2

    revisions_after = _load_revisions(qid)
    assert len(revisions_after) == 2

    rev1_row_after = revisions_after[revisions_after["revision_id"] == rev1_id].iloc[0].to_dict()
    assert rev1_row_after == rev1_header_before, "Revision #1's header row must be byte-identical after resubmit"

    rev1_lines_after = _load_revision_lines(rev1_id).to_dict("records")
    assert rev1_lines_after == rev1_lines_before, "Revision #1's line rows must be byte-identical after resubmit"


def test_failed_resubmit_does_not_partially_update(
    baseline_quotation, rep_user_id, sales_manager_a_id, any_customer_id
):
    qid = baseline_quotation
    ok, err = manager_request_edit(qid, sales_manager_a_id, "please fix line items")
    assert ok, err

    header_before = _load_quotation_header(qid)
    lines_before = _load_quotation_lines(qid).to_dict("records")
    revisions_before_count = len(_load_revisions(qid))

    # A non-existent product_id trips the FK constraint on quotation_lines
    # inside resubmit_quotation's single transaction — a real, DB-enforced
    # failure, not a simulated one.
    ok2, err2 = resubmit_quotation(
        qid, _base_header(any_customer_id), _base_lines(BAD_PRODUCT_ID), rep_user_id, int(header_before["version"])
    )
    assert ok2 is False
    assert err2

    header_after = _load_quotation_header(qid)
    assert int(header_after["version"]) == int(header_before["version"])
    assert header_after["status"] == header_before["status"] == "EDIT_REQUESTED"

    lines_after = _load_quotation_lines(qid).to_dict("records")
    assert lines_after == lines_before, "Original lines must survive a failed resubmit untouched"

    assert len(_load_revisions(qid)) == revisions_before_count, "No new revision snapshot on a failed resubmit"


def test_duplicate_odoo_reference_is_blocked(
    qids, rep_user_id, sales_manager_a_id, coordinator_user_id, any_customer_id, any_product_id
):
    qid1, _ = _submit(qids, rep_user_id, any_customer_id, any_product_id)
    qid2, _ = _submit(qids, rep_user_id, any_customer_id, any_product_id)

    assert manager_approve(qid1, sales_manager_a_id)[0] is True
    assert manager_approve(qid2, sales_manager_a_id)[0] is True

    ref = f"{TEST_MARKER}-ODOO-{qid1}"
    ok, err = coordinator_mark_done(qid1, coordinator_user_id, ref, None)
    assert ok, err

    # Layer 1: the _odoo_reference_exists() pre-check inside coordinator_mark_done
    # must reject reusing the same reference on a different quotation.
    ok2, err2 = coordinator_mark_done(qid2, coordinator_user_id, ref, None)
    assert ok2 is False
    assert err2 and "odoo" in err2.lower()

    header2 = _load_quotation_header(qid2)
    assert header2["status"] == "APPROVED"
    assert pd.isna(header2.get("odoo_reference"))

    # Layer 2: bypass the Python-level pre-check entirely and write the
    # duplicate reference directly — the DB's unique partial index
    # (idx_qr_odoo_ref_unique) must still reject it as the concurrent-race
    # backstop, independent of any application-level check.
    with pytest.raises(Exception):
        exec_sql(
            "UPDATE quotation_requests SET odoo_reference = :ref WHERE quotation_id = :qid",
            {"ref": ref, "qid": qid2},
        )

    header2_after = _load_quotation_header(qid2)
    assert pd.isna(header2_after.get("odoo_reference")), "Failed UPDATE must not have partially applied"
    assert header2_after["status"] == "APPROVED"
