"""Tenant-isolation coverage for the auto-evidence discovery service.

Locks in the fix on branch qa/full-pass-2026-06-04 that makes
``app.services.auto_evidence_service`` company-scoped. Previously its 13
``_query_*`` helpers counted rows across ALL companies (a cross-tenant leak
into the QMS audit-readiness evidence). Now every helper -- and the public
``discover_evidence_for_clause(db, clause, company_id)`` entry point -- filters
by ``company_id``.

Headline invariant: company A's auto-evidence counts and examples must never
include company B's rows.

Three angles are exercised directly against the service (no HTTP):
1. Audit log -- the originally-reported leak. Rows are seeded through
   ``AuditService`` (the only valid path: the table has NOT-NULL
   ``sequence_number``/``integrity_hash`` and a hash chain). Company-2 rows are
   written by a platform admin switched into company 2, exactly as
   ``get_current_company_id`` would scope a real cross-company write.
2. NonConformanceReport -- a standard tenant-scoped domain model.
3. WorkOrder -- soft-delete aware: company A's count must exclude both company
   B's rows AND company A's own soft-deleted rows.

Each test would FAIL if the ``company_id`` filter were removed from the
corresponding ``_query_*`` helper: company B's rows would inflate company A's
counts (and, for the negative-control assertions, A's identifiers would appear
in B's results).
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.qms_standard import QMSClause, QMSStandard
from app.models.quality import NCRSource, NCRStatus, NonConformanceReport
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.audit_service import AuditService
from app.services.auto_evidence_service import (
    _query_audit_log,
    _query_ncrs,
    _query_work_orders,
    discover_evidence_for_clause,
)

pytestmark = [pytest.mark.unit, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2

# Module-level counter so every fixture row gets a globally unique natural key,
# even across companies and across tests sharing a worker DB under -n auto.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(
            id=company_id,
            name=f"Company {company_id}",
            slug=f"company-{company_id}",
            is_active=True,
        )
        db.add(company)
        db.commit()
    return company


def _make_user(db: Session, *, company_id: int, role: UserRole = UserRole.PLATFORM_ADMIN) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"evidence-user-{n}@co{company_id}.test",
        employee_id=f"EVD-{n:05d}",
        first_name="Evidence",
        last_name=f"C{company_id}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_clause(db: Session, *, title: str, description: str = "") -> QMSClause:
    """Create a throwaway QMSStandard + QMSClause.

    The clause is only used for keyword matching by the service; its text
    selects which ``_query_*`` rule(s) fire. qms_standards/qms_clauses carry no
    ``company_id`` in this schema, so no tenant is needed here.
    """
    n = _next()
    standard = QMSStandard(name=f"STD-{n}", version="2015", is_active=True)
    db.add(standard)
    db.flush()
    clause = QMSClause(
        standard_id=standard.id,
        clause_number=f"{n}.0",
        title=title,
        description=description,
    )
    db.add(clause)
    db.commit()
    db.refresh(clause)
    return clause


# ---------------------------------------------------------------------------
# Seeders
# ---------------------------------------------------------------------------


def _seed_audit_rows(db: Session) -> dict:
    """Write a valid, chained audit trail spanning companies A and B.

    Returns the counts seeded per company. All rows go through AuditService so
    they are stamped with ``company_id`` and linked via the real hash chain;
    company-B rows are written by a platform admin switched into company B.
    """
    writer = _make_user(db, company_id=COMPANY_A, role=UserRole.PLATFORM_ADMIN)

    # 3 rows for company A (home company).
    svc_a = AuditService(db, writer)
    assert svc_a.company_id == COMPANY_A
    a_count = 3
    for _ in range(a_count):
        row = svc_a.log(action="CREATE", resource_type="part", resource_id=_next(), resource_identifier="A-PART")
        assert row is not None and row.company_id == COMPANY_A
    db.flush()

    # 5 rows for company B (via a context switch) -- the would-be leak.
    writer._active_company_id = COMPANY_B
    svc_b = AuditService(db, writer)
    assert svc_b.company_id == COMPANY_B
    b_count = 5
    for _ in range(b_count):
        row = svc_b.log(action="EXPORT", resource_type="shipment", resource_id=_next(), resource_identifier="B-SHIP")
        assert row is not None and row.company_id == COMPANY_B
    db.commit()

    return {"a_count": a_count, "b_count": b_count}


def _make_ncr(db: Session, *, company_id: int, recent: bool = True, status: NCRStatus = NCRStatus.CLOSED) -> None:
    n = _next()
    created = datetime.utcnow() if recent else datetime.utcnow() - timedelta(days=400)
    ncr = NonConformanceReport(
        company_id=company_id,
        ncr_number=f"NCR-{n:06d}",
        title=f"NCR {n}",
        description="dimensional out of spec",
        source=NCRSource.IN_PROCESS,
        status=status,
        created_at=created,
    )
    db.add(ncr)


def _seed_ncrs(db: Session) -> dict:
    _ensure_company(db, COMPANY_A)
    _ensure_company(db, COMPANY_B)

    # Company A: 2 recent + 1 old = 3 total, all in last-12-month window except 1.
    a_recent, a_old = 2, 1
    for _ in range(a_recent):
        _make_ncr(db, company_id=COMPANY_A, recent=True)
    for _ in range(a_old):
        _make_ncr(db, company_id=COMPANY_A, recent=False)

    # Company B: 4 recent NCRs -- these must never count toward A.
    b_recent = 4
    for _ in range(b_recent):
        _make_ncr(db, company_id=COMPANY_B, recent=True)
    db.commit()

    return {"a_total": a_recent + a_old, "a_recent": a_recent, "b_total": b_recent}


def _make_work_order(
    db: Session,
    *,
    company_id: int,
    lot_number: str = "LOT-1",
    is_deleted: bool = False,
) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        company_id=company_id,
        work_order_number=f"WO-{n:06d}",
        part_id=n,  # FK not enforced under the SQLite test engine
        quantity_ordered=10.0,
        status=WorkOrderStatus.RELEASED,
        lot_number=lot_number,
        created_at=datetime.utcnow(),
    )
    if is_deleted:
        wo.soft_delete(user_id=None)
    db.add(wo)
    return wo


def _seed_work_orders(db: Session) -> dict:
    _ensure_company(db, COMPANY_A)
    _ensure_company(db, COMPANY_B)

    # Company A: 3 live + 1 soft-deleted. Only the 3 live ones should count.
    a_live, a_deleted = 3, 1
    for i in range(a_live):
        _make_work_order(db, company_id=COMPANY_A, lot_number=f"A-LOT-{i}")
    _make_work_order(db, company_id=COMPANY_A, lot_number="A-LOT-DELETED", is_deleted=True)

    # Company B: 5 live work orders -- must never count toward A.
    b_live = 5
    for i in range(b_live):
        _make_work_order(db, company_id=COMPANY_B, lot_number=f"B-LOT-{i}")
    db.commit()

    return {"a_live": a_live, "a_deleted": a_deleted, "b_live": b_live}


# ---------------------------------------------------------------------------
# 1. Audit log evidence is scoped to the active company
# ---------------------------------------------------------------------------


def test_audit_helper_counts_only_own_company(db_session: Session):
    """``_query_audit_log`` for company A reports only A's audit rows."""
    seeded = _seed_audit_rows(db_session)

    result_a = _query_audit_log(db_session, COMPANY_A)
    assert result_a["total_count"] == seeded["a_count"]
    assert result_a["recent_count"] == seeded["a_count"]

    # Symmetric control: company B sees only its own rows.
    result_b = _query_audit_log(db_session, COMPANY_B)
    assert result_b["total_count"] == seeded["b_count"]

    # The two companies' counts are disjoint slices of the shared chain;
    # without the filter, both would report a_count + b_count.
    assert result_a["total_count"] + result_b["total_count"] == seeded["a_count"] + seeded["b_count"]


