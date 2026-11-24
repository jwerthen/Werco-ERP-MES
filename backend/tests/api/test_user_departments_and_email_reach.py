"""Department is a pick list, and Email Recipients shows who each email reaches (plan alerts_health/2 step 5).

Department:
  * the list is fixed and covers every department the notification catalog names (so no catalog
    audience points at a department nobody can be put in);
  * create, update, approval, ``/auth/register`` and the CSV import store the canonical spelling
    of a listed name (case- and whitespace-insensitive) and refuse anything else -- 422 with the
    list on the routes, a row error on the import;
  * an update that re-sends a legacy value unchanged is allowed, so the correction is not forced
    on an unrelated edit; changing TO a legacy value is not;
  * ``GET /users/departments`` (Admin only) lists the pick list and every user whose stored value
    is not on it, and writes nothing.

Email Recipients: each event carries ``audience_source``, ``reaches_user_ids``,
``depends_on_record`` and ``reaches_nobody`` -- the automatic audience is the catalog's roles OR
departments with each person's own email choice applied, a saved list is exclusive, and only
deliverable addresses count.
"""

import pytest

from app.models.notification import NotificationPreference
from app.models.user import User, UserRole
from app.services.notification_catalog import CATALOG
from app.services.user_departments import DEPARTMENT_INVALID_DETAIL, DEPARTMENTS, canonical_department
from tests.api.test_email_recipients import URL as EMAIL_URL
from tests.api.test_email_recipients import events, headers, make_user, set_ids

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

USERS = "/api/v1/users"


def test_the_pick_list_covers_every_catalog_department():
    named = {department for entry in CATALOG.values() for department in entry.departments}
    assert named <= set(DEPARTMENTS)
    assert set(DEPARTMENTS) == {
        "Purchasing",
        "Inventory",
        "Sales",
        "Quality",
        "Maintenance",
        "Shipping",
        "Production",
        "Other",
    }


def test_canonical_department():
    assert canonical_department("  quality ") == "Quality"
    assert canonical_department("") is None and canonical_department(None) is None
    with pytest.raises(ValueError, match="Department must be one of"):
        canonical_department("Machining")


def _create(client, admin_user, department, n):
    return client.post(
        f"{USERS}/",
        headers=headers(admin_user),
        json={
            "email": f"dept-{n}@fictional-shop.com",
            "employee_id": f"DEPT-{n}",
            "first_name": "Dee",
            "last_name": "Partment",
            "password": "correct horse battery staple 9",
            "role": "operator",
            "department": department,
        },
    )


def test_create_and_update_store_the_canonical_spelling_and_refuse_others(client, db_session, admin_user):
    created = _create(client, admin_user, "purchasing", 1)
    assert created.status_code == 200, created.text
    assert created.json()["department"] == "Purchasing"

    refused = _create(client, admin_user, "Machining", 2)
    assert refused.status_code == 422
    assert refused.json()["detail"] == DEPARTMENT_INVALID_DETAIL
    assert db_session.query(User).filter(User.employee_id == "DEPT-2").count() == 0

    user = db_session.get(User, created.json()["id"])
    response = client.put(f"{USERS}/{user.id}", headers=headers(admin_user), json={"department": "Weld Shop"})
    assert response.status_code == 422 and response.json()["detail"] == DEPARTMENT_INVALID_DETAIL
    db_session.refresh(user)
    assert user.department == "Purchasing"


def test_a_legacy_value_survives_an_unrelated_edit_but_cannot_be_chosen(client, db_session, admin_user):
    legacy = make_user(db_session, "legacy-dept@example.test", department="Machining")
    response = client.put(
        f"{USERS}/{legacy.id}",
        headers=headers(admin_user),
        json={"first_name": "Renamed", "department": "Machining"},
    )
    assert response.status_code == 200, response.text
    db_session.refresh(legacy)
    assert legacy.first_name == "Renamed" and legacy.department == "Machining"

    other = make_user(db_session, "other-dept@example.test", department="Quality")
    response = client.put(f"{USERS}/{other.id}", headers=headers(admin_user), json={"department": "Machining"})
    assert response.status_code == 422


