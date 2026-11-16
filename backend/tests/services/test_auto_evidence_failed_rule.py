"""A failing auto-evidence rule is REPORTED as not assessed, never silently dropped.

Covers ``discover_evidence_for_clause`` in ``app.services.auto_evidence_service``.
Before this fix a rule whose ``query_fn`` raised was logged and its row dropped, so:

* a clause matching calibration + maintenance rolled up ``compliant`` from calibration
  alone while the maintenance query was failing (a false "compliant" in an
  auditor-facing AS9100D view), and
* on PostgreSQL the failed statement aborted the whole transaction, so every LATER
  rule failed (and was dropped) too.

Now each rule runs in its own savepoint; a raise rolls back to that savepoint and the
rule's row becomes the ``no_data`` / ``not_assessed`` placeholder from
``_evaluation_failed_result``. ``compute_overall_compliance`` then rolls such a clause up
``not_assessed`` -- unless a computed row says ``non_compliant`` or ``partial``, which still
wins -- never ``compliant`` and never ``partial`` from the failure alone.

How a rule is made to fail: ``EVIDENCE_MAPPING_RULES`` holds dicts carrying FUNCTION
REFERENCES, so patching the module attribute (``_query_maintenance``) would change
nothing. The tests ``monkeypatch.setitem`` the ``query_fn`` on the rule's own dict.
Every test also asserts which rules its clause text matches, so a keyword edit cannot
turn it vacuous.

The suite runs on SQLite (CLAUDE.md -> "Why the tests run on SQLite"). PostgreSQL's
aborted-transaction semantics cannot be executed here, so the proof offered for the
"later rules still run" half is (a) a raiser that issues a real failing SQL statement
and (b) a rollback-to-savepoint isolation test: the rule's own writes vanish while the
caller's earlier flushed write survives, which only a SAVEPOINT (not a full rollback,
and not no rollback at all) produces.
"""

import re
from datetime import datetime

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.calibration import CalibrationStatus, Equipment
from app.models.company import Company
from app.models.maintenance import MaintenanceAsset, MaintenanceSchedule
from app.models.qms_standard import QMSClause, QMSStandard
from app.models.quality import NCRSource, NCRStatus, NonConformanceReport
from app.schemas.qms_standard import AutoEvidenceResult
from app.services.auto_evidence_service import (
    EVALUATION_FAILED_DESCRIPTION,
    EVALUATION_FAILED_DETAIL,
    EVALUATION_FAILED_KEY,
    EVIDENCE_MAPPING_RULES,
    _evaluation_failed_result,
    _match_rules,
    compute_overall_compliance,
    discover_evidence_for_clause,
    is_evaluation_failed,
)

pytestmark = [pytest.mark.unit, pytest.mark.requires_db]

COMPANY_ID = 1
# A company with no domain rows at all, for the drift guard.
EMPTY_COMPANY_ID = 4242

INTERNAL_KEYS = {"_rule_id", EVALUATION_FAILED_KEY}

# Matches exactly [calibration, maintenance] -- asserted in every test that uses it.
#   "monitoring and measuring"  -> calibration
#   "equipment maintenance" / "infrastructure" -> maintenance
CAL_MAINT_TITLE = "Calibration and equipment maintenance"
CAL_MAINT_DESCRIPTION = "Monitoring and measuring resources; upkeep of infrastructure"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


class _RuleBoom(RuntimeError):
    """The deliberate failure a patched rule raises."""


def _rule(rule_id: str) -> dict:
    matches = [r for r in EVIDENCE_MAPPING_RULES if r["id"] == rule_id]
    assert len(matches) == 1, f"expected exactly one rule {rule_id!r}"
    return matches[0]


def _rule_index(rule_id: str) -> int:
    return [r["id"] for r in EVIDENCE_MAPPING_RULES].index(rule_id)


