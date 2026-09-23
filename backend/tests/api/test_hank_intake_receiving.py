"""Evidence-only receiving drafts and source-bound, employee-reviewed receipts."""

from uuid import uuid4

import pytest

from app.models.company import Company
from app.models.hank import HankTask
from app.models.hank_intake import HankIntakeBatch, HankIntakeFile
from app.models.inventory import InventoryItem
from app.models.purchasing import POReceipt, PurchaseOrderLine
from app.models.role_permission import RolePermission
from app.schemas.hank_intake import IntakeExtraction
from app.services.hank_intake_receiving_service import _quantity
from app.services.hank_task_service import _digest, _row_values

from .test_hank_operations import delivery_input
from .test_hank_operations import po as _receiving_po
from .test_hank_tasks import execute, propose

BASE = '/api/v1/hank/intake/files'
po = _receiving_po


@pytest.fixture
def source(db_session, test_user, test_part, po):
    batch = HankIntakeBatch(
        company_id=1,
        owner_id=test_user.id,
        credential_key='user',
        request_key=str(uuid4()),
        request_hash='a' * 64,
    )
    db_session.add(batch)
    db_session.flush()
    row = HankIntakeFile(
        company_id=1,
        batch_id=batch.id,
        ordinal=0,
        filename='packing-slip.pdf',
        content_sha256='b' * 64,
        storage_ref='private-test.pdf',
        file_size=100,
        status='awaiting_review',
        version=2,
        analysis_json=IntakeExtraction(
            classification='packing_slip',
            confidence='high',
            summary='Received materials',
            fields=[
                {
                    'name': 'po_number',
                    'value': po[0].po_number,
                    'confidence': 'high',
                    'evidence': [{'page': 1, 'excerpt': po[0].po_number}],
                },
                {
                    'name': 'packing_slip_number',
                    'value': 'SLIP-1',
                    'confidence': 'high',
                    'evidence': [{'page': 1, 'excerpt': 'SLIP-1'}],
                },
            ],
            lines=[
                {
                    'part_number': test_part.part_number,
                    'description': 'Delivered material',
                    'quantity': '3',
                    'unit_of_measure': 'ea',
                    'lot_number': 'LOT-1',
                    'heat_number': 'HEAT-1',
                    'confidence': 'high',
                    'evidence': [{'page': 1, 'excerpt': f'{test_part.part_number} 3 ea LOT-1 HEAT-1'}],
                }
            ],
        ).model_dump(),
    )
    db_session.add(row)
    db_session.commit()
    return row


def edit_source(db, source, *, fields=None, lines=None, **kwargs):
    data = dict(source.analysis_json)
    if fields is not None:
        data['fields'] = fields
    if lines is not None:
        data['lines'] = lines
    data.update(kwargs)
    source.analysis_json = data
    db.commit()


def draft(client, headers, source, query=''):
    return client.get(f'{BASE}/{source.id}/receiving-draft{query}', headers=headers)


def source_input(po, source):
    return {
        **delivery_input(po, requires_inspection=True, packing_slip_number='SLIP-1'),
        'source_intake_file_id': source.id,
        'source_intake_version': source.version,
    }


def test_legacy_manual_receipt_snapshot_remains_executable(
    client,
    auth_headers,
    db_session,
    po,
    test_part,
):
    task = propose(client, auth_headers, 'receive_delivery', delivery_input(po)).json()
    saved = db_session.get(HankTask, task['id'])
    # Persist the exact pre-PDF shape, including absence of the new input fields.
    saved.input_json = {
        key: value
        for key, value in saved.input_json.items()
        if key not in {'source_intake_file_id', 'source_intake_version', 'acknowledge_duplicate_source'}
    }
    saved.source_versions_json = {
        'operational_source': _digest(
            {
                'po': [_row_values(po[0])],
                'lines': [_row_values(po[1])],
                'parts': [_row_values(test_part)],
                'locations': [],
                'documents': [],
            }
        ),
    }
    db_session.commit()
    response = execute(client, auth_headers, task)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'completed'
    assert db_session.query(POReceipt).one().quantity_received == 3


