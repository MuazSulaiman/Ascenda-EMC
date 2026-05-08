"""
Integration tests for admin_targets_db DB helpers.
Requires DATABASE_URL env var pointing to a dev database.
Each test cleans up after itself.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app_pages.admin_targets_db import (
    create_year, get_year, update_year, transition_year_status,
    get_all_years, get_reps_for_year, upsert_rep, remove_rep,
    get_rep_breakdown_count, add_breakdown_row, delete_breakdown_row,
    check_duplicate_breakdown, get_breakdown_rows, get_breakdown_totals,
)
from db_ops import exec_sql


TEST_YEAR = 2099  # unlikely to clash with real data
TEST_USER_ID = None  # set in fixture


@pytest.fixture(autouse=True)
def cleanup():
    """Remove test data before and after each test."""
    exec_sql("DELETE FROM target_breakdown WHERE year = :y", {"y": TEST_YEAR})
    exec_sql("DELETE FROM target_rep WHERE year = :y", {"y": TEST_YEAR})
    exec_sql("DELETE FROM target_year WHERE year = :y", {"y": TEST_YEAR})
    yield
    exec_sql("DELETE FROM target_breakdown WHERE year = :y", {"y": TEST_YEAR})
    exec_sql("DELETE FROM target_rep WHERE year = :y", {"y": TEST_YEAR})
    exec_sql("DELETE FROM target_year WHERE year = :y", {"y": TEST_YEAR})


@pytest.fixture
def admin_user_id():
    from db_ops import query_scalar
    uid = query_scalar("SELECT user_id FROM users WHERE role = 'admin' LIMIT 1")
    assert uid is not None, "Need at least one admin user in DB"
    return int(uid)


@pytest.fixture
def any_user_id():
    from db_ops import query_scalar
    uid = query_scalar("SELECT user_id FROM users LIMIT 1")
    assert uid is not None, "Need at least one user in DB"
    return int(uid)


def test_create_and_get_year(admin_user_id):
    create_year(TEST_YEAR, 500000, 300, admin_user_id)
    row = get_year(TEST_YEAR)
    assert row is not None
    assert float(row["target_amount"]) == 500000.0
    assert int(row["target_visits"]) == 300
    assert row["status"] == "DRAFT"


def test_create_duplicate_year_raises(admin_user_id):
    create_year(TEST_YEAR, 500000, 300, admin_user_id)
    with pytest.raises(Exception):
        create_year(TEST_YEAR, 999999, 100, admin_user_id)


def test_update_year(admin_user_id):
    create_year(TEST_YEAR, 500000, 300, admin_user_id)
    update_year(TEST_YEAR, 600000, 400, admin_user_id)
    row = get_year(TEST_YEAR)
    assert float(row["target_amount"]) == 600000.0
    assert int(row["target_visits"]) == 400


def test_transition_year_status(admin_user_id):
    create_year(TEST_YEAR, 500000, 300, admin_user_id)
    changed = transition_year_status(TEST_YEAR, "ACTIVE", admin_user_id, "DRAFT")
    assert changed is True
    row = get_year(TEST_YEAR)
    assert row["status"] == "ACTIVE"


def test_transition_year_status_active_to_locked(admin_user_id):
    create_year(TEST_YEAR, 500000, 300, admin_user_id)
    transition_year_status(TEST_YEAR, "ACTIVE", admin_user_id, "DRAFT")
    changed = transition_year_status(TEST_YEAR, "LOCKED", admin_user_id, "ACTIVE")
    assert changed is True
    row = get_year(TEST_YEAR)
    assert row["status"] == "LOCKED"


def test_transition_nonexistent_year_returns_false(admin_user_id):
    changed = transition_year_status(9999, "ACTIVE", admin_user_id, "DRAFT")
    assert changed is False


def test_transition_wrong_expected_status_does_not_change_row(admin_user_id):
    create_year(TEST_YEAR, 500000, 300, admin_user_id)
    changed = transition_year_status(TEST_YEAR, "LOCKED", admin_user_id, "ACTIVE")
    assert changed is False
    row = get_year(TEST_YEAR)
    assert row["status"] == "DRAFT"


def test_upsert_and_get_rep(admin_user_id, any_user_id):
    create_year(TEST_YEAR, 500000, 300, admin_user_id)
    upsert_rep(TEST_YEAR, any_user_id, 100000, 60, admin_user_id)
    reps = get_reps_for_year(TEST_YEAR)
    assert len(reps) == 1
    assert float(reps.iloc[0]["target_amount"]) == 100000.0


def test_remove_rep_with_no_breakdowns(admin_user_id, any_user_id):
    create_year(TEST_YEAR, 500000, 300, admin_user_id)
    upsert_rep(TEST_YEAR, any_user_id, 100000, 60, admin_user_id)
    reps = get_reps_for_year(TEST_YEAR)
    rep_id = int(reps.iloc[0]["target_rep_id"])
    assert get_rep_breakdown_count(rep_id) == 0
    remove_rep(rep_id)
    assert get_reps_for_year(TEST_YEAR).empty


def test_add_and_delete_breakdown_row(admin_user_id, any_user_id):
    create_year(TEST_YEAR, 500000, 300, admin_user_id)
    upsert_rep(TEST_YEAR, any_user_id, 100000, 60, admin_user_id)
    rep_id = int(get_reps_for_year(TEST_YEAR).iloc[0]["target_rep_id"])
    row = {
        "target_rep_id": rep_id, "year": TEST_YEAR, "user_id": any_user_id,
        "breakdown_level": "rep",
        "customer_id": None, "business_unit_id": None,
        "product_category_id": None, "business_line_id": None, "article_id": None,
        "target_amount": 50000, "target_visits": 30,
    }
    bd_id = add_breakdown_row(row, admin_user_id)
    assert bd_id is not None
    rows = get_breakdown_rows(rep_id)
    assert len(rows) == 1
    delete_breakdown_row(bd_id)
    assert get_breakdown_rows(rep_id).empty


def test_duplicate_breakdown_detection(admin_user_id, any_user_id):
    create_year(TEST_YEAR, 500000, 300, admin_user_id)
    upsert_rep(TEST_YEAR, any_user_id, 100000, 60, admin_user_id)
    rep_id = int(get_reps_for_year(TEST_YEAR).iloc[0]["target_rep_id"])
    dims = {"customer_id": None, "business_unit_id": None,
            "product_category_id": None, "business_line_id": None, "article_id": None}
    assert check_duplicate_breakdown(rep_id, dims) is False
    row = {"target_rep_id": rep_id, "year": TEST_YEAR, "user_id": any_user_id,
           "breakdown_level": "rep", **dims, "target_amount": 50000, "target_visits": 30}
    add_breakdown_row(row, admin_user_id)
    assert check_duplicate_breakdown(rep_id, dims) is True
