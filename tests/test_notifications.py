# tests/test_notifications.py
"""
Integration tests for the in-app notification system (Tasks 1-5): the
`notifications` table, `app_pages/notification_helpers.py`'s notify_users/
notify_role/get_unread_count/list_notifications/mark_read/mark_all_read, and
the notification hooks wired into every quotation and change-request
transition function.

Requires DATABASE_URL env var. Runs against the live DB, no mocking. Mirrors
tests/test_quotation_workflow_race.py's and tests/test_admin_change_requests_race.py's
structure: a TEST_MARKER constant, self-cleaning fixtures (yield + teardown),
and direct DB-state assertions (querying `notifications` directly rather than
only through list_notifications, so these tests aren't circularly validating
the read helper against itself).

There is no tests/conftest.py in this repo (fixtures aren't shared across
test files), so the `_create_test_user` / `_delete_test_user` helper pattern
from test_quotation_workflow_race.py is duplicated here verbatim rather than
imported.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uuid
from datetime import date

import pytest
from sqlalchemy import text

from app_pages.quotation_helpers import (
    submit_quotation,
    manager_approve,
    manager_reject,
    manager_request_edit,
    manager_return_for_revision,
    coordinator_mark_done,
)
from app_pages.sample_request_helpers import (
    submit_sample_request,
    manager_approve as sr_manager_approve,
    manager_reject as sr_manager_reject,
    manager_request_edit as sr_manager_request_edit,
    manager_return_for_revision as sr_manager_return_for_revision,
    coordinator_mark_done as sr_coordinator_mark_done,
)
from app_pages.change_request import _insert_request_and_details
from app_pages.admin_change_requests import _apply_changes, _reject_request
from app_pages.notification_helpers import (
    get_unread_count,
    list_notifications,
    mark_read,
    mark_all_read,
)
from db import engine
from db_ops import query_df, query_scalar, exec_sql

TEST_MARKER = "PYTEST_NOTIFICATIONS_TMP"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures: reuse existing DB users/customers/items/objectives where possible;
# create marked, self-cleaning throwaway users only where a test needs a
# guaranteed-distinct or guaranteed-inactive account.
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
def any_objective_id():
    oid = query_scalar("SELECT objective_id FROM objectives LIMIT 1")
    assert oid is not None, "Need at least one objective in DB"
    return int(oid)


def _delete_test_user(uid: int):
    """
    Delete a throwaway test user created by this file. Defensively removes any
    quotation_requests and sample_requests rows still referencing this user
    (as rep/manager/coordinator/submitted_by/withdrawn_by) and any
    notifications rows referencing this user (as recipient or actor) first —
    all of these FKs to users have no ON DELETE CASCADE, so a plain
    `DELETE FROM users` would 23503 if a test left rows behind (e.g. if this
    fixture's teardown runs before the sids/qids fixture's, which fixture
    teardown LIFO ordering does not guarantee against). Safe no-op otherwise.
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


def _create_test_user(role: str, label: str, is_active: bool = True) -> int:
    email = f"pytest.{label}.{uuid.uuid4().hex[:10]}@example.test"
    with engine.begin() as conn:
        uid = conn.execute(
            text("""
                INSERT INTO users (email, password_hash, name, role, is_active)
                VALUES (:email, 'pytest-not-a-real-hash', :name, :role, :is_active)
                RETURNING user_id
            """),
            {"email": email, "name": f"{TEST_MARKER} {label}", "role": role, "is_active": is_active},
        ).scalar_one()
    return int(uid)


@pytest.fixture
def sales_manager_a_id():
    uid = _create_test_user("sales manager", "mgr_a")
    yield uid
    _delete_test_user(uid)


@pytest.fixture
def coordinator_user_id():
    uid = _create_test_user("sales coordinator", "coord")
    yield uid
    _delete_test_user(uid)


@pytest.fixture
def inactive_sales_manager_id():
    """An active-role-matching but is_active=FALSE user — must never be
    picked up by notify_role's `is_active = TRUE` filter."""
    uid = _create_test_user("sales manager", "inactive_mgr", is_active=False)
    yield uid
    _delete_test_user(uid)


