"""Laser-nest dispatch import mechanics: laser auto-detect preference, standalone
``due_date``, per-row work-center overrides, and the operation work-center
reassignment endpoint.

Covers:
  - ``_find_laser_work_center`` auto-detect preference: Ermaksan/fiber beats a
    plain laser beats a tube laser (id is the tiebreak within a tier); a tube
    laser is chosen only when it is the ONLY laser; an explicit id always wins;
    unknown / inactive / cross-tenant explicit ids are 404.
  - Standalone import ``due_date``: stamped on the created WO (past dates
    allowed -- an open WO can already be overdue at import); absent -> NULL.
  - Per-row ``work_center_id`` on PDF confirm-and-commit rows: each op lands on
    ITS row's work center with ``operation_group`` derived from that work
    center; a bad row override 404s BEFORE anything commits; the
    import-replaces-everything wipe still removes nest ops whose group is not
    LASER (the widened, id-based wipe).
  - ``PUT /work-orders/operations/{id}`` work-center reassignment: happy path
    (WC + operation_group change, audited old->new), 409 while IN_PROGRESS,
    409 with an open TimeEntry despite an idle status, 404 inactive target,
    404 cross-tenant target, and the no-op cases (same WC / explicit null).

Offline by contract: CNC-file packages and the PDF confirm-and-commit path
only; the AI extractor is patched to fail the test if ever invoked.
"""

import io
import json
import zipfile
from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

import app.api.endpoints.work_orders as work_orders_endpoint
from app.api.endpoints.work_orders import _find_laser_work_center
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"dimport-{n}@co{company_id}.test",
        employee_id=f"DIMP-{n:05d}",
        first_name="DispatchImport",
        last_name=f"Co{company_id}",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_work_center(
    db: Session,
    *,
    name: str,
    wc_type: str = "laser",
    code: str = None,
    company_id: int = COMPANY_A,
    is_active: bool = True,
) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=name,
        code=code or f"WC-DI-{n}",
        work_center_type=wc_type,
        description="fixture",
        hourly_rate=120,
        is_active=is_active,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def _cnc_zip(*names: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, "M30")
    return buf.getvalue()


def _pdf_zip(*names: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, b"%PDF-1.4\n%stub nest report\n")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def upload_dir(tmp_path, monkeypatch):
    """Keep storage + laser package roots hermetic (same as the PDF-import tests)."""
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch):
    """Every path exercised here is AI-free; any extractor call is a bug."""
    monkeypatch.setattr(
        work_orders_endpoint,
        "extract_nest_fields_from_pdf",
        lambda *a, **k: pytest.fail("dispatch-import laser-nest tests must not call the AI extractor"),
    )


def _standalone_import(client, headers, zip_bytes, *, rows=None, work_center_id=None, due_date=None, name="nests.zip"):
    data = {}
    if rows is not None:
        data["rows"] = json.dumps(rows)
    if work_center_id is not None:
        data["work_center_id"] = str(work_center_id)
    if due_date is not None:
        data["due_date"] = due_date
    return client.post(
        "/api/v1/work-orders/laser-nest-packages/standalone/import",
        headers=headers,
        data=data,
        files={"file": (name, io.BytesIO(zip_bytes), "application/zip")},
    )


def _wo_import(client, headers, wo_id, zip_bytes, *, rows=None, work_center_id=None, name="nests.zip"):
    data = {}
    if rows is not None:
        data["rows"] = json.dumps(rows)
    if work_center_id is not None:
        data["work_center_id"] = str(work_center_id)
    return client.post(
        f"/api/v1/work-orders/{wo_id}/laser-nest-packages/import",
        headers=headers,
        data=data,
        files={"file": (name, io.BytesIO(zip_bytes), "application/zip")},
    )


def _assert_nothing_persisted(db: Session) -> None:
    """A failed import must leave no WO / package / nest / operation behind."""
    assert db.query(WorkOrder).count() == 0
    assert db.query(LaserNestPackage).count() == 0
    assert db.query(LaserNest).count() == 0
    assert db.query(WorkOrderOperation).count() == 0