def _fail_rule(monkeypatch, rule_id: str, raiser=None) -> list:
    """Make ``rule_id``'s query raise. Returns a list that records each call's company id."""
    calls: list = []

    def _default_raiser(db, company_id):
        calls.append(company_id)
        raise _RuleBoom(f"{rule_id} query exploded")

    def _wrapped(db, company_id):
        calls.append(company_id)
        return raiser(db, company_id)

    monkeypatch.setitem(_rule(rule_id), "query_fn", _default_raiser if raiser is None else _wrapped)
    return calls


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def _make_clause(db: Session, *, title: str, description: str) -> QMSClause:
    _ensure_company(db, COMPANY_ID)
    n = _next()
    standard = QMSStandard(name=f"STD-FAIL-{n}", version="2016", is_active=True, company_id=COMPANY_ID)
    db.add(standard)
    db.flush()
    clause = QMSClause(
        standard_id=standard.id,
        company_id=COMPANY_ID,
        clause_number=f"7.1.{n}",
        title=title,
        description=description,
    )
    db.add(clause)
    db.commit()
    db.refresh(clause)
    return clause


def _cal_maint_clause(db: Session) -> QMSClause:
    clause = _make_clause(db, title=CAL_MAINT_TITLE, description=CAL_MAINT_DESCRIPTION)
    assert [r["id"] for r in _match_rules(clause)] == ["calibration", "maintenance"]
    return clause


def _seed_compliant_calibration(db: Session, *, count: int = 2) -> None:
    """Active, current equipment: ``_query_calibration`` answers ``compliant``."""
    for _ in range(count):
        n = _next()
        db.add(
            Equipment(
                company_id=COMPANY_ID,
                equipment_id=f"GAUGE-{n:04d}",
                name=f"Caliper {n}",
                equipment_type="caliper",
                status=CalibrationStatus.ACTIVE,
                is_active=True,
            )
        )
    db.commit()


def _seed_compliant_maintenance(db: Session) -> None:
    """One PM schedule, no overdue work orders: ``_query_maintenance`` answers ``compliant``."""
    n = _next()
    asset = MaintenanceAsset(company_id=COMPANY_ID, asset_tag=f"ASSET-{n:04d}", name=f"Air compressor {n}")
    db.add(asset)
    db.flush()
    db.add(MaintenanceSchedule(company_id=COMPANY_ID, asset_id=asset.id, name=f"Quarterly PM {n}"))
    db.commit()


def _by_rule(results: list) -> dict:
    by_id = {r["_rule_id"]: r for r in results}
    assert len(by_id) == len(results), "duplicate rule rows"
    return by_id


def _public_keys(row: dict) -> set:
    return set(row) - INTERNAL_KEYS


# ---------------------------------------------------------------------------
# 1. The placeholder row
# ---------------------------------------------------------------------------


def test_failed_rule_reported_as_not_assessed_placeholder(db_session: Session, monkeypatch):
    """The raising rule keeps a row: ``_rule_id``, flagged failed, no_data / not_assessed,
    and the same public key set as a computed row."""
    _seed_compliant_calibration(db_session)
    clause = _cal_maint_clause(db_session)
    calls = _fail_rule(monkeypatch, "maintenance")

    results = discover_evidence_for_clause(db_session, clause, COMPANY_ID)

    assert calls == [COMPANY_ID], "the patched maintenance query was not the one invoked"
    assert [r["_rule_id"] for r in results] == ["calibration", "maintenance"]
    rows = _by_rule(results)
    computed, placeholder = rows["calibration"], rows["maintenance"]

    rule = _rule("maintenance")
    assert is_evaluation_failed(placeholder) is True
    assert placeholder[EVALUATION_FAILED_KEY] is True
    assert placeholder["_rule_id"] == "maintenance"
    assert placeholder["evidence_type"] == rule["evidence_type"]
    assert placeholder["title"] == rule["title"]
    assert placeholder["module_reference"] == rule["module_reference"]
    assert placeholder["description"] == EVALUATION_FAILED_DESCRIPTION
    assert placeholder["health_status"] == "no_data"
    assert placeholder["health_detail"] == EVALUATION_FAILED_DETAIL
    assert placeholder["suggested_compliance"] == "not_assessed"
    assert placeholder["total_count"] == 0
    assert placeholder["recent_count"] == 0
    assert placeholder["examples"] == []

    # The computed row is NOT flagged, and both rows have the same public shape.
    assert is_evaluation_failed(computed) is False
    assert EVALUATION_FAILED_KEY not in computed
    assert _public_keys(placeholder) == _public_keys(computed)
    # ...and the placeholder satisfies the response schema the API serializes it through.
    AutoEvidenceResult.model_validate({k: v for k, v in placeholder.items() if k not in INTERNAL_KEYS})


