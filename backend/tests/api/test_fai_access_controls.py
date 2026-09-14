"""FAI writes preserve tenant boundaries, inspection authority and completed evidence."""

from datetime import date

import pytest
from sqlalchemy import inspect

from app.models.audit_log import AuditLog
from app.models.operational_event import OperationalEvent
from app.models.process_sheet import OperationStepRecord, ProcessSheet, WOOperationStep
from app.models.quality import FAICharacteristic, FAIStatus, FirstArticleInspection
from app.models.user import UserRole
from app.services.audit_service import AuditService
from tests.api.kiosk_test_helpers import make_user, make_wo_with_operation, make_work_center, user_headers

pytestmark = [pytest.mark.api, pytest.mark.requires_db]
BASE = '/api/v1/quality/fai'
WRITES = ['create', 'update', 'approve', 'add', 'measure', 'delete', 'prefill']


@pytest.fixture
def inspections(db_session):
    result = []
    for company_id in (1, 2):
        wo, operation = make_wo_with_operation(
            db_session, company_id=company_id, work_center=make_work_center(db_session, company_id=company_id)
        )
        fai = FirstArticleInspection(
            company_id=company_id,
            fai_number=f'FAI-ACCESS-{company_id}',
            part_id=wo.part_id,
            work_order_id=wo.id,
            total_characteristics=1,
        )
        db_session.add(fai)
        db_session.flush()
        char = FAICharacteristic(company_id=company_id, fai_id=fai.id, char_number=1, characteristic='Bore diameter')
        db_session.add(char)
        recorder = make_user(db_session, company_id=company_id, role=UserRole.QUALITY)
        sheet = ProcessSheet(company_id=company_id, sheet_number=f'PS-FAI-{company_id}', title='Inspection plan')
        db_session.add(sheet)
        db_session.flush()
        step = WOOperationStep(
            company_id=company_id,
            work_order_operation_id=operation.id,
            source_sheet_id=sheet.id,
            source_sheet_revision='A',
            sequence=10,
            label='Bore diameter',
            step_type='measurement',
            config={'nominal': 1.0, 'lsl': 0.9, 'usl': 1.1},
        )
        db_session.add(step)
        db_session.flush()
        db_session.add(
            OperationStepRecord(
                company_id=company_id,
                wo_operation_step_id=step.id,
                work_order_operation_id=operation.id,
                recorded_by=recorder.id,
                value_numeric=1.005,
                is_conforming=True,
            )
        )
        db_session.commit()
        result.append((fai, char, wo, operation))
    return result


def snapshot(db):
    """Compare persisted domain/evidence rows, excluding unrelated authentication state."""
    db.expire_all()
    return {
        model.__tablename__: [
            {column.key: getattr(row, column.key) for column in inspect(model).columns}
            for row in db.query(model).order_by(model.id).all()
        ]
        for model in (FirstArticleInspection, FAICharacteristic, OperationStepRecord, AuditLog, OperationalEvent)
    }


def write(client, action, row, headers):
    fai, char, wo, _ = row
    url = f'{BASE}/{fai.id}'
    if action == 'create':
        return client.post(BASE, headers=headers, json={'part_id': wo.part_id, 'work_order_id': wo.id})
    if action == 'update':
        return client.put(url, headers=headers, json={'version': 0, 'notes': 'Inspector notes'})
    if action == 'approve':
        return client.put(url, headers=headers, json={'version': 0, 'status': 'passed'})
    if action == 'add':
        return client.post(
            f'{url}/characteristics', headers=headers, json={'char_number': 2, 'characteristic': 'Overall width'}
        )
    if action == 'measure':
        return client.put(
            f'{url}/characteristics/{char.id}', headers=headers, json={'actual_value': '1.005', 'is_conforming': True}
        )
    if action == 'delete':
        return client.delete(f'{url}/characteristics/{char.id}', headers=headers)
    return client.post(f'{url}/prefill-from-steps', headers=headers)


