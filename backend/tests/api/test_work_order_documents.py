import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.models.part import Part
from app.models.work_order import WorkOrder


@pytest.fixture(autouse=True)
def upload_dir(tmp_path, monkeypatch):
    from app.api.endpoints import documents as documents_endpoint

    monkeypatch.setattr(documents_endpoint, "UPLOAD_DIR", str(tmp_path))


@pytest.mark.api
@pytest.mark.requires_db
class TestWorkOrderDocumentsAPI:
    def test_upload_pdf_to_work_order(
        self,
        client: TestClient,
        auth_headers: dict,
        test_work_order: WorkOrder,
    ):
        response = client.post(
            "/api/v1/documents/upload",
            headers=auth_headers,
            data={
                "title": "WO Drawing",
                "document_type": "drawing",
                "revision": "A",
                "work_order_id": str(test_work_order.id),
            },
            files={"file": ("wo-drawing.pdf", b"%PDF-1.4\n", "application/pdf")},
        )

        assert response.status_code == status.HTTP_200_OK
        uploaded = response.json()
        assert uploaded["work_order_id"] == test_work_order.id
        assert uploaded["mime_type"] == "application/pdf"

        list_response = client.get(
            "/api/v1/documents/",
            headers=auth_headers,
            params={"work_order_id": test_work_order.id},
        )
        assert list_response.status_code == status.HTTP_200_OK
        assert [document["id"] for document in list_response.json()] == [uploaded["id"]]

    def test_attach_existing_pdf_to_work_order(
        self,
        client: TestClient,
        auth_headers: dict,
        test_work_order: WorkOrder,
    ):
        upload_response = client.post(
            "/api/v1/documents/upload",
            headers=auth_headers,
            data={"title": "Existing PDF", "document_type": "drawing", "revision": "A"},
            files={"file": ("existing.pdf", b"%PDF-1.4\n", "application/pdf")},
        )
        assert upload_response.status_code == status.HTTP_200_OK
        document_id = upload_response.json()["id"]

        attach_response = client.post(
            f"/api/v1/documents/{document_id}/attach-work-order",
            headers=auth_headers,
            json={"work_order_id": test_work_order.id},
        )

        assert attach_response.status_code == status.HTTP_200_OK
        assert attach_response.json()["work_order_id"] == test_work_order.id

    def test_attach_existing_non_pdf_to_work_order_rejected(
        self,
        client: TestClient,
        auth_headers: dict,
        test_work_order: WorkOrder,
    ):
        upload_response = client.post(
            "/api/v1/documents/upload",
            headers=auth_headers,
            data={"title": "Setup Text", "document_type": "work_instruction", "revision": "A"},
            files={"file": ("setup.txt", b"setup instructions", "text/plain")},
        )
        assert upload_response.status_code == status.HTTP_200_OK
        document_id = upload_response.json()["id"]

        attach_response = client.post(
            f"/api/v1/documents/{document_id}/attach-work-order",
            headers=auth_headers,
            json={"work_order_id": test_work_order.id},
        )

        assert attach_response.status_code == status.HTTP_400_BAD_REQUEST
        assert "Only PDF" in attach_response.json()["detail"]


@pytest.mark.api
@pytest.mark.requires_db
class TestPartDocumentsAPI:
    def test_upload_pdf_to_part(
        self,
        client: TestClient,
        auth_headers: dict,
        test_part: Part,
    ):
        response = client.post(
            "/api/v1/documents/upload",
            headers=auth_headers,
            data={
                "title": "Part Drawing",
                "document_type": "drawing",
                "revision": "A",
                "part_id": str(test_part.id),
            },
            files={"file": ("part-drawing.pdf", b"%PDF-1.4\n", "application/pdf")},
        )

        assert response.status_code == status.HTTP_200_OK
        uploaded = response.json()
        assert uploaded["part_id"] == test_part.id
        assert uploaded["mime_type"] == "application/pdf"

        list_response = client.get(
            "/api/v1/documents/",
            headers=auth_headers,
            params={"part_id": test_part.id},
        )
        assert list_response.status_code == status.HTTP_200_OK
        assert [document["id"] for document in list_response.json()] == [uploaded["id"]]
