import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.work_center import WorkCenter


@pytest.mark.api
@pytest.mark.requires_db
class TestWorkCenters:
    def test_update_work_center_type_persists(
        self,
        client: TestClient,
        auth_headers: dict,
        db_session: Session,
        test_work_center: WorkCenter,
    ):
        response = client.put(
            f"/api/v1/work-centers/{test_work_center.id}",
            headers=auth_headers,
            json={
                "version": getattr(test_work_center, "version", 0),
                "name": test_work_center.name,
                "work_center_type": "laser",
            },
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["work_center_type"] == "laser"

        db_session.refresh(test_work_center)
        assert test_work_center.work_center_type == "laser"
