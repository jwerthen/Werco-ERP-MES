"""Unencodable text in a report definition: refused at the door, survivable when stored.

The defect (compliance review, blocking): ``ReportFilter.value`` is ``Any``, and JSON allows
an unpaired UTF-16 surrogate (``"\\ud800"``) that ``json.loads`` keeps in a ``str``. The only
text rule was the NUL refusal, so such a value passed validation, ``create_template``
COMMITTED it (a JSON column stores the escape), and then rendering the response raised
``UnicodeEncodeError`` -- a 500 after the commit. From then on every
``GET /analytics/custom-report/templates`` (any role) and ``GET .../templates/{id}`` 500'd for
the whole company.

What this file pins:

* every place a definition carries caller text -- a filter value of any type, an ``in``
  item, ``value2`` (read or not), an alias, a template name / description -- is a 422
  (a real one: the 422 handler no longer 500s echoing the input), and nothing is stored or
  audited;
* a LEGACY row already holding such text still lists and opens (200, U+FFFD in place of each
  bad character, ``UNENCODABLE_DEFINITION_PROBLEM`` first in ``definition_errors``), while run
  and export answer 409 -- one judgment, so they cannot disagree -- and it can be repaired
  and deleted, with audit snapshots that stay renderable.

Bodies are sent as ``json.dumps`` bytes (``ensure_ascii`` escapes the surrogate, as a real
client's JSON would): httpx's own ``json=`` encodes to UTF-8 and cannot carry one.
"""

import json
from typing import Any, Dict, List

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.analytics import ReportTemplate
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.report_builder import UNENCODABLE_TEXT_PROBLEM, VALUE2_NOT_SCALAR_PROBLEM
from app.services.report_template_service import UNENCODABLE_DEFINITION_PROBLEM
from tests.api.export_audit_helpers import committed_export_rows, fresh_session

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
REPORT = "/api/v1/analytics/custom-report"
TEMPLATES = "/api/v1/analytics/custom-report/templates"
EXPORT = "/api/v1/analytics/custom-report/export"

LONE = "\ud800"  # an unpaired high surrogate, as json.loads("\"\\ud800\"") yields
LOW = "\udfff"  # an unpaired low surrogate
FFFD = "\ufffd"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session) -> None:
    if not db.query(Company).filter(Company.id == COMPANY_A).first():
        db.add(Company(id=COMPANY_A, name="Company 1", slug="company-1", is_active=True))
        db.commit()