@pytest.mark.parametrize('role', [UserRole.VIEWER, UserRole.OPERATOR, UserRole.SHIPPING])
@pytest.mark.parametrize('action', WRITES)
def test_read_only_roles_cannot_mutate_any_fai_surface(client, db_session, inspections, role, action):
    headers = user_headers(make_user(db_session, role=role))
    before = snapshot(db_session)
    response = write(client, action, inspections[0], headers)
    assert response.status_code == 403, response.text
    assert snapshot(db_session) == before


@pytest.mark.parametrize('action', WRITES)
def test_foreign_tenant_writes_are_404_without_changes(client, db_session, inspections, action):
    headers = user_headers(make_user(db_session, role=UserRole.QUALITY))
    before = snapshot(db_session)
    response = write(client, action, inspections[1], headers)
    assert response.status_code == 404, response.text
    assert snapshot(db_session) == before


@pytest.mark.parametrize('action', ['update', 'approve', 'add', 'measure', 'delete', 'prefill'])
@pytest.mark.parametrize('final_state', ['passed', 'failed', 'conditional', 'completed_date'])
def test_final_inspections_are_immutable(client, db_session, inspections, action, final_state):
    fai = inspections[0][0]
    if final_state == 'completed_date':
        fai.completed_date = date(2026, 9, 1)
    else:
        fai.status = FAIStatus(final_state)
    db_session.commit()
    headers = user_headers(make_user(db_session, role=UserRole.QUALITY))
    before = snapshot(db_session)
    response = write(client, action, inspections[0], headers)
    assert response.status_code == 409, response.text
    assert snapshot(db_session) == before


@pytest.mark.parametrize('action', ['measure', 'delete'])
@pytest.mark.parametrize('mismatch', ['other_parent', 'foreign_company'])
def test_characteristic_requires_both_parent_and_company(client, db_session, inspections, action, mismatch):
    fai, _, wo, op = inspections[0]
    foreign_char = inspections[1][1]
    if mismatch == 'foreign_company':
        foreign_char.fai_id = fai.id  # Legacy malformed FK: same parent but another tenant.
        db_session.commit()
    headers = user_headers(make_user(db_session, role=UserRole.QUALITY))
    before = snapshot(db_session)
    response = write(client, action, (fai, foreign_char, wo, op), headers)
    assert response.status_code == 404, response.text
    assert snapshot(db_session) == before


@pytest.mark.parametrize('role', [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY])
def test_manual_characteristic_creation_sets_tenant_and_audits(client, db_session, inspections, role):
    user = make_user(db_session, role=role)
    response = write(client, 'add', inspections[0], user_headers(user))
    assert response.status_code == 200, response.text
    char = db_session.get(FAICharacteristic, response.json()['id'])
    assert char.company_id == 1
    assert inspections[0][0].total_characteristics == 2
    entry = db_session.query(AuditLog).filter_by(resource_type='fai_characteristic', resource_id=char.id).one()
    assert entry.action == 'CREATE' and entry.company_id == 1 and entry.user_id == user.id
    assert entry.new_values['characteristic'] == 'Overall width'


@pytest.mark.parametrize('final_status', ['passed', 'failed', 'conditional'])
def test_supervisor_can_inspect_but_cannot_approve(client, db_session, inspections, final_status):
    headers = user_headers(make_user(db_session, role=UserRole.SUPERVISOR))
    assert write(client, 'measure', inspections[0], headers).status_code == 200
    before = snapshot(db_session)
    response = client.put(
        f'{BASE}/{inspections[0][0].id}',
        headers=headers,
        json={'version': 0, 'status': final_status, 'notes': 'No write'},
    )
    assert response.status_code == 403, response.text
    assert snapshot(db_session) == before


