"""The tool crib saves its ledger rows, stays inside the caller's tenant, is role-gated and audited.

``api/endpoints/tool_management.py`` had the same root cause as the calibration router:

* **Checkout and usage logging could not save.** ``ToolCheckout`` / ``ToolUsageLog`` carry
  ``TenantMixin.company_id`` (NOT NULL) and both handlers built rows without it -- every
  checkout from the Tool Management page was a 500.
* **Five reads were cross-tenant.** ``/tools/{id}/history`` resolved the tool by bare id, and
  the dashboard / checked-out / replacement-due / inspection-due lists took no company at all.
* **No role gate, no audit.** A Viewer could create, edit (blind setattr, incl. ``status``) and
  retire tools, and nothing recorded who checked a tool out or back in.

Write cases assert the STORED rows and the audit-row count, not just the status code.
"""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Query, Session

from app.models.audit_log import AuditLog
from app.models.tool_management import Tool, ToolCheckout, ToolStatus, ToolUsageLog
from app.models.user import UserRole
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    COMPANY_B,
    ensure_company,
    make_user,
    make_wo_with_operation,
    make_work_center,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

TOOLS_URL = "/api/v1/tool-management/tools"
FOREIGN_TOOL_NAME = "ACME-SECRET 5-axis trunnion fixture"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def make_tool(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    status: ToolStatus = ToolStatus.AVAILABLE,
    name: str = "Shell mill",
    max_life_hours: float = None,
    current_life_hours: float = 0,
) -> Tool:
    tool = Tool(
        tool_id=f"TL-INT-{_next():05d}",
        name=name,
        status=status,
        max_life_hours=max_life_hours,
        current_life_hours=current_life_hours,
        is_active=True,
        company_id=company_id,
    )
    db.add(tool)
    db.commit()
    db.refresh(tool)
    return tool


def headers_for(db: Session, *, company_id: int = COMPANY_A, role: UserRole = UserRole.OPERATOR) -> dict:
    return user_headers(make_user(db, company_id=company_id, role=role))


def audit_count(db: Session) -> int:
    return db.query(AuditLog).count()


def ledger_counts(db: Session) -> tuple:
    return db.query(ToolCheckout).count(), db.query(ToolUsageLog).count()


# ---------------------------------------------------------------------------
# 1. The ledger rows save, stamped with the tool's company, and are audited
# ---------------------------------------------------------------------------