def make_manager(db: Session) -> User:
    _ensure_company(db)
    n = _next()
    user = User(
        email=f"rbu-{n}@co1.test",
        employee_id=f"RBU-{n:05d}",
        first_name="Report",
        last_name=f"Unencodable{n}",
        hashed_password=TEST_PASSWORD_HASH,
        role=UserRole.MANAGER,
        is_active=True,
        is_superuser=False,
        company_id=COMPANY_A,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> Dict[str, str]:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {
        "Authorization": f"Bearer {token}",
        "X-Requested-With": "XMLHttpRequest",
        "content-type": "application/json",
    }


def make_work_order(db: Session) -> WorkOrder:
    _ensure_company(db)
    part = Part(
        part_number=f"RBU-P-{_next():05d}",
        name="Fixture",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(part)
    db.commit()
    wo = WorkOrder(
        work_order_number=f"RBU-WO-{_next():05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=5,
        status=WorkOrderStatus.IN_PROGRESS,
        priority=5,
        company_id=COMPANY_A,
    )
    db.add(wo)
    db.commit()
    return wo


def make_template(db: Session, owner: User, **definition: Any) -> ReportTemplate:
    """Insert a template ROW directly, as a legacy row saved before the rule existed would be.
    The JSON column stores ``\\ud800`` escaped and reads it back as the lone surrogate."""
    template = ReportTemplate(
        name=definition.pop("name", f"RBU Template {_next()}"),
        description=None,
        data_source=definition.pop("data_source", "work_orders"),
        columns=definition.pop("columns", [{"field": "work_order_number"}]),
        filters=definition.pop("filters", []),
        group_by=[],
        sort=[],
        is_shared=False,
        created_by=owner.id,
        company_id=COMPANY_A,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


def send(client: TestClient, method: str, url: str, user: User, body: Any):
    """Send ``body`` as ASCII JSON: the surrogate travels as the ``\\ud800`` escape."""
    return client.request(method, url, headers=headers_for(user), content=json.dumps(body).encode("ascii"))


def _f(field: str, operator: str, value: Any, value2: Any = None) -> Dict[str, Any]:
    return {"field": field, "operator": operator, "value": value, "value2": value2}


def _definition(**overrides: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {"data_source": "work_orders", "columns": [{"field": "work_order_number"}]}
    body.update(overrides)
    return body


def assert_real_422(resp) -> List[Dict[str, Any]]:
    """A 422 whose body is valid UTF-8 JSON and echoes no raw surrogate."""
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    resp.content.decode("utf-8")  # strict: a raw surrogate could not have been encoded
    detail = resp.json()["detail"]
    assert LONE not in json.dumps(detail, ensure_ascii=False) and LOW not in json.dumps(detail, ensure_ascii=False)
    return detail


def committed_template_actions() -> List[str]:
    with fresh_session() as verify:
        return [
            row.action
            for row in verify.query(AuditLog).filter(AuditLog.resource_type == "report_template").order_by(AuditLog.id)
        ]


# (case id, definition override, the problem text the 422 must carry). A ``None`` problem
# means the text never reaches the definition rules (an unknown field or key) -- still a
# 422, never a 500.
REFUSED = [
    ("string value", {"filters": [_f("customer_name", "eq", f"Acme{LONE}")]}, f"a value {UNENCODABLE_TEXT_PROBLEM}"),
    ("like value", {"filters": [_f("customer_name", "like", LOW)]}, f"a value {UNENCODABLE_TEXT_PROBLEM}"),
    (
        "in item",
        {"filters": [_f("customer_name", "in", ["Acme", f"x{LONE}y"])]},
        f"a value {UNENCODABLE_TEXT_PROBLEM}",
    ),
    ("enum value", {"filters": [_f("status", "eq", LONE)]}, f"a value {UNENCODABLE_TEXT_PROBLEM}"),
    ("number field given text", {"filters": [_f("quantity_ordered", "eq", LONE)]}, UNENCODABLE_TEXT_PROBLEM),
    ("date value", {"filters": [_f("due_date", "gte", LONE)]}, f"a value {UNENCODABLE_TEXT_PROBLEM}"),
    (
        "value2 of between",
        {"filters": [_f("due_date", "between", "2026-01-01", f"2026-12-31{LONE}")]},
        f"value2 {UNENCODABLE_TEXT_PROBLEM}",
    ),
    (
        "value2 an operator never reads",
        {"filters": [_f("quantity_ordered", "eq", 1, LONE)]},
        f"value2 {UNENCODABLE_TEXT_PROBLEM}",
    ),
    # A list or object value2 is refused by its type alone (it must be null or a scalar).
    (
        "value2 nested key",
        {"filters": [_f("quantity_ordered", "eq", 1, {LONE: ["a"]})]},
        VALUE2_NOT_SCALAR_PROBLEM,
    ),
    (
        "alias",
        {"columns": [{"field": "work_order_number", "alias": f"WO{LONE}"}]},
        f"an alias {UNENCODABLE_TEXT_PROBLEM}",
    ),
    ("unknown field", {"columns": [{"field": LONE}]}, None),
    ("unknown key", {"columns": [{"field": "work_order_number", LONE: 1}]}, None),
]


def _assert_refused(resp, problem) -> None:
    detail = assert_real_422(resp)
    if problem is not None:
        messages = " | ".join(entry["msg"] for entry in detail)
        assert problem in messages, messages


@pytest.mark.parametrize("case,override,problem", REFUSED, ids=[c[0] for c in REFUSED])
def test_ad_hoc_run_refuses_unencodable_text_with_a_real_422(
    client: TestClient, db_session: Session, case: str, override: dict, problem
):
    manager = make_manager(db_session)
    make_work_order(db_session)
    _assert_refused(send(client, "POST", REPORT, manager, _definition(**override)), problem)


@pytest.mark.parametrize("case,override,problem", REFUSED, ids=[c[0] for c in REFUSED])
def test_template_create_refuses_unencodable_text_and_stores_nothing(
    client: TestClient, db_session: Session, case: str, override: dict, problem
):
    manager = make_manager(db_session)
    _assert_refused(send(client, "POST", TEMPLATES, manager, {"name": "T", **_definition(**override)}), problem)
    assert db_session.query(ReportTemplate).count() == 0
    assert committed_template_actions() == []
    # The failure mode was a poisoned LIST: it must still answer.
    assert client.get(TEMPLATES, headers=headers_for(manager)).status_code == status.HTTP_200_OK


@pytest.mark.parametrize("case,override,problem", REFUSED, ids=[c[0] for c in REFUSED])
def test_template_patch_refuses_unencodable_text_and_changes_nothing(
    client: TestClient, db_session: Session, case: str, override: dict, problem
):
    manager = make_manager(db_session)
    template = make_template(db_session, manager, name="Unchanged")
    resp = send(client, "PATCH", f"{TEMPLATES}/{template.id}", manager, {"definition": _definition(**override)})
    _assert_refused(resp, problem)
    db_session.expire_all()
    assert db_session.query(ReportTemplate).filter(ReportTemplate.id == template.id).one().columns == [
        {"field": "work_order_number"}
    ]
    assert committed_template_actions() == []


# A surrogate in a CONSTRAINED str is refused by pydantic-core itself (``string_unicode``) --
# whose 422 echoed the input and so was a 500 until the handler fix; ``unstorable_text`` is
# the template text validator (NUL, and a surrogate should the constraint ever stop catching it).
_REFUSED_TEXT_TYPES = {"string_unicode", "unstorable_text"}


@pytest.mark.parametrize(
    "fields,refused_by",
    [
        ({"name": f"Report{LONE}"}, _REFUSED_TEXT_TYPES),
        ({"description": f"Notes{LOW}"}, _REFUSED_TEXT_TYPES),
        ({"name": "Re\x00port"}, {"unstorable_text"}),
        ({"description": "a\x00b"}, {"unstorable_text"}),
    ],
    ids=["surrogate name", "surrogate description", "NUL name", "NUL description"],
)
def test_template_name_and_description_refuse_unstorable_text(
    client: TestClient, db_session: Session, fields: dict, refused_by: set
):
    """Neither column can hold it (a NUL fails at bind on Postgres; a surrogate cannot be
    encoded at all), so both used to be a 500 for an input error."""
    manager = make_manager(db_session)
    created = send(client, "POST", TEMPLATES, manager, {"name": "T", **_definition(), **fields})
    assert {entry["type"] for entry in assert_real_422(created)} & refused_by
    assert db_session.query(ReportTemplate).count() == 0

    template = make_template(db_session, manager, name="Unchanged")
    patched = send(client, "PATCH", f"{TEMPLATES}/{template.id}", manager, fields)
    assert {entry["type"] for entry in assert_real_422(patched)} & refused_by
    db_session.expire_all()
    assert db_session.query(ReportTemplate).filter(ReportTemplate.id == template.id).one().name == "Unchanged"
    assert committed_template_actions() == []


# ===========================================================================
# Legacy rows already holding it: list/get 200, run/export 409, repairable
# ===========================================================================


def test_legacy_row_with_unencodable_text_lists_opens_and_refuses_to_run(client: TestClient, db_session: Session):
    manager = make_manager(db_session)
    make_work_order(db_session)
    value = make_template(db_session, manager, name="Value", filters=[_f("customer_name", "eq", f"Acme{LONE}")])
    alias = make_template(
        db_session, manager, name="Alias", columns=[{"field": "work_order_number", "alias": f"WO{LOW}"}]
    )
    # Unencodable text AND an ordinary problem: both are reported.
    mixed = make_template(
        db_session,
        manager,
        name="Mixed",
        columns=[{"field": "work_order_number"}, {"field": "estimated_hours"}],
        filters=[_f("quantity_ordered", "eq", 1, {"k": [LONE]})],
    )
    # In a stored KEY: the problem that quotes the key back is rendered too.
    key = make_template(db_session, manager, name="Key", columns=[{"field": "work_order_number", LONE: 1}])
    good = make_template(db_session, manager, name="Good")

    listed = client.get(TEMPLATES, headers=headers_for(manager))
    assert listed.status_code == status.HTTP_200_OK, listed.text
    listed.content.decode("utf-8")
    by_name = {t["name"]: t for t in listed.json()}
    assert by_name["Good"]["definition_errors"] == []
    assert by_name["Value"]["filters"][0]["value"] == f"Acme{FFFD}"
    assert by_name["Value"]["definition_errors"] == [UNENCODABLE_DEFINITION_PROBLEM]
    assert by_name["Alias"]["columns"][0]["alias"] == f"WO{FFFD}"
    assert by_name["Alias"]["definition_errors"] == [UNENCODABLE_DEFINITION_PROBLEM]
    mixed_errors = by_name["Mixed"]["definition_errors"]
    assert mixed_errors[0] == UNENCODABLE_DEFINITION_PROBLEM
    assert any("estimated_hours" in problem for problem in mixed_errors[1:]), mixed_errors
    assert any(VALUE2_NOT_SCALAR_PROBLEM in problem for problem in mixed_errors[1:]), mixed_errors
    assert by_name["Mixed"]["filters"][0]["value2"] == {"k": [FFFD]}
    assert by_name["Key"]["definition_errors"][0] == UNENCODABLE_DEFINITION_PROBLEM
    assert any(f"columns.0.{FFFD}" in problem for problem in by_name["Key"]["definition_errors"])

    for template in (value, alias, mixed, key):
        got = client.get(f"{TEMPLATES}/{template.id}", headers=headers_for(manager))
        assert got.status_code == status.HTTP_200_OK, got.text
        assert got.json()["definition_errors"] == by_name[template.name]["definition_errors"]

        ran = client.post(f"{TEMPLATES}/{template.id}/run", headers=headers_for(manager))
        assert ran.status_code == status.HTTP_409_CONFLICT, ran.text
        assert ran.json()["detail"].startswith(f'Saved report "{template.name}" can no longer run: ')
        assert UNENCODABLE_DEFINITION_PROBLEM in ran.json()["detail"]

        exported = client.get(f"{EXPORT}?template_id={template.id}", headers=headers_for(manager))
        assert exported.status_code == status.HTTP_409_CONFLICT, exported.text
    assert committed_export_rows() == []
    assert client.post(f"{TEMPLATES}/{good.id}/run", headers=headers_for(manager)).status_code == 200


def test_legacy_row_is_repaired_and_deleted_with_renderable_audit_snapshots(client: TestClient, db_session: Session):
    """An audit row is immutable, so a snapshot carrying the raw surrogate would be an entry no
    audit-log read could ever render: the snapshot is rendered like a response."""
    manager = make_manager(db_session)
    repaired = make_template(db_session, manager, name="Repair", filters=[_f("customer_name", "eq", f"Acme{LONE}")])
    doomed = make_template(db_session, manager, name="Doomed", filters=[_f("customer_name", "eq", f"Zed{LOW}")])

    fixed = send(
        client,
        "PATCH",
        f"{TEMPLATES}/{repaired.id}",
        manager,
        {"definition": _definition(filters=[_f("customer_name", "eq", "Acme")])},
    )
    assert fixed.status_code == status.HTTP_200_OK, fixed.text
    assert fixed.json()["definition_errors"] == []
    assert client.post(f"{TEMPLATES}/{repaired.id}/run", headers=headers_for(manager)).status_code == 200

    deleted = client.delete(f"{TEMPLATES}/{doomed.id}", headers=headers_for(manager))
    assert deleted.status_code == status.HTTP_200_OK, deleted.text
    db_session.expire_all()
    assert db_session.query(ReportTemplate).filter(ReportTemplate.id == doomed.id).count() == 0

    with fresh_session() as verify:
        rows = {
            row.action: (row.old_values, row.new_values)
            for row in verify.query(AuditLog).filter(AuditLog.resource_type == "report_template")
        }
    assert rows["UPDATE"][0]["filters"][0]["value"] == f"Acme{FFFD}"
    assert rows["UPDATE"][1]["filters"][0]["value"] == "Acme"
    assert rows["DELETE"][0]["filters"][0]["value"] == f"Zed{FFFD}"
    json.dumps(rows, ensure_ascii=False).encode("utf-8")  # every recorded value renders
