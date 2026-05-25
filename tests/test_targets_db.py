"""
Integration tests for admin_targets_db.
Requires DATABASE_URL env var. Each test cleans up after itself.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app_pages.admin_targets_db import (
    create_year, get_year, update_year, transition_year_status, get_all_years,
    get_reps_for_year, add_rep_to_year, remove_rep, get_rep_breakdown_count,
    add_breakdown_row, delete_breakdown_row, check_duplicate_breakdown,
    get_breakdown_rows, get_breakdown_totals,
)
from db_ops import exec_sql

TEST_YEAR = 2099


@pytest.fixture(autouse=True)
def cleanup():
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
    assert uid is not None
    return int(uid)


# ── Year tests ────────────────────────────────────────────────────────────────

def test_create_and_get_year(admin_user_id):
    create_year(TEST_YEAR, 500000, 300, admin_user_id)
    row = get_year(TEST_YEAR)
    assert row is not None
    assert float(row["budget_amount"]) == 500000.0
    assert int(row["budget_visits"]) == 300
    assert row["status"] == "DRAFT"


def test_create_duplicate_year_raises(admin_user_id):
    create_year(TEST_YEAR, 500000, 300, admin_user_id)
    with pytest.raises(Exception):
        create_year(TEST_YEAR, 999999, 100, admin_user_id)


def test_update_year(admin_user_id):
    create_year(TEST_YEAR, 500000, 300, admin_user_id)
    update_year(TEST_YEAR, 600000, 400, admin_user_id)
    row = get_year(TEST_YEAR)
    assert float(row["budget_amount"]) == 600000.0
    assert int(row["budget_visits"]) == 400


def test_transition_draft_to_active(admin_user_id):
    create_year(TEST_YEAR, 500000, 300, admin_user_id)
    changed = transition_year_status(TEST_YEAR, "ACTIVE", admin_user_id, "DRAFT")
    assert changed is True
    assert get_year(TEST_YEAR)["status"] == "ACTIVE"


def test_transition_active_to_locked(admin_user_id):
    create_year(TEST_YEAR, 500000, 300, admin_user_id)
    transition_year_status(TEST_YEAR, "ACTIVE", admin_user_id, "DRAFT")
    changed = transition_year_status(TEST_YEAR, "LOCKED", admin_user_id, "ACTIVE")
    assert changed is True
    assert get_year(TEST_YEAR)["status"] == "LOCKED"


def test_transition_wrong_expected_status_is_noop(admin_user_id):
    create_year(TEST_YEAR, 500000, 300, admin_user_id)
    changed = transition_year_status(TEST_YEAR, "LOCKED", admin_user_id, "ACTIVE")
    assert changed is False
    assert get_year(TEST_YEAR)["status"] == "DRAFT"


def test_transition_nonexistent_year_returns_false(admin_user_id):
    assert transition_year_status(9999, "ACTIVE", admin_user_id, "DRAFT") is False


# ── Rep tests ─────────────────────────────────────────────────────────────────

def test_add_rep_and_get_reps(admin_user_id, any_user_id):
    create_year(TEST_YEAR, 500000, 300, admin_user_id)
    rep_id = add_rep_to_year(TEST_YEAR, any_user_id, admin_user_id)
    assert rep_id is not None
    reps = get_reps_for_year(TEST_YEAR)
    assert len(reps) == 1
    assert int(reps.iloc[0]["user_id"]) == any_user_id


def test_rep_planned_amount_is_zero_before_breakdown(admin_user_id, any_user_id):
    create_year(TEST_YEAR, 500000, 300, admin_user_id)
    add_rep_to_year(TEST_YEAR, any_user_id, admin_user_id)
    reps = get_reps_for_year(TEST_YEAR)
    assert float(reps.iloc[0]["planned_amount"]) == 0.0


def test_remove_rep(admin_user_id, any_user_id):
    create_year(TEST_YEAR, 500000, 300, admin_user_id)
    rep_id = add_rep_to_year(TEST_YEAR, any_user_id, admin_user_id)
    remove_rep(rep_id)
    assert get_reps_for_year(TEST_YEAR).empty


# ── Breakdown tests ───────────────────────────────────────────────────────────

def _make_rep(admin_user_id, any_user_id):
    create_year(TEST_YEAR, 500000, 300, admin_user_id)
    return add_rep_to_year(TEST_YEAR, any_user_id, admin_user_id)


def _base_dims():
    return {
        "customer_id": None, "business_unit_id": None,
        "product_category_id": None, "business_line_id": None, "article_id": None,
    }


def test_add_and_delete_breakdown_row(admin_user_id, any_user_id):
    rep_id = _make_rep(admin_user_id, any_user_id)
    row = {
        "target_rep_id": rep_id, "year": TEST_YEAR, "user_id": any_user_id,
        "breakdown_level": "business_unit", **_base_dims(),
        "target_amount": 50000, "target_visits": 30,
    }
    bd_id = add_breakdown_row(row, admin_user_id)
    assert bd_id is not None
    rows = get_breakdown_rows(rep_id)
    assert len(rows) == 1
    delete_breakdown_row(bd_id)
    assert get_breakdown_rows(rep_id).empty


def test_breakdown_totals_sum_from_rows(admin_user_id, any_user_id):
    rep_id = _make_rep(admin_user_id, any_user_id)
    for level, amount in [("business_unit", 50000), ("customer", 30000)]:
        add_breakdown_row({
            "target_rep_id": rep_id, "year": TEST_YEAR, "user_id": any_user_id,
            "breakdown_level": level, **_base_dims(),
            "target_amount": amount, "target_visits": 10,
        }, admin_user_id)
    totals = get_breakdown_totals(rep_id)
    assert totals["amount"] == 80000.0
    assert totals["visits"] == 20


def test_rep_planned_amount_reflects_breakdown(admin_user_id, any_user_id):
    rep_id = _make_rep(admin_user_id, any_user_id)
    add_breakdown_row({
        "target_rep_id": rep_id, "year": TEST_YEAR, "user_id": any_user_id,
        "breakdown_level": "business_unit", **_base_dims(),
        "target_amount": 120000, "target_visits": 60,
    }, admin_user_id)
    reps = get_reps_for_year(TEST_YEAR)
    assert float(reps.iloc[0]["planned_amount"]) == 120000.0


def test_remove_rep_cascades_breakdown(admin_user_id, any_user_id):
    rep_id = _make_rep(admin_user_id, any_user_id)
    add_breakdown_row({
        "target_rep_id": rep_id, "year": TEST_YEAR, "user_id": any_user_id,
        "breakdown_level": "business_unit", **_base_dims(),
        "target_amount": 50000, "target_visits": 30,
    }, admin_user_id)
    remove_rep(rep_id)
    assert get_reps_for_year(TEST_YEAR).empty


def test_duplicate_breakdown_detection(admin_user_id, any_user_id):
    rep_id = _make_rep(admin_user_id, any_user_id)
    dims = _base_dims()
    assert check_duplicate_breakdown(rep_id, "business_unit", dims) is False
    add_breakdown_row({
        "target_rep_id": rep_id, "year": TEST_YEAR, "user_id": any_user_id,
        "breakdown_level": "business_unit", **dims,
        "target_amount": 50000, "target_visits": 30,
    }, admin_user_id)
    assert check_duplicate_breakdown(rep_id, "business_unit", dims) is True


def test_same_dims_different_level_not_duplicate(admin_user_id, any_user_id):
    rep_id = _make_rep(admin_user_id, any_user_id)
    dims = _base_dims()
    add_breakdown_row({
        "target_rep_id": rep_id, "year": TEST_YEAR, "user_id": any_user_id,
        "breakdown_level": "business_unit", **dims,
        "target_amount": 50000, "target_visits": 30,
    }, admin_user_id)
    # Same null dims but different level — must NOT be a duplicate
    assert check_duplicate_breakdown(rep_id, "customer", dims) is False


def test_get_rep_breakdown_count(admin_user_id, any_user_id):
    rep_id = _make_rep(admin_user_id, any_user_id)
    assert get_rep_breakdown_count(rep_id) == 0
    add_breakdown_row({
        "target_rep_id": rep_id, "year": TEST_YEAR, "user_id": any_user_id,
        "breakdown_level": "business_unit", **_base_dims(),
        "target_amount": 10000, "target_visits": 5,
    }, admin_user_id)
    assert get_rep_breakdown_count(rep_id) == 1
