"""A QMS clause's ``parent_clause_id`` may only name a live clause of the active company in the same standard.

``qms_clauses.parent_clause_id`` is a plain self-referencing foreign key, and the three routes that
take it from a request body -- ``POST /qms-standards/{standard_id}/clauses``,
``POST /qms-standards/{standard_id}/clauses/bulk`` and ``PUT /qms-standards/clauses/{clause_id}``
-- wrote it unchecked. Company A could graft its clause under company B's, and B's
``GET /qms-standards/{standard_id}`` and ``GET /qms-standards/{standard_id}/clauses`` then rendered
A's clause, with its evidence, in ``sub_clauses`` (CLAUDE.md invariant 1). A nonexistent id failed
the FK at flush (a 500 on Postgres; SQLite, the test backend, does not enforce FKs) while an
existing foreign one answered 201: an existence oracle. Within one company, a clause made its own
parent, or placed under one of its own sub-clauses, committed a cycle that failed response
serialization and then every read of the standard.

Proved here:

* an own-company parent in the same standard is accepted, and renders the child in ``sub_clauses``;
* another company's id, a nonexistent id, a deleted clause and a clause of another standard all get
  the byte-identical 404 ``Parent clause not found``, with no row written or changed and no audit row;
* on the update, the clause itself and any of its descendants are refused 422, nothing written, and
  the standard still reads;
* ``parent_clause_id: null`` clears the parent, and an update that does not send it is not judged on
  the value already stored;
* a foreign graft stored before this check existed no longer renders in the other company's reads
  (GET standard, GET clauses, and the PUT clause and PUT standard responses);
* a re-parent takes the company's advisory lock before it reads the parent, so two opposite moves in
  flight at once cannot both pass the descendant walk (the lock is a no-op on SQLite: recorded here);
* the check uses the ACTIVE company, never the user's home one.

"No audit row" is counted with no rollback first: the ``client`` fixture shares this one open session
with the endpoint, so a flushed-but-uncommitted audit row would be visible here too. "No write" is
checked the same way: the count query autoflushes any attribute the route set, and ``refresh`` then
reads what the database holds.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.qms_standard import QMSClause, QMSClauseEvidence, QMSStandard
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
NOT_FOUND = {"detail": "Parent clause not found"}
IS_SELF = {"detail": "A clause cannot be its own parent clause"}
IS_DESCENDANT = {"detail": "A clause cannot be placed under one of its own sub-clauses"}

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> None:
    if db.query(Company).filter(Company.id == company_id).first() is None:
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def _make_user(db: Session, *, company_id: int = COMPANY_A, role: UserRole = UserRole.QUALITY) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"qms-parent-{n}@co{company_id}.test",
        employee_id=f"QMSPC-{n:05d}",
        first_name="Qms",
        last_name="Parent",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",  # tokens are minted directly; never used for login
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers_for(user: User, *, active_company_id: int = None) -> dict:
    """Auth headers. ``active_company_id`` mints the token for another company than the user's
    home one, the way a platform admin's switched context is simulated."""
    cid = active_company_id if active_company_id is not None else user.company_id
    token = create_access_token(subject=user.id, company_id=cid)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _make_standard(db: Session, *, company_id: int = COMPANY_A) -> QMSStandard:
    _ensure_company(db, company_id)
    standard = QMSStandard(company_id=company_id, name=f"PARENT-STD-{_next()}", version="2016", is_active=True)
    db.add(standard)
    db.commit()
    db.refresh(standard)
    return standard


def _make_clause(db: Session, *, standard: QMSStandard, parent: QMSClause = None, **extra) -> QMSClause:
    """A clause stored directly, bypassing the routes: how a pre-existing (possibly foreign) graft is
    simulated when ``parent`` belongs to another company."""
    n = _next()
    clause = QMSClause(
        company_id=extra.pop("company_id", standard.company_id),
        standard_id=standard.id,
        clause_number=f"{n}.1",
        title=f"Clause {n}",
        compliance_status="not_assessed",
        parent_clause_id=parent.id if parent is not None else None,
        **extra,
    )
    db.add(clause)
    db.commit()
    db.refresh(clause)
    return clause


