"""Three report-template review fixes, pinned at the HTTP boundary.

* **Admin read (N1).** An Admin may change or delete any template, and a change answers
  with the whole definition -- yet the read rule refused an Admin a colleague's private
  template (403), so the refusal protected nothing and only made the two rules disagree.
  Read / run / export is now own, shared, OR an Admin (a superuser or Platform Admin counts,
  as for change and delete). A Manager is still refused a colleague's private template, and
  the LIST is unchanged (own plus shared, for every role).
* **Export gate (N2).** ``GET /analytics/custom-report/export`` used to read the template and
  run the up-to-10,001-row query before ``log_export`` refused a narrowed (read-only /
  AI-external) API token, and the over-cap / empty 400s answered before that refusal -- a
  row-count oracle. The narrowed token is now refused 403 first, before any read.
* **Required delete audit (N3).** The delete is physical, so its audit snapshot is the only
  trace. If that row cannot be written the delete rolls back: 503, the template still exists.
"""

from typing import Dict, List

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.endpoints import analytics as analytics_endpoints
from app.core.export_control import READ_ONLY_FILE_REFUSED_DETAIL
from app.core.security import create_access_token
from app.models.analytics import ReportTemplate
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services import report_template_service
from app.services.audit_service import AuditService
from app.services.report_builder import ReportBuilderService
from tests.api.export_audit_helpers import committed_export_rows, fresh_session
from tests.api.export_control_helpers import bearer, issue_token

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
TEMPLATES = "/api/v1/analytics/custom-report/templates"
EXPORT = "/api/v1/analytics/custom-report/export"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session) -> None:
    if not db.query(Company).filter(Company.id == COMPANY_A).first():
        db.add(Company(id=COMPANY_A, name="Company 1", slug="company-1", is_active=True))
        db.commit()


