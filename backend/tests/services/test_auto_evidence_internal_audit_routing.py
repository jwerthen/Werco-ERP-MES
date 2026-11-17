"""Internal-audit / management-review clauses are not assessed, never "compliant" from the change log.

The auto-evidence ``audit`` rule (``_query_audit_log``, the ERP's own system change log) used
to match "internal audit", "audit program" and "management review" as well as "audit trail" /
"audit log". The change log has entries on any working system, so an AS9100D / ISO 9001
internal-audit (9.2) or management-review (9.3) clause rolled up ``compliant`` with no record
that an audit was run or a review held -- a false "compliant" in an auditor-facing view.

Those three phrases now route to the ``internal_audit_review`` rule, whose row says the ERP
records neither (``not_assessed`` / ``no_data``, zero counts, internal ``_not_tracked`` key).
The ``audit`` rule keeps only "audit trail" / "audit log". ``compute_overall_compliance`` is
unchanged, so the not-tracked row alone gives ``not_assessed`` and, beside compliant rows,
``partial`` -- never ``compliant``.

Three layers are pinned here, directly against the service (no HTTP):

1. Keyword routing on transient ``QMSClause`` objects (``_match_rules`` / ``matched_rule_ids``).
2. The not-tracked row's shape, and that a change-log row is NOT not-tracked.
3. The DB-backed roll-up -- the headline regression: with change-log rows present (seeded
   through ``AuditService``, never inserted directly), an "Internal audit" clause reads
   ``not_assessed`` and gets no ``audit`` row. Before the fix it read ``compliant``.

Clause text in this file is paraphrased test wording, not quoted standard text.

The HTTP side (GET auto-evidence, and auto-link persisting / retiring evidence) is in
``tests/api/test_auto_evidence_internal_audit_api.py``.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.qms_standard import QMSClause, QMSStandard
from app.models.quality import NCRSource, NCRStatus, NonConformanceReport
from app.models.user import User, UserRole
from app.schemas.qms_standard import AutoEvidenceResult
from app.services.audit_service import AuditService
from app.services.auto_evidence_service import (
    EVIDENCE_MAPPING_RULES,
    INTERNAL_AUDIT_REVIEW_TITLE,
    _match_rules,
    _query_audit_log,
    _query_internal_audit_review,
    compute_overall_compliance,
    discover_evidence_for_clause,
    is_not_tracked,
    matched_rule_ids,
)

pytestmark = [pytest.mark.unit, pytest.mark.requires_db]

COMPANY_A = 1

IAR = "internal_audit_review"
AUDIT = "audit"
AUDIT_LOG_TITLE = "Audit Trail (Immutable Hash-Chain Log)"

# Module-level counter so every fixture row gets a globally unique natural key, even across
# tests sharing a worker DB under -n auto.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _rule(rule_id: str) -> dict:
    rules = [r for r in EVIDENCE_MAPPING_RULES if r["id"] == rule_id]
    assert len(rules) == 1, f"expected exactly one {rule_id!r} rule, got {len(rules)}"
    return rules[0]


def _transient_clause(title: str, description: str = "") -> QMSClause:
    """A never-persisted clause: keyword matching reads only its title and description."""
    return QMSClause(clause_number="0.0", title=title, description=description)


def _ids(clause: QMSClause) -> set:
    """The matched rule ids, asserting ``_match_rules`` and ``matched_rule_ids`` agree."""
    via_match = {rule["id"] for rule in _match_rules(clause)}
    via_public = matched_rule_ids(clause)
    assert via_match == via_public
    return via_public


# ---------------------------------------------------------------------------
# 1. Keyword routing (pure, no database)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,description",
    [
        ("Internal audit", ""),
        ("Internal Audits", ""),  # case-insensitive, and the plural still contains the phrase
        ("INTERNAL AUDIT", ""),
        ("Management review", ""),
        ("Management review inputs", ""),
        ("Management Review Outputs", ""),
        ("Audit planning", "Keep an audit programme covering each department."),  # British spelling
        ("Audit planning", "Maintain the audit program for the year."),
        # Paraphrases a company quality manual uses instead of the standard's wording.
        ("Internal Quality Audits", ""),
        ("Audits", "The quality team conducts audits at planned intervals."),
        ("Audit", "Each department gets an audit at planned intervals."),
        ("QMS review", "Top management shall review the quality system twice a year."),
        ("QMS review", "A review by top management covers the system's results."),
    ],
)
def test_internal_audit_and_management_review_text_routes_to_not_tracked_rule(title, description):
    ids = _ids(_transient_clause(title, description))
    assert IAR in ids
    assert AUDIT not in ids, f"{title!r} / {description!r} must not reach the change-log rule"


@pytest.mark.parametrize("title", ["Internal audit", "Management review"])
def test_bare_internal_audit_and_management_review_titles_route_only_to_not_tracked_rule(title):
    """A bare 9.2 / 9.3 title reaches nothing but the not-tracked row, so it can only roll up
    ``not_assessed`` -- no other rule's evidence can lift it."""
    assert _ids(_transient_clause(title)) == {IAR}


