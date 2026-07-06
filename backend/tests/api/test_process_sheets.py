"""Process Sheets library API tests (PR 1 of docs/PROCESS_SHEETS_SCOPE.md).

Covers the lifecycle (draft-only mutation, release, obsolete, new-revision), the
per-type step-definition validation matrix, RBAC (author vs release roles), soft
delete, tenant isolation, sheet numbering, audit rows, and the routing-operation
attach validation (released same-company sheets only).
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.part import Part
from app.models.process_sheet import ProcessSheet
from app.models.routing import Routing, RoutingOperation
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens are minted directly; never used for login

BASE = "/api/v1/process-sheets"


def _create_sheet(client: TestClient, headers: dict, title: str = "Deburr and final inspect") -> dict:
    response = client.post(f"{BASE}/", json={"title": title}, headers=headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()


def _add_measurement_step(client: TestClient, headers: dict, sheet_id: int, sequence: int = 10) -> dict:
    response = client.post(
        f"{BASE}/{sheet_id}/steps",
        json={
            "sequence": sequence,
            "label": "Bore diameter",
            "step_type": "measurement",
            "config": {"lsl": 0.98, "nominal": 1.0, "usl": 1.02, "unit": "in"},
            "requires_gauge": True,
        },
        headers=headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()


def _make_company2_headers(db_session: Session) -> dict:
    company = db_session.query(Company).filter(Company.id == 2).first()
    if not company:
        company = Company(id=2, name="Other Co", slug="other-co", is_active=True)
        db_session.add(company)
        db_session.commit()
    user = User(
        email="manager2@other.com",
        employee_id="EMP-C2-001",
        first_name="Other",
        last_name="Manager",
        hashed_password=TEST_PASSWORD_HASH,
        role=UserRole.MANAGER,
        is_active=True,
        company_id=2,
    )
    db_session.add(user)
    db_session.commit()
    token = create_access_token(subject=user.id, company_id=2)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _make_routing(db_session: Session, company_id: int = 1) -> tuple[Routing, WorkCenter]:
    part = Part(
        part_number=f"PS-ATTACH-{company_id}",
        name="Attach Part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    work_center = WorkCenter(
        code=f"WC-PS-{company_id}",
        name="PS Work Center",
        work_center_type="machining",
        is_active=True,
        company_id=company_id,
    )
    db_session.add_all([part, work_center])
    db_session.flush()
    routing = Routing(part_id=part.id, revision="A", status="draft", is_active=True, company_id=company_id)
    db_session.add(routing)
    db_session.commit()
    return routing, work_center


class TestProcessSheetLifecycle:
    def test_create_get_and_numbering(self, client: TestClient, auth_headers: dict, db_session: Session):
        first = _create_sheet(client, auth_headers, title="Sheet one")
        second = _create_sheet(client, auth_headers, title="Sheet two")

        assert first["sheet_number"] == "PS-000001"
        assert second["sheet_number"] == "PS-000002"
        assert first["status"] == "draft"
        assert first["revision"] == "A"

        detail = client.get(f"{BASE}/{first['id']}", headers=auth_headers)
        assert detail.status_code == status.HTTP_200_OK
        assert detail.json()["steps"] == []

        listing = client.get(f"{BASE}/", headers=auth_headers)
        assert listing.status_code == status.HTTP_200_OK
        assert {row["sheet_number"] for row in listing.json()} == {"PS-000001", "PS-000002"}

        # Tamper-evident audit row written through AuditService
        audit_row = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "process_sheet", AuditLog.resource_id == first["id"])
            .first()
        )
        assert audit_row is not None
        assert audit_row.action == "CREATE"
        assert audit_row.company_id == 1

    def test_release_requires_steps_then_locks_editing(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        sheet = _create_sheet(client, auth_headers)

        empty_release = client.post(f"{BASE}/{sheet['id']}/release", headers=auth_headers)
        assert empty_release.status_code == status.HTTP_400_BAD_REQUEST

        step = _add_measurement_step(client, auth_headers, sheet["id"])

        released = client.post(f"{BASE}/{sheet['id']}/release", headers=auth_headers)
        assert released.status_code == status.HTTP_200_OK
        body = released.json()
        assert body["status"] == "released"
        assert body["effective_date"] is not None

        # Every mutation on a non-DRAFT sheet is a 409 with a clear detail
        assert (
            client.patch(f"{BASE}/{sheet['id']}", json={"title": "New"}, headers=auth_headers).status_code
            == status.HTTP_409_CONFLICT
        )
        assert (
            client.post(
                f"{BASE}/{sheet['id']}/steps",
                json={"sequence": 20, "label": "x", "step_type": "checkbox"},
                headers=auth_headers,
            ).status_code
            == status.HTTP_409_CONFLICT
        )
        assert (
            client.patch(
                f"{BASE}/{sheet['id']}/steps/{step['id']}", json={"label": "y"}, headers=auth_headers
            ).status_code
            == status.HTTP_409_CONFLICT
        )
        assert (
            client.delete(f"{BASE}/{sheet['id']}/steps/{step['id']}", headers=auth_headers).status_code
            == status.HTTP_409_CONFLICT
        )
        assert client.delete(f"{BASE}/{sheet['id']}", headers=auth_headers).status_code == status.HTTP_409_CONFLICT

        status_change = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "process_sheet",
                AuditLog.resource_id == sheet["id"],
                AuditLog.action == "STATUS_CHANGE",
            )
            .first()
        )
        assert status_change is not None

    def test_new_revision_copies_steps_and_obsolete(self, client: TestClient, auth_headers: dict):
        sheet = _create_sheet(client, auth_headers)
        _add_measurement_step(client, auth_headers, sheet["id"], sequence=10)
        client.post(
            f"{BASE}/{sheet['id']}/steps",
            json={"sequence": 20, "label": "Read drawing note 4", "step_type": "instruction"},
            headers=auth_headers,
        )
        assert client.post(f"{BASE}/{sheet['id']}/release", headers=auth_headers).status_code == status.HTTP_200_OK

        # revising a draft is a 409 (edit it instead) — checked via a fresh draft
        draft = _create_sheet(client, auth_headers, title="Still a draft")
        assert (
            client.post(f"{BASE}/{draft['id']}/new-revision", headers=auth_headers).status_code
            == status.HTTP_409_CONFLICT
        )

        revised = client.post(f"{BASE}/{sheet['id']}/new-revision", headers=auth_headers)
        assert revised.status_code == status.HTTP_200_OK
        rev_b = revised.json()
        assert rev_b["revision"] == "B"
        assert rev_b["status"] == "draft"
        assert rev_b["sheet_number"] == sheet["sheet_number"]
        assert len(rev_b["steps"]) == 2

        # A second new-revision while the B draft exists is a 409
        assert (
            client.post(f"{BASE}/{sheet['id']}/new-revision", headers=auth_headers).status_code
            == status.HTTP_409_CONFLICT
        )

        obsoleted = client.post(f"{BASE}/{sheet['id']}/obsolete", headers=auth_headers)
        assert obsoleted.status_code == status.HTTP_200_OK
        assert obsoleted.json()["status"] == "obsolete"
        assert obsoleted.json()["obsolete_date"] is not None

        # obsolete only applies to released sheets
        assert (
            client.post(f"{BASE}/{rev_b['id']}/obsolete", headers=auth_headers).status_code == status.HTTP_409_CONFLICT
        )

    def test_soft_delete_draft_only(self, client: TestClient, auth_headers: dict, db_session: Session):
        sheet = _create_sheet(client, auth_headers)
        assert client.delete(f"{BASE}/{sheet['id']}", headers=auth_headers).status_code == status.HTTP_200_OK
        assert client.get(f"{BASE}/{sheet['id']}", headers=auth_headers).status_code == status.HTTP_404_NOT_FOUND

        # Soft delete, never physical: the row survives with is_deleted set
        row = db_session.query(ProcessSheet).filter(ProcessSheet.id == sheet["id"]).first()
        assert row is not None
        assert row.is_deleted is True
        assert row.deleted_by is not None

    def test_sheet_numbers_not_reused_after_soft_delete(self, client: TestClient, auth_headers: dict):
        _create_sheet(client, auth_headers, title="First")  # PS-000001
        second = _create_sheet(client, auth_headers, title="Second")  # PS-000002
        assert client.delete(f"{BASE}/{second['id']}", headers=auth_headers).status_code == status.HTTP_200_OK

        third = _create_sheet(client, auth_headers, title="Third")
        assert third["sheet_number"] == "PS-000003"  # a soft-deleted number is never reissued

    def test_revision_letters_roll_over_z_to_aa(self, client: TestClient, auth_headers: dict, db_session: Session):
        sheet = _create_sheet(client, auth_headers)
        _add_measurement_step(client, auth_headers, sheet["id"])
        assert client.post(f"{BASE}/{sheet['id']}/release", headers=auth_headers).status_code == status.HTTP_200_OK

        # Fast-forward the released row to Rev Z (walking 25 release/revise cycles adds nothing).
        row = db_session.query(ProcessSheet).filter(ProcessSheet.id == sheet["id"]).first()
        row.revision = "Z"
        db_session.commit()

        revised = client.post(f"{BASE}/{sheet['id']}/new-revision", headers=auth_headers)
        assert revised.status_code == status.HTTP_200_OK, revised.text
        rev_aa = revised.json()
        assert rev_aa["revision"] == "AA"
        assert rev_aa["status"] == "draft"
        assert rev_aa["sheet_number"] == sheet["sheet_number"]
        assert len(rev_aa["steps"]) == 1

    def test_update_delete_and_step_mutations_write_audit_rows(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        sheet = _create_sheet(client, auth_headers)
        step = _add_measurement_step(client, auth_headers, sheet["id"])

        assert (
            client.patch(f"{BASE}/{sheet['id']}", json={"title": "Renamed"}, headers=auth_headers).status_code
            == status.HTTP_200_OK
        )
        assert (
            client.patch(
                f"{BASE}/{sheet['id']}/steps/{step['id']}", json={"label": "Bore dia (pin gauge)"}, headers=auth_headers
            ).status_code
            == status.HTTP_200_OK
        )
        assert (
            client.delete(f"{BASE}/{sheet['id']}/steps/{step['id']}", headers=auth_headers).status_code
            == status.HTTP_200_OK
        )
        assert client.delete(f"{BASE}/{sheet['id']}", headers=auth_headers).status_code == status.HTTP_200_OK

        def actions(resource_type: str, resource_id: int) -> set:
            rows = (
                db_session.query(AuditLog)
                .filter(AuditLog.resource_type == resource_type, AuditLog.resource_id == resource_id)
                .all()
            )
            return {row.action for row in rows}

        assert {"CREATE", "UPDATE", "DELETE"} <= actions("process_sheet", sheet["id"])
        assert {"CREATE", "UPDATE", "DELETE"} <= actions("process_sheet_step", step["id"])


class TestListFiltersAndPagination:
    def test_status_search_filters_and_soft_delete_exclusion(self, client: TestClient, auth_headers: dict):
        released = _create_sheet(client, auth_headers, title="Anodize rack check")
        _add_measurement_step(client, auth_headers, released["id"])
        assert client.post(f"{BASE}/{released['id']}/release", headers=auth_headers).status_code == status.HTTP_200_OK
        draft = _create_sheet(client, auth_headers, title="Deburr bench check")
        ghost = _create_sheet(client, auth_headers, title="Ghost sheet")
        assert client.delete(f"{BASE}/{ghost['id']}", headers=auth_headers).status_code == status.HTTP_200_OK

        def rows(query: str) -> list:
            response = client.get(f"{BASE}/{query}", headers=auth_headers)
            assert response.status_code == status.HTTP_200_OK, response.text
            return response.json()

        # Unfiltered: newest sheet number first, soft-deleted rows never listed
        assert [r["id"] for r in rows("")] == [draft["id"], released["id"]]

        assert [r["id"] for r in rows("?status=released")] == [released["id"]]
        assert [r["id"] for r in rows("?status=draft")] == [draft["id"]]
        assert rows("?status=released")[0]["step_count"] == 1

        assert [r["id"] for r in rows("?search=deburr")] == [draft["id"]]  # title match, case-insensitive
        assert [r["id"] for r in rows(f"?search={released['sheet_number']}")] == [released["id"]]  # number match
        assert rows("?search=Ghost") == []  # deleted rows are unsearchable too

    def test_pagination_and_status_enum_validation(self, client: TestClient, auth_headers: dict):
        first = _create_sheet(client, auth_headers, title="Sheet one")
        second = _create_sheet(client, auth_headers, title="Sheet two")

        page_one = client.get(f"{BASE}/?limit=1", headers=auth_headers).json()
        page_two = client.get(f"{BASE}/?skip=1&limit=1", headers=auth_headers).json()
        assert [row["id"] for row in page_one] == [second["id"]]  # PS-000002 sorts first (desc)
        assert [row["id"] for row in page_two] == [first["id"]]

        assert client.get(f"{BASE}/?status=bogus", headers=auth_headers).status_code == 422
        assert client.get(f"{BASE}/?limit=0", headers=auth_headers).status_code == 422
        assert client.get(f"{BASE}/?skip=-1", headers=auth_headers).status_code == 422


class TestStepValidation:
    @pytest.mark.parametrize(
        "config",
        [
            None,
            {"lsl": 1, "usl": 2},  # missing nominal
            {"lsl": "1", "nominal": 1.5, "usl": 2},  # non-numeric
            {"lsl": 2, "nominal": 1, "usl": 3},  # nominal outside lsl..usl
            {"lsl": 2, "nominal": 2, "usl": 2},  # lsl not < usl
        ],
    )
    def test_measurement_config_rejected(self, client: TestClient, auth_headers: dict, config):
        sheet = _create_sheet(client, auth_headers)
        response = client.post(
            f"{BASE}/{sheet['id']}/steps",
            json={"sequence": 10, "label": "OD", "step_type": "measurement", "config": config},
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text

    def test_requires_gauge_only_on_measurement(self, client: TestClient, auth_headers: dict):
        sheet = _create_sheet(client, auth_headers)
        response = client.post(
            f"{BASE}/{sheet['id']}/steps",
            json={"sequence": 10, "label": "Visual check", "step_type": "checkbox", "requires_gauge": True},
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_list_requires_nonempty_options(self, client: TestClient, auth_headers: dict):
        sheet = _create_sheet(client, auth_headers)
        bad = client.post(
            f"{BASE}/{sheet['id']}/steps",
            json={"sequence": 10, "label": "Finish", "step_type": "list", "config": {"options": []}},
            headers=auth_headers,
        )
        assert bad.status_code == status.HTTP_400_BAD_REQUEST
        good = client.post(
            f"{BASE}/{sheet['id']}/steps",
            json={"sequence": 10, "label": "Finish", "step_type": "list", "config": {"options": ["anodize"]}},
            headers=auth_headers,
        )
        assert good.status_code == status.HTTP_200_OK

    def test_instruction_never_required(self, client: TestClient, auth_headers: dict):
        sheet = _create_sheet(client, auth_headers)
        response = client.post(
            f"{BASE}/{sheet['id']}/steps",
            json={"sequence": 10, "label": "Read note", "step_type": "instruction", "is_required": True},
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["is_required"] is False

    def test_update_validates_merged_definition(self, client: TestClient, auth_headers: dict):
        """Switching type away from measurement while requires_gauge stays True must fail."""
        sheet = _create_sheet(client, auth_headers)
        step = _add_measurement_step(client, auth_headers, sheet["id"])
        response = client.patch(
            f"{BASE}/{sheet['id']}/steps/{step['id']}", json={"step_type": "checkbox"}, headers=auth_headers
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_update_rejects_inverted_measurement_limits(self, client: TestClient, auth_headers: dict):
        """A config-only PATCH is validated against the merged definition, not just the delta."""
        sheet = _create_sheet(client, auth_headers)
        step = _add_measurement_step(client, auth_headers, sheet["id"])

        inverted = client.patch(
            f"{BASE}/{sheet['id']}/steps/{step['id']}",
            json={"config": {"lsl": 1.02, "nominal": 1.0, "usl": 0.98, "unit": "in"}},
            headers=auth_headers,
        )
        assert inverted.status_code == status.HTTP_400_BAD_REQUEST, inverted.text

        # The stored definition is untouched by the rejected update
        detail = client.get(f"{BASE}/{sheet['id']}", headers=auth_headers)
        assert detail.json()["steps"][0]["config"]["lsl"] == 0.98

    def test_instruction_stays_unrequired_across_updates(self, client: TestClient, auth_headers: dict):
        sheet = _create_sheet(client, auth_headers)
        instruction = client.post(
            f"{BASE}/{sheet['id']}/steps",
            json={"sequence": 10, "label": "Read drawing note 4", "step_type": "instruction", "is_required": True},
            headers=auth_headers,
        ).json()
        assert instruction["is_required"] is False

        # PATCHing is_required=true on an instruction step is silently normalized back to False
        forced = client.patch(
            f"{BASE}/{sheet['id']}/steps/{instruction['id']}", json={"is_required": True}, headers=auth_headers
        )
        assert forced.status_code == status.HTTP_200_OK
        assert forced.json()["is_required"] is False

        # Switching a required step TO instruction drops the flag as part of the merged validation
        checkbox = client.post(
            f"{BASE}/{sheet['id']}/steps",
            json={"sequence": 20, "label": "Confirm torque", "step_type": "checkbox", "is_required": True},
            headers=auth_headers,
        ).json()
        switched = client.patch(
            f"{BASE}/{sheet['id']}/steps/{checkbox['id']}", json={"step_type": "instruction"}, headers=auth_headers
        )
        assert switched.status_code == status.HTTP_200_OK
        assert switched.json()["is_required"] is False

    def test_spc_characteristic_measurement_only_and_must_exist(self, client: TestClient, auth_headers: dict):
        sheet = _create_sheet(client, auth_headers)
        wrong_type = client.post(
            f"{BASE}/{sheet['id']}/steps",
            json={"sequence": 10, "label": "v", "step_type": "value", "spc_characteristic_id": 999},
            headers=auth_headers,
        )
        assert wrong_type.status_code == status.HTTP_400_BAD_REQUEST
        missing = client.post(
            f"{BASE}/{sheet['id']}/steps",
            json={
                "sequence": 10,
                "label": "m",
                "step_type": "measurement",
                "config": {"lsl": 0, "nominal": 1, "usl": 2},
                "spc_characteristic_id": 999,
            },
            headers=auth_headers,
        )
        assert missing.status_code == status.HTTP_404_NOT_FOUND


class TestRBAC:
    def test_operator_cannot_author_but_can_read(self, client: TestClient, auth_headers: dict, operator_headers: dict):
        sheet = _create_sheet(client, auth_headers)

        denied = client.post(f"{BASE}/", json={"title": "Nope"}, headers=operator_headers)
        assert denied.status_code == status.HTTP_403_FORBIDDEN

        assert client.get(f"{BASE}/", headers=operator_headers).status_code == status.HTTP_200_OK
        assert client.get(f"{BASE}/{sheet['id']}", headers=operator_headers).status_code == status.HTTP_200_OK

    def test_supervisor_can_author_but_not_release(self, client: TestClient, auth_headers: dict, db_session: Session):
        supervisor = User(
            email="supervisor-ps@werco.com",
            employee_id="EMP-SUP-PS-001",
            first_name="Super",
            last_name="Visor",
            hashed_password=TEST_PASSWORD_HASH,
            role=UserRole.SUPERVISOR,
            is_active=True,
            company_id=1,
        )
        db_session.add(supervisor)
        db_session.commit()
        token = create_access_token(subject=supervisor.id, company_id=1)
        supervisor_headers = {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}

        sheet = _create_sheet(client, supervisor_headers)
        _add_measurement_step(client, supervisor_headers, sheet["id"])

        denied = client.post(f"{BASE}/{sheet['id']}/release", headers=supervisor_headers)
        assert denied.status_code == status.HTTP_403_FORBIDDEN

        allowed = client.post(f"{BASE}/{sheet['id']}/release", headers=auth_headers)  # manager
        assert allowed.status_code == status.HTTP_200_OK


class TestTenantIsolation:
    def test_sheets_invisible_across_companies_and_numbering_is_per_company(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        sheet = _create_sheet(client, auth_headers)
        company2_headers = _make_company2_headers(db_session)

        assert client.get(f"{BASE}/{sheet['id']}", headers=company2_headers).status_code == status.HTTP_404_NOT_FOUND
        assert client.get(f"{BASE}/", headers=company2_headers).json() == []
        assert (
            client.patch(f"{BASE}/{sheet['id']}", json={"title": "steal"}, headers=company2_headers).status_code
            == status.HTTP_404_NOT_FOUND
        )
        assert (
            client.post(f"{BASE}/{sheet['id']}/release", headers=company2_headers).status_code
            == status.HTTP_404_NOT_FOUND
        )

        other = _create_sheet(client, company2_headers, title="Company 2 sheet")
        assert other["sheet_number"] == "PS-000001"  # numbering restarts per company

    def test_step_and_lifecycle_writes_404_across_companies(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        sheet = _create_sheet(client, auth_headers)
        step = _add_measurement_step(client, auth_headers, sheet["id"])
        company2_headers = _make_company2_headers(db_session)

        step_payload = {"sequence": 20, "label": "steal", "step_type": "checkbox"}
        assert (
            client.post(f"{BASE}/{sheet['id']}/steps", json=step_payload, headers=company2_headers).status_code
            == status.HTTP_404_NOT_FOUND
        )
        assert (
            client.patch(
                f"{BASE}/{sheet['id']}/steps/{step['id']}", json={"label": "steal"}, headers=company2_headers
            ).status_code
            == status.HTTP_404_NOT_FOUND
        )
        assert (
            client.delete(f"{BASE}/{sheet['id']}/steps/{step['id']}", headers=company2_headers).status_code
            == status.HTTP_404_NOT_FOUND
        )
        assert client.delete(f"{BASE}/{sheet['id']}", headers=company2_headers).status_code == status.HTTP_404_NOT_FOUND
        assert (
            client.post(f"{BASE}/{sheet['id']}/new-revision", headers=company2_headers).status_code
            == status.HTTP_404_NOT_FOUND
        )


class TestRoutingAttach:
    def _make_routing(self, db_session: Session, company_id: int = 1) -> tuple[Routing, WorkCenter]:
        return _make_routing(db_session, company_id)

    def _operation_payload(self, work_center_id: int, process_sheet_id: int) -> dict:
        return {
            "sequence": 10,
            "name": "Mill",
            "work_center_id": work_center_id,
            "process_sheet_id": process_sheet_id,
        }

    def test_attach_requires_released_same_company_sheet(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        routing, work_center = self._make_routing(db_session)
        sheet = _create_sheet(client, auth_headers)

        draft_attach = client.post(
            f"/api/v1/routing/{routing.id}/operations",
            json=self._operation_payload(work_center.id, sheet["id"]),
            headers=auth_headers,
        )
        assert draft_attach.status_code == status.HTTP_409_CONFLICT  # draft sheet can't reach a traveler

        _add_measurement_step(client, auth_headers, sheet["id"])
        assert client.post(f"{BASE}/{sheet['id']}/release", headers=auth_headers).status_code == status.HTTP_200_OK

        released_attach = client.post(
            f"/api/v1/routing/{routing.id}/operations",
            json=self._operation_payload(work_center.id, sheet["id"]),
            headers=auth_headers,
        )
        assert released_attach.status_code == status.HTTP_200_OK, released_attach.text
        operation = released_attach.json()
        assert operation["process_sheet_id"] == sheet["id"]

        # Detach via explicit null on update
        detached = client.put(
            f"/api/v1/routing/{routing.id}/operations/{operation['id']}",
            json={"process_sheet_id": None},
            headers=auth_headers,
        )
        assert detached.status_code == status.HTTP_200_OK
        assert detached.json()["process_sheet_id"] is None

    def test_attach_rejects_cross_company_sheet(self, client: TestClient, auth_headers: dict, db_session: Session):
        company2_headers = _make_company2_headers(db_session)
        foreign_sheet = _create_sheet(client, company2_headers, title="Foreign")
        _add_measurement_step(client, company2_headers, foreign_sheet["id"])
        assert (
            client.post(f"{BASE}/{foreign_sheet['id']}/release", headers=company2_headers).status_code
            == status.HTTP_200_OK
        )

        routing, work_center = self._make_routing(db_session)
        response = client.post(
            f"/api/v1/routing/{routing.id}/operations",
            json=self._operation_payload(work_center.id, foreign_sheet["id"]),
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND  # tenant miss, even though it is released

    def test_attach_soft_deleted_is_404_and_obsolete_is_409(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        routing, work_center = self._make_routing(db_session)

        deleted = _create_sheet(client, auth_headers, title="Deleted draft")
        assert client.delete(f"{BASE}/{deleted['id']}", headers=auth_headers).status_code == status.HTTP_200_OK
        gone = client.post(
            f"/api/v1/routing/{routing.id}/operations",
            json=self._operation_payload(work_center.id, deleted["id"]),
            headers=auth_headers,
        )
        assert gone.status_code == status.HTTP_404_NOT_FOUND  # soft-deleted is invisible, not a status conflict

        missing = client.post(
            f"/api/v1/routing/{routing.id}/operations",
            json=self._operation_payload(work_center.id, 999999),
            headers=auth_headers,
        )
        assert missing.status_code == status.HTTP_404_NOT_FOUND

        obsolete = _create_sheet(client, auth_headers, title="Obsoleted sheet")
        _add_measurement_step(client, auth_headers, obsolete["id"])
        assert client.post(f"{BASE}/{obsolete['id']}/release", headers=auth_headers).status_code == status.HTTP_200_OK
        assert client.post(f"{BASE}/{obsolete['id']}/obsolete", headers=auth_headers).status_code == status.HTTP_200_OK
        stale = client.post(
            f"/api/v1/routing/{routing.id}/operations",
            json=self._operation_payload(work_center.id, obsolete["id"]),
            headers=auth_headers,
        )
        assert stale.status_code == status.HTTP_409_CONFLICT  # only RELEASED content may reach a traveler

    def test_copy_routing_carries_attached_sheet(self, client: TestClient, auth_headers: dict, db_session: Session):
        routing, work_center = self._make_routing(db_session)
        sheet = _create_sheet(client, auth_headers)
        _add_measurement_step(client, auth_headers, sheet["id"])
        assert client.post(f"{BASE}/{sheet['id']}/release", headers=auth_headers).status_code == status.HTTP_200_OK
        attached = client.post(
            f"/api/v1/routing/{routing.id}/operations",
            json=self._operation_payload(work_center.id, sheet["id"]),
            headers=auth_headers,
        )
        assert attached.status_code == status.HTTP_200_OK, attached.text

        target = Part(
            part_number="PS-COPY-TARGET",
            name="Copy Target",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        db_session.add(target)
        db_session.commit()

        copied = client.post(
            f"/api/v1/routing/{routing.id}/copy?target_part_id={target.id}&new_revision=B",
            headers=auth_headers,
        )
        assert copied.status_code == status.HTTP_200_OK, copied.text
        new_routing_id = copied.json()["new_routing_id"]

        copied_op = db_session.query(RoutingOperation).filter(RoutingOperation.routing_id == new_routing_id).one()
        assert copied_op.process_sheet_id == sheet["id"]

    def test_released_routing_rejects_sheet_change_as_structural(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        routing, work_center = self._make_routing(db_session)
        bare = client.post(
            f"/api/v1/routing/{routing.id}/operations",
            json={"sequence": 10, "name": "Mill", "work_center_id": work_center.id},
            headers=auth_headers,
        )
        assert bare.status_code == status.HTTP_200_OK, bare.text
        operation_id = bare.json()["id"]
        assert (
            client.post(f"/api/v1/routing/{routing.id}/release", headers=auth_headers).status_code == status.HTTP_200_OK
        )

        sheet = _create_sheet(client, auth_headers)
        _add_measurement_step(client, auth_headers, sheet["id"])
        assert client.post(f"{BASE}/{sheet['id']}/release", headers=auth_headers).status_code == status.HTTP_200_OK

        # Both the routing and the sheet are RELEASED — the 400 must come from the
        # structural-field gate (process_sheet_id is not a time standard), not sheet validation.
        response = client.put(
            f"/api/v1/routing/{routing.id}/operations/{operation_id}",
            json={"process_sheet_id": sheet["id"]},
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "time standards" in response.json()["detail"]


class TestRoutingOperationAuditPersistence:
    """The CREATE audit row for a routing operation must COMMIT with the operation.

    AuditService.log() only flushes, and production get_db teardown closes the session,
    rolling back any transaction left open after the endpoint's terminal commit — so an
    audit call placed AFTER db.commit() is silently discarded. Same technique as
    tests/api/test_customers_audit_persistence.py: db.rollback() before reading discards
    any flushed-but-uncommitted audit rows, exactly like the production teardown would.
    """

    def test_add_operation_create_audit_row_is_committed(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        routing, work_center = _make_routing(db_session)

        response = client.post(
            f"/api/v1/routing/{routing.id}/operations",
            json={"sequence": 10, "name": "Mill", "work_center_id": work_center.id},
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        operation_id = response.json()["id"]

        # Simulate the production request teardown: roll back anything the endpoint
        # left flushed but uncommitted after its terminal commit.
        db_session.rollback()

        row = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "routing_operation",
                AuditLog.resource_id == operation_id,
                AuditLog.action == "CREATE",
            )
            .first()
        )
        assert row is not None, "routing_operation CREATE audit row was not committed with the operation"
        assert row.company_id == 1


class TestUnchangedAttachEchoNotRevalidated:
    def test_full_payload_echo_of_stale_sheet_keeps_attach_and_applies_edit(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        """A PUT echoing the CURRENT process_sheet_id must not re-validate the sheet.

        Once an attached sheet is later obsoleted (or soft-deleted), full-payload edits
        that merely echo the existing attach — e.g. a legitimate rename or time-standards
        edit — must still succeed; only an actual CHANGE of process_sheet_id validates.
        """
        routing, work_center = _make_routing(db_session)
        sheet = _create_sheet(client, auth_headers)
        _add_measurement_step(client, auth_headers, sheet["id"])
        assert client.post(f"{BASE}/{sheet['id']}/release", headers=auth_headers).status_code == status.HTTP_200_OK

        attached = client.post(
            f"/api/v1/routing/{routing.id}/operations",
            json={"sequence": 10, "name": "Mill", "work_center_id": work_center.id, "process_sheet_id": sheet["id"]},
            headers=auth_headers,
        )
        assert attached.status_code == status.HTTP_200_OK, attached.text
        operation_id = attached.json()["id"]

        # The sheet later leaves RELEASED state; already-attached operations keep it.
        assert client.post(f"{BASE}/{sheet['id']}/obsolete", headers=auth_headers).status_code == status.HTTP_200_OK

        response = client.put(
            f"/api/v1/routing/{routing.id}/operations/{operation_id}",
            json={"name": "Mill finish pass", "process_sheet_id": sheet["id"]},
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["name"] == "Mill finish pass"
        assert response.json()["process_sheet_id"] == sheet["id"]  # attach untouched

        # An actual CHANGE still validates: a draft target sheet is refused.
        draft_target = _create_sheet(client, auth_headers, title="Draft target")
        change = client.put(
            f"/api/v1/routing/{routing.id}/operations/{operation_id}",
            json={"process_sheet_id": draft_target["id"]},
            headers=auth_headers,
        )
        assert change.status_code == status.HTTP_409_CONFLICT


class TestExplicitNullRejection:
    """Explicit JSON null on fields backing NOT NULL columns must be a clean 422.

    The Optional[...] on the update schemas means "may be omitted" (PATCH semantics),
    not "may be null" — without rejection, an explicit null survives exclude_unset and
    setattr()s None onto a NOT NULL column (IntegrityError 500), or flows None into the
    step-type validator producing a misleading 400.
    """

    def test_patch_sheet_null_title_is_422(self, client: TestClient, auth_headers: dict):
        sheet = _create_sheet(client, auth_headers)
        response = client.patch(f"{BASE}/{sheet['id']}", json={"title": None}, headers=auth_headers)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.text

    @pytest.mark.parametrize(
        "payload",
        [
            {"step_type": None},
            {"label": None},
            {"sequence": None},
            {"is_required": None},
            {"requires_gauge": None},
        ],
    )
    def test_patch_step_null_non_nullable_field_is_422(self, client: TestClient, auth_headers: dict, payload: dict):
        sheet = _create_sheet(client, auth_headers)
        step = _add_measurement_step(client, auth_headers, sheet["id"])
        response = client.patch(f"{BASE}/{sheet['id']}/steps/{step['id']}", json=payload, headers=auth_headers)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.text

    def test_patch_step_null_on_nullable_field_still_clears(self, client: TestClient, auth_headers: dict):
        """instruction_text maps to a nullable column — explicit null legitimately clears it."""
        sheet = _create_sheet(client, auth_headers)
        step = client.post(
            f"{BASE}/{sheet['id']}/steps",
            json={"sequence": 10, "label": "Torque check", "step_type": "value", "instruction_text": "Wrench 12"},
            headers=auth_headers,
        ).json()

        response = client.patch(
            f"{BASE}/{sheet['id']}/steps/{step['id']}", json={"instruction_text": None}, headers=auth_headers
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["instruction_text"] is None