# --------------------------------------------------------------------------- #
# _find_laser_work_center auto-detect preference matrix
# --------------------------------------------------------------------------- #
class TestFindLaserWorkCenterPreference:
    def test_ermaksan_beats_plain_laser_beats_tube(self, db_session):
        """Preference wins over id order: the Ermaksan is created LAST (highest
        id) and still picked over the tube (lowest id) and the plain laser."""
        tube = make_work_center(db_session, name="HSG Tube Laser")
        plain = make_work_center(db_session, name="Bystronic Laser")
        ermaksan = make_work_center(db_session, name="Ermaksan Laser")
        assert tube.id < plain.id < ermaksan.id

        picked = _find_laser_work_center(db_session, COMPANY_A)
        assert picked.id == ermaksan.id

    def test_fiber_counts_as_top_tier(self, db_session):
        make_work_center(db_session, name="Bystronic Laser")  # lower id, tier 1
        fiber = make_work_center(db_session, name="Fiber Laser 2")

        picked = _find_laser_work_center(db_session, COMPANY_A)
        assert picked.id == fiber.id

    def test_plain_laser_beats_tube_even_with_lower_tube_id(self, db_session):
        make_work_center(db_session, name="HSG Tube Laser")  # lower id, tier 2
        plain = make_work_center(db_session, name="Trumpf Laser")

        picked = _find_laser_work_center(db_session, COMPANY_A)
        assert picked.id == plain.id

    def test_tube_chosen_only_when_it_is_the_only_laser(self, db_session):
        tube = make_work_center(db_session, name="HSG Tube Laser")
        make_work_center(db_session, name="Press Brake 1", wc_type="press_brake")  # not a laser candidate

        picked = _find_laser_work_center(db_session, COMPANY_A)
        assert picked.id == tube.id

    def test_id_tiebreak_within_a_tier(self, db_session):
        first = make_work_center(db_session, name="Trumpf Laser")
        make_work_center(db_session, name="Bystronic Laser")  # same tier, higher id

        picked = _find_laser_work_center(db_session, COMPANY_A)
        assert picked.id == first.id

    def test_explicit_id_wins_over_preference(self, db_session):
        tube = make_work_center(db_session, name="HSG Tube Laser")
        make_work_center(db_session, name="Ermaksan Fiber Laser")

        picked = _find_laser_work_center(db_session, COMPANY_A, work_center_id=tube.id)
        assert picked.id == tube.id

    def test_explicit_unknown_id_is_404(self, db_session):
        make_work_center(db_session, name="Ermaksan Fiber Laser")

        with pytest.raises(HTTPException) as exc:
            _find_laser_work_center(db_session, COMPANY_A, work_center_id=999_999)
        assert exc.value.status_code == 404
        assert exc.value.detail == "Laser work center not found"

    def test_explicit_inactive_or_cross_tenant_id_is_404(self, db_session):
        inactive = make_work_center(db_session, name="Retired Laser", is_active=False)
        foreign = make_work_center(db_session, name="Ermaksan Fiber Laser", company_id=COMPANY_B)

        for bad_id in (inactive.id, foreign.id):
            with pytest.raises(HTTPException) as exc:
                _find_laser_work_center(db_session, COMPANY_A, work_center_id=bad_id)
            assert exc.value.status_code == 404

    def test_auto_detect_ignores_inactive_and_foreign_lasers(self, db_session):
        make_work_center(db_session, name="Ermaksan Fiber Laser", is_active=False)  # inactive, tier 0
        make_work_center(db_session, name="Ermaksan Fiber Laser", company_id=COMPANY_B)  # other tenant
        plain = make_work_center(db_session, name="Trumpf Laser")

        picked = _find_laser_work_center(db_session, COMPANY_A)
        assert picked.id == plain.id
        assert picked.company_id == COMPANY_A

    def test_no_laser_candidates_is_400(self, db_session):
        make_work_center(db_session, name="Press Brake 1", wc_type="press_brake")

        with pytest.raises(HTTPException) as exc:
            _find_laser_work_center(db_session, COMPANY_A)
        assert exc.value.status_code == 400


