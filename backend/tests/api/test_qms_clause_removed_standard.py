"""A clause of a removed QMS standard is removed with it: the clause routes answer 404.

Removing a standard (``DELETE /qms-standards/{id}``) soft-deletes only the standard row. Its
clauses keep ``is_deleted = false``, and the routes that resolve a clause by id checked only that
flag, so on a removed standard's clause they still answered (a 2026-11-17 compliance review
reproduced each one):

* ``PUT /qms-standards/clauses/{id}``: 200, and it could change ``compliance_status``, a quality
  record, on a standard that had left the record;
* ``DELETE /qms-standards/clauses/{id}``: 204 and an audit row;
* ``GET /qms-standards/clauses/{id}/auto-evidence``: 200;
* ``GET /qms-standards/{standard_id}/clauses`` never checked the standard at all, so it listed a
  removed standard's clauses and their evidence links, ``document_id`` included.

They now resolve the clause through ``_live_clause_or_404`` (standard joined, inside the active
company) and the list checks its standard, so each answers 404 and writes nothing. These tests pin
that for a removed standard, for a standard of another company (bad data), and for a removal the
session has not seen, and pin that a live standard is unaffected.
"""

from typing import Any, Dict, List

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.qms_standard import QMSClause, QMSClauseEvidence, QMSStandard

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

BASE = "/api/v1/qms-standards"
COMPANY_A = 1
COMPANY_B = 2

# The routes that resolve a clause by id.
CLAUSE_ROUTES = ["update", "delete", "auto_evidence"]


def _ensure_company(db: Session, company_id: int) -> None:
    if db.query(Company).filter(Company.id == company_id).first() is None:
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def _clause(db: Session, *, standard_company: int = COMPANY_A) -> QMSClause:
    """A company-A clause with one evidence link, under a standard of ``standard_company``."""
    for company_id in {standard_company, COMPANY_A}:
        _ensure_company(db, company_id)
    standard = QMSStandard(company_id=standard_company, name="AS9100D", version="2016", is_active=True)
    db.add(standard)
    db.flush()
    clause = QMSClause(
        company_id=COMPANY_A,
        standard_id=standard.id,
        clause_number="8.5.2",
        title="Identification and traceability",
        compliance_status="not_assessed",
    )
    db.add(clause)
    db.flush()
    db.add(
        QMSClauseEvidence(company_id=COMPANY_A, clause_id=clause.id, evidence_type="module", title="Lot traceability")
    )
    db.commit()
    db.refresh(clause)
    return clause


def _remove_standard(client: TestClient, headers: dict, clause: QMSClause) -> None:
    response = client.delete(f"{BASE}/{clause.standard_id}", headers=headers)
    assert response.status_code == status.HTTP_204_NO_CONTENT, response.text


def _call(client: TestClient, route: str, clause_id: int, headers: dict):
    if route == "update":
        return client.put(
            f"{BASE}/clauses/{clause_id}",
            json={"compliance_status": "compliant", "title": "Edited title"},
            headers=headers,
        )
    if route == "delete":
        return client.delete(f"{BASE}/clauses/{clause_id}", headers=headers)
    return client.get(f"{BASE}/clauses/{clause_id}/auto-evidence", headers=headers)


def _columns(db: Session, clause_id: int) -> Dict[str, Any]:
    db.expire_all()
    row = db.get(QMSClause, clause_id)
    return {c.key: getattr(row, c.key) for c in row.__table__.columns}


def _clause_audit_rows(db: Session, clause_id: int) -> List[AuditLog]:
    return db.query(AuditLog).filter(AuditLog.resource_type == "qms_clause", AuditLog.resource_id == clause_id).all()


def _assert_404_and_nothing_written(db: Session, response, clause_id: int, before: Dict[str, Any]) -> None:
    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert response.json()["detail"] == "Clause not found"
    db.rollback()
    assert _columns(db, clause_id) == before
    assert _clause_audit_rows(db, clause_id) == []