def test_receiving_draft_matches_evidence_without_creating_records(client, auth_headers, db_session, source, po):
    response = draft(client, auth_headers, source)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['purchase_order_id'] == po[0].id
    assert data['file_version'] == source.version
    assert data['packing_slip_number'] == 'SLIP-1'
    assert data['lines'][0]['po_line_id'] == po[1].id
    assert data['lines'][0]['quantity_received'] == 3
    assert data['lines'][0]['lot_number'] == 'LOT-1'
    assert data['lines'][0]['heat_number'] == 'HEAT-1'
    assert data['lines'][0]['candidates'][0]['quantity_remaining'] == 10
    assert data['lines'][0]['evidence'][0]['page'] == 1
    assert 'requires_inspection' not in data['lines'][0]
    assert db_session.query(POReceipt).count() == db_session.query(HankTask).count() == 0


def test_draft_enforces_owner_tenant_and_receiving_permission(
    client,
    auth_headers,
    admin_headers,
    db_session,
    source,
    po,
    test_user,
):
    assert draft(client, admin_headers, source).status_code == 404
    assert draft(client, auth_headers, source, '?purchase_order_id=999999').status_code == 404
    db_session.add(Company(id=2, name='Other', slug='other-receiving'))
    db_session.commit()
    po[0].company_id = 2
    db_session.commit()
    assert draft(client, auth_headers, source, f'?purchase_order_id={po[0].id}').status_code == 404
    assert draft(client, auth_headers, source).json()['purchase_orders'] == []
    db_session.add(RolePermission(company_id=1, role=test_user.role, permissions=['receiving:view']))
    db_session.commit()
    assert draft(client, auth_headers, source).status_code == 403


def test_draft_requires_analysis_and_rejects_cancelled_source(client, auth_headers, db_session, source):
    for status in ('queued', 'analyzing', 'cancelled', 'failed'):
        source.status = status
        db_session.commit()
        assert draft(client, auth_headers, source).status_code == 409


def test_ambiguous_order_line_and_repeated_pdf_part_are_not_auto_matched(
    client,
    auth_headers,
    db_session,
    source,
    po,
):
    extra = PurchaseOrderLine(
        company_id=1,
        purchase_order_id=po[0].id,
        part_id=po[1].part_id,
        line_number=2,
        quantity_ordered=10,
        unit_price=2,
    )
    db_session.add(extra)
    db_session.commit()
    data = draft(client, auth_headers, source).json()
    assert data['lines'][0]['po_line_id'] is None
    assert len(data['lines'][0]['candidates']) == 2
    db_session.delete(extra)
    db_session.commit()
    edit_source(db_session, source, lines=[source.analysis_json['lines'][0]] * 2)
    data = draft(client, auth_headers, source).json()
    assert all(line['po_line_id'] is None and line['quantity_received'] is None for line in data['lines'])
    assert 'more than once' in ' '.join(data['lines'][0]['warnings'])


@pytest.mark.parametrize(
    'change,warning',
    [
        ({'confidence': 'low'}, 'uncertain'),
        ({'evidence': []}, 'page evidence'),
        ({'quantity': '3 of 10'}, 'ambiguous'),
        ({'unit_of_measure': 'lbs'}, 'unit differs'),
        ({'unit_of_measure': None}, 'No unit was extracted'),
        ({'part_number': 'NOT-OUR-PART'}, 'No unique'),
    ],
)
def test_uncertain_values_require_manual_entry(client, auth_headers, db_session, source, change, warning):
    edit_source(db_session, source, lines=[{**source.analysis_json['lines'][0], **change}])
    line = draft(client, auth_headers, source).json()['lines'][0]
    assert line['quantity_received'] is None
    assert warning in ' '.join(line['warnings'])