# --------------------------------------------------------------------------- #
# Standalone import due_date
# --------------------------------------------------------------------------- #
class TestStandaloneImportDueDate:
    def test_due_date_is_stamped_on_created_wo_including_past(self, client, db_session):
        """A PAST due date is accepted -- an open WO can already be overdue at
        import (matching the WO import-loader posture)."""
        admin = make_user(db_session)
        wc = make_work_center(db_session, name="Ermaksan Fiber Laser")

        resp = _standalone_import(
            client,
            headers_for(admin),
            _cnc_zip("N1_QTY2.nc"),
            work_center_id=wc.id,
            due_date="2026-06-01",  # in the past relative to any run after mid-2026
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        child = resp.json()["child_work_order"]
        assert str(child["due_date"]).startswith("2026-06-01")

        wo = db_session.query(WorkOrder).filter_by(id=child["id"]).one()
        assert wo.due_date == date(2026, 6, 1)

    def test_absent_due_date_stays_null(self, client, db_session):
        admin = make_user(db_session)
        wc = make_work_center(db_session, name="Ermaksan Fiber Laser")

        resp = _standalone_import(client, headers_for(admin), _cnc_zip("N2_QTY3.nc"), work_center_id=wc.id)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        child = resp.json()["child_work_order"]
        assert child["due_date"] is None

        wo = db_session.query(WorkOrder).filter_by(id=child["id"]).one()
        assert wo.due_date is None


# --------------------------------------------------------------------------- #
# Per-row work-center overrides on PDF confirm-and-commit rows
# --------------------------------------------------------------------------- #
class TestPerRowWorkCenter:
    def test_rows_spread_ops_across_work_centers_with_group_per_row(self, client, db_session):
        """Each op lands on ITS row's work center and derives operation_group
        from that work center (not blanket 'LASER'); rows without an override
        fall back to the package-level laser."""
        admin = make_user(db_session)
        laser = make_work_center(db_session, name="Ermaksan Fiber Laser")
        brake = make_work_center(db_session, name="Press Brake 2", wc_type="press_brake")

        rows = [
            {"source_file": "n1.pdf", "cnc_number": "N1", "planned_runs": 2},
            {"source_file": "n2.pdf", "cnc_number": "N2", "planned_runs": 3, "work_center_id": brake.id},
        ]
        resp = _standalone_import(
            client, headers_for(admin), _pdf_zip("n1.pdf", "n2.pdf"), rows=rows, work_center_id=laser.id
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        child = resp.json()["child_work_order"]

        ops = (
            db_session.query(WorkOrderOperation)
            .filter(WorkOrderOperation.work_order_id == child["id"])
            .order_by(WorkOrderOperation.sequence)
            .all()
        )
        assert [(op.work_center_id, op.operation_group) for op in ops] == [
            (laser.id, "LASER"),
            (brake.id, "BEND"),
        ]
        assert [op.status for op in ops] == [OperationStatus.READY, OperationStatus.READY]

    def test_unknown_row_work_center_404s_before_anything_commits(self, client, db_session):
        admin = make_user(db_session)
        laser = make_work_center(db_session, name="Ermaksan Fiber Laser")

        rows = [
            {"source_file": "n1.pdf", "cnc_number": "N1", "planned_runs": 2},
            {"source_file": "n2.pdf", "cnc_number": "N2", "planned_runs": 3, "work_center_id": 999_999},
        ]
        resp = _standalone_import(
            client, headers_for(admin), _pdf_zip("n1.pdf", "n2.pdf"), rows=rows, work_center_id=laser.id
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        assert resp.json()["detail"] == "Laser work center not found"
        _assert_nothing_persisted(db_session)

    def test_inactive_row_work_center_404s_before_anything_commits(self, client, db_session):
        admin = make_user(db_session)
        laser = make_work_center(db_session, name="Ermaksan Fiber Laser")
        retired = make_work_center(db_session, name="Retired Laser", is_active=False)

        rows = [{"source_file": "n1.pdf", "cnc_number": "N1", "planned_runs": 2, "work_center_id": retired.id}]
        resp = _standalone_import(client, headers_for(admin), _pdf_zip("n1.pdf"), rows=rows, work_center_id=laser.id)
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        _assert_nothing_persisted(db_session)

    def test_cross_tenant_row_work_center_404s_before_anything_commits(self, client, db_session):
        """Tenant isolation: another company's work center id must not be
        resolvable as a per-row override."""
        admin = make_user(db_session)
        laser = make_work_center(db_session, name="Ermaksan Fiber Laser")
        foreign = make_work_center(db_session, name="Foreign Laser", company_id=COMPANY_B)

        rows = [{"source_file": "n1.pdf", "cnc_number": "N1", "planned_runs": 2, "work_center_id": foreign.id}]
        resp = _standalone_import(client, headers_for(admin), _pdf_zip("n1.pdf"), rows=rows, work_center_id=laser.id)
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        _assert_nothing_persisted(db_session)

    def test_reimport_wipes_nest_ops_regardless_of_operation_group(self, client, db_session):
        """The widened wipe: a nest op on a non-LASER-group work center (per-row
        override) is still replaced by a re-import -- the wipe is keyed by the
        nest-backed op ids, not just operation_group == 'LASER'."""
        admin = make_user(db_session)
        laser = make_work_center(db_session, name="Ermaksan Fiber Laser")
        brake = make_work_center(db_session, name="Press Brake 2", wc_type="press_brake")

        rows = [{"source_file": "n1.pdf", "cnc_number": "N1", "planned_runs": 2, "work_center_id": brake.id}]
        first = _standalone_import(client, headers_for(admin), _pdf_zip("n1.pdf"), rows=rows, work_center_id=laser.id)
        assert first.status_code == status.HTTP_200_OK, first.text
        child = first.json()["child_work_order"]
        assert db_session.query(WorkOrderOperation).filter_by(work_order_id=child["id"]).one().operation_group == "BEND"

        resp = _wo_import(
            client, headers_for(admin), child["id"], _cnc_zip("N9_QTY5.nc"), work_center_id=laser.id, name="cnc2.zip"
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        # Exactly ONE op remains and it is the NEW package's nest on the laser --
        # the old BEND-group op (which the narrow `operation_group == "LASER"`
        # wipe would have missed) is gone. (Identity is asserted by content, not
        # id: SQLite reuses the deleted row's autoincrement id on the rebuild.)
        remaining = db_session.query(WorkOrderOperation).filter_by(work_order_id=child["id"]).all()
        assert len(remaining) == 1
        assert remaining[0].name == "Laser Cut - N9"
        assert remaining[0].operation_group == "LASER"
        assert remaining[0].work_center_id == laser.id
        nests = db_session.query(LaserNest).all()
        assert [nest.nest_name for nest in nests] == ["N9"]


# --------------------------------------------------------------------------- #
# PUT /work-orders/operations/{id} work-center reassignment
# --------------------------------------------------------------------------- #
class TestOperationWorkCenterReassignment:
    def _one_nest_wo(self, client, db_session, admin, wc) -> WorkOrderOperation:
        resp = _standalone_import(client, headers_for(admin), _cnc_zip("N1_QTY2.nc"), work_center_id=wc.id)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        child = resp.json()["child_work_order"]
        return db_session.query(WorkOrderOperation).filter_by(work_order_id=child["id"]).one()

    def _reassign(self, client, user, op: WorkOrderOperation, target_wc_id, **extra):
        payload = {"version": op.version, "work_center_id": target_wc_id, **extra}
        return client.put(f"/api/v1/work-orders/operations/{op.id}", json=payload, headers=headers_for(user))

    def test_happy_path_moves_wc_updates_group_and_audits(self, client, db_session):
        admin = make_user(db_session)
        laser = make_work_center(db_session, name="Ermaksan Fiber Laser")
        brake = make_work_center(db_session, name="Press Brake 2", wc_type="press_brake")
        op = self._one_nest_wo(client, db_session, admin, laser)
        op_id, old_wc_id = op.id, op.work_center_id

        resp = self._reassign(client, admin, op, brake.id)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["work_center_id"] == brake.id
        assert resp.json()["operation_group"] == "BEND"

        db_session.expire_all()
        op = db_session.get(WorkOrderOperation, op_id)
        assert op.work_center_id == brake.id
        assert op.operation_group == "BEND"

        row = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "work_order_operation",
                AuditLog.action == "UPDATE",
                AuditLog.resource_id == op_id,
            )
            .one()
        )
        assert row.old_values["work_center_id"] == old_wc_id
        assert row.new_values["work_center_id"] == brake.id
        assert row.old_values["operation_group"] == "LASER"
        assert row.new_values["operation_group"] == "BEND"
        assert row.company_id == admin.company_id

    def test_refused_while_in_progress(self, client, db_session):
        admin = make_user(db_session)
        laser = make_work_center(db_session, name="Ermaksan Fiber Laser")
        other = make_work_center(db_session, name="Trumpf Laser")
        op = self._one_nest_wo(client, db_session, admin, laser)
        op.status = OperationStatus.IN_PROGRESS
        db_session.commit()
        db_session.refresh(op)
        op_id = op.id

        resp = self._reassign(client, admin, op, other.id)
        assert resp.status_code == status.HTTP_409_CONFLICT
        assert resp.json()["detail"] == "Clock out before moving the operation to another work center"

        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op_id).work_center_id == laser.id

    def test_refused_with_open_time_entry_despite_idle_status(self, client, db_session):
        """Belt-and-braces gate: the op is READY (not IN_PROGRESS) but an open
        TimeEntry (clock_out IS NULL) still blocks the move; closing the entry
        unblocks the same request."""
        admin = make_user(db_session)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        laser = make_work_center(db_session, name="Ermaksan Fiber Laser")
        other = make_work_center(db_session, name="Trumpf Laser")
        op = self._one_nest_wo(client, db_session, admin, laser)
        assert op.status == OperationStatus.READY
        op_id = op.id

        entry = TimeEntry(
            user_id=operator.id,
            work_order_id=op.work_order_id,
            operation_id=op.id,
            work_center_id=op.work_center_id,
            entry_type=TimeEntryType.RUN,
            clock_in=datetime.utcnow() - timedelta(hours=1),
            clock_out=None,
            company_id=COMPANY_A,
        )
        db_session.add(entry)
        db_session.commit()

        resp = self._reassign(client, admin, op, other.id)
        assert resp.status_code == status.HTTP_409_CONFLICT
        assert resp.json()["detail"] == "Clock out before moving the operation to another work center"
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op_id).work_center_id == laser.id

        # Close the session -> the identical reassignment now succeeds.
        entry.clock_out = datetime.utcnow()
        db_session.commit()
        op = db_session.get(WorkOrderOperation, op_id)
        resp = self._reassign(client, admin, op, other.id)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op_id).work_center_id == other.id

    def test_inactive_target_is_404_and_mutates_nothing(self, client, db_session):
        admin = make_user(db_session)
        laser = make_work_center(db_session, name="Ermaksan Fiber Laser")
        retired = make_work_center(db_session, name="Retired Laser", is_active=False)
        op = self._one_nest_wo(client, db_session, admin, laser)
        op_id = op.id

        resp = self._reassign(client, admin, op, retired.id)
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        assert resp.json()["detail"] == "Work center not found"

        db_session.expire_all()
        op = db_session.get(WorkOrderOperation, op_id)
        assert op.work_center_id == laser.id
        assert op.operation_group == "LASER"
        audit_rows = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "work_order_operation",
                AuditLog.action == "UPDATE",
                AuditLog.resource_id == op_id,
            )
            .all()
        )
        assert audit_rows == []

    def test_cross_tenant_target_is_404(self, client, db_session):
        """Tenant isolation: another company's work center can never become the
        target of a reassignment."""
        admin = make_user(db_session)
        laser = make_work_center(db_session, name="Ermaksan Fiber Laser")
        foreign = make_work_center(db_session, name="Foreign Laser", company_id=COMPANY_B)
        op = self._one_nest_wo(client, db_session, admin, laser)
        op_id = op.id

        resp = self._reassign(client, admin, op, foreign.id)
        assert resp.status_code == status.HTTP_404_NOT_FOUND

        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op_id).work_center_id == laser.id

    def test_same_wc_and_explicit_null_are_noops(self, client, db_session):
        """Sending the CURRENT work center (or an explicit null) is accepted and
        changes nothing -- the idle/active gates are not even consulted (the op
        is IN_PROGRESS here and the request still succeeds)."""
        admin = make_user(db_session)
        laser = make_work_center(db_session, name="Ermaksan Fiber Laser")
        op = self._one_nest_wo(client, db_session, admin, laser)
        op.status = OperationStatus.IN_PROGRESS
        db_session.commit()
        db_session.refresh(op)
        op_id = op.id

        resp = self._reassign(client, admin, op, laser.id)
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.expire_all()
        op = db_session.get(WorkOrderOperation, op_id)
        assert op.work_center_id == laser.id

        resp = client.put(
            f"/api/v1/work-orders/operations/{op_id}",
            json={"version": op.version, "work_center_id": None},
            headers=headers_for(admin),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op_id).work_center_id == laser.id