class TestToolLedgerWrites:
    def test_checkout_then_checkin_saves_a_scoped_ledger_and_audits_both(self, client: TestClient, db_session: Session):
        tool = make_tool(db_session)
        work_center = make_work_center(db_session)
        headers = headers_for(db_session)

        out = client.post(f"{TOOLS_URL}/{tool.id}/checkout", json={"work_center_id": work_center.id}, headers=headers)
        # Was a NOT NULL 500 before company_id was stamped.
        assert out.status_code == 200, out.text
        checkout = db_session.query(ToolCheckout).filter(ToolCheckout.id == out.json()["id"]).one()
        assert checkout.company_id == COMPANY_A
        db_session.refresh(tool)
        assert tool.status == ToolStatus.IN_USE
        assert tool.current_work_center_id == work_center.id

        back = client.post(f"{TOOLS_URL}/{tool.id}/checkin", json={"condition_in": "damaged"}, headers=headers)
        assert back.status_code == 200, back.text
        db_session.refresh(checkout)
        assert checkout.checked_in_at is not None
        db_session.refresh(tool)
        assert tool.status == ToolStatus.DAMAGED

        rows = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "tool", AuditLog.resource_id == tool.id)
            .order_by(AuditLog.id)
            .all()
        )
        assert [(r.action, r.old_values, r.new_values) for r in rows] == [
            ("STATUS_CHANGE", {"status": "available"}, {"status": "in_use"}),
            ("STATUS_CHANGE", {"status": "in_use"}, {"status": "damaged"}),
        ]
        assert all(r.company_id == COMPANY_A for r in rows)

    def test_usage_log_saves_scoped_and_audits(self, client: TestClient, db_session: Session):
        tool = make_tool(db_session, max_life_hours=100)
        response = client.post(
            f"{TOOLS_URL}/{tool.id}/log-usage", json={"usage_hours": 12.5}, headers=headers_for(db_session)
        )
        assert response.status_code == 200, response.text
        log = db_session.query(ToolUsageLog).filter(ToolUsageLog.id == response.json()["id"]).one()
        assert log.company_id == COMPANY_A
        db_session.refresh(tool)
        assert tool.current_life_hours == 12.5
        assert (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "tool_usage_log", AuditLog.resource_id == log.id)
            .count()
            == 1
        )

    @pytest.mark.parametrize("payload", [{"usage_hours": -80}, {"usage_cycles": -5}])
    def test_negative_usage_is_422_and_writes_nothing(self, client: TestClient, db_session: Session, payload: dict):
        tool = make_tool(db_session, max_life_hours=100, current_life_hours=90)
        headers = headers_for(db_session)
        ledger_before = ledger_counts(db_session)
        audit_before = audit_count(db_session)

        response = client.post(f"{TOOLS_URL}/{tool.id}/log-usage", json=payload, headers=headers)

        assert response.status_code == 422
        assert ledger_counts(db_session) == ledger_before
        assert audit_count(db_session) == audit_before
        db_session.refresh(tool)
        assert (tool.current_life_hours, tool.current_life_cycles or 0) == (90, 0)

    def test_create_update_retire_are_audited(self, client: TestClient, db_session: Session):
        headers = headers_for(db_session, role=UserRole.SUPERVISOR)
        created = client.post(
            f"{TOOLS_URL}/", json={"tool_id": f"NEW-{_next()}", "name": "Boring bar"}, headers=headers
        )
        assert created.status_code == 200, created.text
        tool_id = created.json()["id"]
        assert db_session.query(Tool).filter(Tool.id == tool_id).one().company_id == COMPANY_A

        updated = client.put(
            f"{TOOLS_URL}/{tool_id}", json={"location": "Crib B", "status": "maintenance"}, headers=headers
        )
        assert updated.status_code == 200, updated.text
        retired = client.delete(f"{TOOLS_URL}/{tool_id}", headers=headers)
        assert retired.status_code == 200, retired.text

        actions = [
            row.action
            for row in db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "tool", AuditLog.resource_id == tool_id)
            .order_by(AuditLog.id)
        ]
        assert actions == ["CREATE", "UPDATE", "STATUS_CHANGE", "STATUS_CHANGE"]
        tool = db_session.query(Tool).filter(Tool.id == tool_id).one()
        assert tool.status == ToolStatus.RETIRED
        assert tool.is_active is False


# ---------------------------------------------------------------------------
# 2. PUT is an explicit field update; IN_USE is reachable only by checkout/checkin
# ---------------------------------------------------------------------------


