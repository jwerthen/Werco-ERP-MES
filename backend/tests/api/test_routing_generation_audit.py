"""Accepting an AI-generated routing writes the routing's CREATE row on the audit chain.

``POST /routing/create-from-generation`` is the step where a named person accepts a
drawing-to-routing proposal. It created the ``Routing`` + ``RoutingOperation`` rows and
committed with only AI-learning telemetry (``AILearningService.record_interaction``) --
no ``AuditService`` row, unlike the manual ``POST /routing/`` path (invariant 2).

Pinned here:

* exactly one committed ``routing`` CREATE row, attributed to the accepting user, whose
  ``extra_data`` names the generation session, the model and prompt version that produced
  the proposal, the extraction confidence and the number of human corrections;
* ``generate-from-drawing`` now RECORDS that model / prompt version on the session at
  generation time (``learned_context``), which is where the approval reads it from;
* a soft-deleted part is refused 404 on both doors and nothing is written.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.endpoints.routing import _generation_provenance
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.part import Part
from app.models.routing import Routing
from app.models.routing_learning import RoutingGenerationSession
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.services.routing_learning_service import create_generation_session

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _user(db: Session, role: UserRole = UserRole.MANAGER) -> User:
    n = _next()
    user = User(
        email=f"route-gen-{n}@werco.test",
        employee_id=f"RGEN-{n:05d}",
        first_name="Route",
        last_name="Approver",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        company_id=1,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _part_and_center(db: Session):
    n = _next()
    part = Part(
        part_number=f"RGEN-PART-{n:05d}",
        name="Generated bracket",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
    )
    center = WorkCenter(
        code=f"RGEN-WC-{n:05d}",
        name="Brake",
        work_center_type="press_brake",
        is_active=True,
        company_id=1,
    )
    db.add_all([part, center])
    db.commit()
    return part, center


def _session(db: Session, part: Part, *, learned_context=None) -> RoutingGenerationSession:
    session = create_generation_session(
        db,
        company_id=1,
        part_id=part.id,
        created_by=None,
        file_name="bracket.dxf",
        file_type="dxf",
        file_size=100,
        file_path="uploads/routing_generation/bracket.dxf",
        drawing_text="Bracket with one flange.",
        geometry={"bend_count": 1},
        drawing_info={"material": "Aluminum"},
        proposed_operations=[
            {
                "sequence": 10,
                "operation_name": "Brake Flange",
                "work_center_type": "press_brake",
                "work_center_id": None,
            }
        ],
        warnings=[],
        extraction_confidence="high",
        source_was_ocr=False,
        learned_context=learned_context,
    )
    db.commit()
    return session


def _payload(part: Part, center: WorkCenter, *, session_id=None, name="Form Flange") -> dict:
    return {
        "part_id": part.id,
        "generation_session_id": session_id,
        "revision": "A",
        "description": "Accepted proposal",
        "operations": [
            {
                "sequence": 10,
                "name": name,
                "work_center_id": center.id,
                "setup_hours": 0.1,
                "run_hours_per_unit": 0.05,
                "move_hours": 0,
                "queue_hours": 0,
                "is_inspection_point": False,
                "is_outside_operation": False,
            }
        ],
    }


def _committed_routing_rows(db: Session):
    db.rollback()
    db.expire_all()
    return db.query(AuditLog).filter(AuditLog.resource_type == "routing").order_by(AuditLog.sequence_number).all()


def test_accepting_a_generated_routing_writes_one_create_row_with_provenance(client: TestClient, db_session: Session):
    approver = _user(db_session)
    part, center = _part_and_center(db_session)
    session = _session(
        db_session, part, learned_context={"aliases": {}, "model": "claude-test-model", "prompt_version": "9.9.9"}
    )

    response = client.post(
        "/api/v1/routing/create-from-generation",
        headers=_headers(approver),
        json=_payload(part, center, session_id=session.id),
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    routing_id = response.json()["id"]

    rows = _committed_routing_rows(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "CREATE"
    assert row.resource_id == routing_id
    assert row.resource_identifier == f"{part.part_number} Rev A"
    assert row.user_id == approver.id and row.company_id == 1
    assert row.new_values["status"] == "draft"
    extra = row.extra_data
    assert extra["source"] == "ai_routing_generation"
    assert extra["generation_session_id"] == session.id
    assert extra["model"] == "claude-test-model"
    assert extra["prompt_version"] == "9.9.9"
    assert extra["extraction_confidence"] == "high"
    assert extra["operation_count"] == 1
    assert extra["approved_by"] == approver.id
    # The human renamed the proposed "Brake Flange" -> "Form Flange": one recorded correction.
    assert extra["correction_count"] >= 1


def test_session_without_recorded_provenance_still_audits(client: TestClient, db_session: Session):
    part, center = _part_and_center(db_session)
    session = _session(db_session, part)

    response = client.post(
        "/api/v1/routing/create-from-generation",
        headers=_headers(_user(db_session)),
        json=_payload(part, center, session_id=session.id, name="Brake Flange"),
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    (row,) = _committed_routing_rows(db_session)
    assert row.extra_data["generation_session_id"] == session.id
    assert row.extra_data["model"] is None and row.extra_data["prompt_version"] is None


def test_form_without_a_generation_session_is_audited_as_such(client: TestClient, db_session: Session):
    part, center = _part_and_center(db_session)

    response = client.post(
        "/api/v1/routing/create-from-generation",
        headers=_headers(_user(db_session)),
        json=_payload(part, center),
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    (row,) = _committed_routing_rows(db_session)
    assert row.extra_data["source"] == "create_from_generation"
    assert row.extra_data["generation_session_id"] is None
    assert row.extra_data["correction_count"] == 0


def test_soft_deleted_part_is_refused_and_nothing_is_written(client: TestClient, db_session: Session):
    approver = _user(db_session)
    part, center = _part_and_center(db_session)
    session = _session(db_session, part)
    part.soft_delete(approver.id)
    db_session.commit()

    response = client.post(
        "/api/v1/routing/create-from-generation",
        headers=_headers(approver),
        json=_payload(part, center, session_id=session.id),
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    db_session.rollback()
    assert db_session.query(Routing).filter(Routing.part_id == part.id).count() == 0
    assert _committed_routing_rows(db_session) == []


def test_viewer_cannot_accept_a_generated_routing(client: TestClient, db_session: Session):
    part, center = _part_and_center(db_session)
    session = _session(db_session, part)

    response = client.post(
        "/api/v1/routing/create-from-generation",
        headers=_headers(_user(db_session, UserRole.VIEWER)),
        json=_payload(part, center, session_id=session.id),
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    db_session.rollback()
    assert db_session.query(Routing).filter(Routing.part_id == part.id).count() == 0
    assert _committed_routing_rows(db_session) == []


def test_generate_records_model_and_prompt_version_on_the_session(
    client: TestClient, db_session: Session, monkeypatch, tmp_path
):
    """The approval can only name what generation recorded. ``learned_context["model"]`` was
    read by the AI-learning event for a long time and written by nothing."""
    import app.services.routing_generation_service as generation_service
    import app.services.routing_geometry as routing_geometry

    monkeypatch.chdir(tmp_path)  # the endpoint writes the upload under ./uploads
    monkeypatch.setattr(routing_geometry, "inspect_routing_geometry", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        generation_service,
        "generate_draft_routing",
        lambda **kwargs: {
            "part_info": {},
            "operations": [{"sequence": 10, "operation_name": "Cut", "work_center_type": "laser"}],
            "extraction_confidence": "medium",
            "warnings": [],
            "_extraction_metadata": {
                "model": "claude-generation-model",
                "model_tier": "standard",
                "prompt_version": "3.2.1",
                "source_was_ocr": False,
            },
        },
    )
    part, _center = _part_and_center(db_session)

    response = client.post(
        "/api/v1/routing/generate-from-drawing",
        headers=_headers(_user(db_session)),
        data={"part_id": str(part.id)},
        files={"file": ("bracket.step", b"ISO-10303-21;", "application/octet-stream")},
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    db_session.expire_all()
    session = db_session.get(RoutingGenerationSession, response.json()["generation_session_id"])
    assert session.learned_context["model"] == "claude-generation-model"
    assert session.learned_context["prompt_version"] == "3.2.1"
    assert session.learned_context["model_tier"] == "standard"


def test_generate_refuses_a_soft_deleted_part(client: TestClient, db_session: Session):
    approver = _user(db_session)
    part, _center = _part_and_center(db_session)
    part.soft_delete(approver.id)
    db_session.commit()

    response = client.post(
        "/api/v1/routing/generate-from-drawing",
        headers=_headers(approver),
        data={"part_id": str(part.id)},
        files={"file": ("bracket.step", b"ISO-10303-21;", "application/octet-stream")},
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text


def test_generation_provenance_drops_unreported_keys():
    assert _generation_provenance({}) == {}
    assert _generation_provenance({"_extraction_metadata": {"model": None, "source_was_ocr": False}}) == {}
    assert _generation_provenance({"_extraction_metadata": {"model": "m", "prompt_version": "1.0.0"}}) == {
        "model": "m",
        "prompt_version": "1.0.0",
    }