class TestOperationVersionEnforcement:
    """Optimistic locking (invariant 4) is REAL on this endpoint: a stale client
    version is a 409, and the client can never write the version counter."""

    def test_stale_version_is_409_and_mutates_nothing(self, client, db_session):
        admin = make_user(db_session)
        laser = make_work_center(db_session, name="Ermaksan Fiber Laser")
        brake = make_work_center(db_session, name="Press Brake 2", wc_type="press_brake")
        resp = _standalone_import(client, headers_for(admin), _cnc_zip("N1_QTY2.nc"), work_center_id=laser.id)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        op = db_session.query(WorkOrderOperation).filter_by(work_order_id=resp.json()["child_work_order"]["id"]).one()
        op_id, real_version, old_wc_id = op.id, op.version, op.work_center_id

        resp = client.put(
            f"/api/v1/work-orders/operations/{op_id}",
            json={"version": real_version + 41, "work_center_id": brake.id},
            headers=headers_for(admin),
        )

        assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
        assert "modified" in resp.json()["detail"]
        db_session.expire_all()
        fresh = db_session.get(WorkOrderOperation, op_id)
        assert fresh.work_center_id == old_wc_id
        assert fresh.version == real_version  # counter never moved by the client

    def test_matching_version_succeeds(self, client, db_session):
        admin = make_user(db_session)
        laser = make_work_center(db_session, name="Ermaksan Fiber Laser")
        brake = make_work_center(db_session, name="Press Brake 2", wc_type="press_brake")
        resp = _standalone_import(client, headers_for(admin), _cnc_zip("N1_QTY2.nc"), work_center_id=laser.id)
        op = db_session.query(WorkOrderOperation).filter_by(work_order_id=resp.json()["child_work_order"]["id"]).one()

        resp = client.put(
            f"/api/v1/work-orders/operations/{op.id}",
            json={"version": op.version, "work_center_id": brake.id},
            headers=headers_for(admin),
        )

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["work_center_id"] == brake.id