class TestToolExplicitUpdate:
    def test_put_cannot_mark_a_tool_in_use(self, client: TestClient, db_session: Session):
        tool = make_tool(db_session)
        before = audit_count(db_session)
        response = client.put(
            f"{TOOLS_URL}/{tool.id}", json={"status": "in_use"}, headers=headers_for(db_session, role=UserRole.MANAGER)
        )
        assert response.status_code == 409
        db_session.refresh(tool)
        assert tool.status == ToolStatus.AVAILABLE
        assert audit_count(db_session) == before

    def test_a_checked_out_tool_cannot_change_status_or_retire(self, client: TestClient, db_session: Session):
        tool = make_tool(db_session, status=ToolStatus.IN_USE)
        headers = headers_for(db_session, role=UserRole.MANAGER)
        before = audit_count(db_session)

        put = client.put(f"{TOOLS_URL}/{tool.id}", json={"status": "available"}, headers=headers)
        retire = client.delete(f"{TOOLS_URL}/{tool.id}", headers=headers)

        assert (put.status_code, retire.status_code) == (409, 409)
        assert "checkin" in put.json()["detail"]
        db_session.refresh(tool)
        assert tool.status == ToolStatus.IN_USE
        assert tool.is_active is True
        assert audit_count(db_session) == before

    def test_a_refused_status_writes_none_of_the_fields_sent_with_it(self, client: TestClient, db_session: Session):
        # Every refusal is raised before the first setattr: a 409 that renamed the tool anyway
        # would pass a status-only assertion.
        checked_out = make_tool(db_session, status=ToolStatus.IN_USE)
        available = make_tool(db_session)
        headers = headers_for(db_session, role=UserRole.MANAGER)
        before = audit_count(db_session)
        edits = {"name": "Renamed", "location": "Crib Z"}

        for tool, extra in (
            (checked_out, {"status": "available"}),
            (checked_out, {"is_active": False}),
            (available, {"status": "in_use"}),
            (available, {"status": "retired"}),
        ):
            response = client.put(f"{TOOLS_URL}/{tool.id}", json={**edits, **extra}, headers=headers)
            assert response.status_code == 409, (extra, response.text)

        for tool, status in ((checked_out, ToolStatus.IN_USE), (available, ToolStatus.AVAILABLE)):
            db_session.refresh(tool)
            assert (tool.name, tool.location, tool.status, tool.is_active) == ("Shell mill", None, status, True)
        assert audit_count(db_session) == before

    def test_unknown_keys_are_ignored_forward_guard(self, client: TestClient, db_session: Session):
        """Forward guard, not a regression test: it passes against the pre-allowlist code too.

        Pydantic already drops keys ``ToolUpdate`` does not declare. What this pins is that
        nothing outside ``_TOOL_EDITABLE_FIELDS`` is written if one of these columns is ever
        added to ``ToolUpdate`` for another reason.
        """
        tool = make_tool(db_session, max_life_hours=100, current_life_hours=40)
        response = client.put(
            f"{TOOLS_URL}/{tool.id}",
            json={"current_life_hours": 0, "company_id": COMPANY_B, "current_user_id": 1, "name": "Renamed"},
            headers=headers_for(db_session, role=UserRole.MANAGER),
        )
        assert response.status_code == 200, response.text
        db_session.refresh(tool)
        assert tool.name == "Renamed"
        assert tool.current_life_hours == 40
        assert tool.company_id == COMPANY_A
        assert tool.current_user_id is None

    @pytest.mark.parametrize("payload", [{"status": "bogus"}, {"tool_type": "laser"}, {"name": None}])
    def test_invalid_edits_are_422(self, client: TestClient, db_session: Session, payload: dict):
        tool = make_tool(db_session)
        response = client.put(
            f"{TOOLS_URL}/{tool.id}", json=payload, headers=headers_for(db_session, role=UserRole.MANAGER)
        )
        assert response.status_code == 422

    def test_put_cannot_hide_a_checked_out_tool(self, client: TestClient, db_session: Session):
        # ``is_active: false`` on an IN_USE tool dropped it off /tools/checked-out and stranded its
        # open checkout -- exactly what DELETE's 409 exists to prevent.
        tool = make_tool(db_session, status=ToolStatus.IN_USE)
        before = audit_count(db_session)
        response = client.put(
            f"{TOOLS_URL}/{tool.id}", json={"is_active": False}, headers=headers_for(db_session, role=UserRole.MANAGER)
        )
        assert response.status_code == 409, response.text
        assert "checkin" in response.json()["detail"]
        db_session.refresh(tool)
        assert (tool.is_active, tool.status) == (True, ToolStatus.IN_USE)
        assert audit_count(db_session) == before

    def test_put_cannot_retire_a_tool_delete_is_the_one_door(self, client: TestClient, db_session: Session):
        # PUT ``status: retired`` left ``is_active`` true, so the "retired" tool stayed on every
        # active list and in the dashboard counts; DELETE clears both.
        tool = make_tool(db_session)
        before = audit_count(db_session)
        response = client.put(
            f"{TOOLS_URL}/{tool.id}", json={"status": "retired"}, headers=headers_for(db_session, role=UserRole.MANAGER)
        )
        assert response.status_code == 409, response.text
        assert f"DELETE /tools/{tool.id}" in response.json()["detail"]
        db_session.refresh(tool)
        assert (tool.status, tool.is_active) == (ToolStatus.AVAILABLE, True)
        assert audit_count(db_session) == before

    def test_retiring_a_retired_tool_writes_no_second_row(self, client: TestClient, db_session: Session):
        tool = make_tool(db_session)
        headers = headers_for(db_session, role=UserRole.MANAGER)
        assert client.delete(f"{TOOLS_URL}/{tool.id}", headers=headers).status_code == 200
        after_first = audit_count(db_session)

        again = client.delete(f"{TOOLS_URL}/{tool.id}", headers=headers)

        assert again.status_code == 200, again.text
        assert "already retired" in again.json()["message"]
        assert audit_count(db_session) == after_first


