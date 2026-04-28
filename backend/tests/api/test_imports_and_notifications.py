from io import BytesIO

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.notification import NotificationLog
from app.models.part import Part
from app.models.purchasing import Vendor
from app.models.work_center import WorkCenter


def _csv_file(text: str):
    return {"file": ("import.csv", BytesIO(text.encode("utf-8")), "text/csv")}


@pytest.mark.api
@pytest.mark.requires_db
class TestMasterDataImports:
    def test_import_parts_customers_vendors_and_work_centers(
        self,
        client: TestClient,
        auth_headers: dict,
        db_session: Session,
    ):
        part_response = client.post(
            "/api/v1/parts/import-csv",
            headers=auth_headers,
            files=_csv_file("part_number,name,part_type,unit_of_measure\nIMP-001,Imported Part,manufactured,each\n"),
        )
        customer_response = client.post(
            "/api/v1/customers/import-csv",
            headers=auth_headers,
            files=_csv_file("code,name,email\nCIMP,Imported Customer,customer@example.com\n"),
        )
        vendor_response = client.post(
            "/api/v1/purchasing/vendors/import-csv",
            headers=auth_headers,
            files=_csv_file("code,name,is_approved\nVIMP,Imported Vendor,true\n"),
        )
        work_center_response = client.post(
            "/api/v1/work-centers/import-csv",
            headers=auth_headers,
            files=_csv_file("code,name,work_center_type,hourly_rate\nWCIMP,Imported Work Center,fabrication,95\n"),
        )

        assert part_response.status_code == status.HTTP_200_OK
        assert customer_response.status_code == status.HTTP_200_OK
        assert vendor_response.status_code == status.HTTP_200_OK
        assert work_center_response.status_code == status.HTTP_200_OK

        assert part_response.json()["imported_count"] == 1
        assert customer_response.json()["imported_count"] == 1
        assert vendor_response.json()["imported_count"] == 1
        assert work_center_response.json()["imported_count"] == 1

        assert db_session.query(Part).filter_by(part_number="IMP-001", company_id=1).count() == 1
        assert db_session.query(Customer).filter_by(code="CIMP", company_id=1).count() == 1
        assert db_session.query(Vendor).filter_by(code="VIMP", company_id=1).count() == 1
        assert db_session.query(WorkCenter).filter_by(code="WCIMP", company_id=1).count() == 1

    def test_notification_logs_feed_action_inbox(
        self,
        client: TestClient,
        auth_headers: dict,
        test_user,
        db_session: Session,
    ):
        db_session.add(
            NotificationLog(
                company_id=1,
                user_id=test_user.id,
                event_type="WO_LATE",
                channel="email",
                subject="Late work order",
                body="WO-100 is late",
                sent=True,
                related_type="WorkOrder",
                related_id=100,
            )
        )
        db_session.commit()

        response = client.get("/api/v1/notifications/logs", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) == 1
        assert data[0]["event_type"] == "WO_LATE"
        assert data[0]["subject"] == "Late work order"
