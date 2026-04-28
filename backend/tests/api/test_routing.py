import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.part import Part
from app.models.routing import Routing, RoutingOperation
from app.models.work_center import WorkCenter


@pytest.mark.api
@pytest.mark.requires_db
class TestRoutingAPI:
    def test_get_routing_by_part_is_company_scoped(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
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
