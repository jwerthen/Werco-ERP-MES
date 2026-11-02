"""Signed-in drawing entry exercises the normal engineering authority boundaries."""

import pytest

from app.models.audit_log import AuditLog
from app.models.part import Part
from app.models.routing import Routing
from app.models.work_order import WorkOrder


def post(client, headers, path, payload=None):
    response = client.post('/api/v1' + path, headers=headers, json=payload)
    assert response.status_code in (200, 201), response.text
    return response.json()


def test_assembly_to_draft_wo_retains_quantities_revisions_and_audits(
    client, db_session, admin_headers, admin_user, test_work_center
):
    parts = []
    for pn, kind in [('DRAW-ASM', 'assembly'), ('DRAW-DETAIL', 'manufactured')]:
        parts.append(
            post(
                client,
                admin_headers,
                '/parts/',
                {
                    'part_number': pn,
                    'revision': 'B',
                    'name': pn,
                    'part_type': kind,
                    'unit_of_measure': 'each',
                    'drawing_number': 'DRAW-001',
                    'description': 'HANK-DRAWING:synthetic; page 1; reviewed by author',
                },
            )
        )
    owner, detail = parts
    bom = post(
        client,
        admin_headers,
        '/bom/',
        {
            'part_id': owner['id'],
            'revision': 'B',
            'items': [
                {
                    'component_part_id': detail['id'],
                    'item_number': 10,
                    'quantity': 2,
                    'unit_of_measure': 'each',
                    'item_type': 'make',
                    'notes': 'Page 1: two details per assembly',
                }
            ],
        },
    )
    routing = post(client, admin_headers, '/routing/', {'part_id': owner['id'], 'revision': 'B'})
    operation = post(
        client,
        admin_headers,
        f'/routing/{routing["id"]}/operations',
        {
            'sequence': 10,
            'name': 'DRAW-DETAIL - Cut and deburr',
            'work_center_id': test_work_center.id,
            'setup_hours': 0.2,
            'run_hours_per_unit': 0.05,
            'work_instructions': 'Deburr all edges. Source: page 1. Times explicitly approved.',
        },
    )
    assert bom['status'] == routing['status'] == 'draft'
    assert db_session.query(WorkOrder).count() == 0
    post(client, admin_headers, f'/bom/{bom["id"]}/release')
    post(client, admin_headers, f'/routing/{routing["id"]}/release')
    denied = client.put(
        f'/api/v1/routing/{routing["id"]}/operations/{operation["id"]}',
        headers=admin_headers,
        json={'name': 'Unreviewed change'},
    )
    assert denied.status_code in (400, 409), denied.text
    wo = post(
        client,
        admin_headers,
        '/work-orders/',
        {
            'part_id': owner['id'],
            'quantity_ordered': 4,
            'notes': 'HANK-DRAWING:synthetic; Jon requested four',
        },
    )
    observed = client.get(f'/api/v1/work-orders/{wo["id"]}', headers=admin_headers).json()
    assert observed['status'] == 'draft'
    assert observed['quantity_ordered'] == 4
    assert len(observed['operations']) == 1
    assert observed['operations'][0]['component_part_id'] == detail['id']
    assert observed['operations'][0]['component_quantity'] == 8
    assert observed['operations'][0]['status'] == 'pending'
    assert observed['operations'][0]['name'] == 'DRAW-DETAIL - Cut and deburr'
    audits = db_session.query(AuditLog).filter(AuditLog.action == 'CREATE').all()
    assert {'part', 'bom', 'bom_line', 'routing', 'routing_operation', 'work_order'} <= {
        row.resource_type for row in audits
    }
    assert all(row.company_id == 1 and row.user_id == admin_user.id for row in audits)
    duplicate = client.post(
        '/api/v1/parts/',
        headers=admin_headers,
        json={
            'part_number': 'DRAW-ASM',
            'revision': 'B',
            'name': 'Duplicate',
            'part_type': 'assembly',
        },
    )
    assert duplicate.status_code == 400
    assert db_session.query(Part).count() == 2


@pytest.mark.parametrize(
    'path,payload',
    [
        ('/parts/', {'part_number': 'DENIED', 'name': 'Denied part', 'part_type': 'manufactured'}),
        ('/bom/', {'part_id': 1, 'revision': 'A'}),
        ('/routing/', {'part_id': 1, 'revision': 'A'}),
        ('/work-orders/', {'part_id': 1, 'quantity_ordered': 1}),
    ],
)
def test_operator_cannot_bypass_workspace_authority(client, db_session, operator_headers, path, payload):
    response = client.post('/api/v1' + path, headers=operator_headers, json=payload)
    assert response.status_code == 403, response.text
    assert (
        db_session.query(Part).count() == db_session.query(Routing).count() == db_session.query(WorkOrder).count() == 0
    )


def test_hank_ui_workflow_is_not_an_executable_model_action(client, auth_headers):
    caps = client.get('/api/v1/hank/capabilities', headers=auth_headers)
    assert caps.status_code == 200, caps.text
    assert caps.json()['engineering_ui_path'] == '/hank-engineering'
    response = client.post(
        '/api/v1/hank/tasks',
        headers=auth_headers,
        json={
            'expected_company_id': 1,
            'request_key': '74004c0e-c64e-4de5-bcd3-280b12dc7951',
            'kind': 'drawing_engineering',
            'input': {},
        },
    )
    assert response.status_code == 422


def test_read_only_author_cannot_create_routing(client, db_session, admin_user, test_part):
    from app.core.security import create_access_token

    token = create_access_token(subject=admin_user.id, company_id=1, read_only=True)
    response = client.post(
        '/api/v1/routing/',
        headers={
            'Authorization': f'Bearer {token}',
            'X-Requested-With': 'XMLHttpRequest',
        },
        json={'part_id': test_part.id, 'revision': 'A'},
    )
    assert response.status_code == 403
    assert db_session.query(Routing).count() == 0


def test_cross_company_part_cannot_be_used_to_create_engineering(client, db_session, admin_headers):
    from app.models.company import Company
    from app.models.part import PartType

    foreign = Company(name='Other tenant', slug='drawing-other', is_active=True)
    db_session.add(foreign)
    db_session.flush()
    part = Part(company_id=foreign.id, part_number='PRIVATE', name='Other tenant part', part_type=PartType.ASSEMBLY)
    db_session.add(part)
    db_session.commit()
    response = client.post('/api/v1/routing/', headers=admin_headers, json={'part_id': part.id, 'revision': 'A'})
    assert response.status_code == 404
    assert db_session.query(Routing).count() == 0