@pytest.mark.parametrize(
    "title,description",
    [
        ("Audit trail", ""),
        ("audit log retention", ""),
        ("System records", "Changes are kept in an audit trail."),
        ("Electronic records", "The system audit log is reviewed monthly."),
    ],
)
def test_audit_trail_and_audit_log_text_routes_to_change_log_rule(title, description):
    ids = _ids(_transient_clause(title, description))
    assert AUDIT in ids
    assert IAR not in ids, f"{title!r} / {description!r} names the change log, not an internal audit"


def test_text_naming_both_reaches_both_rules():
    ids = _ids(_transient_clause("Audit trail and management review"))
    assert {AUDIT, IAR} <= ids


def test_quality_manual_internal_audit_with_paper_audit_log_reaches_not_tracked_rule():
    """A manual's "Internal Quality Audits" clause that mentions a (paper) audit log reaches the
    change-log rule through "audit log" -- and, since the paraphrase keywords, the not-tracked
    rule too, so its change-log row can never lift it to ``compliant`` on its own."""
    ids = _ids(
        _transient_clause(
            "Internal Quality Audits",
            "The Quality Manager keeps an audit log of scheduled and completed audits.",
        )
    )
    assert {AUDIT, IAR} <= ids


@pytest.mark.parametrize(
    "title,description",
    [
        ("Supplier quality audit", "Critical suppliers receive an on-site quality audit."),
        ("Supplier audits", "An audit plan and audit schedule are kept for each supplier."),
        ("Audit results", ""),
    ],
)
def test_supplier_and_generic_audit_text_does_not_reach_not_tracked_rule(title, description):
    """The paraphrase keywords are specific on purpose: supplier-audit wording must not get an
    internal-audit row."""
    assert IAR not in _ids(_transient_clause(title, description))


def test_audit_results_alone_reaches_neither_rule():
    """The bare word "audit" belongs to no rule: "Audit results" is neither the change log nor
    an internal-audit / management-review clause."""
    ids = _ids(_transient_clause("Audit results"))
    assert AUDIT not in ids
    assert IAR not in ids
    assert ids == set()


def test_rule_keywords_are_split_as_decided():
    assert {kw.lower() for kw in _rule(AUDIT)["keywords"]} == {"audit trail", "audit log"}
    assert {kw.lower() for kw in _rule(IAR)["keywords"]} == {
        "internal audit",
        "audit program",
        "management review",
        "internal quality audit",
        "audits at planned intervals",
        "audit at planned intervals",
        "top management shall review",
        "review by top management",
    }
    assert _rule(IAR)["query_fn"] is _query_internal_audit_review
    assert _rule(AUDIT)["query_fn"] is _query_audit_log