@pytest.mark.parametrize('role', [UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])
def test_quality_approval_is_attributed_and_audited(client, db_session, inspections, role):
    user = make_user(db_session, role=role)
    response = write(client, 'approve', inspections[0], user_headers(user))
    assert response.status_code == 200, response.text
    fai = inspections[0][0]
    db_session.refresh(fai)
    assert fai.status == FAIStatus.PASSED and fai.approved_by == user.id and fai.completed_date == date.today()
    entry = db_session.query(AuditLog).filter_by(resource_type='fai', resource_id=fai.id, action='STATUS_CHANGE').one()
    assert entry.old_values['status'] == 'pending' and entry.new_values['status'] == 'passed'
    assert entry.new_values['approved_by'] == user.id


@pytest.mark.parametrize('action', WRITES)
def test_required_audit_failure_rolls_back_mutation(client, db_session, inspections, monkeypatch, action):
    headers = user_headers(make_user(db_session, role=UserRole.QUALITY))
    before = snapshot(db_session)
    monkeypatch.setattr(AuditService, 'log', lambda *args, **kwargs: None)
    response = write(client, action, inspections[0], headers)
    assert response.status_code == 503, response.text
    assert response.json() == {'detail': 'Unable to save audit record'}
    assert snapshot(db_session) == before


def test_foreign_inspector_reference_is_404(client, db_session, inspections):
    headers = user_headers(make_user(db_session, role=UserRole.QUALITY))
    foreign_user = make_user(db_session, company_id=2, role=UserRole.QUALITY)
    before = snapshot(db_session)
    response = client.put(
        f'{BASE}/{inspections[0][0].id}', headers=headers, json={'version': 0, 'inspector_id': foreign_user.id}
    )
    assert response.status_code == 404, response.text
    assert snapshot(db_session) == before


def test_reads_exclude_foreign_characteristics_even_with_corrupt_parent_link(client, db_session, inspections):
    fai, _, _, _ = inspections[0]
    foreign_char = inspections[1][1]
    foreign_char.fai_id = fai.id
    db_session.commit()
    headers = user_headers(make_user(db_session, role=UserRole.VIEWER))
    detail = client.get(f'{BASE}/{fai.id}', headers=headers)
    assert detail.status_code == 200
    assert {row['id'] for row in detail.json()['characteristics']} == {inspections[0][1].id}
    listing = client.get(BASE, headers=headers)
    assert listing.status_code == 200
    assert {row['id'] for row in listing.json()} == {fai.id}
    assert {row['id'] for row in listing.json()[0]['characteristics']} == {inspections[0][1].id}


@pytest.mark.parametrize('action', ['create', 'update', 'measure', 'delete', 'prefill'])
@pytest.mark.parametrize('role', [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY])
def test_inspector_writes_succeed_with_audit(client, db_session, inspections, role, action):
    user = make_user(db_session, role=role)
    before = db_session.query(AuditLog).count()
    response = write(client, action, inspections[0], user_headers(user))
    assert response.status_code == 200, response.text
    entries = db_session.query(AuditLog).order_by(AuditLog.id).all()
    assert len(entries) == before + 1
    assert entries[-1].user_id == user.id and entries[-1].company_id == 1
    assert entries[-1].integrity_hash
    if action == 'prefill':
        assert response.json()['prefilled_count'] == 1
        assert inspections[0][1].is_conforming is None
    elif action == 'delete':
        assert entries[-1].action == 'DELETE'
        assert entries[-1].old_values['characteristic'] == 'Bore diameter'
        assert entries[-1].extra_data['new_counts']['total_characteristics'] == 0


@pytest.mark.parametrize('previous', [None, True, False])
@pytest.mark.parametrize('new', [None, True, False])
def test_three_state_disposition_counts_remain_correct(client, db_session, inspections, previous, new):
    fai, char, _, _ = inspections[0]
    char.is_conforming = previous
    fai.characteristics_passed = int(previous is True)
    fai.characteristics_failed = int(previous is False)
    db_session.commit()
    headers = user_headers(make_user(db_session, role=UserRole.QUALITY))
    response = client.put(f'{BASE}/{fai.id}/characteristics/{char.id}', headers=headers, json={'is_conforming': new})
    assert response.status_code == 200, response.text
    db_session.refresh(fai)
    assert (fai.characteristics_passed, fai.characteristics_failed) == (int(new is True), int(new is False))
    assert fai.total_characteristics == 1


