import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app_pages.admin_targets_db import (
    calc_productivity,
    derive_breakdown_level,
)


# ── calc_productivity ──────────────────────────────────────────────────────────

def test_productivity_normal():
    assert calc_productivity(420000, 280) == 1500.0

def test_productivity_rounded():
    assert calc_productivity(100000, 300) == 333.33

def test_productivity_zero_visits_returns_none():
    assert calc_productivity(500000, 0) is None

def test_productivity_zero_amount_zero_visits_returns_none():
    assert calc_productivity(0, 0) is None

def test_productivity_zero_amount_nonzero_visits():
    assert calc_productivity(0, 100) == 0.0


# ── derive_breakdown_level ─────────────────────────────────────────────────────

def test_level_article():
    assert derive_breakdown_level("PROD-001", 5, 2, 1, 10) == "article"

def test_level_business_line():
    assert derive_breakdown_level(None, 5, 2, 1, 10) == "business_line"

def test_level_product_category():
    assert derive_breakdown_level(None, None, 2, 1, 10) == "product_category"

def test_level_business_unit():
    assert derive_breakdown_level(None, None, None, 1, 10) == "business_unit"

def test_level_customer():
    assert derive_breakdown_level(None, None, None, None, 10) == "customer"

def test_level_rep_all_none():
    assert derive_breakdown_level(None, None, None, None, None) == "rep"