def test_the_import_refuses_an_unlisted_department_as_a_row_error(client, db_session, admin_user):
    csv_content = (
        "employee_id,first_name,last_name,role,department\n"
        "DEPT-IMP-1,Ana,Lyst,operator,  SHIPPING \n"
        "DEPT-IMP-2,Bo,Gus,operator,Fabrication\n"
        "DEPT-IMP-3,Cy,Blank,operator,\n"
    )
    response = client.post(
        f"{USERS}/import-csv",
        headers=headers(admin_user),
        files={"file": ("users.csv", csv_content, "text/csv")},
        data={"default_password": "correct horse battery staple 9"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created_count"] == 2
    assert [(e["row"], e["reason"]) for e in body["errors"]] == [(3, DEPARTMENT_INVALID_DETAIL)]
    assert db_session.query(User).filter(User.employee_id == "DEPT-IMP-1").one().department == "Shipping"
    assert db_session.query(User).filter(User.employee_id == "DEPT-IMP-3").one().department is None


def test_approval_takes_a_listed_department_only(client, db_session, admin_user):
    pending = make_user(db_session, "pending-dept@example.test", active=False, role=UserRole.VIEWER)
    refused = client.post(
        f"{USERS}/{pending.id}/approve", headers=headers(admin_user), json={"role": "operator", "department": "Ops"}
    )
    assert refused.status_code == 422
    db_session.refresh(pending)
    assert pending.is_active is False
    approved = client.post(
        f"{USERS}/{pending.id}/approve", headers=headers(admin_user), json={"role": "operator", "department": "sales"}
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["department"] == "Sales"


def test_the_departments_read_lists_unlisted_values_for_correction(client, db_session, admin_user):
    legacy = make_user(db_session, "legacy-list@example.test", department="Machining")
    spaced = make_user(db_session, "spaced-list@example.test", department="quality")  # wrong case: matches nothing
    make_user(db_session, "listed@example.test", department="Quality")
    make_user(db_session, "blank@example.test", department="")
    response = client.get(f"{USERS}/departments", headers=headers(admin_user))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["departments"] == list(DEPARTMENTS)
    unlisted = {row["id"]: row["department"] for row in body["unlisted"]}
    assert unlisted.get(legacy.id) == "Machining" and unlisted.get(spaced.id) == "quality"
    assert all(value not in DEPARTMENTS for value in unlisted.values())

    manager = make_user(db_session, "mgr-list@example.test", role=UserRole.MANAGER)
    assert client.get(f"{USERS}/departments", headers=headers(manager)).status_code == 403


# ---------------------------------------------------------------------------
# Email Recipients: who each email actually reaches
# ---------------------------------------------------------------------------


def test_email_recipients_show_who_each_event_reaches(client, db_session, admin_user):
    supervisor = make_user(db_session, "sup-reach@example.test", role=UserRole.SUPERVISOR)
    manager = make_user(db_session, "mgr-reach@example.test", role=UserRole.MANAGER)
    opted_out = make_user(db_session, "mgr-optout@example.test", role=UserRole.MANAGER)
    db_session.add(
        NotificationPreference(
            company_id=1,
            user_id=opted_out.id,
            preferences={"wo.late": {"in_app": True, "email": False, "sms": False, "digest": False}},
        )
    )
    badge = make_user(db_session, "badge-reach@users.werco.com", role=UserRole.SUPERVISOR)
    db_session.commit()

    result = events(client.get(EMAIL_URL, headers=headers(admin_user)))
    late = result["wo.late"]
    assert late["audience_source"] == "automatic"
    reaches = set(late["reaches_user_ids"])
    assert {supervisor.id, manager.id} <= reaches
    assert opted_out.id not in reaches, "a person's own email choice applies to the automatic audience"
    assert badge.id not in reaches, "an undeliverable address reaches nobody"
    assert late["reaches_nobody"] is False and late["depends_on_record"] is False

    # A saved empty list switches the email off: it reaches nobody, and says so.
    set_ids(db_session, "wo.late", [])
    late = events(client.get(EMAIL_URL, headers=headers(admin_user)))["wo.late"]
    assert late["audience_source"] == "selected"
    assert late["reaches_user_ids"] == [] and late["reaches_nobody"] is True


def test_an_event_with_a_record_derived_audience_is_never_flagged_reaches_nobody(client, db_session, admin_user):
    result = events(client.get(EMAIL_URL, headers=headers(admin_user)))
    derived = [key for key, event in result.items() if CATALOG[key].resolver is not None]
    assert derived, "the catalog has email events whose audience comes from the record"
    for key in derived:
        if result[key]["audience_source"] == "automatic":
            assert result[key]["depends_on_record"] is True and result[key]["reaches_nobody"] is False


def test_an_event_whose_caller_names_the_recipient_is_never_flagged_reaches_nobody(client, db_session, admin_user):
    """``visitor.check_in`` has no roles, departments or resolver: the visitor log names the host.
    Flagging it 'reaches nobody' would invite an Admin to tick people, and a selected list is
    exclusive -- the named host would stop getting the email."""
    entry = CATALOG["visitor.check_in"]
    assert not entry.roles and not entry.departments and entry.resolver is None
    visitor = events(client.get(EMAIL_URL, headers=headers(admin_user)))["visitor.check_in"]
    assert visitor["audience_source"] == "automatic"
    assert visitor["depends_on_record"] is True and visitor["reaches_nobody"] is False


def test_the_preview_audience_is_catalog_audience(client, db_session, admin_user):
    """The preview filters the directory in memory; it must name exactly ``catalog_audience``'s people
    (roles OR departments, active only) for every automatic event."""
    from app.services.notification_dispatch import catalog_audience

    mine = {
        make_user(db_session, "qual-dept@example.test", department="Quality").id,
        make_user(db_session, "qual-role@example.test", role=UserRole.QUALITY).id,
        make_user(db_session, "buyer-dept@example.test", department="Purchasing").id,
        make_user(db_session, "sup-aud@example.test", role=UserRole.SUPERVISOR).id,
        make_user(db_session, "gone-mgr@example.test", role=UserRole.MANAGER, active=False).id,
    }

    result = events(client.get(EMAIL_URL, headers=headers(admin_user)))
    checked = 0
    for key, event in result.items():
        if event["audience_source"] != "automatic":
            continue
        expected = sorted(user.id for user in catalog_audience(db_session, CATALOG[key], 1) if user.id in mine)
        assert sorted(i for i in event["reaches_user_ids"] if i in mine) == expected, key
        checked += 1
    assert checked
