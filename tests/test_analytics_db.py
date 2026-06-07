"""
Read-only integration tests for the 5 new analytics DB functions.
Requires DATABASE_URL env var pointing to a Postgres DB with data.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from datetime import date
from db_ops import (
    get_analytics_kpis_previous_period,
    get_analytics_drilldown,
    get_analytics_objective_categories,
    get_analytics_attendance,
    get_analytics_visits_per_rep,
    query_scalar,
)

D_FROM = date(2024, 1, 1)
D_TO   = date.today()
FILT   = {}
REPS   = None


@pytest.fixture(scope="module")
def admin_uid():
    uid = query_scalar("SELECT user_id FROM users WHERE role = 'admin' LIMIT 1")
    assert uid is not None, "Need at least one admin user in the test DB"
    return int(uid)


def test_previous_period_returns_dict_with_keys(admin_uid):
    result = get_analytics_kpis_previous_period(admin_uid, "admin", D_FROM, D_TO, FILT, REPS)
    assert isinstance(result, dict)
    if result.get("total_visits", 0) == 0:
        pytest.skip("No visits in previous period — cannot verify key set")
    for key in ("total_visits", "total_customers", "total_audiences",
                "visits_per_customer", "audiences_per_customer",
                "customers_per_day", "avg_customers_per_month", "avg_bl_per_month"):
        assert key in result, f"Missing key: {key}"


def test_previous_period_visits_non_negative(admin_uid):
    result = get_analytics_kpis_previous_period(admin_uid, "admin", D_FROM, D_TO, FILT, REPS)
    assert result["total_visits"] >= 0


def test_drilldown_has_required_columns(admin_uid):
    df = get_analytics_drilldown(admin_uid, "admin", D_FROM, D_TO, FILT, REPS)
    for col in ("region", "city", "sector", "customer_name",
                "business_unit", "product_category", "rep", "visit_count"):
        assert col in df.columns, f"Missing column: {col}"


def test_drilldown_visit_count_positive(admin_uid):
    df = get_analytics_drilldown(admin_uid, "admin", D_FROM, D_TO, FILT, REPS)
    if not df.empty:
        assert (df["visit_count"] > 0).all()


def test_drilldown_city_filter_reduces_results(admin_uid):
    df_all = get_analytics_drilldown(admin_uid, "admin", D_FROM, D_TO, {}, REPS)
    real_cities = df_all["city"].dropna()
    real_cities = real_cities[real_cities != "(No City)"]
    if real_cities.empty:
        pytest.skip("No city data in test DB")
    city = real_cities.iloc[0]
    df_city = get_analytics_drilldown(admin_uid, "admin", D_FROM, D_TO, {"city": city}, REPS)
    assert len(df_city) < len(df_all), "City filter should reduce row count"
    if not df_city.empty:
        assert df_city["city"].eq(city).all(), "All returned rows should match the filtered city"


def test_objective_categories_has_required_columns(admin_uid):
    df = get_analytics_objective_categories(admin_uid, "admin", D_FROM, D_TO, FILT, REPS)
    for col in ("objective_category", "objective_name", "count"):
        assert col in df.columns, f"Missing column: {col}"


def test_objective_categories_count_positive(admin_uid):
    df = get_analytics_objective_categories(admin_uid, "admin", D_FROM, D_TO, FILT, REPS)
    if not df.empty:
        assert (df["count"] > 0).all()


def test_attendance_has_required_columns(admin_uid):
    df = get_analytics_attendance(admin_uid, "admin", D_FROM, D_TO, REPS)
    for col in ("date", "rep_name", "visit_count"):
        assert col in df.columns, f"Missing column: {col}"


def test_attendance_visit_count_positive(admin_uid):
    df = get_analytics_attendance(admin_uid, "admin", D_FROM, D_TO, REPS)
    if not df.empty:
        assert (df["visit_count"] > 0).all()


def test_visits_per_rep_has_required_columns(admin_uid):
    df = get_analytics_visits_per_rep(admin_uid, "admin", D_FROM, D_TO, FILT, REPS)
    for col in ("rep", "total_visits", "total_customers"):
        assert col in df.columns, f"Missing column: {col}"


def test_visits_per_rep_sorted_descending(admin_uid):
    df = get_analytics_visits_per_rep(admin_uid, "admin", D_FROM, D_TO, FILT, REPS)
    if len(df) > 1:
        assert list(df["total_visits"]) == sorted(df["total_visits"].tolist(), reverse=True)


def test_kpis_all_keys_present(admin_uid):
    from db_ops import get_analytics_kpis
    result = get_analytics_kpis(admin_uid, "admin", D_FROM, D_TO, {}, None)
    for key in (
        "total_visits", "total_customers", "total_audiences",
        "visits_per_customer", "audiences_per_customer",
        "customers_per_day", "avg_customers_per_month", "avg_bl_per_month",
    ):
        assert key in result, f"Missing key after refactor: {key}"
    assert result["total_visits"] >= 0
    assert result["total_customers"] >= 0


def test_coverage_rate_shape(admin_uid):
    from db_ops import get_analytics_coverage_rate
    result = get_analytics_coverage_rate(admin_uid, "admin", D_FROM, D_TO, {}, None)
    assert "visited" in result
    assert "total_active" in result
    assert "coverage_pct" in result
    assert 0.0 <= result["coverage_pct"] <= 100.0