# ---------------------------------------------------------------------------
# 2b. Check-in records the condition the person picked
# ---------------------------------------------------------------------------


class TestCheckinCondition:
    """``condition_in`` (alias ``condition``, the page's key) is a closed vocabulary.

    The page sends ``condition`` with ``good`` / ``worn`` / ``damaged`` / ``needs_repair``; the API
    read only ``condition_in`` and defaulted it to ``good``. So a tool reported DAMAGED went back
    to AVAILABLE -- and, once checkout saved, the new audit row said ``condition_in: good``.
    """

    def _checked_out(self, client: TestClient, db_session: Session, headers: dict) -> Tool:
        tool = make_tool(db_session)
        assert client.post(f"{TOOLS_URL}/{tool.id}/checkout", json={}, headers=headers).status_code == 200
        return tool

    @pytest.mark.parametrize(
        "payload, expected_status, recorded",
        [
            ({"condition": "damaged"}, ToolStatus.DAMAGED, "damaged"),
            ({"condition": "needs_repair"}, ToolStatus.MAINTENANCE, "needs_repair"),
            ({"condition": "worn"}, ToolStatus.AVAILABLE, "worn"),
            ({"condition": " Damaged "}, ToolStatus.DAMAGED, "damaged"),
            ({"condition_in": "needs_maintenance"}, ToolStatus.MAINTENANCE, "needs_maintenance"),
            ({}, ToolStatus.AVAILABLE, "good"),
        ],
    )
    def test_the_pages_condition_is_what_is_recorded(
        self, client: TestClient, db_session: Session, payload: dict, expected_status: ToolStatus, recorded: str
    ):
        headers = headers_for(db_session)
        tool = self._checked_out(client, db_session, headers)

        response = client.post(f"{TOOLS_URL}/{tool.id}/checkin", json=payload, headers=headers)

        assert response.status_code == 200, response.text
        db_session.refresh(tool)
        assert tool.status == expected_status
        checkout = db_session.query(ToolCheckout).filter(ToolCheckout.tool_id == tool.id).one()
        assert checkout.condition_in == recorded
        row = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "tool", AuditLog.resource_id == tool.id)
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert row.extra_data["condition_in"] == recorded

    @pytest.mark.parametrize("payload", [{"condition": "smashed"}, {"condition_in": "fine"}, {"condition": ""}])
    def test_an_unknown_condition_is_422_and_the_tool_stays_checked_out(
        self, client: TestClient, db_session: Session, payload: dict
    ):
        headers = headers_for(db_session)
        tool = self._checked_out(client, db_session, headers)
        before = audit_count(db_session)

        response = client.post(f"{TOOLS_URL}/{tool.id}/checkin", json=payload, headers=headers)

        assert response.status_code == 422, response.text
        db_session.refresh(tool)
        assert tool.status == ToolStatus.IN_USE
        checkout = db_session.query(ToolCheckout).filter(ToolCheckout.tool_id == tool.id).one()
        assert checkout.checked_in_at is None
        assert audit_count(db_session) == before