# ---------------------------------------------------------------------------
# The routes that resolve a clause by id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", CLAUSE_ROUTES)
def test_clause_of_a_removed_standard_is_404_and_writes_nothing(
    route: str, client: TestClient, db_session: Session, admin_headers: dict
):
    clause = _clause(db_session)
    clause_id = clause.id
    _remove_standard(client, admin_headers, clause)
    before = _columns(db_session, clause_id)
    assert before["is_deleted"] is False, "removing a standard leaves its clauses' own flag clear"

    response = _call(client, route, clause_id, admin_headers)

    _assert_404_and_nothing_written(db_session, response, clause_id, before)


@pytest.mark.parametrize("route", CLAUSE_ROUTES)
def test_clause_whose_standard_is_another_companys_is_404(
    route: str, client: TestClient, db_session: Session, admin_headers: dict
):
    """A company-A clause filed under company B's standard (bad data) is not the caller's to touch."""
    clause = _clause(db_session, standard_company=COMPANY_B)
    clause_id = clause.id
    before = _columns(db_session, clause_id)

    response = _call(client, route, clause_id, admin_headers)

    _assert_404_and_nothing_written(db_session, response, clause_id, before)


@pytest.mark.parametrize("route", CLAUSE_ROUTES)
def test_standard_removed_behind_a_stale_session_copy_is_still_seen(
    route: str, client: TestClient, db_session: Session, admin_headers: dict
):
    """The ``client`` fixture shares ``db_session`` with the route; the session's standard still says live.

    The standard's flag is judged in the query's ``WHERE``, so the database decides, not the copy.
    """
    clause = _clause(db_session)
    clause_id = clause.id
    standard = clause.standard
    db_session.execute(
        update(QMSStandard)
        .where(QMSStandard.id == standard.id)
        .values(is_deleted=True)
        .execution_options(synchronize_session=False)
    )
    assert standard.is_deleted is False, "the session's copy must still say live for this test to mean anything"
    before = {c.key: getattr(clause, c.key) for c in clause.__table__.columns}

    response = _call(client, route, clause_id, admin_headers)

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert response.json()["detail"] == "Clause not found"
    assert {c.key: getattr(clause, c.key) for c in clause.__table__.columns} == before
    assert clause not in db_session.dirty
    assert _clause_audit_rows(db_session, clause_id) == []


@pytest.mark.parametrize("route", CLAUSE_ROUTES)
def test_clause_of_a_live_standard_still_works(
    route: str, client: TestClient, db_session: Session, admin_headers: dict
):
    clause = _clause(db_session)

    response = _call(client, route, clause.id, admin_headers)

    expected = status.HTTP_204_NO_CONTENT if route == "delete" else status.HTTP_200_OK
    assert response.status_code == expected, response.text
    after = _columns(db_session, clause.id)
    if route == "update":
        assert after["compliance_status"] == "compliant" and after["title"] == "Edited title"
    elif route == "delete":
        assert after["is_deleted"] is True
    else:
        assert response.json()["clause_id"] == clause.id


# ---------------------------------------------------------------------------
# GET /qms-standards/{standard_id}/clauses
# ---------------------------------------------------------------------------


def test_listing_a_removed_standards_clauses_is_404(client: TestClient, db_session: Session, admin_headers: dict):
    clause = _clause(db_session)
    _remove_standard(client, admin_headers, clause)

    response = client.get(f"{BASE}/{clause.standard_id}/clauses", headers=admin_headers)

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert response.json()["detail"] == "QMS standard not found"


@pytest.mark.parametrize("case", ["missing", "other_company"])
def test_listing_a_standard_that_is_not_the_callers_is_the_same_404(
    case: str, client: TestClient, db_session: Session, admin_headers: dict
):
    """A missing id and another company's id get one answer (both used to be an empty list)."""
    if case == "missing":
        standard_id = 987_654
    else:
        standard_id = _clause(db_session, standard_company=COMPANY_B).standard_id

    response = client.get(f"{BASE}/{standard_id}/clauses", headers=admin_headers)

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert response.json()["detail"] == "QMS standard not found"