def test_is_evaluation_failed_false_for_rows_without_the_flag():
    assert is_evaluation_failed({}) is False
    assert is_evaluation_failed({"suggested_compliance": "not_assessed"}) is False
    assert is_evaluation_failed({EVALUATION_FAILED_KEY: False}) is False


# ---------------------------------------------------------------------------
# 2. The roll-up can no longer say "compliant" over a failed rule
# ---------------------------------------------------------------------------


def test_failed_rule_prevents_compliant_rollup(db_session: Session, monkeypatch):
    """Calibration genuinely compliant + maintenance failing -> ``not_assessed``: neither
    ``compliant`` (evidence nobody computed) nor ``partial`` (a query failure presented as a
    compliance gap). The control run on the SAME data (no failure) is ``compliant``, so it is
    the failure -- nothing else -- that changes the verdict."""
    _seed_compliant_calibration(db_session)
    _seed_compliant_maintenance(db_session)
    clause = _cal_maint_clause(db_session)

    # Control: both rules compute and both are compliant.
    control = discover_evidence_for_clause(db_session, clause, COMPANY_ID)
    assert {r["_rule_id"]: r["suggested_compliance"] for r in control} == {
        "calibration": "compliant",
        "maintenance": "compliant",
    }
    assert not any(is_evaluation_failed(r) for r in control)
    assert compute_overall_compliance(control) == "compliant"

    _fail_rule(monkeypatch, "maintenance")
    results = discover_evidence_for_clause(db_session, clause, COMPANY_ID)
    rows = _by_rule(results)

    assert rows["calibration"]["suggested_compliance"] == "compliant"
    assert is_evaluation_failed(rows["maintenance"])
    overall = compute_overall_compliance(results)
    assert overall != "compliant"
    assert overall == "not_assessed"

    # The pre-fix behavior -- dropping the failed row -- is exactly what produced the false
    # "compliant"; this pins that the regression would be visible.
    dropped = [r for r in results if not is_evaluation_failed(r)]
    assert compute_overall_compliance(dropped) == "compliant"


def _computed_row(rule_id: str, suggested: str) -> dict:
    rule = _rule(rule_id)
    return {
        "evidence_type": rule["evidence_type"],
        "title": rule["title"],
        "description": "computed",
        "module_reference": rule["module_reference"],
        "total_count": 1,
        "recent_count": 1,
        "health_status": "healthy",
        "health_detail": "computed",
        "examples": [],
        "suggested_compliance": suggested,
        "_rule_id": rule_id,
    }


@pytest.mark.parametrize(
    ("computed_status", "expected"),
    [
        # A finding from a row that WAS computed still wins over the failure...
        ("non_compliant", "non_compliant"),
        ("partial", "partial"),
        # ...otherwise the failure makes the clause not_assessed, never compliant or partial.
        ("compliant", "not_assessed"),
        ("not_assessed", "not_assessed"),
    ],
)
def test_rollup_of_computed_row_plus_failed_rule(computed_status: str, expected: str):
    rows = [_computed_row("calibration", computed_status), _evaluation_failed_result(_rule("maintenance"))]
    assert compute_overall_compliance(rows) == expected
    # Order does not matter.
    assert compute_overall_compliance(list(reversed(rows))) == expected


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["compliant", "compliant"], "compliant"),
        (["compliant", "not_assessed"], "partial"),
        (["not_assessed", "not_assessed"], "not_assessed"),
        (["compliant", "partial"], "partial"),
        (["partial", "non_compliant"], "non_compliant"),
    ],
)
def test_rollup_without_failures_keeps_the_old_rules(statuses: list, expected: str):
    """Control: with no failed rule the roll-up is unchanged -- in particular a computed
    ``not_assessed`` next to a ``compliant`` row is still ``partial``, which is what makes the
    failed-rule ``not_assessed`` above a distinct rule rather than a coincidence."""
    rows = [_computed_row(rule_id, s) for rule_id, s in zip(["calibration", "maintenance"], statuses)]
    assert compute_overall_compliance(rows) == expected


