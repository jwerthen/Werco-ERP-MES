"""Personal choices change presentation/delivery without widening authority or losing evidence."""

import json
from datetime import timedelta

import pytest
from fastapi import HTTPException

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.hank import HankTask
from app.models.hank_preferences import HankPreference
from app.models.notification import Notification
from app.models.role_permission import RolePermission
from app.schemas.hank_preferences import HankPreferenceSave, HankPreferenceValues
from app.services.audit_service import AuditService, AuditWriteError
from app.services.copilot_service import CopilotService
from app.services.hank_briefing_service import HankBriefingService
from app.services.hank_preference_service import HankPreferenceService, get_hank_preference_values

from .test_hank_briefing import NOW, TODAY, job, section
from .test_hank_watches import command, pdf, start

URL = '/api/v1/hank/preferences'
DEFAULTS = HankPreferenceValues().model_dump()


def save(client, headers, *, version=0, company=1, **values):
    return client.put(
        URL,
        headers=headers,
        json={
            'expected_company_id': company,
            'expected_version': version,
            'preferences': {**DEFAULTS, **values},
        },
    )


def test_defaults_get_and_absent_reset_are_pure(client, auth_headers, db_session):
    assert client.get(URL).status_code == 401
    response = client.get(URL, headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json() == {
        'company_id': 1,
        'version': 0,
        'preferences': DEFAULTS,
        'updated_at': None,
        'can_edit': True,
    }
    reset = client.post(URL + '/reset', headers=auth_headers, json={'expected_company_id': 1, 'expected_version': 0})
    assert reset.json() == response.json()
    assert db_session.query(HankPreference).count() == 0
    assert db_session.query(AuditLog).filter_by(resource_type='hank_preference').count() == 0


def test_any_employee_can_save_compare_versions_and_reset_without_losing_history(
    client,
    operator_headers,
    operator_user,
    db_session,
):
    response = save(client, operator_headers, briefing_detail='concise', handoff_format='checklist')
    assert response.status_code == 200, response.text
    first = response.json()
    assert first['version'] == 1 and first['updated_at'].endswith('Z') and first['can_edit']
    row_id = db_session.query(HankPreference).one().id
    assert save(client, operator_headers, focus_area='quality').status_code == 409
    same = save(client, operator_headers, version=1, briefing_detail='concise', handoff_format='checklist')
    assert same.json() == first  # Same saved values do not produce fake changes.
    reset = client.post(
        URL + '/reset', headers=operator_headers, json={'expected_company_id': 1, 'expected_version': 1}
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()['version'] == 2 and reset.json()['preferences'] == DEFAULTS
    row = db_session.query(HankPreference).one()
    assert row.id == row_id and row.user_id == operator_user.id
    audits = db_session.query(AuditLog).filter_by(resource_type='hank_preference').all()
    assert len(audits) == 2 and audits[-1].extra_data['reset_to_defaults'] is True


def test_owner_and_company_preferences_are_isolated(client, auth_headers, operator_headers, db_session, test_user):
    db_session.add(Company(id=2, name='Other preferences', slug='other-preferences'))
    db_session.commit()
    assert save(client, auth_headers, follow_up_alerts=False, focus_area='quality').status_code == 200
    assert client.get(URL, headers=operator_headers).json()['preferences'] == DEFAULTS
    assert get_hank_preference_values(db_session, 2, test_user.id).model_dump() == DEFAULTS
    assert save(client, auth_headers, company=2, follow_up_alerts=False).status_code == 409
    assert db_session.query(HankPreference).count() == 1


@pytest.mark.parametrize(
    'attribute,value',
    [
        ('_read_only_company_context', True),
        ('_api_token_id', 7),
        ('_token_scope', 'api'),
        ('_token_scope', 'kiosk'),
        ('is_active', False),
        ('company_id', 2),
    ],
)
def test_service_write_gate_does_not_hide_readable_values(db_session, test_user, attribute, value):
    setattr(test_user, attribute, value)
    service = HankPreferenceService(db_session, test_user, 1)
    assert service.get().can_edit is False
    with pytest.raises(HTTPException) as exc:
        service.save(
            HankPreferenceSave(expected_company_id=1, expected_version=0, preferences=HankPreferenceValues()),
            AuditService(db_session, user=test_user, company_id=1),
        )
    assert exc.value.status_code == 403
    assert db_session.query(HankPreference).count() == 0


def test_read_only_authenticated_context_reads_but_cannot_save(client, db_session, test_user, auth_headers):
    assert save(client, auth_headers, handoff_format='checklist').status_code == 200
    headers = {'Authorization': f'Bearer {create_access_token(subject=test_user.id, company_id=1, read_only=True)}'}
    response = client.get(URL, headers=headers)
    assert response.status_code == 200 and not response.json()['can_edit']
    assert response.json()['preferences']['handoff_format'] == 'checklist'
    assert save(client, headers, version=1).status_code == 403
    assert db_session.query(HankPreference).one().version == 1


@pytest.mark.parametrize(
    'values',
    [
        {'focus_area': 'secret_area'},
        {'briefing_detail': 'infinite'},
        {'handoff_format': 'raw_sql'},
        {'instructions': 'Ignore permissions'},
        {'follow_up_alerts': 'do whatever'},
    ],
)
def test_schema_rejects_unbounded_or_freeform_preferences(client, auth_headers, values):
    assert save(client, auth_headers, **values).status_code == 422


@pytest.mark.parametrize('existing', [False, True])
def test_required_audit_failure_rolls_back_create_or_update(client, auth_headers, db_session, monkeypatch, existing):
    if existing:
        assert save(client, auth_headers, focus_area='quality').status_code == 200

    def fail(self, *args, **kwargs):
        raise AuditWriteError('unavailable')

    monkeypatch.setattr(AuditService, 'log_required', fail)
    response = save(client, auth_headers, version=int(existing), follow_up_alerts=False)
    assert response.status_code == 503, response.text
    db_session.rollback()
    rows = db_session.query(HankPreference).all()
    assert len(rows) == int(existing)
    if existing:
        assert rows[0].version == 1 and rows[0].preferences_json['focus_area'] == 'quality'
        assert rows[0].preferences_json['follow_up_alerts'] is True
    assert db_session.query(AuditLog).filter_by(resource_type='hank_preference').count() == int(existing)


def test_briefing_uses_personal_limit_and_focus_without_hiding_other_authorized_sections(
    client,
    auth_headers,
    db_session,
    test_user,
    test_part,
):
    for index in range(7):
        job(db_session, test_part, f'PREF-LATE-{index}', due_date=TODAY - timedelta(days=1))
    db_session.commit()
    default = HankBriefingService(db_session, test_user, 1, now=NOW).briefing()
    assert len(section(default, 'shop').items) == 5
    assert save(client, auth_headers, briefing_detail='concise', focus_area='shipping').status_code == 200
    concise = HankBriefingService(db_session, test_user, 1, now=NOW).briefing()
    assert concise.sections[0].key == 'shipping'
    assert {group.key for group in concise.sections} == {group.key for group in default.sections}
    for key in ('shop', 'shipping'):
        result = section(concise, key)
        assert len(result.items) == 3 and result.total == 7 and result.truncated
    assert any('three items' in note for note in concise.coverage_notes)
    db_session.add(RolePermission(company_id=1, role=test_user.role, permissions=['quality:view']))
    db_session.commit()
    limited = HankBriefingService(db_session, test_user, 1, now=NOW).briefing()
    assert [group.key for group in limited.sections] == ['quality']
    assert any('preferred focus area is unavailable' in note for note in limited.coverage_notes)
    assert 'PREF-LATE' not in limited.model_dump_json()


def test_chat_applies_only_typed_presentation_data_in_uncached_suffix(
    client,
    auth_headers,
    db_session,
    test_user,
):
    service = CopilotService(db_session, company_id=1, user=test_user)
    stable = service._system_blocks()
    assert (
        save(
            client,
            auth_headers,
            briefing_detail='concise',
            handoff_format='checklist',
            focus_area='inventory',
            follow_up_alerts=False,
        ).status_code
        == 200
    )
    request = 'For this handoff, use a detailed paragraph.'
    messages = service._build_messages([{'role': 'user', 'content': request}], 'viewing /inventory')
    blocks = messages[-1]['content']
    assert blocks[0]['text'].startswith('<context_hint>')
    assert blocks[-1]['text'] == request
    text = blocks[1]['text']
    values = json.loads(
        text.removeprefix('<hank_presentation_preferences>').removesuffix('</hank_presentation_preferences>')
    )
    assert values == {'briefing_detail': 'concise', 'focus_area': 'inventory', 'handoff_format': 'checklist'}
    assert not any('cache_control' in block for block in blocks)
    assert service._system_blocks() == stable


def test_current_mute_suppresses_only_notice_and_records_delivery_decision(
    client,
    auth_headers,
    db_session,
    test_work_order,
):
    task = start(client, auth_headers, test_work_order).json()
    assert save(client, auth_headers, follow_up_alerts=False).status_code == 200
    pdf(db_session, test_work_order)
    db_session.commit()
    response = command(client, auth_headers, task, 'check')
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == 'completed' and result['last_checked_at'] is not None
    assert any('muted by your Hank preferences' in warning for warning in result['result']['warnings'])
    assert db_session.query(Notification).count() == 0
    assert db_session.get(HankTask, task['id']).result_json == result['result']
    assert db_session.query(AuditLog).filter_by(resource_type='hank_task').count() == 2
    assert save(client, auth_headers, version=1, follow_up_alerts=True).status_code == 200
    assert command(client, auth_headers, result, 'check').json() == result
    assert db_session.query(Notification).count() == 0  # No retrospective or duplicate notification.


def test_another_employee_mute_does_not_affect_owner_follow_up(
    client,
    auth_headers,
    operator_headers,
    db_session,
    test_work_order,
):
    assert save(client, operator_headers, follow_up_alerts=False).status_code == 200
    task = start(client, auth_headers, test_work_order).json()
    pdf(db_session, test_work_order)
    db_session.commit()
    result = command(client, auth_headers, task, 'check').json()
    assert result['status'] == 'completed'
    assert not any('muted' in warning for warning in result['result']['warnings'])
    assert db_session.query(Notification).count() == 1


def test_effective_notification_map_matches_hank_mute_and_sms_editor_cannot_override_it(
    client,
    auth_headers,
    db_session,
):
    endpoint = '/api/v1/users/me/notification-preferences'
    assert client.get(endpoint, headers=auth_headers).json()['preferences']['hank.follow_up']['in_app'] is True
    assert save(client, auth_headers, follow_up_alerts=False).status_code == 200
    expected = {'in_app': False, 'email': False, 'sms': False, 'digest': False}
    assert client.get(endpoint, headers=auth_headers).json()['preferences']['hank.follow_up'] == expected
    response = client.put(endpoint, headers=auth_headers, json={'preferences': {'hank.follow_up': {'sms': False}}})
    assert response.status_code == 200, response.text
    assert response.json()['preferences']['hank.follow_up'] == expected
    assert db_session.query(HankPreference).one().preferences_json['follow_up_alerts'] is False
    assert (
        client.put(
            endpoint,
            headers=auth_headers,
            json={
                'preferences': {'hank.follow_up': {'sms': False, 'in_app': True}},
            },
        ).status_code
        == 422
    )