def test_audit_evidence_via_discover_excludes_other_company(db_session: Session):
    """The public entry point routes an 'audit trail' clause to the audit
    helper and still reports only company A's count."""
    seeded = _seed_audit_rows(db_session)
    clause = _make_clause(db_session, title="Audit trail and management review", description="internal audit program")

    results = discover_evidence_for_clause(db_session, clause, COMPANY_A)

    audit_results = [r for r in results if r["_rule_id"] == "audit"]
    assert len(audit_results) == 1, [r["_rule_id"] for r in results]
    assert audit_results[0]["total_count"] == seeded["a_count"]


# ---------------------------------------------------------------------------
# 2. NCR (standard domain model) evidence is scoped to the active company
# ---------------------------------------------------------------------------


def test_ncr_helper_counts_only_own_company(db_session: Session):
    """``_query_ncrs`` for company A counts only A's NCRs (total + recent)."""
    seeded = _seed_ncrs(db_session)

    result_a = _query_ncrs(db_session, COMPANY_A)
    assert result_a["total_count"] == seeded["a_total"]
    assert result_a["recent_count"] == seeded["a_recent"]

    # Company B's larger NCR population does not bleed into A.
    result_b = _query_ncrs(db_session, COMPANY_B)
    assert result_b["total_count"] == seeded["b_total"]


