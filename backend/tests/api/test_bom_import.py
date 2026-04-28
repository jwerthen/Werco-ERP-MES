import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.bom import BOM, BOMItem
from app.models.part import Part


@pytest.mark.api
@pytest.mark.requires_db
class TestBOMImport:
    def test_commit_bom_import_creates_assembly_and_items(
        self,
        client: TestClient,
        auth_headers: dict,
        db_session: Session,
    ):
        response = client.post(
            "/api/v1/bom/import/commit",
            headers=auth_headers,
            json={
                "document_type": "bom",
                "assembly": {
                    "part_number": "ASSY-100",
                    "name": "Imported Assembly",
                    "revision": "A",
                    "part_type": "manufactured",
                },
                "items": [
                    {
                        "line_number": 10,
                        "part_number": "COMP-100",
                        "description": "Machined bracket",
                        "quantity": 2,
                        "item_type": "make",
                        "line_type": "component",
                    },
                    {
                        "line_number": 20,
                        "part_number": "BUY-100",
                        "description": "Purchased spacer",
                        "quantity": 4,
                        "item_type": "buy",
                        "line_type": "component",
                    },
                ],
                "create_missing_parts": True,
            },
        )

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["bom_id"] is not None
        assert data["created_bom_items"] == 2

        assembly = db_session.query(Part).filter(Part.part_number == "ASSY-100").one()
        assert assembly.part_type == "assembly"

        make_component = db_session.query(Part).filter(Part.part_number == "COMP-100").one()
        buy_component = db_session.query(Part).filter(Part.part_number == "BUY-100").one()
        assert make_component.part_type == "manufactured"
        assert buy_component.part_type == "purchased"

        bom = db_session.query(BOM).filter(BOM.id == data["bom_id"]).one()
        items = db_session.query(BOMItem).filter(BOMItem.bom_id == bom.id).all()
        assert bom.part_id == assembly.id
        assert {item.component_part_id for item in items} == {make_component.id, buy_component.id}
