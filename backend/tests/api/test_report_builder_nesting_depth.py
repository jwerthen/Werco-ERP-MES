"""Nesting depth in a report definition: refused at the door, survivable when stored.

Two defects, one shape -- a value nested so deeply that something walking it fails:

* **F1 (introduced by the unencodable-text fix).** The ``value2`` check walked ``value2``
  recursively, so an ``eq`` filter whose (unread) ``value2`` was nested a few hundred levels
  deep answered 500 instead of 200. Now ``value2`` must be null or ONE scalar for every
  operator -- refused by its type, never walked -- so nothing nested can be stored that way.
* **F2 (pre-existing).** ``value2`` used to take any JSON at all. A stored template whose
  value was nested past pydantic's serializer limit (about 255) 500'd every
  ``GET /custom-report/templates`` for the company, and creating one 500'd inside
  ``_definition_json``. A stored definition nested past ``MAX_STORED_DEFINITION_DEPTH`` now
  renders with U+FFFD at the cut and ``TOO_DEEP_DEFINITION_PROBLEM`` first in its
  ``definition_errors``: list/get 200, run/export 409, and it can be repaired or deleted.

Deep bodies are written as JSON text directly (``"[" * n``): ``json.dumps`` on the test's own
stack would itself recurse. Responses that echo a very deep body are not parsed for the same
reason.
"""