class TestToolWritesLockTheTool:
    """Every write verb row-locks the tool first (``Tool`` has no ``version`` column).

    Without it two simultaneous checkouts both read AVAILABLE and leave two open ledger rows,
    and two usage logs lose an increment. Asserted on the query each verb built, compiled for
    Postgres -- SQLite renders no ``FOR UPDATE``.
    """

    @pytest.mark.parametrize("verb", ["put", "delete", "checkout", "checkin", "log-usage"])
    def test_the_verb_locks_the_tool_row(self, client: TestClient, db_session: Session, monkeypatch, verb: str):
        tool = make_tool(db_session, status=ToolStatus.IN_USE if verb == "checkin" else ToolStatus.AVAILABLE)
        headers = headers_for(db_session, role=UserRole.MANAGER)
        built: list = []
        original = Query.with_for_update

        def spy(self, *args, **kwargs):
            locked = original(self, *args, **kwargs)
            built.append(locked)
            return locked

        monkeypatch.setattr(Query, "with_for_update", spy)

        if verb == "put":
            response = client.put(f"{TOOLS_URL}/{tool.id}", json={"location": "Crib B"}, headers=headers)
        elif verb == "delete":
            response = client.delete(f"{TOOLS_URL}/{tool.id}", headers=headers)
        else:
            response = client.post(f"{TOOLS_URL}/{tool.id}/{verb}", json={"usage_hours": 1}, headers=headers)

        assert response.status_code == 200, response.text
        compiled = [str(q.statement.compile(dialect=postgresql.dialect())) for q in built]
        assert any("FROM tools" in sql and "FOR UPDATE OF tools" in sql for sql in compiled), compiled


# ---------------------------------------------------------------------------
# 3. RBAC: Viewer refused everywhere; Operator keeps checkout/checkin/usage
# ---------------------------------------------------------------------------


class TestToolRbac:
    def test_viewer_is_refused_on_every_write_with_no_side_effects(self, client: TestClient, db_session: Session):
        available = make_tool(db_session)
        in_use = make_tool(db_session, status=ToolStatus.IN_USE)
        headers = headers_for(db_session, role=UserRole.VIEWER)
        tools_before = db_session.query(Tool).count()
        ledger_before = ledger_counts(db_session)
        audit_before = audit_count(db_session)

        statuses = [
            client.post(f"{TOOLS_URL}/", json={"tool_id": f"V-{_next()}", "name": "x"}, headers=headers).status_code,
            client.put(f"{TOOLS_URL}/{available.id}", json={"name": "y"}, headers=headers).status_code,
            client.delete(f"{TOOLS_URL}/{available.id}", headers=headers).status_code,
            client.post(f"{TOOLS_URL}/{available.id}/checkout", json={}, headers=headers).status_code,
            client.post(f"{TOOLS_URL}/{in_use.id}/checkin", json={}, headers=headers).status_code,
            client.post(f"{TOOLS_URL}/{available.id}/log-usage", json={"usage_hours": 1}, headers=headers).status_code,
        ]

        assert statuses == [403] * 6
        assert db_session.query(Tool).count() == tools_before
        assert ledger_counts(db_session) == ledger_before
        assert audit_count(db_session) == audit_before
        db_session.refresh(available)
        db_session.refresh(in_use)
        assert (available.name, available.status) == ("Shell mill", ToolStatus.AVAILABLE)
        assert in_use.status == ToolStatus.IN_USE

    def test_operator_cannot_manage_tool_records(self, client: TestClient, db_session: Session):
        tool = make_tool(db_session)
        headers = headers_for(db_session, role=UserRole.OPERATOR)
        audit_before = audit_count(db_session)
        assert (
            client.post(f"{TOOLS_URL}/", json={"tool_id": f"O-{_next()}", "name": "x"}, headers=headers).status_code
            == 403
        )
        assert client.put(f"{TOOLS_URL}/{tool.id}", json={"name": "y"}, headers=headers).status_code == 403
        assert client.delete(f"{TOOLS_URL}/{tool.id}", headers=headers).status_code == 403
        assert audit_count(db_session) == audit_before

    @pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.QUALITY, UserRole.SHIPPING])
    def test_floor_roles_keep_checkout_and_checkin(self, client: TestClient, db_session: Session, role: UserRole):
        tool = make_tool(db_session)
        headers = headers_for(db_session, role=role)
        assert client.post(f"{TOOLS_URL}/{tool.id}/checkout", json={}, headers=headers).status_code == 200
        assert client.post(f"{TOOLS_URL}/{tool.id}/checkin", json={}, headers=headers).status_code == 200

    def test_reads_stay_open_to_a_viewer(self, client: TestClient, db_session: Session):
        tool = make_tool(db_session)
        headers = headers_for(db_session, role=UserRole.VIEWER)
        assert client.get(f"{TOOLS_URL}/", headers=headers).status_code == 200
        assert client.get(f"{TOOLS_URL}/{tool.id}/history", headers=headers).status_code == 200