def test_rollup_needs_the_internal_failure_flag():
    """The roll-up reads the internal flag: on the stripped (serialized) shape a placeholder is
    indistinguishable from a computed ``not_assessed`` row, so callers must roll up BEFORE
    stripping ``_``-prefixed keys (both endpoints do)."""
    placeholder = _evaluation_failed_result(_rule("maintenance"))
    rows = [_computed_row("calibration", "compliant"), placeholder]
    stripped = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]

    assert compute_overall_compliance(rows) == "not_assessed"
    assert compute_overall_compliance(stripped) == "partial"


def test_rollup_of_only_failed_rules_is_not_assessed():
    rows = [_evaluation_failed_result(_rule("calibration")), _evaluation_failed_result(_rule("maintenance"))]
    assert compute_overall_compliance(rows) == "not_assessed"


def test_clause_whose_only_rule_failed_is_not_assessed(db_session: Session, monkeypatch):
    clause = _make_clause(db_session, title="Preventive maintenance", description="infrastructure upkeep")
    assert [r["id"] for r in _match_rules(clause)] == ["maintenance"]
    _fail_rule(monkeypatch, "maintenance")

    results = discover_evidence_for_clause(db_session, clause, COMPANY_ID)

    assert len(results) == 1 and is_evaluation_failed(results[0])
    assert compute_overall_compliance(results) == "not_assessed"


# ---------------------------------------------------------------------------
# 3. Rules after the failing one still run
# ---------------------------------------------------------------------------


def _raise_python_error(db, company_id):
    raise _RuleBoom("calibration query exploded")


def _raise_real_sql_error(db, company_id):
    # A genuine failing statement through the same connection the later rules use.
    db.execute(text("SELECT * FROM no_such_table_xyz"))
    raise AssertionError("unreachable: the statement above must fail")


@pytest.mark.parametrize(
    "raiser",
    [_raise_python_error, _raise_real_sql_error],
    ids=["python-exception", "real-sql-failure"],
)
def test_rules_after_a_failed_rule_still_compute(db_session: Session, monkeypatch, raiser):
    """Calibration (earlier in ``EVIDENCE_MAPPING_RULES``) fails; maintenance (later) still
    returns its real, computed row from the database."""
    assert _rule_index("calibration") < _rule_index("maintenance")
    _seed_compliant_maintenance(db_session)
    clause = _cal_maint_clause(db_session)
    calls = _fail_rule(monkeypatch, "calibration", raiser)

    results = discover_evidence_for_clause(db_session, clause, COMPANY_ID)

    assert calls == [COMPANY_ID]
    assert [r["_rule_id"] for r in results] == ["calibration", "maintenance"]
    rows = _by_rule(results)
    assert is_evaluation_failed(rows["calibration"])

    maintenance = rows["maintenance"]
    assert not is_evaluation_failed(maintenance)
    assert maintenance["description"] != EVALUATION_FAILED_DESCRIPTION
    # Values that only a successful query against the seeded rows can produce.
    assert maintenance["suggested_compliance"] == "compliant"
    assert maintenance["health_status"] == "healthy"
    assert maintenance["recent_count"] == 1  # one PM schedule

    # The session is still usable for the caller afterwards.
    assert db_session.execute(text("SELECT 1")).scalar() == 1


def test_real_sql_failure_raiser_actually_fails(db_session: Session):
    """Guard for the parametrized case above: the statement really is a DB error, so that
    variant exercises a failed statement, not just a Python exception."""
    with pytest.raises(OperationalError):
        with db_session.begin_nested():
            _raise_real_sql_error(db_session, COMPANY_ID)


# ---------------------------------------------------------------------------
# 4. Rollback is to the rule's savepoint -- not a full rollback, not no rollback
# ---------------------------------------------------------------------------


def _ncr(number: str) -> NonConformanceReport:
    return NonConformanceReport(
        company_id=COMPANY_ID,
        ncr_number=number,
        title=f"Savepoint probe {number}",
        description="harmless probe row",
        source=NCRSource.IN_PROCESS,
        status=NCRStatus.CLOSED,
        created_at=datetime.utcnow(),
    )


def _ncr_exists(db: Session, number: str) -> bool:
    return (
        db.query(NonConformanceReport)
        .filter(NonConformanceReport.company_id == COMPANY_ID, NonConformanceReport.ncr_number == number)
        .count()
        == 1
    )