def _missing_clause_id(db: Session) -> int:
    """An id no clause has, in any company."""
    return (db.query(func.max(QMSClause.id)).scalar() or 0) + 100_000


def _audit_count(db: Session) -> int:
    """Every audit row the shared session can see, flushed or committed (no rollback first)."""
    return db.query(func.count(AuditLog.id)).scalar()


def _clause_count(db: Session) -> int:
    return db.query(func.count(QMSClause.id)).scalar()


def _refused_parent_id(db: Session, which: str, *, standard: QMSStandard) -> int:
    """A parent id the routes must refuse with ``NOT_FOUND`` for a clause in ``standard`` (company A)."""
    if which == "foreign":
        return _make_clause(db, standard=_make_standard(db, company_id=COMPANY_B)).id
    if which == "missing":
        return _missing_clause_id(db)
    if which == "deleted":
        clause = _make_clause(db, standard=standard)
        clause.soft_delete(None)
        db.commit()
        return clause.id
    if which == "other_standard":
        return _make_clause(db, standard=_make_standard(db, company_id=standard.company_id)).id
    raise AssertionError(which)


def _sub_clause_ids(clause_body: dict) -> list:
    return [c["id"] for c in clause_body["sub_clauses"]]


REFUSED_KINDS = ["foreign", "missing", "deleted", "other_standard"]


# ---------------------------------------------------------------------------
# POST /qms-standards/{standard_id}/clauses
# ---------------------------------------------------------------------------


def test_create_clause_under_own_parent_is_accepted_and_rendered(client: TestClient, db_session: Session):
    standard = _make_standard(db_session)
    parent = _make_clause(db_session, standard=standard)
    user = _make_user(db_session)

    resp = client.post(
        f"/api/v1/qms-standards/{standard.id}/clauses",
        headers=_headers_for(user),
        json={"clause_number": "8.5.1", "title": "Control of production", "parent_clause_id": parent.id},
    )

    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    child_id = resp.json()["id"]
    assert resp.json()["parent_clause_id"] == parent.id

    tree = client.get(f"/api/v1/qms-standards/{standard.id}", headers=_headers_for(user))
    assert tree.status_code == status.HTTP_200_OK, tree.text
    by_id = {c["id"]: c for c in tree.json()["clauses"]}
    assert _sub_clause_ids(by_id[parent.id]) == [child_id]