def make_user(db: Session, *, role: UserRole = UserRole.MANAGER, is_superuser: bool = False) -> User:
    _ensure_company(db)
    n = _next()
    user = User(
        email=f"rta-{n}@co1.test",
        employee_id=f"RTA-{n:05d}",
        first_name="Report",
        last_name=f"Access{n}",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        is_superuser=is_superuser,
        company_id=COMPANY_A,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> Dict[str, str]:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_work_order(db: Session, customer: str = "Acme") -> WorkOrder:
    _ensure_company(db)
    part = Part(
        part_number=f"RTA-P-{_next():05d}",
        name="Fixture",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(part)
    db.commit()
    wo = WorkOrder(
        work_order_number=f"RTA-WO-{_next():05d}",
        customer_name=customer,
        part_id=part.id,
        quantity_ordered=5,
        status=WorkOrderStatus.IN_PROGRESS,
        priority=5,
        company_id=COMPANY_A,
    )
    db.add(wo)
    db.commit()
    return wo


def make_template(db: Session, owner: User, *, name: str, shared: bool = False, filters=None) -> ReportTemplate:
    template = ReportTemplate(
        name=name,
        description=None,
        data_source="work_orders",
        columns=[{"field": "work_order_number"}, {"field": "customer_name"}],
        filters=filters or [],
        group_by=[],
        sort=[],
        is_shared=shared,
        created_by=owner.id,
        company_id=COMPANY_A,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


# ===========================================================================
# N1: an Admin reads, runs and exports any template; a Manager peer does not
# ===========================================================================


@pytest.mark.parametrize(
    "role,is_superuser",
    [(UserRole.ADMIN, False), (UserRole.PLATFORM_ADMIN, False), (UserRole.MANAGER, True)],
    ids=["admin", "platform admin", "superuser manager"],
)
def test_an_admin_reads_runs_and_exports_a_colleagues_private_template(
    client: TestClient, db_session: Session, role: UserRole, is_superuser: bool
):
    author = make_user(db_session)
    admin = make_user(db_session, role=role, is_superuser=is_superuser)
    wo = make_work_order(db_session)
    private = make_template(db_session, author, name="Author Private")
    headers = headers_for(admin)

    got = client.get(f"{TEMPLATES}/{private.id}", headers=headers)
    assert got.status_code == status.HTTP_200_OK, got.text
    assert got.json()["created_by"] == author.id

    ran = client.post(f"{TEMPLATES}/{private.id}/run", headers=headers)
    assert ran.status_code == status.HTTP_200_OK, ran.text
    assert ran.json()["rows"] == [{"work_order_number": wo.work_order_number, "customer_name": "Acme"}]

    exported = client.get(f"{EXPORT}?template_id={private.id}", headers=headers)
    assert exported.status_code == status.HTTP_200_OK, exported.text
    [row] = committed_export_rows("custom_report")
    assert (row.resource_id, row.user_id) == (private.id, admin.id)

    # Change stays the author-or-admin rule it was; the two rules now name the same admin.
    patched = client.patch(f"{TEMPLATES}/{private.id}", headers=headers, json={"description": "reviewed"})
    assert patched.status_code == status.HTTP_200_OK, patched.text

    # The LIST is unchanged: own plus shared, so a colleague's private template is opened by id.
    assert private.id not in {t["id"] for t in client.get(TEMPLATES, headers=headers).json()}


def test_a_manager_is_still_refused_a_colleagues_private_template(client: TestClient, db_session: Session):
    author = make_user(db_session)
    peer = make_user(db_session)
    make_work_order(db_session)
    private = make_template(db_session, author, name="Author Private")
    headers = headers_for(peer)
    for resp in (
        client.get(f"{TEMPLATES}/{private.id}", headers=headers),
        client.post(f"{TEMPLATES}/{private.id}/run", headers=headers),
        client.get(f"{EXPORT}?template_id={private.id}", headers=headers),
    ):
        assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
        assert resp.json()["detail"] == report_template_service.READ_DENIED_DETAIL
    assert committed_export_rows() == []


# ===========================================================================
# N2: a narrowed token is refused before the template read and the query
# ===========================================================================


@pytest.fixture
def no_reads(monkeypatch) -> List[str]:
    """Record every template read and report query the export would make."""
    calls: List[str] = []
    real_readable = report_template_service.require_readable
    real_execute = ReportBuilderService.execute_report

    def readable(*args, **kwargs):
        calls.append("require_readable")
        return real_readable(*args, **kwargs)

    def execute(self, *args, **kwargs):
        calls.append("execute_report")
        return real_execute(self, *args, **kwargs)

    monkeypatch.setattr(report_template_service, "require_readable", readable)
    monkeypatch.setattr(ReportBuilderService, "execute_report", execute)
    return calls


def test_a_read_only_token_is_refused_the_export_before_any_read(
    client: TestClient, db_session: Session, admin_headers: dict, monkeypatch, no_reads: List[str]
):
    """The over-cap and the empty template (and an id that does not exist) all answer the same
    403: none of them gets far enough to say how many rows it matches, or whether it exists."""
    monkeypatch.setattr(analytics_endpoints, "MAX_EXPORT_ROWS", 1)
    manager = make_user(db_session)
    for _ in range(2):
        make_work_order(db_session)
    over_cap = make_template(db_session, manager, name="Over Cap")
    empty = make_template(
        db_session,
        manager,
        name="Empty",
        filters=[{"field": "customer_name", "operator": "eq", "value": "Nobody", "value2": None}],
    )
    _token_id, read_only = issue_token(client, admin_headers, manager, scope="read_only", ai_external=False)
    # An AI-external token is narrowed too; the route is off its allowlist, so an earlier gate
    # answers it -- the handler's own refusal is the floor if it is ever allowlisted.
    _token_id, ai_external = issue_token(client, admin_headers, manager, scope="read_only", ai_external=True)

    for template_id in (over_cap.id, empty.id, 999_999):
        resp = client.get(f"{EXPORT}?template_id={template_id}", headers=bearer(read_only))
        assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
        assert resp.json() == {"detail": READ_ONLY_FILE_REFUSED_DETAIL}
        refused = client.get(f"{EXPORT}?template_id={template_id}", headers=bearer(ai_external))
        assert refused.status_code == status.HTTP_403_FORBIDDEN, refused.text
    assert no_reads == []
    assert committed_export_rows() == []

    # Control: the same person, signed in, reaches the template and the query -- and the 400s.
    person = headers_for(manager)
    assert client.get(f"{EXPORT}?template_id={over_cap.id}", headers=person).status_code == 400
    assert client.get(f"{EXPORT}?template_id={empty.id}", headers=person).status_code == 400
    assert no_reads.count("execute_report") == 2


# ===========================================================================
# N3: the delete's audit row is required
# ===========================================================================


def test_a_delete_whose_audit_row_cannot_be_saved_rolls_back(client: TestClient, db_session: Session, monkeypatch):
    manager = make_user(db_session)
    template = make_template(db_session, manager, name="Kept By Its Audit")
    template_id = template.id
    real_log = AuditService.log

    def failing_log(self, action, resource_type, *args, **kwargs):
        if resource_type == report_template_service.AUDIT_RESOURCE_TYPE:
            return None  # the best-effort log's "could not write" -- log_required raises on it
        return real_log(self, action, resource_type, *args, **kwargs)

    monkeypatch.setattr(AuditService, "log", failing_log)
    resp = client.delete(f"{TEMPLATES}/{template_id}", headers=headers_for(manager))
    assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, resp.text
    assert resp.json()["detail"] == (
        "The audit record for this delete could not be saved; the template was not deleted. Retry."
    )

    with fresh_session() as verify:
        assert verify.query(ReportTemplate).filter(ReportTemplate.id == template_id).count() == 1
        assert verify.query(AuditLog).filter(AuditLog.resource_type == "report_template").count() == 0
    db_session.expire_all()
    assert db_session.query(ReportTemplate).filter(ReportTemplate.id == template_id).count() == 1

    # With the audit working again the same delete goes through, with its required row.
    monkeypatch.setattr(AuditService, "log", real_log)
    assert client.delete(f"{TEMPLATES}/{template_id}", headers=headers_for(manager)).status_code == 200
    with fresh_session() as verify:
        [row] = verify.query(AuditLog).filter(AuditLog.resource_type == "report_template").all()
        assert (row.action, row.resource_id, row.resource_identifier) == ("DELETE", template_id, "Kept By Its Audit")
        assert row.old_values["name"] == "Kept By Its Audit" and row.extra_data["soft_delete"] is False
        assert row.description == "Deleted report_template: Kept By Its Audit"