@pytest.fixture
def notif_owner_id():
    uid = _create_test_user("rep", "notif_owner")
    yield uid
    _delete_test_user(uid)


@pytest.fixture
def notif_other_id():
    uid = _create_test_user("rep", "notif_other")
    yield uid
    _delete_test_user(uid)


@pytest.fixture
def qids():
    """List of quotation_id created during a test; each is deleted in teardown
    regardless of test outcome. notifications has no FK to quotation_requests
    (it only carries quotation_id inside the JSONB link_params column), so
    it isn't reached by quotation_requests' cascade — it must be cleaned up
    explicitly, or rows referencing real rep/manager/coordinator/admin users
    would persist in the live dev DB."""
    ids: list[int] = []
    yield ids
    for qid in ids:
        exec_sql(
            "DELETE FROM notifications WHERE (link_params->>'quotation_id')::int = :qid",
            {"qid": qid},
        )
        exec_sql("DELETE FROM quotation_requests WHERE quotation_id = :qid", {"qid": qid})


@pytest.fixture
def sids():
    """List of sample_request_id created during a test; each is deleted in
    teardown regardless of test outcome. sample_request_lines /
    sample_request_revisions / sample_request_revision_lines /
    sample_request_status_events all reference
    sample_requests.sample_request_id with ON DELETE CASCADE, so deleting the
    header row cascades to every child row for that sample request.

    notifications has no FK to sample_requests (it only carries
    sample_request_id inside the JSONB link_params column), so it isn't
    reached by that cascade — it must be cleaned up explicitly, mirroring
    the qids fixture above for quotations."""
    ids: list[int] = []
    yield ids
    for sid in ids:
        exec_sql(
            "DELETE FROM notifications WHERE (link_params->>'sample_request_id')::int = :sid",
            {"sid": sid},
        )
        exec_sql("DELETE FROM sample_requests WHERE sample_request_id = :sid", {"sid": sid})


@pytest.fixture
def temp_visit(rep_user_id, any_objective_id, any_customer_id):
    """Create a throwaway visit; delete it (and any request_changes /
    notifications referencing it) afterward. request_change_details cascades
    off request_changes via ON DELETE CASCADE."""
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
    exec_sql(
        "DELETE FROM notifications WHERE (link_params->>'visit_id')::int = :vid", {"vid": visit_id}
    )
    # CR_SUBMITTED notifications (Task 4, fixed to deep-link admins by request_id)
    # carry link_params={"preselect": request_id}, not visit_id, so they aren't
    # caught by the visit_id-keyed DELETE above — clean them up via the
    # request_changes rows for this visit before those rows are removed.
    exec_sql(
        """
        DELETE FROM notifications
        WHERE (link_params->>'preselect')::int IN (
            SELECT request_id FROM request_changes WHERE visit_id = :vid
        )
        """,
        {"vid": visit_id},
    )
    exec_sql("DELETE FROM request_changes WHERE visit_id = :vid", {"vid": visit_id})
    exec_sql("DELETE FROM visits WHERE visit_id = :vid", {"vid": visit_id})