@pytest.mark.parametrize('reference', ['part', 'work_order'])
@pytest.mark.parametrize('invalid', ['foreign', 'deleted'])
def test_create_references_require_live_tenant_owned_rows(client, db_session, inspections, reference, invalid):
    own_wo, foreign_wo = inspections[0][2], inspections[1][2]
    target = foreign_wo if invalid == 'foreign' else own_wo
    payload = {'part_id': own_wo.part_id}
    if reference == 'part':
        target = target.part
        payload['part_id'] = target.id
    else:
        payload['work_order_id'] = target.id
    if invalid == 'deleted':
        target.is_deleted = True
        db_session.commit()
    headers = user_headers(make_user(db_session, role=UserRole.QUALITY))
    before = snapshot(db_session)
    response = client.post(BASE, headers=headers, json=payload)
    assert response.status_code == 404, response.text
    assert snapshot(db_session) == before


def test_prefill_ignores_foreign_characteristic_attached_to_owned_parent(client, db_session, inspections):
    fai, _, _, _ = inspections[0]
    foreign_char = inspections[1][1]
    foreign_char.fai_id = fai.id
    foreign_char.char_number = 2
    db_session.commit()
    headers = user_headers(make_user(db_session, role=UserRole.QUALITY))
    response = write(client, 'prefill', inspections[0], headers)
    assert response.status_code == 200, response.text
    assert response.json()['prefilled_count'] == 1
    db_session.refresh(foreign_char)
    assert foreign_char.actual_value is None
    audit = db_session.query(AuditLog).filter_by(resource_type='fai', resource_id=fai.id).one()
    assert set(audit.new_values['actual_values']) == {'1'}


def test_prefill_refuses_legacy_foreign_work_order_reference(client, db_session, inspections):
    inspections[0][0].work_order_id = inspections[1][2].id
    db_session.commit()
    headers = user_headers(make_user(db_session, role=UserRole.QUALITY))
    before = snapshot(db_session)
    response = write(client, 'prefill', inspections[0], headers)
    assert response.status_code == 404, response.text
    assert snapshot(db_session) == before


def test_platform_active_company_controls_write_and_audit_tenant(client, db_session, inspections):
    platform = make_user(db_session, company_id=1, role=UserRole.PLATFORM_ADMIN)
    response = write(client, 'add', inspections[1], user_headers(platform, active_company_id=2))
    assert response.status_code == 200, response.text
    char = db_session.get(FAICharacteristic, response.json()['id'])
    assert char.company_id == 2
    audit = db_session.query(AuditLog).filter_by(resource_type='fai_characteristic', resource_id=char.id).one()
    assert audit.company_id == 2 and audit.user_id == platform.id


def test_prefill_audit_preserves_blank_values_and_duplicate_balloon_identities(client, db_session, inspections):
    fai, char, _, _ = inspections[0]
    char.actual_value = '  '
    duplicate = FAICharacteristic(
        company_id=1, fai_id=fai.id, char_number=1, characteristic=char.characteristic, actual_value=''
    )
    db_session.add(duplicate)
    db_session.commit()
    headers = user_headers(make_user(db_session, role=UserRole.QUALITY))
    response = write(client, 'prefill', inspections[0], headers)
    assert response.status_code == 200, response.text
    assert response.json()['prefilled_count'] == 2
    audit = db_session.query(AuditLog).filter_by(resource_type='fai', resource_id=fai.id).one()
    changes = {entry['characteristic_id']: entry for entry in audit.extra_data['characteristic_changes']}
    assert changes[char.id]['old_values']['actual_value'] == '  '
    assert changes[duplicate.id]['old_values']['actual_value'] == ''
    assert {entry['new_values']['actual_value'] for entry in changes.values()} == {'1.005'}
