# tests/test_admin_change_requests_race.py
"""
Integration tests for the resolve-race guard on change-request Approve/Reject.
Requires DATABASE_URL env var. Each test cleans up after itself.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from sqlalchemy import text

from app_pages.admin_change_requests import _apply_changes, _reject_request
from db import engine
from db_ops import exec_sql, query_scalar, query_df

TEST_MARKER = "PYTEST_RACE_GUARD_TMP"


@pytest.fixture
def admin_user_id():
    uid = query_scalar("SELECT user_id FROM users WHERE role = 'admin' LIMIT 1")
    assert uid is not None, "Need at least one admin user in DB"
    return int(uid)


@pytest.fixture
def rep_user_id():
    uid = query_scalar("SELECT user_id FROM users LIMIT 1")
    assert uid is not None
    return int(uid)


@pytest.fixture
def any_objective_id():
    oid = query_scalar("SELECT objective_id FROM objectives LIMIT 1")
    assert oid is not None, "Need at least one objective in DB"
    return int(oid)


@pytest.fixture
def any_customer_id():
    cid = query_scalar("SELECT customer_id FROM customers LIMIT 1")
    assert cid is not None, "Need at least one customer in DB"
    return int(cid)


@pytest.fixture
def temp_visit(rep_user_id, any_objective_id, any_customer_id):
    """Create a throwaway visit; delete it (and any cascaded rows) afterward."""
    with engine.begin() as conn:
        visit_id = conn.execute(
            text(
                """
                INSERT INTO visits (user_id, customer_id, objective_id, notes, evaluation, visit_type)
                VALUES (:uid, :cid, :oid, :marker, 'Neutral', 'Actual Visit')
                RETURNING visit_id
                """
            ),
            {"uid": rep_user_id, "cid": any_customer_id, "oid": any_objective_id, "marker": TEST_MARKER},
        ).scalar_one()
    yield int(visit_id)
    exec_sql("DELETE FROM visits WHERE visit_id = :vid", {"vid": visit_id})


@pytest.fixture
def temp_request(temp_visit, rep_user_id):
    """Create an IN_REVIEW request with one detail row changing visits.notes."""
    with engine.begin() as conn:
        request_id = conn.execute(
            text(
                """
                INSERT INTO request_changes (visit_id, change_source, requested_by, request_note, status, request_date)
                VALUES (:vid, 'REQUEST', :uid, 'pytest race guard', 'IN_REVIEW', NOW())
                RETURNING request_id
                """
            ),
            {"vid": temp_visit, "uid": rep_user_id},
        ).scalar_one()
        conn.execute(
            text(
                """
                INSERT INTO request_change_details (request_id, field, old_value, new_value)
                VALUES (:rid, 'visits.notes', :old, 'changed by pytest')
                """
            ),
            {"rid": request_id, "old": TEST_MARKER},
        )
    yield int(request_id), temp_visit
    exec_sql("DELETE FROM request_changes WHERE request_id = :rid", {"rid": request_id})


def test_reject_after_approve_is_rejected(temp_request, admin_user_id):
    """A stale admin tab rejecting an already-approved request must be a no-op."""
    request_id, visit_id = temp_request

    ok, err = _apply_changes(request_id, visit_id, admin_user_id)
    assert ok, err

    accepted = _reject_request(request_id, admin_user_id, "too late")
    assert accepted is False

    status = query_scalar("SELECT status FROM request_changes WHERE request_id = :rid", {"rid": request_id})
    assert status == "APPROVED"


def test_apply_error_cleared_on_success(temp_request, admin_user_id):
    """A successful (re-)approve must clear any stale apply_error from a prior failed attempt."""
    request_id, visit_id = temp_request
    exec_sql(
        "UPDATE request_changes SET apply_error = :err WHERE request_id = :rid",
        {"err": "stale failure from a previous attempt", "rid": request_id},
    )

    ok, err = _apply_changes(request_id, visit_id, admin_user_id)
    assert ok, err

    apply_error = query_scalar(
        "SELECT apply_error FROM request_changes WHERE request_id = :rid", {"rid": request_id}
    )
    assert apply_error is None


def test_reject_twice_second_is_noop(temp_request, admin_user_id):
    request_id, visit_id = temp_request

    assert _reject_request(request_id, admin_user_id, "first rejection") is True
    assert _reject_request(request_id, admin_user_id, "second rejection attempt") is False

    row = query_df(
        "SELECT status, reject_note FROM request_changes WHERE request_id = :rid", {"rid": request_id}
    ).iloc[0]
    assert row["status"] == "REJECTED"
    assert row["reject_note"] == "first rejection"