def test_no_other_rule_carries_the_internal_audit_review_keywords():
    """Only ``internal_audit_review`` may own these phrases. A second rule carrying one -- or a
    keyword contained in one, which would fire on every such clause -- would put real evidence
    beside the not-tracked row and lift a bare 9.2 / 9.3 clause off ``not_assessed``."""
    own = {kw.lower() for kw in _rule(IAR)["keywords"]}
    for rule in EVIDENCE_MAPPING_RULES:
        if rule["id"] == IAR:
            continue
        theirs = {kw.lower() for kw in rule["keywords"]}
        assert not (own & theirs), f"rule {rule['id']!r} also carries {sorted(own & theirs)}"
        for kw in theirs:
            for phrase in own:
                assert kw not in phrase, f"rule {rule['id']!r} keyword {kw!r} fires on every {phrase!r} clause"


# ---------------------------------------------------------------------------
# 2. The not-tracked row's shape
# ---------------------------------------------------------------------------


def test_internal_audit_review_row_is_not_assessed_no_data_and_not_tracked():
    # The rule runs no query; ``db`` is unused, which ``None`` proves.
    row = _query_internal_audit_review(None, COMPANY_A)

    assert row["title"] == INTERNAL_AUDIT_REVIEW_TITLE
    assert row["suggested_compliance"] == "not_assessed"
    assert row["health_status"] == "no_data"
    assert row["total_count"] == 0
    assert row["recent_count"] == 0
    assert row["examples"] == []
    assert is_not_tracked(row) is True
    # The text claims only what auto-evidence can see: it cannot assess the clause. It must not
    # say the ERP holds nothing about audits -- a person may have linked an audit report.
    assert "Not Assessed Automatically" in row["title"]
    assert "cannot assess" in row["description"]
    for false_claim in ("nothing in the ERP", "does not record", "Not Recorded"):
        assert false_claim not in row["title"] + row["description"] + row["health_detail"]

    # It serializes as an ordinary result once the internal keys are stripped.
    public = AutoEvidenceResult(**{k: v for k, v in row.items() if not k.startswith("_")})
    assert public.suggested_compliance == "not_assessed"

    # Alone it rolls up not_assessed; beside a compliant row, partial -- never compliant.
    assert compute_overall_compliance([row]) == "not_assessed"
    assert compute_overall_compliance([row, {"suggested_compliance": "compliant"}]) == "partial"


def test_change_log_row_is_not_not_tracked(db_session: Session):
    _seed_change_log(db_session, company_id=COMPANY_A, count=2)
    row = _query_audit_log(db_session, COMPANY_A)
    assert row["total_count"] >= 2
    assert is_not_tracked(row) is False
    assert is_not_tracked({}) is False


# ---------------------------------------------------------------------------
# 3. DB-backed roll-up
# ---------------------------------------------------------------------------


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def _make_user(db: Session, *, company_id: int) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"iar-routing-{n}@co{company_id}.test",
        employee_id=f"IARR-{n:05d}",
        first_name="Routing",
        last_name=f"C{company_id}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=UserRole.QUALITY,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_change_log(db: Session, *, company_id: int, count: int) -> None:
    """Write ``count`` change-log rows through ``AuditService`` (the only valid path: the
    table carries a hash chain and NOT-NULL ``sequence_number`` / ``integrity_hash``)."""
    writer = _make_user(db, company_id=company_id)
    svc = AuditService(db, writer)
    assert svc.company_id == company_id
    for _ in range(count):
        row = svc.log(action="CREATE", resource_type="part", resource_id=_next(), resource_identifier="IAR-PART")
        assert row is not None and row.company_id == company_id
    db.commit()


def _make_clause(db: Session, *, clause_number: str, title: str, description: str = "") -> QMSClause:
    _ensure_company(db, COMPANY_A)
    standard = QMSStandard(name=f"STD-IAR-{_next()}", version="2016", is_active=True, company_id=COMPANY_A)
    db.add(standard)
    db.flush()
    clause = QMSClause(
        standard_id=standard.id,
        company_id=COMPANY_A,
        clause_number=clause_number,
        title=title,
        description=description,
    )
    db.add(clause)
    db.commit()
    db.refresh(clause)
    return clause