from typing import Any

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.analytics import ReportTemplate
from app.models.audit_log import AuditLog
from app.services.report_builder import VALUE2_NOT_SCALAR_PROBLEM
from app.services.report_template_service import MAX_STORED_DEFINITION_DEPTH, TOO_DEEP_DEFINITION_PROBLEM
from tests.api.export_audit_helpers import committed_export_rows, fresh_session
from tests.api.test_report_builder_unencodable_text import (
    EXPORT,
    REPORT,
    TEMPLATES,
    _definition,
    _f,
    committed_template_actions,
    headers_for,
    make_manager,
    make_template,
    make_work_order,
    send,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

FFFD = "�"


def nested_text(depth: int, leaf: str = "1") -> str:
    return "[" * depth + leaf + "]" * depth


def nested(depth: int, leaf: Any = 1) -> Any:
    value = leaf
    for _ in range(depth):
        value = [value]
    return value


def body_with_value2(value2_json: str, operator: str = "eq", field: str = "quantity_ordered", value: str = "1") -> str:
    return (
        '{"data_source":"work_orders","columns":[{"field":"work_order_number"}],'
        f'"filters":[{{"field":"{field}","operator":"{operator}","value":{value},"value2":{value2_json}}}]}}'
    )


def post_raw(client: TestClient, url: str, user, body: str, method: str = "POST"):
    return client.request(method, url, headers=headers_for(user), content=body.encode("ascii"))


# ===========================================================================
# F1 / (c): value2 is null or one scalar, for every operator
# ===========================================================================


def test_an_eq_filter_with_a_deeply_nested_value2_is_a_422_not_a_500(client: TestClient, db_session: Session):
    manager = make_manager(db_session)
    make_work_order(db_session)
    resp = post_raw(client, REPORT, manager, body_with_value2(nested_text(600)))
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text[:300]
    assert VALUE2_NOT_SCALAR_PROBLEM.encode() in resp.content


@pytest.mark.parametrize(
    "value2_json,operator,field,value",
    [
        ("[1]", "between", "quantity_ordered", "1"),
        (nested_text(40), "between", "quantity_ordered", "1"),
        ('["2026-12-31"]', "between", "due_date", '"2026-01-01"'),
        ("[1]", "eq", "quantity_ordered", "1"),
        ('{"a": 1}', "eq", "quantity_ordered", "1"),
        ('{"a": 1}', "in", "customer_name", '["Acme"]'),
        ("[]", "like", "customer_name", '"Ac"'),
    ],
    ids=["between list", "between deep", "between date list", "eq list", "eq object", "in object", "like empty list"],
)
def test_a_list_or_object_value2_is_refused_for_every_operator(
    client: TestClient, db_session: Session, value2_json: str, operator: str, field: str, value: str
):
    manager = make_manager(db_session)
    make_work_order(db_session)
    resp = post_raw(client, REPORT, manager, body_with_value2(value2_json, operator, field, value))
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    assert VALUE2_NOT_SCALAR_PROBLEM in resp.json()["detail"][0]["msg"]


@pytest.mark.parametrize(
    "value2_json,problem",
    [('"a\\u0000b"', "value2 cannot contain a NUL"), ('"' + "x" * 501 + '"', "value2 may be at most 500 characters")],
    ids=["NUL", "too long"],
)
def test_an_unread_text_value2_obeys_the_text_storage_rules(
    client: TestClient, db_session: Session, value2_json: str, problem: str
):
    manager = make_manager(db_session)
    resp = post_raw(client, REPORT, manager, body_with_value2(value2_json))
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    assert problem in resp.json()["detail"][0]["msg"]


@pytest.mark.parametrize("value2_json", ["5", '"stale"', "true", "0.5", '"' + "x" * 500 + '"'])
def test_an_unread_scalar_value2_is_still_accepted(client: TestClient, db_session: Session, value2_json: str):
    """A screen that switches a filter from ``between`` to ``eq`` may leave the old bound behind."""
    manager = make_manager(db_session)
    wo = make_work_order(db_session)
    resp = post_raw(client, REPORT, manager, body_with_value2(value2_json, value="5"))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["rows"] == [{"work_order_number": wo.work_order_number}]


def test_creating_or_changing_a_template_with_a_deep_value2_is_a_422_and_stores_nothing(
    client: TestClient, db_session: Session
):
    """F2's write half: this used to raise inside ``_definition_json`` (pydantic's serializer
    depth limit) -- a 500 before the insert."""
    manager = make_manager(db_session)
    deep = body_with_value2(nested_text(300))
    created = post_raw(client, TEMPLATES, manager, '{"name":"Deep",' + deep[1:])
    assert created.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, created.text[:300]
    assert db_session.query(ReportTemplate).count() == 0

    template = make_template(db_session, manager, name="Unchanged")
    patched = post_raw(client, f"{TEMPLATES}/{template.id}", manager, '{"definition":' + deep + "}", method="PATCH")
    assert patched.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, patched.text[:300]
    db_session.expire_all()
    assert db_session.query(ReportTemplate).filter(ReportTemplate.id == template.id).one().filters == []
    assert committed_template_actions() == []


# ===========================================================================
# F2: a legacy row nested too deeply lists and opens, and never runs
# ===========================================================================


def _innermost(value: Any) -> Any:
    levels = 0
    while isinstance(value, list):
        value, levels = (value[0] if value else None), levels + 1
    return value, levels


def test_a_legacy_row_nested_300_deep_lists_opens_and_refuses_to_run(client: TestClient, db_session: Session):
    manager = make_manager(db_session)
    make_work_order(db_session)
    deep = make_template(db_session, manager, name="Deep", filters=[_f("quantity_ordered", "eq", 1, nested(300))])
    # Just past the bound: a definition is cut at MAX_STORED_DEFINITION_DEPTH, so a value2 whose
    # containers reach one level deeper (the filter's value2 sits at level 4) is cut too.
    edge = make_template(
        db_session,
        manager,
        name="Edge",
        filters=[_f("quantity_ordered", "eq", 1, nested(MAX_STORED_DEFINITION_DEPTH - 2))],
    )
    good = make_template(db_session, manager, name="Good")

    listed = client.get(TEMPLATES, headers=headers_for(manager))
    assert listed.status_code == status.HTTP_200_OK, listed.text[:300]
    by_name = {t["name"]: t for t in listed.json()}
    assert by_name["Good"]["definition_errors"] == []
    for name in ("Deep", "Edge"):
        errors = by_name[name]["definition_errors"]
        assert errors[0] == TOO_DEEP_DEFINITION_PROBLEM, errors
        assert any(VALUE2_NOT_SCALAR_PROBLEM in problem for problem in errors[1:]), errors
        leaf, levels = _innermost(by_name[name]["filters"][0]["value2"])
        assert leaf == FFFD and levels < MAX_STORED_DEFINITION_DEPTH

    for template in (deep, edge):
        got = client.get(f"{TEMPLATES}/{template.id}", headers=headers_for(manager))
        assert got.status_code == status.HTTP_200_OK, got.text[:300]
        assert got.json()["definition_errors"] == by_name[template.name]["definition_errors"]

        ran = client.post(f"{TEMPLATES}/{template.id}/run", headers=headers_for(manager))
        assert ran.status_code == status.HTTP_409_CONFLICT, ran.text[:300]
        assert TOO_DEEP_DEFINITION_PROBLEM in ran.json()["detail"]

        exported = client.get(f"{EXPORT}?template_id={template.id}", headers=headers_for(manager))
        assert exported.status_code == status.HTTP_409_CONFLICT, exported.text[:300]
    assert committed_export_rows() == []
    assert client.post(f"{TEMPLATES}/{good.id}/run", headers=headers_for(manager)).status_code == 200


def test_a_deep_legacy_row_is_repaired_and_deleted_with_bounded_audit_snapshots(
    client: TestClient, db_session: Session
):
    manager = make_manager(db_session)
    repaired = make_template(db_session, manager, name="Repair", filters=[_f("quantity_ordered", "eq", 1, nested(300))])
    doomed = make_template(db_session, manager, name="Doomed", filters=[_f("quantity_ordered", "eq", 1, nested(300))])

    fixed = send(client, "PATCH", f"{TEMPLATES}/{repaired.id}", manager, {"definition": _definition()})
    assert fixed.status_code == status.HTTP_200_OK, fixed.text[:300]
    assert fixed.json()["definition_errors"] == []
    assert client.delete(f"{TEMPLATES}/{doomed.id}", headers=headers_for(manager)).status_code == 200

    with fresh_session() as verify:
        rows = {
            row.action: row.old_values
            for row in verify.query(AuditLog).filter(AuditLog.resource_type == "report_template")
        }
    for action in ("UPDATE", "DELETE"):
        leaf, levels = _innermost(rows[action]["filters"][0]["value2"])
        assert leaf == FFFD and levels < MAX_STORED_DEFINITION_DEPTH, action