def test_failed_rule_writes_are_rolled_back_to_its_savepoint_only(db_session: Session, monkeypatch):
    """The failing rule's own flushed write disappears; the caller's write flushed BEFORE
    ``discover_evidence_for_clause`` survives (and commits). Only a SAVEPOINT gives both:
    no rollback would keep the rule's row, a full rollback would lose the caller's."""
    clause = _cal_maint_clause(db_session)
    caller_number = f"NCR-CALLER-{_next()}"
    rule_number = f"NCR-RULE-{_next()}"

    # Recorded rather than asserted inside the raiser: an AssertionError there would be
    # swallowed by the very except-clause under test.
    visible_inside_savepoint: list = []

    def _write_then_raise(db, company_id):
        db.add(_ncr(rule_number))
        db.flush()
        visible_inside_savepoint.append(_ncr_exists(db, rule_number))
        raise _RuleBoom("raised after writing")

    calls = _fail_rule(monkeypatch, "calibration", _write_then_raise)

    # The caller's pending write, flushed (not committed) before discovery.
    db_session.add(_ncr(caller_number))
    db_session.flush()

    results = discover_evidence_for_clause(db_session, clause, COMPANY_ID)

    assert calls == [COMPANY_ID]
    assert visible_inside_savepoint == [True], "the rule's write never reached the database"
    rows = _by_rule(results)
    assert is_evaluation_failed(rows["calibration"])
    assert not is_evaluation_failed(rows["maintenance"])  # the next rule still ran

    assert not _ncr_exists(db_session, rule_number)
    assert _ncr_exists(db_session, caller_number)

    # Committing the caller's transaction keeps its row and does not resurrect the rule's.
    db_session.commit()
    db_session.expire_all()
    assert _ncr_exists(db_session, caller_number)
    assert not _ncr_exists(db_session, rule_number)


_SAVEPOINT_SQL = re.compile(r"^\s*(SAVEPOINT|ROLLBACK TO SAVEPOINT|RELEASE SAVEPOINT)\s+(\S+)", re.IGNORECASE)
_PROBE_SQL = "SELECT 1 AS auto_evidence_probe"


def _probe_then_raise(db, company_id):
    db.execute(text(_PROBE_SQL))
    raise _RuleBoom("raised after a successful statement")


@pytest.mark.parametrize(
    ("raiser", "failing_body"),
    [
        (_probe_then_raise, [("sql", "probe")]),
        (_raise_real_sql_error, [("sql", "failing statement")]),
    ],
    ids=["statement-then-python-exception", "real-sql-failure"],
)
def test_savepoint_sql_wraps_each_rule(db_session: Session, monkeypatch, raiser, failing_body):
    """The SQL actually sent to the database: the failing rule's statements run between
    ``SAVEPOINT x`` and ``ROLLBACK TO SAVEPOINT x``; the succeeding rule after it runs between
    ``SAVEPOINT y`` and ``RELEASE SAVEPOINT y``. SQLite never aborts a transaction on a failed
    statement, so this trace is what pins the PostgreSQL-relevant mechanism here.

    Captured with the engine's ``before_cursor_execute`` event: SQLAlchemy emits savepoint
    SQL through ``Connection.execute``, so it reaches the cursor like any other statement.

    SQLAlchemy emits the ``SAVEPOINT`` LAZILY -- not at ``begin_nested()`` but immediately
    before the first statement executed inside it -- so each rule's marker precedes its
    ``SAVEPOINT``. Every statement a rule runs is still inside its savepoint.
    """
    clause = _cal_maint_clause(db_session)
    trace: list = []

    def _failing(db, company_id):
        trace.append(("rule", "calibration:start"))
        raiser(db, company_id)

    original_maintenance = _rule("maintenance")["query_fn"]

    def _succeeding(db, company_id):
        trace.append(("rule", "maintenance:start"))
        result = original_maintenance(db, company_id)
        trace.append(("rule", "maintenance:end"))
        return result

    monkeypatch.setitem(_rule("calibration"), "query_fn", _failing)
    monkeypatch.setitem(_rule("maintenance"), "query_fn", _succeeding)

    def _record(conn, cursor, statement, parameters, context, executemany):
        match = _SAVEPOINT_SQL.match(statement)
        if match:
            trace.append((match.group(1).upper(), match.group(2)))
        elif "no_such_table_xyz" in statement:
            trace.append(("sql", "failing statement"))
        elif _PROBE_SQL in statement:
            trace.append(("sql", "probe"))

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        results = discover_evidence_for_clause(db_session, clause, COMPANY_ID)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    rows = _by_rule(results)
    assert is_evaluation_failed(rows["calibration"])
    assert not is_evaluation_failed(rows["maintenance"])
    assert rows["maintenance"]["health_detail"] == "No maintenance schedules or records found"

    savepoint_names = [name for kind, name in trace if kind == "SAVEPOINT"]
    assert len(savepoint_names) == 2 and savepoint_names[0] != savepoint_names[1], trace
    failed_sp, ok_sp = savepoint_names

    assert trace == [
        ("rule", "calibration:start"),
        ("SAVEPOINT", failed_sp),
        *failing_body,
        ("ROLLBACK TO SAVEPOINT", failed_sp),
        ("rule", "maintenance:start"),
        ("SAVEPOINT", ok_sp),
        # (maintenance's own SELECTs run here; not recorded)
        ("rule", "maintenance:end"),
        ("RELEASE SAVEPOINT", ok_sp),
    ]