@pytest.mark.parametrize("which", REFUSED_KINDS)
def test_create_clause_refuses_parent_that_is_not_a_live_own_clause_of_the_standard(
    client: TestClient, db_session: Session, which: str
):
    standard = _make_standard(db_session)
    parent_id = _refused_parent_id(db_session, which, standard=standard)
    user = _make_user(db_session)
    clauses_before = _clause_count(db_session)
    audit_before = _audit_count(db_session)

    resp = client.post(
        f"/api/v1/qms-standards/{standard.id}/clauses",
        headers=_headers_for(user),
        json={"clause_number": "9.9", "title": "Grafted clause", "parent_clause_id": parent_id},
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json() == NOT_FOUND
    assert _audit_count(db_session) == audit_before, "a refused clause writes no audit row"
    assert _clause_count(db_session) == clauses_before, "no clause row"


def test_create_clause_foreign_and_missing_answers_are_identical(client: TestClient, db_session: Session):
    """The oracle the check closes: another company's clause id and an unused id read the same."""
    standard = _make_standard(db_session)
    user = _make_user(db_session)
    foreign_id = _refused_parent_id(db_session, "foreign", standard=standard)
    missing_id = _missing_clause_id(db_session)

    answers = [
        client.post(
            f"/api/v1/qms-standards/{standard.id}/clauses",
            headers=_headers_for(user),
            json={"clause_number": "9.9", "title": "Probe", "parent_clause_id": parent_id},
        )
        for parent_id in (foreign_id, missing_id)
    ]

    assert [a.status_code for a in answers] == [status.HTTP_404_NOT_FOUND] * 2
    assert answers[0].content == answers[1].content


def test_foreign_graft_attempt_never_reaches_the_other_companys_tree(client: TestClient, db_session: Session):
    """The reported attack end to end: A posts a clause under B's clause; B's reads stay clean."""
    standard_b = _make_standard(db_session, company_id=COMPANY_B)
    clause_b = _make_clause(db_session, standard=standard_b)
    standard_a = _make_standard(db_session, company_id=COMPANY_A)
    user_a = _make_user(db_session, company_id=COMPANY_A)
    user_b = _make_user(db_session, company_id=COMPANY_B)

    attack = client.post(
        f"/api/v1/qms-standards/{standard_a.id}/clauses",
        headers=_headers_for(user_a),
        json={"clause_number": "X.1", "title": "Injected by A", "parent_clause_id": clause_b.id},
    )
    assert attack.status_code == status.HTTP_404_NOT_FOUND, attack.text

    read = client.get(f"/api/v1/qms-standards/{standard_b.id}", headers=_headers_for(user_b))
    assert read.status_code == status.HTTP_200_OK, read.text
    (only,) = read.json()["clauses"]
    assert only["sub_clauses"] == []


def test_create_clause_judges_the_active_company_not_the_home_one(client: TestClient, db_session: Session):
    """A platform admin whose home company is A, switched into B, adds a clause to B's standard.

    The check must use the active company (``get_current_company_id``): B's parent is accepted, A's
    is refused. Every other test here has home == active, so a check reading the home company would
    pass all of them."""
    standard_b = _make_standard(db_session, company_id=COMPANY_B)
    parent_b = _make_clause(db_session, standard=standard_b)
    parent_a = _make_clause(db_session, standard=_make_standard(db_session, company_id=COMPANY_A))
    platform_admin = _make_user(db_session, company_id=COMPANY_A, role=UserRole.PLATFORM_ADMIN)
    headers = _headers_for(platform_admin, active_company_id=COMPANY_B)

    refused = client.post(
        f"/api/v1/qms-standards/{standard_b.id}/clauses",
        headers=headers,
        json={"clause_number": "1.1", "title": "Under home-company clause", "parent_clause_id": parent_a.id},
    )
    accepted = client.post(
        f"/api/v1/qms-standards/{standard_b.id}/clauses",
        headers=headers,
        json={"clause_number": "1.2", "title": "Under active-company clause", "parent_clause_id": parent_b.id},
    )

    assert refused.status_code == status.HTTP_404_NOT_FOUND, refused.text
    assert refused.json() == NOT_FOUND
    assert accepted.status_code == status.HTTP_201_CREATED, accepted.text
    assert accepted.json()["parent_clause_id"] == parent_b.id


# ---------------------------------------------------------------------------
# POST /qms-standards/{standard_id}/clauses/bulk
# ---------------------------------------------------------------------------


def test_bulk_create_under_own_parent_is_accepted(client: TestClient, db_session: Session):
    standard = _make_standard(db_session)
    parent = _make_clause(db_session, standard=standard)
    user = _make_user(db_session)

    resp = client.post(
        f"/api/v1/qms-standards/{standard.id}/clauses/bulk",
        headers=_headers_for(user),
        json={
            "clauses": [
                {"clause_number": "8.5.1", "title": "Child one", "parent_clause_id": parent.id},
                {"clause_number": "8.6", "title": "Top level"},
            ]
        },
    )

    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    assert [c["parent_clause_id"] for c in resp.json()] == [parent.id, None]


@pytest.mark.parametrize("which", REFUSED_KINDS)
def test_bulk_create_refuses_the_whole_batch_on_one_bad_parent(client: TestClient, db_session: Session, which: str):
    """All-or-nothing: the good line beside the bad one is not created either."""
    standard = _make_standard(db_session)
    good_parent = _make_clause(db_session, standard=standard)
    bad_parent_id = _refused_parent_id(db_session, which, standard=standard)
    user = _make_user(db_session)
    clauses_before = _clause_count(db_session)
    audit_before = _audit_count(db_session)

    resp = client.post(
        f"/api/v1/qms-standards/{standard.id}/clauses/bulk",
        headers=_headers_for(user),
        json={
            "clauses": [
                {"clause_number": "8.5.1", "title": "Good line", "parent_clause_id": good_parent.id},
                {"clause_number": "8.5.2", "title": "Bad line", "parent_clause_id": bad_parent_id},
            ]
        },
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json() == NOT_FOUND
    assert _audit_count(db_session) == audit_before, "a refused batch writes no audit row"
    assert _clause_count(db_session) == clauses_before, "no clause row, the good line included"


def test_bulk_create_foreign_and_missing_answers_are_identical(client: TestClient, db_session: Session):
    standard = _make_standard(db_session)
    user = _make_user(db_session)
    foreign_id = _refused_parent_id(db_session, "foreign", standard=standard)
    missing_id = _missing_clause_id(db_session)

    answers = [
        client.post(
            f"/api/v1/qms-standards/{standard.id}/clauses/bulk",
            headers=_headers_for(user),
            json={"clauses": [{"clause_number": "9.9", "title": "Probe", "parent_clause_id": parent_id}]},
        )
        for parent_id in (foreign_id, missing_id)
    ]

    assert [a.status_code for a in answers] == [status.HTTP_404_NOT_FOUND] * 2
    assert answers[0].content == answers[1].content


# ---------------------------------------------------------------------------
# PUT /qms-standards/clauses/{clause_id}
# ---------------------------------------------------------------------------


def test_update_clause_moves_under_own_parent(client: TestClient, db_session: Session):
    standard = _make_standard(db_session)
    parent = _make_clause(db_session, standard=standard)
    clause = _make_clause(db_session, standard=standard)
    user = _make_user(db_session)
    audit_before = _audit_count(db_session)

    resp = client.put(
        f"/api/v1/qms-standards/clauses/{clause.id}",
        headers=_headers_for(user),
        json={"parent_clause_id": parent.id},
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["parent_clause_id"] == parent.id
    assert _audit_count(db_session) == audit_before + 1, "the move is audited as before"


@pytest.mark.parametrize("which", REFUSED_KINDS)
def test_update_clause_refuses_parent_that_is_not_a_live_own_clause_of_the_standard(
    client: TestClient, db_session: Session, which: str
):
    standard = _make_standard(db_session)
    original_parent = _make_clause(db_session, standard=standard)
    clause = _make_clause(db_session, standard=standard, parent=original_parent)
    parent_id = _refused_parent_id(db_session, which, standard=standard)
    user = _make_user(db_session)
    audit_before = _audit_count(db_session)

    resp = client.put(
        f"/api/v1/qms-standards/clauses/{clause.id}",
        headers=_headers_for(user),
        json={"parent_clause_id": parent_id, "title": "Renamed in the same request", "compliance_status": "compliant"},
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json() == NOT_FOUND
    assert _audit_count(db_session) == audit_before, "a refused update writes no audit row"
    db_session.refresh(clause)
    assert clause.parent_clause_id == original_parent.id
    assert clause.title != "Renamed in the same request", "the other fields in the request are not applied"
    assert clause.compliance_status == "not_assessed"


def test_update_clause_foreign_and_missing_answers_are_identical(client: TestClient, db_session: Session):
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard=standard)
    user = _make_user(db_session)
    foreign_id = _refused_parent_id(db_session, "foreign", standard=standard)
    missing_id = _missing_clause_id(db_session)

    answers = [
        client.put(
            f"/api/v1/qms-standards/clauses/{clause.id}",
            headers=_headers_for(user),
            json={"parent_clause_id": parent_id},
        )
        for parent_id in (foreign_id, missing_id)
    ]

    assert [a.status_code for a in answers] == [status.HTTP_404_NOT_FOUND] * 2
    assert answers[0].content == answers[1].content


def test_update_clause_refuses_itself_as_parent(client: TestClient, db_session: Session):
    standard = _make_standard(db_session)
    clause = _make_clause(db_session, standard=standard)
    user = _make_user(db_session)
    audit_before = _audit_count(db_session)

    resp = client.put(
        f"/api/v1/qms-standards/clauses/{clause.id}",
        headers=_headers_for(user),
        json={"parent_clause_id": clause.id},
    )

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    assert resp.json() == IS_SELF
    assert _audit_count(db_session) == audit_before
    db_session.refresh(clause)
    assert clause.parent_clause_id is None

    read = client.get(f"/api/v1/qms-standards/{standard.id}", headers=_headers_for(user))
    assert read.status_code == status.HTTP_200_OK, "the standard still reads: no cycle was stored"


@pytest.mark.parametrize("depth", [1, 3])
def test_update_clause_refuses_a_descendant_as_parent(client: TestClient, db_session: Session, depth: int):
    """A child (depth 1) or a great-grandchild (depth 3) of the clause cannot become its parent."""
    standard = _make_standard(db_session)
    root = _make_clause(db_session, standard=standard)
    descendant = root
    for _ in range(depth):
        descendant = _make_clause(db_session, standard=standard, parent=descendant)
    user = _make_user(db_session)
    audit_before = _audit_count(db_session)

    resp = client.put(
        f"/api/v1/qms-standards/clauses/{root.id}",
        headers=_headers_for(user),
        json={"parent_clause_id": descendant.id},
    )

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    assert resp.json() == IS_DESCENDANT
    assert _audit_count(db_session) == audit_before
    db_session.refresh(root)
    assert root.parent_clause_id is None

    for path in (f"/api/v1/qms-standards/{standard.id}", f"/api/v1/qms-standards/{standard.id}/clauses"):
        read = client.get(path, headers=_headers_for(user))
        assert read.status_code == status.HTTP_200_OK, f"{path} still reads: no cycle was stored"


def test_update_clause_refuses_a_descendant_through_a_deleted_link(client: TestClient, db_session: Session):
    """root -> deleted middle -> leaf: the stored link through the deleted clause still counts, so
    restoring the middle clause could never bring back a cycle."""
    standard = _make_standard(db_session)
    root = _make_clause(db_session, standard=standard)
    middle = _make_clause(db_session, standard=standard, parent=root)
    leaf = _make_clause(db_session, standard=standard, parent=middle)
    middle.soft_delete(None)
    db_session.commit()
    user = _make_user(db_session)

    resp = client.put(
        f"/api/v1/qms-standards/clauses/{root.id}",
        headers=_headers_for(user),
        json={"parent_clause_id": leaf.id},
    )

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    assert resp.json() == IS_DESCENDANT


def test_update_clause_walk_terminates_on_an_already_cyclic_chain(client: TestClient, db_session: Session):
    """x <-> y is a cycle stored before the check existed. Moving an unrelated clause under x must
    terminate (and succeed: the moved clause is not in that cycle)."""
    standard = _make_standard(db_session)
    x = _make_clause(db_session, standard=standard)
    y = _make_clause(db_session, standard=standard, parent=x)
    x.parent_clause_id = y.id
    db_session.commit()
    moved = _make_clause(db_session, standard=standard)
    user = _make_user(db_session)

    resp = client.put(
        f"/api/v1/qms-standards/clauses/{moved.id}",
        headers=_headers_for(user),
        json={"parent_clause_id": x.id},
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["parent_clause_id"] == x.id


def test_update_clause_null_clears_the_parent(client: TestClient, db_session: Session):
    standard = _make_standard(db_session)
    parent = _make_clause(db_session, standard=standard)
    clause = _make_clause(db_session, standard=standard, parent=parent)
    user = _make_user(db_session)
    audit_before = _audit_count(db_session)

    resp = client.put(
        f"/api/v1/qms-standards/clauses/{clause.id}",
        headers=_headers_for(user),
        json={"parent_clause_id": None},
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["parent_clause_id"] is None
    assert _audit_count(db_session) == audit_before + 1
    db_session.refresh(clause)
    assert clause.parent_clause_id is None


def test_update_clause_null_clears_a_stored_foreign_graft(client: TestClient, db_session: Session):
    """The clean-up path for an existing cross-company link: the owning company PUTs null."""
    clause_b = _make_clause(db_session, standard=_make_standard(db_session, company_id=COMPANY_B))
    grafted_a = _make_clause(db_session, standard=_make_standard(db_session, company_id=COMPANY_A), parent=clause_b)
    user_a = _make_user(db_session, company_id=COMPANY_A)

    resp = client.put(
        f"/api/v1/qms-standards/clauses/{grafted_a.id}",
        headers=_headers_for(user_a),
        json={"parent_clause_id": None},
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.refresh(grafted_a)
    assert grafted_a.parent_clause_id is None


def test_update_clause_without_parent_is_not_judged_on_the_stored_value(client: TestClient, db_session: Session):
    """An assessment on a clause whose STORED parent is a legacy foreign graft still saves: only a
    ``parent_clause_id`` the request sends is checked."""
    clause_b = _make_clause(db_session, standard=_make_standard(db_session, company_id=COMPANY_B))
    grafted_a = _make_clause(db_session, standard=_make_standard(db_session, company_id=COMPANY_A), parent=clause_b)
    user_a = _make_user(db_session, company_id=COMPANY_A)

    resp = client.put(
        f"/api/v1/qms-standards/clauses/{grafted_a.id}",
        headers=_headers_for(user_a),
        json={"compliance_status": "compliant"},
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["compliance_status"] == "compliant"


def test_update_clause_cannot_move_under_another_companys_clause(client: TestClient, db_session: Session):
    """The PUT form of the attack: a company-A clause re-parented under B's clause."""
    standard_b = _make_standard(db_session, company_id=COMPANY_B)
    clause_b = _make_clause(db_session, standard=standard_b)
    clause_a = _make_clause(db_session, standard=_make_standard(db_session, company_id=COMPANY_A))
    user_a = _make_user(db_session, company_id=COMPANY_A)
    user_b = _make_user(db_session, company_id=COMPANY_B)

    attack = client.put(
        f"/api/v1/qms-standards/clauses/{clause_a.id}",
        headers=_headers_for(user_a),
        json={"parent_clause_id": clause_b.id},
    )

    assert attack.status_code == status.HTTP_404_NOT_FOUND, attack.text
    read = client.get(f"/api/v1/qms-standards/{standard_b.id}/clauses", headers=_headers_for(user_b))
    assert read.status_code == status.HTTP_200_OK, read.text
    assert read.json()[0]["sub_clauses"] == []


# ---------------------------------------------------------------------------
# A foreign graft stored before the check existed does not render
# ---------------------------------------------------------------------------


def test_stored_foreign_graft_is_not_rendered_in_the_other_companys_reads(client: TestClient, db_session: Session):
    """B's clause carries a company-A child (and that child carries A's evidence), stored directly
    as the old routes allowed. B's GET standard, GET clauses and PUT response render B's own child
    and never A's. A's own read of its tree is unaffected (and, with B's reads in the same process,
    shows the per-request company is what the lazy sub_clauses load is filtered on)."""
    standard_b = _make_standard(db_session, company_id=COMPANY_B)
    clause_b = _make_clause(db_session, standard=standard_b)
    own_child_b = _make_clause(db_session, standard=standard_b, parent=clause_b)

    standard_a = _make_standard(db_session, company_id=COMPANY_A)
    grafted_a = _make_clause(db_session, standard=standard_a, parent=clause_b)
    db_session.add(
        QMSClauseEvidence(
            company_id=COMPANY_A,
            clause_id=grafted_a.id,
            evidence_type="document",
            title="A-only evidence",
        )
    )
    a_parent = _make_clause(db_session, standard=standard_a)
    a_child = _make_clause(db_session, standard=standard_a, parent=a_parent)

    user_a = _make_user(db_session, company_id=COMPANY_A)
    user_b = _make_user(db_session, company_id=COMPANY_B)

    standard_read = client.get(f"/api/v1/qms-standards/{standard_b.id}", headers=_headers_for(user_b))
    assert standard_read.status_code == status.HTTP_200_OK, standard_read.text
    by_id = {c["id"]: c for c in standard_read.json()["clauses"]}
    assert _sub_clause_ids(by_id[clause_b.id]) == [own_child_b.id]
    assert "A-only evidence" not in standard_read.text

    clauses_read = client.get(f"/api/v1/qms-standards/{standard_b.id}/clauses", headers=_headers_for(user_b))
    assert clauses_read.status_code == status.HTTP_200_OK, clauses_read.text
    by_id = {c["id"]: c for c in clauses_read.json()}
    assert _sub_clause_ids(by_id[clause_b.id]) == [own_child_b.id]
    assert "A-only evidence" not in clauses_read.text

    put = client.put(
        f"/api/v1/qms-standards/clauses/{clause_b.id}",
        headers=_headers_for(user_b),
        json={"compliance_notes": "reviewed"},
    )
    assert put.status_code == status.HTTP_200_OK, put.text
    assert _sub_clause_ids(put.json()) == [own_child_b.id]
    assert "A-only evidence" not in put.text

    a_read = client.get(f"/api/v1/qms-standards/{standard_a.id}", headers=_headers_for(user_a))
    assert a_read.status_code == status.HTTP_200_OK, a_read.text
    by_id = {c["id"]: c for c in a_read.json()["clauses"]}
    assert _sub_clause_ids(by_id[a_parent.id]) == [a_child.id]
    assert grafted_a.id in by_id, "A still sees its own clause (and can clear the stored parent)"


def test_put_response_no_longer_renders_a_deleted_sub_clause(client: TestClient, db_session: Session):
    """The PUT response now goes through the same criteria as the reads: before, its plain refresh
    lazy-loaded sub_clauses unfiltered, deleted children included."""
    standard = _make_standard(db_session)
    parent = _make_clause(db_session, standard=standard)
    live_child = _make_clause(db_session, standard=standard, parent=parent)
    deleted_child = _make_clause(db_session, standard=standard, parent=parent)
    deleted_child.soft_delete(None)
    db_session.commit()
    user = _make_user(db_session)

    resp = client.put(
        f"/api/v1/qms-standards/clauses/{parent.id}",
        headers=_headers_for(user),
        json={"compliance_notes": "reviewed"},
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert _sub_clause_ids(resp.json()) == [live_child.id]


def test_put_standard_response_does_not_render_a_stored_foreign_graft(client: TestClient, db_session: Session):
    """``PUT /qms-standards/{id}`` returns the whole clause tree too. It used to ``db.refresh`` the
    standard, which lazy-loaded clauses, sub_clauses and evidence with no filter at all, so B's own
    rename showed A's grafted clause (and a deleted B clause) in its response."""
    standard_b = _make_standard(db_session, company_id=COMPANY_B)
    clause_b = _make_clause(db_session, standard=standard_b)
    own_child_b = _make_clause(db_session, standard=standard_b, parent=clause_b)
    deleted_b = _make_clause(db_session, standard=standard_b, parent=clause_b)
    deleted_b.soft_delete(None)
    db_session.commit()
    grafted_a = _make_clause(db_session, standard=_make_standard(db_session, company_id=COMPANY_A), parent=clause_b)
    db_session.add(
        QMSClauseEvidence(
            company_id=COMPANY_A, clause_id=grafted_a.id, evidence_type="document", title="A-only evidence"
        )
    )
    db_session.commit()
    user_b = _make_user(db_session, company_id=COMPANY_B)

    resp = client.put(
        f"/api/v1/qms-standards/{standard_b.id}",
        headers=_headers_for(user_b),
        json={"description": "Renamed by B"},
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["description"] == "Renamed by B"
    by_id = {c["id"]: c for c in resp.json()["clauses"]}
    assert set(by_id) == {clause_b.id, own_child_b.id}, "no deleted clause at the top level either"
    assert _sub_clause_ids(by_id[clause_b.id]) == [own_child_b.id]
    assert "A-only evidence" not in resp.text


# ---------------------------------------------------------------------------
# Re-parenting is serialized per company
# ---------------------------------------------------------------------------


def _record_lock_and_check(monkeypatch) -> list:
    """Record, in order, each re-parent lock the update takes and each parent read it makes."""
    import app.api.endpoints.qms_standards as qms

    calls: list = []
    real_check = qms._assert_parent_clauses

    def fake_lock(db, namespace, company_id=None):
        calls.append(("lock", namespace, company_id))

    def spy_check(*args, **kwargs):
        calls.append(("check",))
        return real_check(*args, **kwargs)

    monkeypatch.setattr(qms, "acquire_generator_lock", fake_lock)
    monkeypatch.setattr(qms, "_assert_parent_clauses", spy_check)
    return calls


def test_reparent_takes_the_company_lock_before_reading_the_parent(
    client: TestClient, db_session: Session, monkeypatch
):
    """Two opposite moves in flight at once (C under P, P under C) could each pass the descendant
    walk before the other committed and store a cycle. The update takes the company's re-parent
    advisory lock (a no-op on SQLite, so recorded here) before it reads the parent or walks."""
    standard = _make_standard(db_session, company_id=COMPANY_B)
    parent = _make_clause(db_session, standard=standard)
    clause = _make_clause(db_session, standard=standard)
    user = _make_user(db_session, company_id=COMPANY_B)
    calls = _record_lock_and_check(monkeypatch)

    resp = client.put(
        f"/api/v1/qms-standards/clauses/{clause.id}",
        headers=_headers_for(user),
        json={"parent_clause_id": parent.id},
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert calls == [("lock", "qms_clause_parent", COMPANY_B), ("check",)]


@pytest.mark.parametrize("body", [{"parent_clause_id": None}, {"compliance_notes": "reviewed"}])
def test_update_without_a_new_parent_takes_no_lock(client: TestClient, db_session: Session, monkeypatch, body: dict):
    """Clearing the parent, or an edit that does not send it, cannot make a cycle: no lock, no check."""
    standard = _make_standard(db_session)
    parent = _make_clause(db_session, standard=standard)
    clause = _make_clause(db_session, standard=standard, parent=parent)
    user = _make_user(db_session)
    calls = _record_lock_and_check(monkeypatch)

    resp = client.put(f"/api/v1/qms-standards/clauses/{clause.id}", headers=_headers_for(user), json=body)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert calls == []


def test_update_clause_judges_the_active_company_not_the_home_one(client: TestClient, db_session: Session):
    """The update twin of the create test: a platform admin (home A) switched into B moves B's clause.
    A's clause is refused as a parent, B's is accepted."""
    standard_b = _make_standard(db_session, company_id=COMPANY_B)
    parent_b = _make_clause(db_session, standard=standard_b)
    clause_b = _make_clause(db_session, standard=standard_b)
    parent_a = _make_clause(db_session, standard=_make_standard(db_session, company_id=COMPANY_A))
    platform_admin = _make_user(db_session, company_id=COMPANY_A, role=UserRole.PLATFORM_ADMIN)
    headers = _headers_for(platform_admin, active_company_id=COMPANY_B)

    refused = client.put(
        f"/api/v1/qms-standards/clauses/{clause_b.id}", headers=headers, json={"parent_clause_id": parent_a.id}
    )
    accepted = client.put(
        f"/api/v1/qms-standards/clauses/{clause_b.id}", headers=headers, json={"parent_clause_id": parent_b.id}
    )

    assert refused.status_code == status.HTTP_404_NOT_FOUND, refused.text
    assert refused.json() == NOT_FOUND
    assert accepted.status_code == status.HTTP_200_OK, accepted.text
    assert accepted.json()["parent_clause_id"] == parent_b.id
