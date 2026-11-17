"""API coverage: a failing auto-evidence rule over HTTP.

Companion to ``tests/services/test_auto_evidence_failed_rule.py``. Two routes:

* ``GET /api/v1/qms-standards/clauses/{clause_id}/auto-evidence`` -- the failed rule is
  listed as its ``no_data`` / ``not_assessed`` placeholder row, with every internal
  ``_``-prefixed key stripped, and the clause no longer rolls up ``compliant``.
* ``POST /api/v1/qms-standards/{standard_id}/auto-link`` -- the failed rule counts in the
  roll-up and in ``total_evidence_failed`` but persists NOTHING: an existing auto-linked
  ``QMSClauseEvidence`` row for that rule keeps the counts and ``last_refreshed`` of the
  last run that computed them, no row is created for it, and a clause whose only rule
  failed is a clause WITHOUT evidence. The audit row says how many were not computed, and its
  ``extra_data["evidence_not_computed"]`` names each (clause, rule) pair.

A clause with a failed rule rolls up ``not_assessed`` unless a computed row says
``non_compliant`` or ``partial`` (``compute_overall_compliance``).

Each "left unchanged" assertion has a control run on identical data where the rule
computes and the same row IS rewritten -- otherwise an evidence lookup that simply never
matched would pass the test too.

Failure is injected with ``monkeypatch.setitem`` on the rule's dict in
``EVIDENCE_MAPPING_RULES`` (the list holds function references; patching the module's
``_query_maintenance`` attribute would do nothing).
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.calibration import CalibrationStatus, Equipment
from app.models.company import Company
from app.models.maintenance import MaintenanceAsset, MaintenanceSchedule
from app.models.qms_standard import QMSClause, QMSClauseEvidence, QMSStandard
from app.models.user import User, UserRole
from app.services.auto_evidence_service import (
    EVALUATION_FAILED_DESCRIPTION,
    EVALUATION_FAILED_DETAIL,
    EVIDENCE_MAPPING_RULES,
    _match_rules,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_ID = 1
SEEDED_EQUIPMENT = 3

STALE_LAST_REFRESHED = datetime(2026, 1, 2, 3, 4, 5)
STALE_UPDATED_AT = datetime(2026, 1, 2, 3, 4, 6)

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


class _RuleBoom(RuntimeError):
    pass


def _fail_rule(monkeypatch, rule_id: str) -> list:
    rule = next(r for r in EVIDENCE_MAPPING_RULES if r["id"] == rule_id)
    calls: list = []

    def _raiser(db, company_id):
        calls.append(company_id)
        raise _RuleBoom(f"{rule_id} query exploded")

    monkeypatch.setitem(rule, "query_fn", _raiser)
    return calls


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def _make_user(db: Session, *, role: UserRole = UserRole.QUALITY) -> User:
    _ensure_company(db, COMPANY_ID)
    n = _next()
    user = User(
        email=f"evidence-fail-{n}@co{COMPANY_ID}.test",
        employee_id=f"EVDF-{n:05d}",
        first_name="Evidence",
        last_name="Fail",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",  # tokens are minted directly
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=COMPANY_ID,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _make_standard(db: Session) -> QMSStandard:
    _ensure_company(db, COMPANY_ID)
    standard = QMSStandard(
        company_id=COMPANY_ID, name=f"AS9100D-FAIL-{_next()}", version="2016", standard_body="SAE", is_active=True
    )
    db.add(standard)
    db.commit()
    db.refresh(standard)
    return standard


def _make_clause(db: Session, standard: QMSStandard, *, title: str, description: str, expect_rules: list) -> QMSClause:
    n = _next()
    clause = QMSClause(
        company_id=COMPANY_ID,
        standard_id=standard.id,
        clause_number=f"7.1.{n}",
        title=title,
        description=description,
    )
    db.add(clause)
    db.commit()
    db.refresh(clause)
    # Pin the keyword routing so a keyword-list edit cannot make the test vacuous.
    assert [r["id"] for r in _match_rules(clause)] == expect_rules
    return clause


def _cal_maint_clause(db: Session, standard: QMSStandard) -> QMSClause:
    return _make_clause(
        db,
        standard,
        title="Calibration and equipment maintenance",
        description="Monitoring and measuring resources; upkeep of infrastructure",
        expect_rules=["calibration", "maintenance"],
    )


def _maint_only_clause(db: Session, standard: QMSStandard) -> QMSClause:
    return _make_clause(
        db,
        standard,
        title="Preventive maintenance",
        description="Upkeep of infrastructure",
        expect_rules=["maintenance"],
    )


def _cal_only_clause(db: Session, standard: QMSStandard) -> QMSClause:
    return _make_clause(
        db,
        standard,
        title="Control of monitoring and measuring resources",
        description="Gauges are verified against traceable standards",
        expect_rules=["calibration"],
    )


def _seed_compliant_calibration(db: Session) -> None:
    for _ in range(SEEDED_EQUIPMENT):
        n = _next()
        db.add(
            Equipment(
                company_id=COMPANY_ID,
                equipment_id=f"GAUGE-API-{n:04d}",
                name=f"Micrometer {n}",
                equipment_type="micrometer",
                status=CalibrationStatus.ACTIVE,
                is_active=True,
            )
        )
    db.commit()


def _seed_compliant_maintenance(db: Session) -> None:
    """One active PM schedule, performed and not past due, and no overdue work orders:
    ``_query_maintenance`` answers ``compliant`` (with ``total_count`` -- maintenance WOs -- of 0).
    A never-performed schedule would be ``not_assessed``."""
    n = _next()
    asset = MaintenanceAsset(company_id=COMPANY_ID, asset_tag=f"ASSET-API-{n:04d}", name=f"Air compressor {n}")
    db.add(asset)
    db.flush()
    db.add(
        MaintenanceSchedule(
            company_id=COMPANY_ID,
            asset_id=asset.id,
            name=f"Quarterly PM {n}",
            is_active=True,
            last_completed_date=date.today() - timedelta(days=30),
            next_due_date=date.today() + timedelta(days=60),
        )
    )
    db.commit()


def _stale_auto_evidence(db: Session, clause: QMSClause, *, rule_id: str, live_count: int) -> QMSClauseEvidence:
    """An auto-linked row written by an EARLIER run, with distinctive stale values."""
    evidence = QMSClauseEvidence(
        company_id=COMPANY_ID,
        clause_id=clause.id,
        evidence_type="module",
        title=f"Stale {rule_id} title",
        description=f"Stale {rule_id} description from the last good run",
        module_reference=f"/stale-{rule_id}",
        record_type="module",
        is_auto_linked=True,
        auto_link_query=rule_id,
        live_count=live_count,
        last_refreshed=STALE_LAST_REFRESHED,
        updated_at=STALE_UPDATED_AT,
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return evidence


def _snapshot(evidence: QMSClauseEvidence) -> dict:
    return {
        "title": evidence.title,
        "description": evidence.description,
        "module_reference": evidence.module_reference,
        "record_type": evidence.record_type,
        "evidence_type": evidence.evidence_type,
        "live_count": evidence.live_count,
        "last_refreshed": evidence.last_refreshed,
        "updated_at": evidence.updated_at,
        "is_auto_linked": evidence.is_auto_linked,
        "auto_link_query": evidence.auto_link_query,
        "is_deleted": evidence.is_deleted,
    }


def _clause_evidence(db: Session, clause: QMSClause) -> list:
    return (
        db.query(QMSClauseEvidence)
        .filter(QMSClauseEvidence.clause_id == clause.id, QMSClauseEvidence.company_id == COMPANY_ID)
        .order_by(QMSClauseEvidence.id)
        .all()
    )


def _auto_link_audit_rows(db: Session, standard: QMSStandard) -> list:
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_ID,
            AuditLog.resource_type == "qms_standard",
            AuditLog.resource_id == standard.id,
            AuditLog.description.like("Auto-linked evidence:%"),
        )
        .all()
    )


# ---------------------------------------------------------------------------
# GET /clauses/{id}/auto-evidence
# ---------------------------------------------------------------------------


def test_get_auto_evidence_lists_failed_rule_and_withholds_compliant(
    client: TestClient, db_session: Session, monkeypatch
):
    _seed_compliant_calibration(db_session)
    _seed_compliant_maintenance(db_session)
    standard = _make_standard(db_session)
    clause = _cal_maint_clause(db_session, standard)
    user = _make_user(db_session)
    url = f"/api/v1/qms-standards/clauses/{clause.id}/auto-evidence"

    # Control on the same data: both rules compute and the clause is compliant.
    control = client.get(url, headers=_headers_for(user))
    assert control.status_code == status.HTTP_200_OK, control.text
    assert control.json()["overall_suggested_compliance"] == "compliant"
    assert [r["suggested_compliance"] for r in control.json()["discovered_evidence"]] == ["compliant", "compliant"]

    calls = _fail_rule(monkeypatch, "maintenance")
    resp = client.get(url, headers=_headers_for(user))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert calls == [COMPANY_ID]
    body = resp.json()
    rows = body["discovered_evidence"]
    assert [r["module_reference"] for r in rows] == ["/calibration", "/maintenance"]

    for row in rows:
        assert not [k for k in row if k.startswith("_")], row
    computed, placeholder = rows
    assert set(placeholder) == set(computed)

    assert computed["suggested_compliance"] == "compliant"
    assert computed["total_count"] == SEEDED_EQUIPMENT

    assert placeholder["evidence_type"] == "module"
    assert placeholder["title"] == "Preventive Maintenance & Infrastructure"
    assert placeholder["description"] == EVALUATION_FAILED_DESCRIPTION
    assert placeholder["health_status"] == "no_data"
    assert placeholder["health_detail"] == EVALUATION_FAILED_DETAIL
    assert placeholder["suggested_compliance"] == "not_assessed"
    assert placeholder["total_count"] == 0
    assert placeholder["recent_count"] == 0
    assert placeholder["examples"] == []

    # Calibration's computed row is compliant, so the failure decides: not_assessed --
    # neither compliant (evidence nobody computed) nor partial (a failure posing as a gap).
    assert body["overall_suggested_compliance"] != "compliant"
    assert body["overall_suggested_compliance"] == "not_assessed"


# ---------------------------------------------------------------------------
# POST /{standard_id}/auto-link
# ---------------------------------------------------------------------------


def _auto_link_fixture(db: Session) -> dict:
    """Three clauses:

    * ``mixed``    -- calibration + maintenance, with a stale auto-linked maintenance row;
    * ``maint``    -- maintenance only, no evidence yet;
    * ``cal``      -- calibration only, with a stale auto-linked calibration row.

    Calibration and maintenance data are both seeded compliant, so with no failure every
    clause rolls up ``compliant`` (the control below).
    """
    _seed_compliant_calibration(db)
    _seed_compliant_maintenance(db)
    standard = _make_standard(db)
    mixed = _cal_maint_clause(db, standard)
    maint = _maint_only_clause(db, standard)
    cal = _cal_only_clause(db, standard)
    stale_maint = _stale_auto_evidence(db, mixed, rule_id="maintenance", live_count=7)
    stale_cal = _stale_auto_evidence(db, cal, rule_id="calibration", live_count=99)
    user = _make_user(db)
    return {
        "standard": standard,
        "mixed": mixed,
        "maint": maint,
        "cal": cal,
        "stale_maint": stale_maint,
        "stale_maint_before": _snapshot(stale_maint),
        "stale_cal": stale_cal,
        "user": user,
    }


def test_auto_link_with_failing_rule_persists_nothing_for_it(client: TestClient, db_session: Session, monkeypatch):
    fx = _auto_link_fixture(db_session)
    standard, user = fx["standard"], fx["user"]

    calls = _fail_rule(monkeypatch, "maintenance")
    resp = client.post(f"/api/v1/qms-standards/{standard.id}/auto-link", headers=_headers_for(user))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert calls == [COMPANY_ID, COMPANY_ID], "maintenance should have been attempted on both its clauses"
    summary = resp.json()

    # (d) one failed evaluation per (clause, failing rule): mixed + maint.
    assert summary["total_evidence_failed"] == 2
    # (c) the working rule persisted as before: created on ``mixed``, updated on ``cal``.
    assert summary["total_evidence_created"] == 1
    assert summary["total_evidence_updated"] == 1
    # (f) ``maint`` -- whose only rule failed -- is a clause WITHOUT evidence.
    assert summary["total_clauses"] == 3
    assert summary["clauses_with_evidence"] == 2
    assert summary["clauses_without_evidence"] == 1
    # (e) ``mixed`` (compliant calibration + failed maintenance) and ``maint`` (failed only)
    # are both not_assessed; only ``cal`` is compliant.
    assert summary["compliance_summary"] == {"not_assessed": 2, "compliant": 1}

    db_session.expire_all()

    # (a) the stale maintenance row on ``mixed`` is left exactly as the last good run wrote it.
    mixed_rows = _clause_evidence(db_session, fx["mixed"])
    maint_rows_on_mixed = [r for r in mixed_rows if r.auto_link_query == "maintenance"]
    assert len(maint_rows_on_mixed) == 1
    assert maint_rows_on_mixed[0].id == fx["stale_maint"].id
    assert _snapshot(maint_rows_on_mixed[0]) == fx["stale_maint_before"]

    # (c) ...while the calibration row on ``mixed`` was created from the live query.
    cal_rows_on_mixed = [r for r in mixed_rows if r.auto_link_query == "calibration"]
    assert len(cal_rows_on_mixed) == 1
    created = cal_rows_on_mixed[0]
    assert created.is_auto_linked is True
    assert created.evidence_type == "calibration"
    assert created.title == "Calibration & Measurement Equipment"
    assert created.module_reference == "/calibration"
    assert created.live_count == SEEDED_EQUIPMENT
    assert created.last_refreshed is not None and created.last_refreshed > STALE_LAST_REFRESHED
    assert created.created_by == user.id
    assert len(mixed_rows) == 2

    # (b) nothing was created for the failed rule on the clause that had no evidence.
    assert _clause_evidence(db_session, fx["maint"]) == []

    # (c) and the existing calibration row on ``cal`` was refreshed as before.
    cal_rows = _clause_evidence(db_session, fx["cal"])
    assert len(cal_rows) == 1 and cal_rows[0].id == fx["stale_cal"].id
    assert cal_rows[0].live_count == SEEDED_EQUIPMENT
    assert cal_rows[0].title == "Calibration & Measurement Equipment"
    assert cal_rows[0].last_refreshed > STALE_LAST_REFRESHED

    # (g) the audit row records what was not computed.
    audit_rows = _auto_link_audit_rows(db_session, standard)
    assert len(audit_rows) == 1
    assert audit_rows[0].description == ("Auto-linked evidence: 1 created, 1 updated, 2 not computed (left unchanged)")
    assert "not computed" in audit_rows[0].description
    # ...and which (clause, rule) pairs those were.
    extra = audit_rows[0].extra_data
    assert isinstance(extra, dict), extra
    not_computed = sorted(extra["evidence_not_computed"], key=lambda e: e["clause_id"])
    expected = sorted(
        [
            {"clause_id": fx["mixed"].id, "clause_number": fx["mixed"].clause_number, "rule_id": "maintenance"},
            {"clause_id": fx["maint"].id, "clause_number": fx["maint"].clause_number, "rule_id": "maintenance"},
        ],
        key=lambda e: e["clause_id"],
    )
    assert not_computed == expected
    assert len(extra["evidence_not_computed"]) == summary["total_evidence_failed"]


def test_auto_link_clean_run_reports_zero_failed_and_refreshes_everything(client: TestClient, db_session: Session):
    """Control on identical data with no failure: every rule computes, so the stale
    maintenance row IS rewritten and a maintenance row IS created on ``maint`` -- which is
    what makes the "left unchanged" / "nothing created" assertions above meaningful."""
    fx = _auto_link_fixture(db_session)
    standard, user = fx["standard"], fx["user"]

    resp = client.post(f"/api/v1/qms-standards/{standard.id}/auto-link", headers=_headers_for(user))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    summary = resp.json()
    assert summary["total_evidence_failed"] == 0
    # created: calibration on mixed, maintenance on maint; updated: maintenance on mixed, calibration on cal.
    assert summary["total_evidence_created"] == 2
    assert summary["total_evidence_updated"] == 2
    assert summary["clauses_with_evidence"] == 3
    assert summary["clauses_without_evidence"] == 0
    # With nothing failing, all three clauses are compliant -- so the partial / not_assessed
    # in the failing run above is the failure's doing.
    assert summary["compliance_summary"] == {"compliant": 3}

    db_session.expire_all()

    refreshed = db_session.get(QMSClauseEvidence, fx["stale_maint"].id)
    assert _snapshot(refreshed) != fx["stale_maint_before"]
    assert refreshed.title == "Preventive Maintenance & Infrastructure"
    assert refreshed.module_reference == "/maintenance"
    assert refreshed.live_count == 0  # maintenance total_count is maintenance WOs: none seeded
    assert refreshed.last_refreshed > STALE_LAST_REFRESHED

    maint_rows = _clause_evidence(db_session, fx["maint"])
    assert [(r.auto_link_query, r.is_auto_linked) for r in maint_rows] == [("maintenance", True)]

    audit_rows = _auto_link_audit_rows(db_session, standard)
    assert len(audit_rows) == 1
    assert audit_rows[0].description == "Auto-linked evidence: 2 created, 2 updated"
    assert "not computed" not in audit_rows[0].description
    # The key is absent -- not an empty list -- when nothing failed.
    assert "evidence_not_computed" not in (audit_rows[0].extra_data or {})
