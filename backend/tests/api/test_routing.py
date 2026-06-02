import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.bom import BOM, BOMItem
from app.models.part import Part
from app.models.routing import Routing, RoutingOperation
from app.models.routing_learning import RoutingGenerationSession, RoutingLearnedAlias
from app.models.work_center import WorkCenter
from app.services.routing_learning_service import create_generation_session


@pytest.mark.api
@pytest.mark.requires_db
class TestRoutingAPI:
    def test_get_routing_by_part_is_company_scoped(self, client: TestClient, auth_headers: dict, db_session: Session):
        """Routing by part should not crash when tenant-scoped filters are applied."""
        part = Part(
            part_number="ROUTE-PART-001",
            name="Routing Part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="WC-ROUTE-001",
            name="Routing Work Center",
            work_center_type="machining",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, work_center])
        db_session.flush()

        routing = Routing(
            part_id=part.id,
            revision="A",
            status="released",
            is_active=True,
            company_id=1,
        )
        db_session.add(routing)
        db_session.flush()
        db_session.add(
            RoutingOperation(
                routing_id=routing.id,
                sequence=10,
                operation_number="Op 10",
                name="Machine Part",
                work_center_id=work_center.id,
                setup_hours=0,
                run_hours_per_unit=0.1,
                is_active=True,
                company_id=1,
            )
        )
        db_session.commit()

        response = client.get(f"/api/v1/routing/by-part/{part.id}", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["part_id"] == part.id
        assert data["operations"][0]["name"] == "Machine Part"

    def test_list_routings_hides_bom_components_by_default(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        """Routing list should show parent assemblies, not their BOM component parts."""
        assembly = Part(
            part_number="ASM-ROUTE-001",
            name="Routing Assembly",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        component = Part(
            part_number="COMP-ROUTE-001",
            name="Routing Component",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([assembly, component])
        db_session.flush()

        bom = BOM(part_id=assembly.id, revision="A", status="released", is_active=True, company_id=1)
        db_session.add(bom)
        db_session.flush()
        db_session.add(
            BOMItem(
                bom_id=bom.id,
                component_part_id=component.id,
                item_number=10,
                quantity=1,
                item_type="make",
                line_type="component",
                company_id=1,
            )
        )
        db_session.add_all(
            [
                Routing(part_id=assembly.id, revision="A", status="released", is_active=True, company_id=1),
                Routing(part_id=component.id, revision="A", status="released", is_active=True, company_id=1),
            ]
        )
        db_session.commit()

        response = client.get("/api/v1/routing/", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        part_ids = {routing["part_id"] for routing in response.json()}
        assert assembly.id in part_ids
        assert component.id not in part_ids

        include_response = client.get(
            "/api/v1/routing/",
            params={"include_bom_components": True},
            headers=auth_headers,
        )

        assert include_response.status_code == status.HTTP_200_OK
        included_part_ids = {routing["part_id"] for routing in include_response.json()}
        assert component.id in included_part_ids

    def test_create_from_generation_records_learning_feedback(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        part = Part(
            part_number="ROUTE-LEARN-001",
            name="Routing Learn Part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="BRK-ROUTE-LEARN",
            name="Brake Learn",
            work_center_type="press_brake",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, work_center])
        db_session.flush()

        generation_session = create_generation_session(
            db_session,
            company_id=1,
            part_id=part.id,
            created_by=None,
            file_name="learned-routing.dxf",
            file_type="dxf",
            file_size=100,
            file_path="uploads/routing_generation/learned-routing.dxf",
            drawing_text="Bracket with crease flange.",
            geometry={"cut_length": 30.0, "bend_count": 1},
            drawing_info={"material": "Aluminum", "thickness": "0.125in"},
            proposed_operations=[
                {
                    "sequence": 10,
                    "operation_name": "Crease Flange",
                    "work_center_type": "crease",
                    "work_center_id": None,
                }
            ],
            warnings=[],
            extraction_confidence="medium",
            source_was_ocr=False,
        )
        db_session.commit()

        response = client.post(
            "/api/v1/routing/create-from-generation",
            headers=auth_headers,
            json={
                "part_id": part.id,
                "generation_session_id": generation_session.id,
                "revision": "A",
                "description": "Approved learned routing",
                "operations": [
                    {
                        "sequence": 10,
                        "name": "Form Flange",
                        "description": "Approved operation",
                        "work_center_id": work_center.id,
                        "setup_hours": 0.1,
                        "run_hours_per_unit": 0.05,
                        "move_hours": 0,
                        "queue_hours": 0,
                        "is_inspection_point": False,
                        "is_outside_operation": False,
                        "work_instructions": "Verify bend direction.",
                    }
                ],
            },
        )

        assert response.status_code == status.HTTP_200_OK
        db_session.expire_all()
        learned_session = db_session.get(RoutingGenerationSession, generation_session.id)
        learned_alias = (
            db_session.query(RoutingLearnedAlias)
            .filter(RoutingLearnedAlias.company_id == 1, RoutingLearnedAlias.alias == "crease")
            .first()
        )
        assert learned_session.status == "approved"
        assert learned_session.routing_id == response.json()["id"]
        assert learned_alias.work_center_type == "press_brake"