# ---------------------------------------------------------------------------
# 4. Tenant isolation: another company's tool is a flat 404; lists never show it
# ---------------------------------------------------------------------------


class TestToolTenantIsolation:
    def _victim(self, db: Session) -> Tool:
        # Low life + checked out + inspection overdue: on every list endpoint at once.
        tool = make_tool(
            db,
            company_id=COMPANY_A,
            status=ToolStatus.IN_USE,
            name=FOREIGN_TOOL_NAME,
            max_life_hours=100,
            current_life_hours=95,
        )
        tool.next_inspection_date = date.today() - timedelta(days=3)
        db.add(ToolCheckout(tool_id=tool.id, checked_out_by=make_user(db).id, company_id=COMPANY_A))
        db.commit()
        db.refresh(tool)
        return tool

    def test_history_of_another_tenants_tool_is_404_and_leaks_nothing(self, client: TestClient, db_session: Session):
        tool = self._victim(db_session)
        headers = headers_for(db_session, company_id=COMPANY_B, role=UserRole.ADMIN)

        history = client.get(f"{TOOLS_URL}/{tool.id}/history", headers=headers)
        missing = client.get(f"{TOOLS_URL}/999999/history", headers=headers)

        assert history.status_code == 404
        assert history.json() == missing.json()
        assert FOREIGN_TOOL_NAME not in history.text

    def test_writes_against_another_tenants_tool_are_404_with_no_side_effects(
        self, client: TestClient, db_session: Session
    ):
        tool = self._victim(db_session)
        headers = headers_for(db_session, company_id=COMPANY_B, role=UserRole.ADMIN)
        ledger_before = ledger_counts(db_session)
        audit_before = audit_count(db_session)

        statuses = [
            client.put(f"{TOOLS_URL}/{tool.id}", json={"name": "pwned"}, headers=headers).status_code,
            client.delete(f"{TOOLS_URL}/{tool.id}", headers=headers).status_code,
            client.post(f"{TOOLS_URL}/{tool.id}/checkout", json={}, headers=headers).status_code,
            client.post(f"{TOOLS_URL}/{tool.id}/checkin", json={}, headers=headers).status_code,
            client.post(f"{TOOLS_URL}/{tool.id}/log-usage", json={"usage_hours": 1}, headers=headers).status_code,
        ]

        assert statuses == [404] * 5
        assert ledger_counts(db_session) == ledger_before
        assert audit_count(db_session) == audit_before
        db_session.refresh(tool)
        assert (tool.name, tool.status, tool.is_active, tool.current_life_hours) == (
            FOREIGN_TOOL_NAME,
            ToolStatus.IN_USE,
            True,
            95,
        )

    def test_lists_never_show_another_tenants_tools(self, client: TestClient, db_session: Session):
        tool = self._victim(db_session)
        headers = headers_for(db_session, company_id=COMPANY_B, role=UserRole.ADMIN)

        for path in ("/", "/checked-out", "/replacement-due", "/inspection-due"):
            response = client.get(f"{TOOLS_URL}{path}", headers=headers)
            assert response.status_code == 200, path
            assert tool.id not in [row["id"] for row in response.json()], path
        dashboard = client.get(f"{TOOLS_URL}/dashboard", headers=headers).json()
        assert (dashboard["total_tools"], dashboard["checked_out"]) == (0, 0)

    def test_checkout_against_another_tenants_work_center_is_404(self, client: TestClient, db_session: Session):
        own_tool = make_tool(db_session, company_id=COMPANY_B)
        foreign_wc = make_work_center(db_session, company_id=COMPANY_A)
        headers = headers_for(db_session, company_id=COMPANY_B)
        ledger_before = ledger_counts(db_session)

        response = client.post(
            f"{TOOLS_URL}/{own_tool.id}/checkout", json={"work_center_id": foreign_wc.id}, headers=headers
        )

        assert response.status_code == 404
        assert ledger_counts(db_session) == ledger_before
        db_session.refresh(own_tool)
        assert own_tool.status == ToolStatus.AVAILABLE


