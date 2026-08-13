# tests/test_sample_request_insights.py
"""
Coverage for app_pages.sample_request_helpers.get_rep_sample_stats and
get_customer_sample_stats (Task 3). Requires DATABASE_URL env var and runs
against the live dev DB, no mocking — mirrors the fixture style of
tests/test_sample_request_helpers_smoke.py: a TEST_MARKER constant,
self-cleaning fixtures (yield + teardown), and direct DB-state assertions.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uuid
from datetime import date

import pytest
from sqlalchemy import text

from app_pages.sample_request_helpers import (
    submit_sample_request,
    manager_approve,
    coordinator_mark_done,
    get_rep_sample_stats,
    get_customer_sample_stats,
)
from db import engine
from db_ops import query_scalar, query_df, exec_sql

TEST_MARKER = "PYTEST_SAMPLE_INSIGHTS_TMP"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures (same pattern as test_sample_request_helpers_smoke.py)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def any_product_id():
    pid = query_scalar("SELECT product_id FROM items ORDER BY product_id LIMIT 1")
    assert pid is not None, "Need at least one item in DB"
    return pid


def _delete_test_user(uid: int):
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


def _create_test_customer(label: str) -> int:
    with engine.begin() as conn:
        cid = conn.execute(
            text("""
                INSERT INTO customers (account_name, is_active)
                VALUES (:name, TRUE)
                RETURNING customer_id
            """),
            {"name": f"{TEST_MARKER} {label} {uuid.uuid4().hex[:8]}"},
        ).scalar_one()
    return int(cid)


def _delete_test_customer(cid: int):
    exec_sql("DELETE FROM sample_requests WHERE customer_id = :cid", {"cid": cid})
    exec_sql("DELETE FROM customers WHERE customer_id = :cid", {"cid": cid})


@pytest.fixture
def rep_a_id():
    uid = _create_test_user("rep", "repA")
    yield uid
    _delete_test_user(uid)


@pytest.fixture
def rep_b_id():
    uid = _create_test_user("rep", "repB")
    yield uid
    _delete_test_user(uid)


@pytest.fixture
def sales_manager_id():
    uid = _create_test_user("sales manager", "mgr")
    yield uid
    _delete_test_user(uid)


@pytest.fixture
def coordinator_id():
    uid = _create_test_user("sales coordinator", "coord")
    yield uid
    _delete_test_user(uid)


@pytest.fixture
def test_customer_id():
    cid = _create_test_customer("cust")
    yield cid
    _delete_test_customer(cid)


@pytest.fixture
def sids():
    ids: list[int] = []
    yield ids
    for sid in ids:
        exec_sql(
            "DELETE FROM notifications WHERE (link_params->>'sample_request_id')::int = :sid",
            {"sid": sid},
        )
        exec_sql("DELETE FROM sample_requests WHERE sample_request_id = :sid", {"sid": sid})


def _base_header(customer_id: int) -> dict:
    return {"customer_id": customer_id, "request_date": date.today(), "remarks": TEST_MARKER}


def _submit_approve_done(sids_list, rep_uid, mgr_uid, coord_uid, customer_id, product_id, qty):
    lines = [{"product_id": product_id, "quantity": qty}]
    sid, snum = submit_sample_request(_base_header(customer_id), lines, rep_uid)
    sids_list.append(sid)
    ok, err = manager_approve(sid, mgr_uid)
    assert ok, err
    ok2, err2 = coordinator_mark_done(sid, coord_uid, f"{TEST_MARKER}-{sid}", "handed off")
    assert ok2, err2
    return sid, snum


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_rep_zero_history_returns_all_zero(rep_a_id):
    stats = get_rep_sample_stats(rep_a_id)
    assert stats == {"all_time_qty": 0, "this_month_qty": 0, "recent": []}


def test_customer_zero_history_returns_all_zero(test_customer_id):
    stats = get_customer_sample_stats(test_customer_id)
    assert stats == {"all_time_qty": 0, "this_month_qty": 0, "recent": []}


def test_done_counts_but_approved_does_not(
    sids, rep_a_id, sales_manager_id, coordinator_id, test_customer_id, any_product_id
):
    # DONE request: counts.
    sid_done, snum_done = _submit_approve_done(
        sids, rep_a_id, sales_manager_id, coordinator_id, test_customer_id, any_product_id, qty=3
    )

    # APPROVED-but-not-DONE request: must NOT count.
    lines = [{"product_id": any_product_id, "quantity": 7}]
    sid_approved, _ = submit_sample_request(_base_header(test_customer_id), lines, rep_a_id)
    sids.append(sid_approved)
    ok, err = manager_approve(sid_approved, sales_manager_id)
    assert ok, err
    still_approved = query_scalar(
        "SELECT status FROM sample_requests WHERE sample_request_id = :sid", {"sid": sid_approved}
    )
    assert still_approved == "APPROVED"

    stats = get_rep_sample_stats(rep_a_id)
    assert stats["all_time_qty"] == 3, "only the DONE request's qty (3) should count, not the APPROVED one's (7)"
    assert stats["this_month_qty"] == 3
    assert len(stats["recent"]) == 1
    assert stats["recent"][0]["request_number"] == snum_done
    assert stats["recent"][0]["total_qty"] == 3
    assert "x3" in stats["recent"][0]["item_summary"]


def test_backdated_done_counts_all_time_but_not_this_month(
    sids, rep_a_id, sales_manager_id, coordinator_id, test_customer_id, any_product_id
):
    sid, _ = _submit_approve_done(
        sids, rep_a_id, sales_manager_id, coordinator_id, test_customer_id, any_product_id, qty=4
    )

    # Backdate coordinator_done_at to last month, directly via SQL (test fixture-only mutation).
    exec_sql(
        """
        UPDATE sample_requests
        SET coordinator_done_at = date_trunc('month', CURRENT_DATE) - INTERVAL '1 day'
        WHERE sample_request_id = :sid
        """,
        {"sid": sid},
    )
    backdated_at = query_scalar(
        "SELECT coordinator_done_at FROM sample_requests WHERE sample_request_id = :sid", {"sid": sid}
    )
    assert backdated_at < query_scalar("SELECT date_trunc('month', CURRENT_DATE)")

    stats = get_rep_sample_stats(rep_a_id)
    assert stats["all_time_qty"] == 4, "backdated DONE request must still count toward all_time_qty"
    assert stats["this_month_qty"] == 0, "backdated DONE request must NOT count toward this_month_qty"
    assert len(stats["recent"]) == 1


def test_customer_stats_span_multiple_reps(
    sids, rep_a_id, rep_b_id, sales_manager_id, coordinator_id, test_customer_id, any_product_id
):
    """
    get_customer_sample_stats has deliberately no rep_user_id filter: DONE
    requests from two different reps to the same customer must both count
    toward that customer's all_time_qty (cross-rep scoping), and each
    recent entry must carry the correct rep_name.
    """
    sid_a, snum_a = _submit_approve_done(
        sids, rep_a_id, sales_manager_id, coordinator_id, test_customer_id, any_product_id, qty=2
    )
    sid_b, snum_b = _submit_approve_done(
        sids, rep_b_id, sales_manager_id, coordinator_id, test_customer_id, any_product_id, qty=5
    )

    # Sanity: get_rep_sample_stats for each rep only sees their own request.
    stats_a = get_rep_sample_stats(rep_a_id)
    stats_b = get_rep_sample_stats(rep_b_id)
    assert stats_a["all_time_qty"] == 2
    assert stats_b["all_time_qty"] == 5

    cust_stats = get_customer_sample_stats(test_customer_id)
    assert cust_stats["all_time_qty"] == 7, "both reps' DONE requests to this customer must sum together"
    assert cust_stats["this_month_qty"] == 7
    assert len(cust_stats["recent"]) == 2

    request_numbers = {r["request_number"] for r in cust_stats["recent"]}
    assert request_numbers == {snum_a, snum_b}

    rep_names_by_request = {r["request_number"]: r["rep_name"] for r in cust_stats["recent"]}
    name_a = query_scalar("SELECT name FROM users WHERE user_id = :uid", {"uid": rep_a_id})
    name_b = query_scalar("SELECT name FROM users WHERE user_id = :uid", {"uid": rep_b_id})
    assert rep_names_by_request[snum_a] == name_a
    assert rep_names_by_request[snum_b] == name_b


def test_recent_limited_to_five_most_recent_done(
    sids, rep_a_id, sales_manager_id, coordinator_id, test_customer_id, any_product_id
):
    """recent must be capped at 5 entries even when more than 5 DONE requests exist."""
    for i in range(6):
        _submit_approve_done(
            sids, rep_a_id, sales_manager_id, coordinator_id, test_customer_id, any_product_id, qty=1
        )

    stats = get_rep_sample_stats(rep_a_id)
    assert stats["all_time_qty"] == 6
    assert len(stats["recent"]) == 5