def test_excess_quantity_is_disclosed_and_missing_po_requires_selection(client, auth_headers, db_session, source, po):
    edit_source(db_session, source, lines=[{**source.analysis_json['lines'][0], 'quantity': '12'}])
    line = draft(client, auth_headers, source).json()['lines'][0]
    assert 'exceeds' in ' '.join(line['warnings'])
    edit_source(
        db_session, source, fields=[field for field in source.analysis_json['fields'] if field['name'] != 'po_number']
    )
    data = draft(client, auth_headers, source).json()
    assert data['purchase_order_id'] is None and data['purchase_orders'][0]['id'] == po[0].id
    assert draft(client, auth_headers, source, f'?purchase_order_id={po[0].id}').json()['purchase_order_id'] == po[0].id


@pytest.mark.parametrize('printed', ['0', '-1', 'NaN', 'Infinity', '1e2', '1/2', '1,5', '3 ea', '1.12345'])
def test_quantity_parser_never_guesses(printed):
    assert _quantity(printed) is None


def test_quantity_parser_preserves_decimal_and_grouping():
    assert _quantity('1,234.125') == 1234.125
    assert _quantity('0.125') == 0.125


def test_source_binding_requires_pair_and_rechecks_version_owner(
    client,
    auth_headers,
    admin_headers,
    db_session,
    source,
    po,
):
    data = source_input(po, source)
    assert propose(client, admin_headers, 'receive_delivery', data).status_code == 404
    missing_version = {key: value for key, value in data.items() if key != 'source_intake_version'}
    assert propose(client, auth_headers, 'receive_delivery', missing_version).status_code == 422
    response = propose(client, auth_headers, 'receive_delivery', data)
    assert response.status_code == 200, response.text
    task = response.json()
    assert task['preview']['references'][0]['type'] == 'intake_file'
    assert task['preview']['references'][0]['url'] == f'/?hank_work=intake&hank_id={source.id}'
    source.version += 1
    db_session.commit()
    assert execute(client, auth_headers, task).status_code == 409
    assert db_session.query(POReceipt).count() == 0


@pytest.mark.parametrize('confidence,evidence', [('low', True), ('unknown', True), ('high', False)])
def test_uncertain_header_traceability_is_not_prefilled(
    client,
    auth_headers,
    db_session,
    source,
    confidence,
    evidence,
):
    fields = [field for field in source.analysis_json['fields'] if field['name'] == 'po_number']
    fields += [
        {
            'name': name,
            'value': value,
            'confidence': confidence,
            'evidence': [{'page': 1, 'excerpt': value}] if evidence else [],
        }
        for name, value in [
            ('lot_number', 'MAYBE-LOT'),
            ('heat_number', 'MAYBE-HEAT'),
            ('packing_slip_number', 'MAYBE-SLIP'),
        ]
    ]
    line = {**source.analysis_json['lines'][0], 'lot_number': None, 'heat_number': None}
    edit_source(db_session, source, fields=fields, lines=[line])
    result = draft(client, auth_headers, source)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data['packing_slip_number'] is None
    assert data['lines'][0]['lot_number'] is None and data['lines'][0]['heat_number'] is None
    assert 'packing slip number is uncertain' in ' '.join(data['warnings'])
    assert 'header lot number is uncertain' in ' '.join(data['lines'][0]['warnings'])