@pytest.fixture
def temp_request(temp_visit, rep_user_id):
    """Create an IN_REVIEW request with one detail row changing visits.notes.
    Cleanup of both request_changes and any notifications tied to the visit
    is handled by temp_visit's teardown (which runs after this fixture's,
    since temp_request depends on it) — this fixture's own teardown is just
    a redundant, harmless no-op safety net."""
    with engine.begin() as conn:
        request_id = conn.execute(
            text(
                """
                INSERT INTO request_changes (visit_id, change_source, requested_by, request_note, status, request_date)
                VALUES (:vid, 'REQUEST', :uid, 'pytest notifications', 'IN_REVIEW', NOW())
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


def _quotation_recipients(qid: int, event_type: str) -> set[int]:
    df = query_df(
        """
            SELECT recipient_user_id FROM notifications
            WHERE category = 'quotation' AND event_type = :et
              AND (link_params->>'quotation_id')::int = :qid
        """,
        {"et": event_type, "qid": qid},
    )
    return set(int(x) for x in df["recipient_user_id"])


def _quotation_actors(qid: int, event_type: str) -> set[int]:
    df = query_df(
        """
            SELECT DISTINCT actor_user_id FROM notifications
            WHERE category = 'quotation' AND event_type = :et
              AND (link_params->>'quotation_id')::int = :qid
        """,
        {"et": event_type, "qid": qid},
    )
    return set(int(x) for x in df["actor_user_id"])


def _sample_recipients(sid: int, event_type: str) -> set[int]:
    df = query_df(
        """
            SELECT recipient_user_id FROM notifications
            WHERE category = 'sample_request' AND event_type = :et
              AND (link_params->>'sample_request_id')::int = :sid
        """,
        {"et": event_type, "sid": sid},
    )
    return set(int(x) for x in df["recipient_user_id"])


def _sample_actors(sid: int, event_type: str) -> set[int]:
    df = query_df(
        """
            SELECT DISTINCT actor_user_id FROM notifications
            WHERE category = 'sample_request' AND event_type = :et
              AND (link_params->>'sample_request_id')::int = :sid
        """,
        {"et": event_type, "sid": sid},
    )
    return set(int(x) for x in df["actor_user_id"])


def _cr_recipients(visit_id: int, event_type: str) -> set[int]:
    df = query_df(
        """
            SELECT recipient_user_id FROM notifications
            WHERE category = 'change_request' AND event_type = :et
              AND (link_params->>'visit_id')::int = :vid
        """,
        {"et": event_type, "vid": visit_id},
    )
    return set(int(x) for x in df["recipient_user_id"])


def _cr_recipients_by_request(request_id: int, event_type: str) -> set[int]:
    """Like _cr_recipients, but for CR_SUBMITTED notifications, which link to
    "Review Change Requests" via link_params={"preselect": request_id} (an
    admin-facing deep link keyed by request_id, not visit_id — see
    app_pages/change_request.py::_insert_request_and_details)."""
    df = query_df(
        """
            SELECT recipient_user_id FROM notifications
            WHERE category = 'change_request' AND event_type = :et
              AND (link_params->>'preselect')::int = :rid
        """,
        {"et": event_type, "rid": request_id},
    )
    return set(int(x) for x in df["recipient_user_id"])


def _base_header_sr(customer_id: int, **overrides) -> dict:
    header = {
        "customer_id": customer_id,
        "request_date": date.today(),
        "remarks": TEST_MARKER,
    }
    header.update(overrides)
    return header


def _base_lines_sr(product_id: str, qty=2) -> list[dict]:
    return [{"product_id": product_id, "quantity": qty, "delivery_date": None}]


def _submit_sr(sids_list, rep_uid, customer_id, product_id, **header_overrides):
    header = _base_header_sr(customer_id, **header_overrides)
    sid, snum = submit_sample_request(header, _base_lines_sr(product_id), rep_uid)
    sids_list.append(sid)
    return sid, snum


def _active_role_ids(roles: list[str], exclude_user_id: int | None = None) -> set[int]:
    df = query_df(
        "SELECT user_id FROM users WHERE role = ANY(:roles) AND is_active = TRUE",
        {"roles": roles},
    )
    ids = set(int(x) for x in df["user_id"])
    if exclude_user_id is not None:
        ids.discard(exclude_user_id)
    return ids


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_submit_quotation_notifies_managers_and_admins_excludes_actor(
    qids, sales_manager_a_id, any_customer_id, any_product_id
):
    """submit_quotation must notify every active sales manager/admin, but not
    the submitting actor even though that actor holds the 'sales manager' role."""
    expected = _active_role_ids(["sales manager", "admin"], exclude_user_id=sales_manager_a_id)
    assert expected, "Need at least one other active sales manager/admin in DB"

    qid, _ = _submit(qids, sales_manager_a_id, any_customer_id, any_product_id)

    recipients = _quotation_recipients(qid, "SUBMITTED")
    assert recipients == expected
    assert sales_manager_a_id not in recipients
    assert _quotation_actors(qid, "SUBMITTED") == {sales_manager_a_id}


def test_manager_approve_notifies_rep_and_coordinators_and_admins(
    qids, rep_user_id, sales_manager_a_id, coordinator_user_id, any_customer_id, any_product_id
):
    """manager_approve must notify the submitting rep AND every active sales
    coordinator/admin."""
    qid, _ = _submit(qids, rep_user_id, any_customer_id, any_product_id)

    expected_coord_admin = _active_role_ids(["sales coordinator", "admin"])
    assert coordinator_user_id in expected_coord_admin
    expected = expected_coord_admin | {rep_user_id}

    ok, err = manager_approve(qid, sales_manager_a_id)
    assert ok, err

    recipients = _quotation_recipients(qid, "APPROVED")
    assert recipients == expected
    assert _quotation_actors(qid, "APPROVED") == {sales_manager_a_id}


def test_manager_reject_notifies_rep_only(
    qids, rep_user_id, sales_manager_a_id, any_customer_id, any_product_id
):
    qid, _ = _submit(qids, rep_user_id, any_customer_id, any_product_id)

    ok, err = manager_reject(qid, sales_manager_a_id, "not a fit for this customer")
    assert ok, err

    recipients = _quotation_recipients(qid, "REJECTED")
    assert recipients == {rep_user_id}
    assert _quotation_actors(qid, "REJECTED") == {sales_manager_a_id}


def test_manager_request_edit_notifies_rep_only(
    qids, rep_user_id, sales_manager_a_id, any_customer_id, any_product_id
):
    qid, _ = _submit(qids, rep_user_id, any_customer_id, any_product_id)

    ok, err = manager_request_edit(qid, sales_manager_a_id, "please revise pricing")
    assert ok, err

    recipients = _quotation_recipients(qid, "EDIT_REQUESTED")
    assert recipients == {rep_user_id}
    assert _quotation_actors(qid, "EDIT_REQUESTED") == {sales_manager_a_id}


def test_manager_return_for_revision_notifies_rep_only(
    qids, rep_user_id, sales_manager_a_id, any_customer_id, any_product_id
):
    qid, _ = _submit(qids, rep_user_id, any_customer_id, any_product_id)
    ok, err = manager_approve(qid, sales_manager_a_id)
    assert ok, err

    ok2, err2 = manager_return_for_revision(qid, sales_manager_a_id, "please adjust pricing")
    assert ok2, err2

    recipients = _quotation_recipients(qid, "RETURNED_FOR_REVISION")
    assert recipients == {rep_user_id}
    assert _quotation_actors(qid, "RETURNED_FOR_REVISION") == {sales_manager_a_id}


def test_coordinator_mark_done_notifies_rep_and_deciding_manager(
    qids, rep_user_id, sales_manager_a_id, coordinator_user_id, any_customer_id, any_product_id
):
    qid, _ = _submit(qids, rep_user_id, any_customer_id, any_product_id)
    ok, err = manager_approve(qid, sales_manager_a_id)
    assert ok, err

    ok2, err2 = coordinator_mark_done(qid, coordinator_user_id, None, None)
    assert ok2, err2

    recipients = _quotation_recipients(qid, "MARKED_DONE")
    assert recipients == {rep_user_id, sales_manager_a_id}
    assert _quotation_actors(qid, "MARKED_DONE") == {coordinator_user_id}


def test_change_request_submission_notifies_admins_excludes_submitter(
    temp_visit, rep_user_id
):
    expected = _active_role_ids(["admin"], exclude_user_id=rep_user_id)
    assert expected, "Need at least one active admin in DB"

    request_id = _insert_request_and_details(
        temp_visit, rep_user_id, "pytest notifications",
        [{"field": "visits.notes", "old_value": TEST_MARKER, "new_value": "changed by pytest"}],
    )
    assert request_id is not None

    recipients = _cr_recipients_by_request(request_id, "CR_SUBMITTED")
    assert recipients == expected
    assert rep_user_id not in recipients


def test_change_request_approval_notifies_submitter_only(temp_request, admin_user_id):
    request_id, visit_id = temp_request

    ok, err = _apply_changes(request_id, visit_id, admin_user_id)
    assert ok, err

    recipients = _cr_recipients(visit_id, "CR_APPROVED")
    # temp_request's requester is rep_user_id — resolved via request_changes.requested_by
    requested_by = query_scalar(
        "SELECT requested_by FROM request_changes WHERE request_id = :rid", {"rid": request_id}
    )
    assert recipients == {int(requested_by)}
    assert admin_user_id not in recipients


def test_change_request_rejection_notifies_submitter_only(temp_request, admin_user_id):
    request_id, visit_id = temp_request

    accepted = _reject_request(request_id, admin_user_id, "not applicable")
    assert accepted is True

    requested_by = query_scalar(
        "SELECT requested_by FROM request_changes WHERE request_id = :rid", {"rid": request_id}
    )
    recipients = _cr_recipients(visit_id, "CR_REJECTED")
    assert recipients == {int(requested_by)}
    assert admin_user_id not in recipients


def test_inactive_user_never_notified(
    qids, rep_user_id, inactive_sales_manager_id, any_customer_id, any_product_id
):
    """notify_role must filter on is_active = TRUE — a user whose role
    matches but who is inactive must never receive a notification."""
    qid, _ = _submit(qids, rep_user_id, any_customer_id, any_product_id)

    recipients = _quotation_recipients(qid, "SUBMITTED")
    assert inactive_sales_manager_id not in recipients


def test_mark_read_only_affects_owning_user(notif_owner_id, notif_other_id):
    nid = query_scalar(
        """
            INSERT INTO notifications (recipient_user_id, category, event_type, title)
            VALUES (:rid, 'test', 'TEST_EVENT', 'pytest notification')
            RETURNING notification_id
        """,
        {"rid": notif_owner_id},
    )
    nid = int(nid)

    # Wrong uid — must be a no-op, leaving the notification unread.
    mark_read(nid, notif_other_id)
    is_read = query_scalar(
        "SELECT is_read FROM notifications WHERE notification_id = :nid", {"nid": nid}
    )
    assert is_read is False

    # Correct uid — must flip is_read.
    mark_read(nid, notif_owner_id)
    is_read2 = query_scalar(
        "SELECT is_read FROM notifications WHERE notification_id = :nid", {"nid": nid}
    )
    assert is_read2 is True


def test_mark_all_read_only_affects_calling_user(notif_owner_id, notif_other_id):
    for _ in range(2):
        exec_sql(
            """
                INSERT INTO notifications (recipient_user_id, category, event_type, title)
                VALUES (:rid, 'test', 'TEST_EVENT', 'pytest notification')
            """,
            {"rid": notif_owner_id},
        )
    exec_sql(
        """
            INSERT INTO notifications (recipient_user_id, category, event_type, title)
            VALUES (:rid, 'test', 'TEST_EVENT', 'pytest notification')
        """,
        {"rid": notif_other_id},
    )

    assert get_unread_count(notif_owner_id) == 2
    assert get_unread_count(notif_other_id) == 1

    mark_all_read(notif_owner_id)

    assert get_unread_count(notif_owner_id) == 0
    assert get_unread_count(notif_other_id) == 1

    other_unread = query_df(
        "SELECT is_read FROM notifications WHERE recipient_user_id = :uid", {"uid": notif_other_id}
    )
    assert bool(other_unread.iloc[0]["is_read"]) is False


# ─────────────────────────────────────────────────────────────────────────────
# category="sample_request" fan-out (Task 8) — mirrors the quotation fan-out
# tests above one-for-one, against app_pages/sample_request_helpers.py's
# transition functions instead of quotation_helpers'.
# ─────────────────────────────────────────────────────────────────────────────

def test_submit_sample_request_notifies_managers_and_admins_excludes_actor(
    sids, sales_manager_a_id, any_customer_id, any_product_id
):
    """submit_sample_request must notify every active sales manager/admin, but
    not the submitting actor even though that actor holds the 'sales manager'
    role."""
    expected = _active_role_ids(["sales manager", "admin"], exclude_user_id=sales_manager_a_id)
    assert expected, "Need at least one other active sales manager/admin in DB"

    sid, _ = _submit_sr(sids, sales_manager_a_id, any_customer_id, any_product_id)

    recipients = _sample_recipients(sid, "SUBMITTED")
    assert recipients == expected
    assert sales_manager_a_id not in recipients
    assert _sample_actors(sid, "SUBMITTED") == {sales_manager_a_id}


def test_sample_manager_approve_notifies_rep_and_coordinators_and_admins(
    sids, rep_user_id, sales_manager_a_id, coordinator_user_id, any_customer_id, any_product_id
):
    """manager_approve must notify the submitting rep AND every active sales
    coordinator/admin."""
    sid, _ = _submit_sr(sids, rep_user_id, any_customer_id, any_product_id)

    expected_coord_admin = _active_role_ids(["sales coordinator", "admin"])
    assert coordinator_user_id in expected_coord_admin
    expected = expected_coord_admin | {rep_user_id}

    ok, err = sr_manager_approve(sid, sales_manager_a_id)
    assert ok, err

    recipients = _sample_recipients(sid, "APPROVED")
    assert recipients == expected
    assert _sample_actors(sid, "APPROVED") == {sales_manager_a_id}


def test_sample_manager_reject_notifies_rep_only(
    sids, rep_user_id, sales_manager_a_id, any_customer_id, any_product_id
):
    sid, _ = _submit_sr(sids, rep_user_id, any_customer_id, any_product_id)

    ok, err = sr_manager_reject(sid, sales_manager_a_id, "not a fit for this customer")
    assert ok, err

    recipients = _sample_recipients(sid, "REJECTED")
    assert recipients == {rep_user_id}
    assert _sample_actors(sid, "REJECTED") == {sales_manager_a_id}


def test_sample_manager_request_edit_notifies_rep_only(
    sids, rep_user_id, sales_manager_a_id, any_customer_id, any_product_id
):
    sid, _ = _submit_sr(sids, rep_user_id, any_customer_id, any_product_id)

    ok, err = sr_manager_request_edit(sid, sales_manager_a_id, "please revise quantities")
    assert ok, err

    recipients = _sample_recipients(sid, "EDIT_REQUESTED")
    assert recipients == {rep_user_id}
    assert _sample_actors(sid, "EDIT_REQUESTED") == {sales_manager_a_id}


def test_sample_manager_return_for_revision_notifies_rep_only(
    sids, rep_user_id, sales_manager_a_id, any_customer_id, any_product_id
):
    sid, _ = _submit_sr(sids, rep_user_id, any_customer_id, any_product_id)
    ok, err = sr_manager_approve(sid, sales_manager_a_id)
    assert ok, err

    ok2, err2 = sr_manager_return_for_revision(sid, sales_manager_a_id, "please adjust delivery date")
    assert ok2, err2

    recipients = _sample_recipients(sid, "RETURNED_FOR_REVISION")
    assert recipients == {rep_user_id}
    assert _sample_actors(sid, "RETURNED_FOR_REVISION") == {sales_manager_a_id}


def test_sample_coordinator_mark_done_notifies_rep_and_deciding_manager(
    sids, rep_user_id, sales_manager_a_id, coordinator_user_id, any_customer_id, any_product_id
):
    sid, _ = _submit_sr(sids, rep_user_id, any_customer_id, any_product_id)
    ok, err = sr_manager_approve(sid, sales_manager_a_id)
    assert ok, err

    ok2, err2 = sr_coordinator_mark_done(sid, coordinator_user_id, None, None)
    assert ok2, err2

    recipients = _sample_recipients(sid, "MARKED_DONE")
    assert recipients == {rep_user_id, sales_manager_a_id}
    assert _sample_actors(sid, "MARKED_DONE") == {coordinator_user_id}


def test_sample_inactive_user_never_notified(
    sids, rep_user_id, inactive_sales_manager_id, any_customer_id, any_product_id
):
    """notify_role must filter on is_active = TRUE — a user whose role
    matches but who is inactive must never receive a sample_request
    notification either."""
    sid, _ = _submit_sr(sids, rep_user_id, any_customer_id, any_product_id)

    recipients = _sample_recipients(sid, "SUBMITTED")
    assert inactive_sales_manager_id not in recipients