# ---------------------------------------------------------------------------
# 5. Gaps the first pass left open (test-engineer review)
# ---------------------------------------------------------------------------


class TestToolForeignReferences:
    """``work_center_id`` / ``work_order_id`` on checkout AND log-usage are the caller's own, live rows.

    The first pass covered only a foreign work center on checkout; the same guard on the
    usage ledger, the work-order half, and the soft-deleted-job refusal had no test, so
    deleting any of those lines left the suite green.
    """

    def _own_tool(self, db: Session) -> Tool:
        return make_tool(db, company_id=COMPANY_B, max_life_hours=100, current_life_hours=10)

    def _assert_untouched(self, db: Session, tool: Tool, ledger_before: tuple, audit_before: int) -> None:
        assert ledger_counts(db) == ledger_before
        assert audit_count(db) == audit_before
        db.refresh(tool)
        assert (tool.status, tool.current_life_hours, tool.current_work_center_id) == (
            ToolStatus.AVAILABLE,
            10,
            None,
        )

    def test_log_usage_against_another_tenants_work_center_is_404(self, client: TestClient, db_session: Session):
        tool = self._own_tool(db_session)
        foreign_wc = make_work_center(db_session, company_id=COMPANY_A)
        headers = headers_for(db_session, company_id=COMPANY_B)
        ledger_before, audit_before = ledger_counts(db_session), audit_count(db_session)

        response = client.post(
            f"{TOOLS_URL}/{tool.id}/log-usage",
            json={"usage_hours": 3, "work_center_id": foreign_wc.id},
            headers=headers,
        )

        assert response.status_code == 404, response.text
        self._assert_untouched(db_session, tool, ledger_before, audit_before)

    @pytest.mark.parametrize("verb", ["checkout", "log-usage"])
    def test_another_tenants_work_order_is_404(self, client: TestClient, db_session: Session, verb: str):
        tool = self._own_tool(db_session)
        foreign_wo, _ = make_wo_with_operation(
            db_session, company_id=COMPANY_A, work_center=make_work_center(db_session, company_id=COMPANY_A)
        )
        headers = headers_for(db_session, company_id=COMPANY_B)
        ledger_before, audit_before = ledger_counts(db_session), audit_count(db_session)

        response = client.post(
            f"{TOOLS_URL}/{tool.id}/{verb}", json={"usage_hours": 3, "work_order_id": foreign_wo.id}, headers=headers
        )

        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "Work order not found"
        self._assert_untouched(db_session, tool, ledger_before, audit_before)

    @pytest.mark.parametrize("verb", ["checkout", "log-usage"])
    def test_an_own_soft_deleted_work_order_is_refused(self, client: TestClient, db_session: Session, verb: str):
        tool = self._own_tool(db_session)
        own_wc = make_work_center(db_session, company_id=COMPANY_B)
        own_wo, _ = make_wo_with_operation(db_session, company_id=COMPANY_B, work_center=own_wc)
        own_wo.soft_delete(make_user(db_session, company_id=COMPANY_B, role=UserRole.ADMIN).id)
        db_session.commit()
        headers = headers_for(db_session, company_id=COMPANY_B)
        ledger_before, audit_before = ledger_counts(db_session), audit_count(db_session)

        response = client.post(
            f"{TOOLS_URL}/{tool.id}/{verb}", json={"usage_hours": 3, "work_order_id": own_wo.id}, headers=headers
        )

        assert response.status_code == 404, response.text
        self._assert_untouched(db_session, tool, ledger_before, audit_before)

    def test_an_own_live_work_order_and_work_center_are_accepted(self, client: TestClient, db_session: Session):
        # The positive control for the three refusals above: same shapes, own live rows.
        tool = self._own_tool(db_session)
        own_wc = make_work_center(db_session, company_id=COMPANY_B)
        own_wo, _ = make_wo_with_operation(db_session, company_id=COMPANY_B, work_center=own_wc)
        headers = headers_for(db_session, company_id=COMPANY_B)

        response = client.post(
            f"{TOOLS_URL}/{tool.id}/checkout",
            json={"work_center_id": own_wc.id, "work_order_id": own_wo.id},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        checkout = db_session.query(ToolCheckout).filter(ToolCheckout.id == response.json()["id"]).one()
        assert (checkout.company_id, checkout.work_center_id, checkout.work_order_id) == (
            COMPANY_B,
            own_wc.id,
            own_wo.id,
        )


class TestToolCodeUniqueness:
    def test_a_tool_id_another_tenant_uses_is_accepted_per_company(self, client: TestClient, db_session: Session):
        # ``tool_id`` is unique per company (migration 116): another tenant's identical tool
        # tag is no longer refused, and refusing it would disclose that it exists.
        taken = make_tool(db_session, company_id=COMPANY_A)
        ensure_company(db_session, COMPANY_B)
        headers = headers_for(db_session, company_id=COMPANY_B, role=UserRole.MANAGER)
        audit_before = audit_count(db_session)

        response = client.post(f"{TOOLS_URL}/", json={"tool_id": taken.tool_id, "name": "Mine"}, headers=headers)

        assert response.status_code == 200, response.text
        mine = db_session.query(Tool).filter(Tool.company_id == COMPANY_B).one()
        assert (mine.tool_id, mine.name) == (taken.tool_id, "Mine")
        assert audit_count(db_session) == audit_before + 1

    def test_a_same_company_race_past_the_probe_is_400_not_500_and_writes_nothing(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        from app.api.endpoints import tool_management as tool_endpoint
        from tests.api.test_business_keys_per_company import probe_misses_once

        taken = make_tool(db_session, company_id=COMPANY_A)
        headers = headers_for(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
        audit_before = audit_count(db_session)
        probe_misses_once(monkeypatch, tool_endpoint, Tool)

        response = client.post(f"{TOOLS_URL}/", json={"tool_id": taken.tool_id, "name": "Mine"}, headers=headers)

        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "Tool ID already exists"
        assert db_session.query(Tool).filter(Tool.tool_id == taken.tool_id).count() == 1
        assert audit_count(db_session) == audit_before
        follow_up = client.post(
            f"{TOOLS_URL}/", json={"tool_id": f"{taken.tool_id}-B", "name": "Mine"}, headers=headers
        )
        assert follow_up.status_code == 200, follow_up.text
