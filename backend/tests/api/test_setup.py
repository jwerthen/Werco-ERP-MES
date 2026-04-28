import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.bom import BOM, BOMItem
from app.models.part import Part
from app.models.routing import Routing
from app.models.work_center import WorkCenter


@pytest.mark.api
@pytest.mark.requires_db
class TestSetupHealth:
    def test_setup_health_returns_onboarding_steps(
        self,
        client: TestClient,
        auth_headers: dict,
    ):
        response = client.get("/api/v1/setup/health", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()

        assert data["progress"] == 29
        assert data["counts"]["employees"] == 1
        assert data["counts"]["work_centers"] == 0

        steps = {step["key"]: step for step in data["steps"]}
        assert steps["employees"]["status"] == "complete"
        assert steps["work_centers"]["status"] == "missing"
        assert steps["parts"]["href"] == "/import-center?type=parts"

    def test_setup_health_excludes_bom_components_from_missing_routing_count(
        self,
        client: TestClient,
        auth_headers: dict,
        db_session: Session,
    ):
        assembly = Part(
            part_number="ASM-SETUP-001",
            name="Setup Assembly",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        component = Part(
            part_number="MFG-COMP-001",
            name="Manufactured Component",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([assembly, component])
        db_session.flush()

        bom = BOM(
            part_id=assembly.id,
            revision="A",
            status="released",
            is_active=True,
            company_id=1,
        )
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
        db_session.commit()

        response = client.get("/api/v1/setup/health", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        issues = {issue["key"]: issue for issue in data["issues"]}

        assert "assemblies_without_bom" not in issues
        assert issues["top_level_parts_without_routing"]["count"] == 1

    def test_part_readiness_explains_missing_and_draft_master_data(
        self,
        client: TestClient,
        auth_headers: dict,
        db_session: Session,
    ):
        part = Part(
            part_number="ASM-READY-001",
            name="Readiness Assembly",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        component = Part(
            part_number="BUY-READY-001",
            name="Readiness Component",
            part_type="purchased",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="WC-READY",
            name="Ready Work Center",
            work_center_type="machining",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, component, work_center])
        db_session.flush()

        bom = BOM(
            part_id=part.id,
            revision="A",
            status="draft",
            is_active=True,
            company_id=1,
        )
        routing = Routing(
            part_id=part.id,
            revision="A",
            status="draft",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([bom, routing])
        db_session.flush()
        db_session.add(
            BOMItem(
                bom_id=bom.id,
                component_part_id=component.id,
                item_number=10,
                quantity=1,
                item_type="buy",
                line_type="component",
                company_id=1,
            )
        )
        db_session.commit()

        response = client.get(f"/api/v1/setup/readiness/part/{part.id}", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()

        assert data["ready"] is True
        assert data["checks"]["routing"] == "draft"
        assert data["checks"]["bom"] == "draft"
        assert "Routing exists but is draft." in data["warnings"]
        assert "BOM exists but is draft." in data["warnings"]
