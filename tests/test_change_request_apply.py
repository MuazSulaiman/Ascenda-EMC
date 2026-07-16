# tests/test_change_request_apply.py
"""
Regression test for the NumericValueOutOfRange bug in _apply_changes():
pd.read_sql_query silently turns a NULL new_value into float('nan') when the
same result set also contains non-null string values in that column (e.g. a
target-audience change to "Other", which nulls visits.audience_id alongside
populated other_audience_* string fields). Binding that raw nan straight into
an UPDATE ... integer_column = :val crashes psycopg with NumericValueOutOfRange.

_sql_val() must convert that reconstituted nan back to None before it is used
for validation or SQL binding.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
from sqlalchemy import create_engine, text

from app_pages.change_request_helpers import _sql_val


def test_sql_val_converts_nan_to_none():
    assert _sql_val(float("nan")) is None


def test_sql_val_passes_through_none():
    assert _sql_val(None) is None


def test_sql_val_passes_through_real_values():
    assert _sql_val("3") == "3"
    assert _sql_val("Sales Manager") == "Sales Manager"
    assert _sql_val(0) == 0
    assert _sql_val("") == ""


def test_pandas_reconstitutes_null_as_nan_when_mixed_with_strings():
    """
    Documents the exact pandas quirk that caused the production bug: a NULL
    row in a TEXT column comes back as float('nan') (not None) once other
    rows in the same read_sql_query result contain real strings.
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE request_change_details (field TEXT, new_value TEXT)"))
        conn.execute(
            text("INSERT INTO request_change_details VALUES (:f, :v)"),
            [
                {"f": "visits.audience_id", "v": None},
                {"f": "visits.other_audience_position", "v": "Pharmacist"},
            ],
        )
    with engine.begin() as conn:
        df = pd.read_sql_query(text("SELECT field, new_value FROM request_change_details"), conn)

    raw = df.iloc[0]["new_value"]
    assert isinstance(raw, float) and raw != raw  # nan, not None — the bug's raw material

    # The fix: sanitizing with _sql_val recovers the correct NULL semantics.
    assert _sql_val(raw) is None