def test_rule_raising_before_any_statement_emits_no_savepoint_sql(db_session: Session, monkeypatch):
    """Documents the lazy emission the test above relies on: a rule that raises before running
    any statement leaves no savepoint SQL at all (nothing ran, so nothing needs rolling back),
    and the next rule still gets its own SAVEPOINT / RELEASE."""
    clause = _cal_maint_clause(db_session)
    calls = _fail_rule(monkeypatch, "calibration")
    trace: list = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        match = _SAVEPOINT_SQL.match(statement)
        if match:
            trace.append((match.group(1).upper(), match.group(2)))

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        results = discover_evidence_for_clause(db_session, clause, COMPANY_ID)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert calls == [COMPANY_ID]
    assert is_evaluation_failed(_by_rule(results)["calibration"])
    assert [kind for kind, _ in trace] == ["SAVEPOINT", "RELEASE SAVEPOINT"]
    assert trace[0][1] == trace[1][1]


def test_caller_flush_failure_propagates_instead_of_becoming_a_failed_rule(db_session: Session, monkeypatch):
    """``begin_nested()`` flushes the caller's pending writes before the rule runs. A failure
    there is the caller's, so it must propagate -- not be swallowed into a placeholder row."""
    clause = _cal_maint_clause(db_session)
    calls = _fail_rule(monkeypatch, "calibration")

    # NOT NULL ``name`` missing: the flush inside begin_nested() raises.
    db_session.add(Equipment(company_id=COMPANY_ID, equipment_id=f"BAD-{_next()}", name=None))

    with pytest.raises(IntegrityError):
        discover_evidence_for_clause(db_session, clause, COMPANY_ID)

    assert calls == [], "no rule should have run once the caller's flush failed"
    db_session.rollback()


# ---------------------------------------------------------------------------
# 5. Drift guard: each rule's placeholder metadata equals what its query returns
# ---------------------------------------------------------------------------


def test_rule_ids_are_unique():
    ids = [r["id"] for r in EVIDENCE_MAPPING_RULES]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("rule", EVIDENCE_MAPPING_RULES, ids=[r["id"] for r in EVIDENCE_MAPPING_RULES])
def test_rule_placeholder_metadata_matches_its_query(db_session: Session, rule: dict):
    """Every rule carries ``evidence_type`` / ``title`` / ``module_reference`` equal to what its
    ``query_fn`` returns, and its placeholder row has exactly the computed row's key set."""
    for key in ("evidence_type", "title", "module_reference"):
        assert isinstance(rule.get(key), str) and rule[key], f"rule {rule['id']!r} lacks {key!r}"

    _ensure_company(db_session, EMPTY_COMPANY_ID)
    computed = rule["query_fn"](db_session, EMPTY_COMPANY_ID)

    assert computed["evidence_type"] == rule["evidence_type"]
    assert computed["title"] == rule["title"]
    assert computed["module_reference"] == rule["module_reference"]

    placeholder = _evaluation_failed_result(rule)
    assert placeholder["_rule_id"] == rule["id"]
    assert _public_keys(placeholder) == set(computed)