def test_listing_a_live_standards_clauses_still_works(client: TestClient, db_session: Session, admin_headers: dict):
    clause = _clause(db_session)

    response = client.get(f"{BASE}/{clause.standard_id}/clauses", headers=admin_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert [c["id"] for c in body] == [clause.id]
    assert [e["title"] for e in body[0]["evidence_links"]] == ["Lot traceability"]


# ---------------------------------------------------------------------------
# A removed standard's clause rendered as another clause's sub-clause
# ---------------------------------------------------------------------------


def _graft(db: Session) -> Dict[str, int]:
    """Live standard LIVE with a parent clause and its own sub-clause; standard GONE's clause grafted under it.

    A cross-standard ``parent_clause_id`` could be written before the parent check existed, so
    production may hold one. ``sub_clauses`` is loaded by ``parent_clause_id`` alone.
    """
    _ensure_company(db, COMPANY_A)
    live = QMSStandard(company_id=COMPANY_A, name="LIVE", version="1", is_active=True)
    gone = QMSStandard(company_id=COMPANY_A, name="GONE", version="1", is_active=True)
    db.add_all([live, gone])
    db.flush()
    parent = QMSClause(company_id=COMPANY_A, standard_id=live.id, clause_number="8", title="Operation")
    db.add(parent)
    db.flush()
    own_child = QMSClause(
        company_id=COMPANY_A, standard_id=live.id, clause_number="8.1", title="Planning", parent_clause_id=parent.id
    )
    grafted = QMSClause(
        company_id=COMPANY_A, standard_id=gone.id, clause_number="8.5", title="Grafted", parent_clause_id=parent.id
    )
    db.add_all([own_child, grafted])
    db.flush()
    db.add(QMSClauseEvidence(company_id=COMPANY_A, clause_id=grafted.id, evidence_type="document", title="Doc"))
    db.commit()
    return {"live": live.id, "gone": gone.id, "parent": parent.id, "own_child": own_child.id, "grafted": grafted.id}


def _sub_clause_ids(clauses: List[Dict[str, Any]]) -> set:
    return {s["id"] for c in clauses for s in c["sub_clauses"]}


def _rendered_trees(client: TestClient, ids: Dict[str, int], headers: dict) -> Dict[str, List[Dict[str, Any]]]:
    """Every response that serializes the live standard's clause tree, keyed by route."""
    standard = client.get(f"{BASE}/{ids['live']}", headers=headers)
    listed = client.get(f"{BASE}/{ids['live']}/clauses", headers=headers)
    clause_put = client.put(f"{BASE}/clauses/{ids['parent']}", json={"title": "Operation (rev)"}, headers=headers)
    standard_put = client.put(f"{BASE}/{ids['live']}", json={"description": "touched"}, headers=headers)
    for response in (standard, listed, clause_put, standard_put):
        assert response.status_code == status.HTTP_200_OK, response.text
    return {
        "GET standard": standard.json()["clauses"],
        "GET clauses": listed.json(),
        "PUT clause": [clause_put.json()],
        "PUT standard": standard_put.json()["clauses"],
    }


def test_removed_standards_clause_grafted_under_a_live_clause_is_not_rendered(
    client: TestClient, db_session: Session, admin_headers: dict
):
    ids = _graft(db_session)
    before = _rendered_trees(client, ids, admin_headers)
    for route, clauses in before.items():
        assert ids["grafted"] in _sub_clause_ids(clauses), f"{route}: a live standard's graft renders (unchanged)"

    assert client.delete(f"{BASE}/{ids['gone']}", headers=admin_headers).status_code == status.HTTP_204_NO_CONTENT
    after = _rendered_trees(client, ids, admin_headers)

    for route, clauses in after.items():
        subs = _sub_clause_ids(clauses)
        assert ids["grafted"] not in subs, f"{route}: a removed standard's clause must not render"
        assert ids["own_child"] in subs, f"{route}: the live standard's own sub-clause still renders"
    top_level = {c["id"] for c in after["GET standard"]}
    assert {ids["parent"], ids["own_child"]} <= top_level, "the live standard's own clauses still render"