def test_ncr_evidence_via_discover_excludes_other_company(db_session: Session):
    """A 'control of nonconforming output' clause routes to the NCR helper and
    its examples/counts are company A only -- no B-company NCR identifiers."""
    seeded = _seed_ncrs(db_session)
    clause = _make_clause(
        db_session,
        title="Control of nonconforming output",
        description="handling of nonconforming product",
    )

    results = discover_evidence_for_clause(db_session, clause, COMPANY_A)
    ncr_results = [r for r in results if r["_rule_id"] == "ncr"]
    assert len(ncr_results) == 1, [r["_rule_id"] for r in results]

    ncr = ncr_results[0]
    assert ncr["total_count"] == seeded["a_total"]
    # Examples (max 5) are drawn from company A only: exactly a_total here.
    assert len(ncr["examples"]) == seeded["a_total"]


# ---------------------------------------------------------------------------
# 3. Work orders: scoped to the company AND excluding soft-deleted rows
# ---------------------------------------------------------------------------


def test_work_order_helper_excludes_other_company_and_soft_deleted(db_session: Session):
    """``_query_work_orders`` for company A counts only A's *live* work orders --
    excluding both company B's rows and A's own soft-deleted row."""
    seeded = _seed_work_orders(db_session)

    result_a = _query_work_orders(db_session, COMPANY_A)
    # 3 live A work orders: not 4 (would include the soft-deleted one) and not
    # 8/9 (would include company B's rows).
    assert result_a["total_count"] == seeded["a_live"]
    assert result_a["recent_count"] == seeded["a_live"]

    result_b = _query_work_orders(db_session, COMPANY_B)
    assert result_b["total_count"] == seeded["b_live"]


def test_work_order_evidence_via_discover_excludes_other_company_and_deleted(db_session: Session):
    """A 'traceability' clause routes to the work-order helper; its examples are
    company A's live work orders only -- no B-company lots, no deleted lot."""
    seeded = _seed_work_orders(db_session)
    clause = _make_clause(
        db_session,
        title="Identification and traceability",
        description="lot tracking and serial number control",
    )

    results = discover_evidence_for_clause(db_session, clause, COMPANY_A)
    wo_results = [r for r in results if r["_rule_id"] == "traceability"]
    assert len(wo_results) == 1, [r["_rule_id"] for r in results]

    wo = wo_results[0]
    assert wo["total_count"] == seeded["a_live"]

    example_lots = {e["summary"] for e in wo["examples"]}
    # No company-B lot and no soft-deleted lot leaks into A's examples.
    assert not any("B-LOT" in lot for lot in example_lots)
    assert not any("DELETED" in lot for lot in example_lots)
    assert len(wo["examples"]) == seeded["a_live"]