def test_header_fallback_preserves_verified_traceability_and_bounds_long_fields(
    client,
    auth_headers,
    db_session,
    source,
):
    fields = source.analysis_json['fields'] + [
        {
            'name': name,
            'value': value,
            'confidence': 'high',
            'evidence': [{'page': 1, 'excerpt': value}],
        }
        for name, value in [('lot_number', 'HEADER-LOT'), ('heat_number', 'HEADER-HEAT')]
    ]
    edit_source(
        db_session,
        source,
        fields=fields,
        lines=[{**source.analysis_json['lines'][0], 'lot_number': None, 'heat_number': None}],
    )
    data = draft(client, auth_headers, source).json()
    assert data['lines'][0]['lot_number'] == 'HEADER-LOT'
    assert data['lines'][0]['heat_number'] == 'HEADER-HEAT'
    fields = [
        {'name': name, 'value': 'A' * 200, 'confidence': 'high', 'evidence': [{'page': 1, 'excerpt': 'A' * 200}]}
        for name in ('part_number', 'quantity', 'lot_number', 'heat_number', 'packing_slip_number')
    ]
    edit_source(db_session, source, fields=fields, lines=[])
    response = draft(client, auth_headers, source)
    assert response.status_code == 200, response.text
    assert response.json()['lines'][0]['lot_number'] is None
    assert response.json()['packing_slip_number'] is None


def test_oversized_order_is_explicitly_rejected_instead_of_truncated(
    client,
    auth_headers,
    db_session,
    source,
    po,
    monkeypatch,
):
    monkeypatch.setattr('app.services.hank_intake_receiving_service.PLAN_ROW_LIMIT', 1)
    db_session.add(
        PurchaseOrderLine(
            company_id=1,
            purchase_order_id=po[0].id,
            part_id=po[1].part_id,
            line_number=2,
            quantity_ordered=10,
            unit_price=1,
        )
    )
    db_session.commit()
    response = draft(client, auth_headers, source)
    assert response.status_code == 409
    assert 'too many open lines' in response.json()['detail']


def test_source_receipt_preserves_inspection_and_blocks_unacknowledged_duplicate(
    client,
    auth_headers,
    db_session,
    source,
    po,
):
    data = source_input(po, source)
    first = propose(client, auth_headers, 'receive_delivery', data).json()
    assert 'receive 3 each' in first['preview']['changes'][0]
    second = propose(client, auth_headers, 'receive_delivery', data).json()
    result = execute(client, auth_headers, first)
    assert result.status_code == 200, result.text
    assert any(ref['type'] == 'intake_file' for ref in result.json()['result']['references'])
    assert result.json()['result']['references'][0]['url'] == f'/?hank_work=intake&hank_id={source.id}'
    assert db_session.query(POReceipt).one().requires_inspection
    assert db_session.query(InventoryItem).count() == 0
    assert execute(client, auth_headers, first).json() == result.json()
    assert execute(client, auth_headers, second).status_code == 409
    assert propose(client, auth_headers, 'receive_delivery', data).status_code == 409
    review = draft(client, auth_headers, source).json()
    assert review['requires_duplicate_acknowledgement'] is True
    # Additional partial receipts remain possible after explicit review.
    followup = propose(client, auth_headers, 'receive_delivery', {**data, 'acknowledge_duplicate_source': True})
    assert followup.status_code == 200, followup.text
    assert execute(client, auth_headers, followup.json()).status_code == 200
    assert db_session.query(POReceipt).count() == 2


def test_identical_reupload_is_detected_without_disclosing_private_task_ids(
    client,
    auth_headers,
    db_session,
    source,
    po,
    test_user,
):
    task = propose(client, auth_headers, 'receive_delivery', source_input(po, source)).json()
    assert execute(client, auth_headers, task).status_code == 200
    duplicate = HankIntakeFile(
        company_id=1,
        batch_id=source.batch_id,
        ordinal=1,
        filename='same-material.pdf',
        content_sha256=source.content_sha256,
        storage_ref='copy.pdf',
        file_size=100,
        status='awaiting_review',
        version=1,
        analysis_json=source.analysis_json,
    )
    db_session.add(duplicate)
    db_session.commit()
    data = draft(client, auth_headers, duplicate).json()
    assert data['has_duplicates'] and data['requires_duplicate_acknowledgement']
    assert 'task_id' not in str(data)
    # Removing the slip text cannot evade the content-hash receipt guard.
    pending = source_input(po, duplicate)
    pending['lines'][0]['packing_slip_number'] = None
    assert propose(client, auth_headers, 'receive_delivery', pending).status_code == 409
