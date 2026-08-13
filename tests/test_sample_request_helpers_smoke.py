# tests/test_sample_request_helpers_smoke.py
"""
Basic smoke coverage for the guarded state-transition functions in
app_pages/sample_request_helpers.py (Task 2). Requires DATABASE_URL env var
and runs against the live dev DB, no mocking — mirrors the fixture style of
tests/test_quotation_workflow_race.py: a TEST_MARKER constant, self-cleaning
fixtures (yield + teardown), and direct DB-state assertions alongside the
(ok, err) return-tuple contract.

This file only proves each of the 8 transition functions works end-to-end
once each, plus one guard-rejection case. The full concurrency/race suite
(equivalent to test_quotation_workflow_race.py) is a later task (Task 8).
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uuid
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import text

from app_pages.sample_request_helpers import (
    submit_sample_request,
    resubmit_sample_request,
    manager_approve,
    manager_reject,
    manager_request_edit,
    manager_return_for_revision,
    withdraw_sample_request,
    coordinator_mark_done,
    _load_sample_request_header,
    _load_sample_request_lines,
    _load_revisions,
    _load_revision_lines,
    _load_status_events,
)
from db import engine
from db_ops import query_scalar, exec_sql

TEST_MARKER = "PYTEST_SAMPLE_REQUEST_SMOKE_TMP"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures: reuse existing DB users/customers/items where possible (mirrors
# test_quotation_workflow_race.py's pattern); create marked, self-cleaning
# throwaway users only for roles needed distinctly (sales manager, coordinator).
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def rep_user_id():
    uid = query_scalar("SELECT user_id FROM users WHERE role = 'rep' ORDER BY user_id LIMIT 1")
    assert uid is not None, "Need at least one rep user in DB"
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
    sample_requests rows still referencing this user (as rep/manager/
    coordinator/submitter/withdrawer) first, since sample_requests' FKs to
    users have no ON DELETE CASCADE. Also removes any notifications rows
    referencing this user (as recipient or actor) — those have no ON DELETE
    CASCADE to users either.
    """
    exec_sql(
        """
        DELETE FROM sample_requests
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
def sales_manager_id():
    uid = _create_test_user("sales manager", "mgr")
    yield uid
    _delete_test_user(uid)


@pytest.fixture
def coordinator_user_id():
    uid = _create_test_user("sales coordinator", "coord")
    yield uid
    _delete_test_user(uid)


@pytest.fixture
def sids():
    """List of sample_request_id created during a test; each is deleted in
    teardown regardless of test outcome. sample_request_lines /
    sample_request_revisions / sample_request_revision_lines /
    sample_request_status_events all reference sample_requests.sample_request_id
    with ON DELETE CASCADE, so deleting the header row cascades to every
    child row for that sample request.

    notifications has no FK to sample_requests (it only carries
    sample_request_id inside the JSONB link_params column), so it isn't
    reached by that cascade — the transition functions' notify_users/
    notify_role calls leave rows behind here that must be cleaned up
    explicitly, or they'd persist in the live dev DB (and reference real,
    non-test rep/manager/admin users, unlike the throwaway users
    _delete_test_user cleans up)."""
    ids: list[int] = []
    yield ids
    for sid in ids:
        exec_sql(
            "DELETE FROM notifications WHERE (link_params->>'sample_request_id')::int = :sid",
            {"sid": sid},
        )
        exec_sql("DELETE FROM sample_requests WHERE sample_request_id = :sid", {"sid": sid})


def _base_header(customer_id: int, **overrides) -> dict:
    header = {
        "customer_id": customer_id,
        "request_date": date.today(),
        "remarks": TEST_MARKER,
    }
    header.update(overrides)
    return header


def _base_lines(product_id: str, qty=2) -> list[dict]:
    return [{"product_id": product_id, "quantity": qty, "delivery_date": None}]


def _submit(sids_list, rep_uid, customer_id, product_id, **header_overrides):
    header = _base_header(customer_id, **header_overrides)
    sid, snum = submit_sample_request(header, _base_lines(product_id), rep_uid)
    sids_list.append(sid)
    return sid, snum


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_submit_approve_mark_done_happy_path(
    sids, rep_user_id, sales_manager_id, coordinator_user_id, any_customer_id, any_product_id
):
    sid, snum = _submit(sids, rep_user_id, any_customer_id, any_product_id)
    assert snum.startswith("SDR-")

    header = _load_sample_request_header(sid)
    assert header is not None
    assert header["status"] == "IN_REVIEW"
    assert header["version"] == 0

    lines_df = _load_sample_request_lines(sid)
    assert len(lines_df) == 1
    assert int(lines_df.iloc[0]["quantity"]) == 2

    revisions_df = _load_revisions(sid)
    assert len(revisions_df) == 1
    rev1_id = int(revisions_df.iloc[0]["revision_id"])
    rev1_lines = _load_revision_lines(rev1_id)
    assert len(rev1_lines) == 1

    events_df = _load_status_events(sid)
    assert list(events_df["event_type"]) == ["SUBMITTED"]

    ok, err = manager_approve(sid, sales_manager_id)
    assert ok, err
    header2 = _load_sample_request_header(sid)
    assert header2["status"] == "APPROVED"
    assert int(header2["manager_user_id"]) == sales_manager_id

    ref = f"{TEST_MARKER}-ODOO-{sid}"
    ok2, err2 = coordinator_mark_done(sid, coordinator_user_id, ref, "handed off")
    assert ok2, err2
    header3 = _load_sample_request_header(sid)
    assert header3["status"] == "DONE"
    assert header3["odoo_reference"] == ref
    assert int(header3["coordinator_user_id"]) == coordinator_user_id

    events_df3 = _load_status_events(sid)
    assert list(events_df3["event_type"]) == ["SUBMITTED", "APPROVED", "MARKED_DONE"]

    # Guard-rejection case: approving an already-resolved (DONE) row is a no-op.
    ok3, err3 = manager_approve(sid, sales_manager_id)
    assert ok3 is False
    assert err3
    header4 = _load_sample_request_header(sid)
    assert header4["status"] == "DONE"


def test_submit_reject(sids, rep_user_id, sales_manager_id, any_customer_id, any_product_id):
    sid, _ = _submit(sids, rep_user_id, any_customer_id, any_product_id)

    ok, err = manager_reject(sid, sales_manager_id, "not needed for this customer")
    assert ok, err

    header = _load_sample_request_header(sid)
    assert header["status"] == "REJECTED"
    assert header["manager_comment"] == "not needed for this customer"

    events_df = _load_status_events(sid)
    assert list(events_df["event_type"]) == ["SUBMITTED", "REJECTED"]


def test_submit_request_edit_resubmit_then_withdraw_is_blocked(
    sids, rep_user_id, sales_manager_id, any_customer_id, any_product_id, other_product_id
):
    sid, _ = _submit(sids, rep_user_id, any_customer_id, any_product_id)

    ok, err = manager_request_edit(sid, sales_manager_id, "please adjust quantities")
    assert ok, err
    header = _load_sample_request_header(sid)
    assert header["status"] == "EDIT_REQUESTED"
    version = int(header["version"])

    ok2, err2 = resubmit_sample_request(
        sid, _base_header(any_customer_id), _base_lines(other_product_id, qty=5), rep_user_id, version
    )
    assert ok2, err2
    header2 = _load_sample_request_header(sid)
    assert header2["status"] == "IN_REVIEW"
    assert int(header2["version"]) == version + 1

    lines_df = _load_sample_request_lines(sid)
    assert len(lines_df) == 1
    assert lines_df.iloc[0]["product_id"] == other_product_id
    assert int(lines_df.iloc[0]["quantity"]) == 5

    revisions_df = _load_revisions(sid)
    assert len(revisions_df) == 2

    events_df = _load_status_events(sid)
    assert list(events_df["event_type"]) == ["SUBMITTED", "EDIT_REQUESTED", "RESUBMITTED"]

    # Now IN_REVIEW again — withdraw should still work from IN_REVIEW...
    # but exercise the "withdraw is now blocked" case via APPROVED instead,
    # per the brief: submit -> request_edit -> resubmit -> withdraw-is-blocked.
    ok3, err3 = manager_approve(sid, sales_manager_id)
    assert ok3, err3

    ok4, err4 = withdraw_sample_request(sid, rep_user_id, "trying to withdraw too late")
    assert ok4 is False
    assert err4

    header3 = _load_sample_request_header(sid)
    assert header3["status"] == "APPROVED"


def test_return_for_revision_from_approved(sids, rep_user_id, sales_manager_id, any_customer_id, any_product_id):
    sid, _ = _submit(sids, rep_user_id, any_customer_id, any_product_id)

    ok, err = manager_approve(sid, sales_manager_id)
    assert ok, err

    ok2, err2 = manager_return_for_revision(sid, sales_manager_id, "please adjust delivery date")
    assert ok2, err2

    header = _load_sample_request_header(sid)
    assert header["status"] == "EDIT_REQUESTED"

    still_approved = query_scalar(
        "SELECT 1 FROM sample_requests WHERE sample_request_id = :sid AND status = 'APPROVED'", {"sid": sid}
    )
    assert still_approved is None


def test_withdraw_while_in_review(sids, rep_user_id, any_customer_id, any_product_id):
    sid, _ = _submit(sids, rep_user_id, any_customer_id, any_product_id)

    ok, err = withdraw_sample_request(sid, rep_user_id, "customer no longer interested")
    assert ok, err

    header = _load_sample_request_header(sid)
    assert header["status"] == "WITHDRAWN"
    assert int(header["withdrawn_by"]) == rep_user_id
    assert not pd.isna(header.get("withdrawn_at"))
