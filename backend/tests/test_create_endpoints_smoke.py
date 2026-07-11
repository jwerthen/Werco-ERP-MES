"""
Smoke coverage for create endpoints adjacent to the fixed 500 cluster (Quote/PO/NCR).
Each drives a create with a minimal VALID payload and asserts a 2xx — catching the same
bug classes (duplicate `**model_dump()` kwargs, tenant `company_id` not set on a child,
Decimal/float money mixing). See test_repro_500_cluster.py for the three that were broken.
"""

import app.models as m
from app.core.security import create_access_token


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(subject=user.id, company_id=user.company_id)}"}


def _seed_part(db_session):
    part = m.Part(part_number="P-SMOKE", name="Smoke Part", part_type="manufactured", company_id=1)
    db_session.add(part)
    db_session.commit()
    db_session.refresh(part)
    return part


def _seed_work_center(db_session):
    wc = m.WorkCenter(code="WC-SMOKE", name="Smoke WC", work_center_type="cnc_machining", company_id=1)
    db_session.add(wc)
    db_session.commit()
    db_session.refresh(wc)
    return wc


def test_create_car(client, admin_user, db_session):
    resp = client.post(
        "/api/v1/quality/car",
        headers=_headers(admin_user),
        json={"title": "Smoke CAR title", "problem_description": "A corrective action problem statement."},
    )
    assert resp.status_code in (200, 201), resp.text


def test_create_fai(client, admin_user, db_session):
    part = _seed_part(db_session)
    resp = client.post("/api/v1/quality/fai", headers=_headers(admin_user), json={"part_id": part.id})
    assert resp.status_code in (200, 201), resp.text


def test_create_spc_characteristic(client, admin_user, db_session):
    part = _seed_part(db_session)
    resp = client.post(
        "/api/v1/spc/characteristics",
        headers=_headers(admin_user),
        json={"name": "Bore dia", "part_id": part.id, "characteristic_type": "dimensional"},
    )
    assert resp.status_code in (200, 201), resp.text


def test_create_custom_field(client, admin_user, db_session):
    resp = client.post(
        "/api/v1/custom-fields/definitions",
        headers=_headers(admin_user),
        json={"field_key": "smoke_field", "display_name": "Smoke", "entity_type": "part", "field_type": "text"},
    )
    assert resp.status_code in (200, 201), resp.text


def test_create_maintenance_schedule(client, admin_user, db_session):
    wc = _seed_work_center(db_session)
    resp = client.post(
        "/api/v1/maintenance/schedules",
        headers=_headers(admin_user),
        json={"work_center_id": wc.id, "name": "Monthly PM"},
    )
    assert resp.status_code in (200, 201), resp.text


def test_create_maintenance_work_order(client, admin_user, db_session):
    wc = _seed_work_center(db_session)
    resp = client.post(
        "/api/v1/maintenance/work-orders",
        headers=_headers(admin_user),
        json={"work_center_id": wc.id, "title": "Fix spindle"},
    )
    assert resp.status_code in (200, 201), resp.text


def test_create_tool(client, admin_user, db_session):
    resp = client.post(
        "/api/v1/tool-management/tools/",
        headers=_headers(admin_user),
        json={"tool_id": "T-SMOKE", "name": "Smoke Tool"},
    )
    assert resp.status_code in (200, 201), resp.text