def _seed_closed_ncrs(db: Session, *, count: int) -> None:
    """Closed NCRs: ``_query_ncrs`` reads these healthy / compliant (some on record, <= 5 open)."""
    _ensure_company(db, COMPANY_A)
    for _ in range(count):
        n = _next()
        db.add(
            NonConformanceReport(
                company_id=COMPANY_A,
                ncr_number=f"NCR-IAR-{n:06d}",
                title=f"NCR {n}",
                description="dimension out of tolerance",
                source=NCRSource.IN_PROCESS,
                status=NCRStatus.CLOSED,
                created_at=datetime.utcnow() - timedelta(days=3),
            )
        )
    db.commit()


def test_internal_audit_clause_rolls_up_not_assessed_despite_change_log(db_session: Session):
    """The headline regression. The company's change log is populated, so the change-log rule
    on its own would read compliant -- and before the fix an "Internal audit" clause routed
    to it and rolled up ``compliant``. Now the clause gets only the not-tracked row."""
    _seed_change_log(db_session, company_id=COMPANY_A, count=3)
    change_log = _query_audit_log(db_session, COMPANY_A)
    assert change_log["suggested_compliance"] == "compliant", "precondition: the change log reads compliant"

    clause = _make_clause(db_session, clause_number="9.2", title="Internal audit")
    results = discover_evidence_for_clause(db_session, clause, COMPANY_A)

    assert [r["_rule_id"] for r in results] == [IAR]
    assert not any(r["title"] == AUDIT_LOG_TITLE for r in results)
    assert compute_overall_compliance(results) == "not_assessed"


def test_internal_audit_clause_with_programme_description_is_not_assessed(db_session: Session):
    _seed_change_log(db_session, company_id=COMPANY_A, count=2)
    clause = _make_clause(
        db_session,
        clause_number="9.2",
        title="Internal audit",
        description="Self-inspections run on a schedule set by the audit programme.",
    )
    results = discover_evidence_for_clause(db_session, clause, COMPANY_A)

    assert AUDIT not in {r["_rule_id"] for r in results}
    assert compute_overall_compliance(results) == "not_assessed"


def test_management_review_clause_with_compliant_evidence_rolls_up_partial(db_session: Session):
    """A 9.3 clause whose text also reaches a rule with real, compliant evidence (closed NCRs)
    rolls up ``partial``: the not-tracked row keeps it off ``compliant``."""
    _seed_change_log(db_session, company_id=COMPANY_A, count=2)
    _seed_closed_ncrs(db_session, count=2)
    clause = _make_clause(
        db_session,
        clause_number="9.3",
        title="Management review",
        description="Review inputs include trends in nonconformities.",
    )

    results = discover_evidence_for_clause(db_session, clause, COMPANY_A)
    by_rule = {r["_rule_id"]: r for r in results}

    assert set(by_rule) == {IAR, "ncr"}
    assert by_rule["ncr"]["suggested_compliance"] == "compliant"
    assert by_rule["ncr"]["total_count"] == 2
    assert is_not_tracked(by_rule[IAR]) is True

    overall = compute_overall_compliance(results)
    assert overall == "partial"
    assert overall != "compliant"
    # Control: without the not-tracked row the same evidence would read compliant, so it is
    # that row -- not the NCR evidence -- holding the clause at partial.
    assert compute_overall_compliance([r for r in results if not is_not_tracked(r)]) == "compliant"


def test_clause_naming_audit_trail_and_management_review_rolls_up_partial(db_session: Session):
    """The change-log row is real evidence for the audit-trail half, but the management-review
    half is not recorded, so the clause is partial rather than compliant."""
    _seed_change_log(db_session, company_id=COMPANY_A, count=2)
    clause = _make_clause(db_session, clause_number="9.9", title="Audit trail and management review")

    results = discover_evidence_for_clause(db_session, clause, COMPANY_A)
    by_rule = {r["_rule_id"]: r for r in results}

    assert set(by_rule) == {AUDIT, IAR}
    assert by_rule[AUDIT]["suggested_compliance"] == "compliant"
    assert compute_overall_compliance(results) == "partial"