class TestReviewHardening:
    """Pins the adversarial-review fixes: tube-beats-fiber demotion, no moving
    completed ops, no free-form ops on laser dispatch pools, and the scheduling
    twin endpoint's guard parity."""

    def test_fiber_tube_name_never_wins_auto_detect(self, client, db_session):
        """'HSG Fiber Tube Laser' contains 'fiber' but IS the tube laser — the
        tube demotion must win even against the id tiebreak."""
        admin = make_user(db_session)
        make_work_center(db_session, name="HSG Fiber Tube Laser")  # lower id
        ermaksan = make_work_center(db_session, name="Ermaksan Laser")

        resp = _standalone_import(client, headers_for(admin), _cnc_zip("N1_QTY1.nc"))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        op = db_session.query(WorkOrderOperation).filter_by(work_order_id=resp.json()["child_work_order"]["id"]).one()
        assert op.work_center_id == ermaksan.id

    def test_reassign_complete_operation_is_409(self, client, db_session):
        admin = make_user(db_session)
        laser = make_work_center(db_session, name="Ermaksan Fiber Laser")
        brake = make_work_center(db_session, name="Press Brake 2", wc_type="press_brake")
        resp = _standalone_import(client, headers_for(admin), _cnc_zip("N1_QTY1.nc"), work_center_id=laser.id)
        op = db_session.query(WorkOrderOperation).filter_by(work_order_id=resp.json()["child_work_order"]["id"]).one()
        op.status = OperationStatus.COMPLETE
        db_session.commit()
        db_session.refresh(op)

        resp = client.put(
            f"/api/v1/work-orders/operations/{op.id}",
            json={"version": op.version, "work_center_id": brake.id},
            headers=headers_for(admin),
        )

        assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
        assert "Completed operations" in resp.json()["detail"]

    def test_add_free_form_operation_on_laser_wo_is_400(self, client, db_session):
        admin = make_user(db_session)
        laser = make_work_center(db_session, name="Ermaksan Fiber Laser")
        resp = _standalone_import(client, headers_for(admin), _cnc_zip("N1_QTY1.nc"), work_center_id=laser.id)
        wo_id = resp.json()["child_work_order"]["id"]

        resp = client.post(
            f"/api/v1/work-orders/{wo_id}/operations",
            json={"sequence": 90, "operation_number": "Op 90", "name": "Deburr", "work_center_id": laser.id},
            headers=headers_for(admin),
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert "nest" in resp.json()["detail"].lower()

    def test_per_row_wc_404_reaps_package_dir(self, client, db_session, tmp_path, monkeypatch):
        """A per-row work-center 404 after extraction must not orphan the
        extracted package directory under the laser upload root."""
        import app.api.endpoints.work_orders as wo_endpoint

        upload_root = tmp_path / "laser-root"
        monkeypatch.setattr(wo_endpoint, "_resolve_laser_upload_root", lambda: str(upload_root))
        admin = make_user(db_session)
        make_work_center(db_session, name="Ermaksan Fiber Laser")

        rows = [
            {
                "source_file": "N1_QTY1.nc",
                "cnc_number": "N1",
                "planned_runs": 1,
                "work_center_id": 999999,
            }
        ]
        resp = _standalone_import(client, headers_for(admin), _cnc_zip("N1_QTY1.nc"), rows=rows)

        assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
        leftovers = list(upload_root.glob("*")) if upload_root.exists() else []
        assert leftovers == [], f"orphaned package dirs: {leftovers}"


class TestSchedulingReassignParity:
    """The Scheduling page's PUT /scheduling/operations/{id}/work-center must
    enforce the same contract as the work-orders reassign path."""

    def _make_op(self, client, db_session, admin, wc) -> WorkOrderOperation:
        resp = _standalone_import(client, headers_for(admin), _cnc_zip("N1_QTY2.nc"), work_center_id=wc.id)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        return db_session.query(WorkOrderOperation).filter_by(work_order_id=resp.json()["child_work_order"]["id"]).one()

    def _move(self, client, user, op_id, wc_id):
        return client.put(
            f"/api/v1/scheduling/operations/{op_id}/work-center",
            json={"work_center_id": wc_id},
            headers=headers_for(user),
        )

    def test_happy_path_moves_group_and_audits(self, client, db_session):
        admin = make_user(db_session)
        laser = make_work_center(db_session, name="Ermaksan Fiber Laser")
        brake = make_work_center(db_session, name="Press Brake 2", wc_type="press_brake")
        op = self._make_op(client, db_session, admin, laser)
        op_id = op.id

        resp = self._move(client, admin, op_id, brake.id)

        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        fresh = db_session.get(WorkOrderOperation, op_id)
        assert fresh.work_center_id == brake.id
        assert fresh.operation_group != "LASER"
        audit_row = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "work_order_operation", AuditLog.resource_id == op_id)
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert audit_row is not None and audit_row.action == "UPDATE"

    def test_cross_tenant_operation_is_404(self, client, db_session):
        admin_b = make_user(db_session, company_id=COMPANY_B)
        laser_a = make_work_center(db_session, name="Ermaksan Fiber Laser", company_id=COMPANY_A)
        admin_a = make_user(db_session, company_id=COMPANY_A)
        op = self._make_op(client, db_session, admin_a, laser_a)
        wc_b = make_work_center(db_session, name="Ermaksan Fiber Laser B", company_id=COMPANY_B)

        resp = self._move(client, admin_b, op.id, wc_b.id)

        assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op.id).work_center_id == laser_a.id

    def test_cross_tenant_work_center_is_404(self, client, db_session):
        admin = make_user(db_session, company_id=COMPANY_A)
        laser = make_work_center(db_session, name="Ermaksan Fiber Laser", company_id=COMPANY_A)
        wc_b = make_work_center(db_session, name="Foreign Laser", company_id=COMPANY_B)
        op = self._make_op(client, db_session, admin, laser)

        resp = self._move(client, admin, op.id, wc_b.id)

        assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op.id).work_center_id == laser.id

    def test_in_progress_and_complete_are_409(self, client, db_session):
        admin = make_user(db_session)
        laser = make_work_center(db_session, name="Ermaksan Fiber Laser")
        brake = make_work_center(db_session, name="Press Brake 2", wc_type="press_brake")
        op = self._make_op(client, db_session, admin, laser)

        op.status = OperationStatus.IN_PROGRESS
        db_session.commit()
        resp = self._move(client, admin, op.id, brake.id)
        assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
        assert "Clock out" in resp.json()["detail"]

        op.status = OperationStatus.COMPLETE
        db_session.commit()
        resp = self._move(client, admin, op.id, brake.id)
        assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
        assert "Completed operations" in resp.json()["detail"]
