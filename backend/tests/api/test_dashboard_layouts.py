"""A saved dashboard layout is personal presentation: audited, CAS-guarded, tenant/owner isolated,
writable only by an interactive person, and never a 500 whatever a stored row holds."""

import pytest
from fastapi import HTTPException

from app.api.endpoints.dashboard_layouts import AUDIT_FAILED_DETAIL
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.dashboard_layout import DashboardLayout
from app.models.user import UserRole
from app.schemas.dashboard_layout import (
    MAX_DASHBOARD_WIDGETS,
    DashboardLayoutCommand,
    DashboardLayoutSave,
    DashboardLayoutValues,
    normalize_stored_layout,
)
from app.services.audit_service import AuditService, AuditWriteError
from app.services.dashboard_layout_service import (
    COMPANY_CHANGED_DETAIL,
    NOT_EDITABLE_DETAIL,
    STALE_VERSION_DETAIL,
    DashboardLayoutService,
)

pytestmark = pytest.mark.api

URL = '/api/v1/dashboard/layout'
RESET_URL = URL + '/reset'
RESOURCE = 'dashboard_layout'
LAYOUT = {
    'format_version': 1,
    'widgets': [
        {'id': 'attention', 'size': None},
        {'id': 'kpi.overdue', 'size': None},
        {'id': 'capacity_overview', 'size': 'wide'},
        {'id': 'presence', 'size': 'narrow'},
    ],
}
OTHER_LAYOUT = {'format_version': 1, 'widgets': [{'id': 'quality_pulse', 'size': 'full'}]}
DEFAULT_BODY = {'company_id': 1, 'version': 0, 'layout': None, 'updated_at': None, 'can_edit': True}


def save(client, headers, layout=None, *, version=0, company=1):
    return client.put(
        URL,
        headers=headers,
        json={
            'expected_company_id': company,
            'expected_version': version,
            'layout': LAYOUT if layout is None else layout,
        },
    )


def reset(client, headers, *, version=0, company=1):
    return client.post(RESET_URL, headers=headers, json={'expected_company_id': company, 'expected_version': version})


def audits(db_session):
    return db_session.query(AuditLog).filter_by(resource_type=RESOURCE).order_by(AuditLog.id).all()


def store(db_session, user, raw, *, company_id=1, version=3):
    row = DashboardLayout(company_id=company_id, user_id=user.id, version=version, layout_json=raw)
    db_session.add(row)
    db_session.commit()
    return row


# --------------------------------------------------------------------------- read, save, reset


def test_get_without_a_row_is_the_default_and_never_writes(client, auth_headers, db_session):
    assert client.get(URL).status_code == 401
    response = client.get(URL, headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json() == DEFAULT_BODY
    absent_reset = reset(client, auth_headers)
    assert absent_reset.status_code == 200, absent_reset.text
    assert absent_reset.json() == DEFAULT_BODY
    assert db_session.query(DashboardLayout).count() == 0
    assert audits(db_session) == []


def test_any_role_can_save_compare_versions_and_reset_with_required_audit(
    client, operator_headers, operator_user, db_session
):
    first = save(client, operator_headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body['version'] == 1 and body['layout'] == LAYOUT and body['can_edit'] is True
    assert body['updated_at'].endswith('Z')
    row = db_session.query(DashboardLayout).one()
    row_id = row.id
    assert row.user_id == operator_user.id and row.company_id == 1 and row.layout_json == LAYOUT

    stale = save(client, operator_headers, OTHER_LAYOUT, version=0)
    assert stale.status_code == 409 and stale.json()['detail'] == STALE_VERSION_DETAIL

    # The same layout again is a no-op: no version bump, no audit row.
    same = save(client, operator_headers, version=1)
    assert same.status_code == 200 and same.json() == body
    assert len(audits(db_session)) == 1

    changed = save(client, operator_headers, OTHER_LAYOUT, version=1)
    assert changed.status_code == 200, changed.text
    assert changed.json()['version'] == 2 and changed.json()['layout'] == OTHER_LAYOUT

    back = reset(client, operator_headers, version=2)
    assert back.status_code == 200, back.text
    assert back.json()['version'] == 3 and back.json()['layout'] is None
    db_session.expire_all()
    row = db_session.query(DashboardLayout).one()
    assert row.id == row_id and row.layout_json == {} and row.version == 3

    # A reset of a row already at the default is a no-op too.
    again = reset(client, operator_headers, version=3)
    assert again.status_code == 200 and again.json() == back.json()

    trail = audits(db_session)
    assert [entry.action for entry in trail] == ['CREATE', 'UPDATE', 'UPDATE']
    created, updated, was_reset = trail
    assert created.resource_id == row_id and created.user_id == operator_user.id
    assert created.resource_identifier == f'Employee {operator_user.id} dashboard layout'
    assert created.old_values is None
    assert created.new_values == {'version': 1, 'layout': LAYOUT}
    assert created.extra_data['source'] == RESOURCE and created.extra_data['reset_to_default'] is False
    assert updated.old_values == {'version': 1, 'layout': LAYOUT}
    assert updated.new_values == {'version': 2, 'layout': OTHER_LAYOUT}
    assert was_reset.old_values == {'version': 2, 'layout': OTHER_LAYOUT}
    assert was_reset.new_values == {'version': 3, 'layout': {}}
    assert was_reset.extra_data['reset_to_default'] is True

    # After a reset the next save customizes again from the same row.
    resumed = save(client, operator_headers, version=3)
    assert resumed.status_code == 200 and resumed.json()['version'] == 4 and resumed.json()['layout'] == LAYOUT


def test_an_empty_widget_list_is_an_empty_dashboard_not_the_default(client, auth_headers, db_session):
    empty = {'format_version': 1, 'widgets': []}
    response = save(client, auth_headers, empty)
    assert response.status_code == 200, response.text
    assert response.json()['layout'] == empty
    assert client.get(URL, headers=auth_headers).json()['layout'] == empty
    assert db_session.query(DashboardLayout).one().layout_json == empty


def test_shape_not_membership_unknown_but_well_formed_ids_and_sizes_are_kept(client, auth_headers):
    """The catalog and size vocabulary are the SPA's; the server stores well-formed placements."""
    layout = {'format_version': 1, 'widgets': [{'id': 'some.future_widget', 'size': 'enormous'}]}
    response = save(client, auth_headers, layout)
    assert response.status_code == 200, response.text
    assert response.json()['layout'] == layout
    # format_version may be omitted on input; it defaults to 1.
    defaulted = save(client, auth_headers, {'widgets': [{'id': 'presence'}]}, version=1)
    assert defaulted.status_code == 200, defaulted.text
    assert defaulted.json()['layout'] == {'format_version': 1, 'widgets': [{'id': 'presence', 'size': None}]}


def test_owner_and_company_layouts_are_isolated(client, auth_headers, operator_headers, db_session, test_user):
    db_session.add(Company(id=2, name='Other layouts', slug='other-layouts'))
    db_session.commit()
    elsewhere = store(db_session, test_user, OTHER_LAYOUT, company_id=2, version=5)
    elsewhere_id = elsewhere.id

    # The same person's layout in another company is invisible here.
    assert client.get(URL, headers=auth_headers).json() == DEFAULT_BODY
    assert save(client, auth_headers).status_code == 200
    # Another employee of the same company sees their own default.
    assert client.get(URL, headers=operator_headers).json()['layout'] is None
    # A command aimed at another company is refused before anything is touched.
    wrong = save(client, auth_headers, OTHER_LAYOUT, version=1, company=2)
    assert wrong.status_code == 409 and wrong.json()['detail'] == COMPANY_CHANGED_DETAIL
    wrong_reset = reset(client, auth_headers, version=1, company=2)
    assert wrong_reset.status_code == 409 and wrong_reset.json()['detail'] == COMPANY_CHANGED_DETAIL

    db_session.expire_all()
    rows = {row.company_id: row for row in db_session.query(DashboardLayout).all()}
    assert set(rows) == {1, 2}
    assert rows[1].layout_json == LAYOUT and rows[1].version == 1
    assert rows[2].id == elsewhere_id and rows[2].layout_json == OTHER_LAYOUT and rows[2].version == 5


# --------------------------------------------------------------------------- who may write


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
def test_service_write_gate_does_not_hide_the_readable_layout(db_session, test_user, attribute, value):
    store(db_session, test_user, LAYOUT, version=1)
    setattr(test_user, attribute, value)
    service = DashboardLayoutService(db_session, test_user, 1)
    current = service.get()
    assert current.can_edit is False and current.layout is not None
    assert current.layout.model_dump() == LAYOUT
    audit = AuditService(db_session, user=test_user, company_id=1)
    for command, is_reset in (
        (DashboardLayoutSave(expected_company_id=1, expected_version=1, layout=OTHER_LAYOUT), False),
        (DashboardLayoutCommand(expected_company_id=1, expected_version=1), True),
    ):
        with pytest.raises(HTTPException) as exc:
            service.save(command, audit, reset=is_reset)
        assert exc.value.status_code == 403 and exc.value.detail == NOT_EDITABLE_DETAIL
    assert db_session.query(DashboardLayout).one().layout_json == LAYOUT
    assert audits(db_session) == []


def test_an_inactive_company_cannot_be_written(db_session, test_user):
    db_session.query(Company).filter(Company.id == 1).update({'is_active': False})
    db_session.commit()
    service = DashboardLayoutService(db_session, test_user, 1)
    assert service.get().can_edit is False
    with pytest.raises(HTTPException) as exc:
        service.save(
            DashboardLayoutSave(expected_company_id=1, expected_version=0, layout=LAYOUT),
            AuditService(db_session, user=test_user, company_id=1),
        )
    assert exc.value.status_code == 403


@pytest.mark.parametrize('elevation', ['superuser', 'platform_admin'])
def test_a_platform_principal_may_write_its_own_layout_in_a_writable_switched_company(db_session, test_user, elevation):
    if elevation == 'superuser':
        test_user.is_superuser = True
    else:
        test_user.role = UserRole.PLATFORM_ADMIN
    test_user.company_id = 2  # home company differs from the active one
    service = DashboardLayoutService(db_session, test_user, 1)
    assert service.can_edit() is True
    result = service.save(
        DashboardLayoutSave(expected_company_id=1, expected_version=0, layout=LAYOUT),
        AuditService(db_session, user=test_user, company_id=1),
    )
    assert result.version == 1 and result.layout is not None and result.layout.model_dump() == LAYOUT


def test_save_without_a_layout_is_a_programming_error(db_session, test_user):
    service = DashboardLayoutService(db_session, test_user, 1)
    with pytest.raises(TypeError):
        service.save(DashboardLayoutCommand(expected_company_id=1, expected_version=0), None)
    assert db_session.query(DashboardLayout).count() == 0


def test_read_only_company_context_reads_but_cannot_save_or_reset(client, db_session, test_user, auth_headers):
    assert save(client, auth_headers).status_code == 200
    headers = {
        'Authorization': f'Bearer {create_access_token(subject=test_user.id, company_id=1, read_only=True)}',
        'X-Requested-With': 'XMLHttpRequest',
    }
    response = client.get(URL, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['can_edit'] is False and response.json()['layout'] == LAYOUT
    assert save(client, headers, OTHER_LAYOUT, version=1).status_code == 403
    assert reset(client, headers, version=1).status_code == 403
    db_session.expire_all()
    assert db_session.query(DashboardLayout).one().version == 1
    assert len(audits(db_session)) == 1


def test_a_real_api_token_reads_but_cannot_save_or_reset(client, admin_headers, auth_headers, test_user, db_session):
    assert save(client, auth_headers).status_code == 200
    issued = client.post(
        '/api/v1/api-tokens/', json={'user_id': test_user.id, 'label': 'dashboard probe'}, headers=admin_headers
    )
    assert issued.status_code == 201, issued.text
    headers = {'Authorization': f"Bearer {issued.json()['token']}", 'X-Requested-With': 'XMLHttpRequest'}
    response = client.get(URL, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['can_edit'] is False and response.json()['layout'] == LAYOUT
    refused = save(client, headers, OTHER_LAYOUT, version=1)
    assert refused.status_code == 403 and refused.json()['detail'] == NOT_EDITABLE_DETAIL
    refused_reset = reset(client, headers, version=1)
    assert refused_reset.status_code == 403 and refused_reset.json()['detail'] == NOT_EDITABLE_DETAIL
    db_session.expire_all()
    row = db_session.query(DashboardLayout).one()
    assert row.version == 1 and row.layout_json == LAYOUT
    assert len(audits(db_session)) == 1


def test_a_kiosk_scoped_token_cannot_reach_the_layout_at_all(client, test_user, db_session):
    token = create_access_token(subject=test_user.id, company_id=1, scope='kiosk')
    headers = {'Authorization': f'Bearer {token}', 'X-Requested-With': 'XMLHttpRequest'}
    assert client.get(URL, headers=headers).status_code == 403
    assert save(client, headers).status_code == 403
    assert reset(client, headers).status_code == 403
    assert db_session.query(DashboardLayout).count() == 0


# --------------------------------------------------------------------------- strict input


def _widgets(count):
    return [{'id': f'w{index}'} for index in range(count)]


@pytest.mark.parametrize(
    'body',
    [
        pytest.param({'expected_company_id': 1, 'expected_version': 0}, id='layout_missing'),
        pytest.param({'expected_company_id': 1, 'expected_version': 0, 'layout': None}, id='layout_null'),
        pytest.param({'expected_company_id': 1, 'expected_version': 0, 'layout': {}}, id='layout_empty_object'),
        pytest.param(
            {'expected_company_id': 1, 'expected_version': 0, 'layout': {'format_version': 1}}, id='no_widgets'
        ),
        pytest.param({'expected_company_id': 1, 'expected_version': 0, 'layout': {'widgets': None}}, id='widgets_null'),
        pytest.param(
            {'expected_company_id': 1, 'expected_version': 0, 'layout': {'format_version': 2, 'widgets': []}},
            id='future_format',
        ),
        pytest.param(
            {'expected_company_id': 1, 'expected_version': 0, 'layout': {'widgets': [], 'theme': 'dark'}},
            id='unknown_layout_key',
        ),
        pytest.param(
            {'expected_company_id': 1, 'expected_version': 0, 'layout': {'widgets': [{'id': 'presence', 'x': 1}]}},
            id='unknown_placement_key',
        ),
        pytest.param({'expected_company_id': 1, 'expected_version': 0, 'layout': LAYOUT, 'user_id': 9}, id='extra'),
        pytest.param({'expected_company_id': 0, 'expected_version': 0, 'layout': LAYOUT}, id='company_zero'),
        pytest.param({'expected_company_id': 1, 'expected_version': -1, 'layout': LAYOUT}, id='negative_version'),
        pytest.param({'expected_version': 0, 'layout': LAYOUT}, id='company_missing'),
        pytest.param(
            {
                'expected_company_id': 1,
                'expected_version': 0,
                'layout': {'widgets': _widgets(MAX_DASHBOARD_WIDGETS + 1)},
            },
            id='too_many_widgets',
        ),
        pytest.param(
            {
                'expected_company_id': 1,
                'expected_version': 0,
                'layout': {'widgets': [{'id': 'presence'}, {'id': 'presence', 'size': 'full'}]},
            },
            id='duplicate_ids',
        ),
    ]
    + [
        pytest.param(
            {'expected_company_id': 1, 'expected_version': 0, 'layout': {'widgets': [{'id': bad}]}},
            id=f'bad_id_{index}',
        )
        for index, bad in enumerate(
            ['', 'Presence', '1presence', 'kpi/overdue', '__proto__', '_hidden', 'a' * 65, 'wide id', 'presence\n', 7]
        )
    ]
    + [
        pytest.param(
            {'expected_company_id': 1, 'expected_version': 0, 'layout': {'widgets': [{'id': 'presence', 'size': bad}]}},
            id=f'bad_size_{index}',
        )
        for index, bad in enumerate(['', 'Half', 'half-width', '2x', 'x' * 25, 'two.thirds', 3])
    ],
)
def test_input_is_strict(client, auth_headers, db_session, body):
    response = client.put(URL, headers=auth_headers, json=body)
    assert response.status_code == 422, response.text
    assert db_session.query(DashboardLayout).count() == 0


def test_the_widget_cap_itself_is_accepted(client, auth_headers):
    layout = {'format_version': 1, 'widgets': [{'id': f'w{index}', 'size': None} for index in range(64)]}
    response = save(client, auth_headers, layout)
    assert response.status_code == 200, response.text
    assert response.json()['layout'] == layout


def test_reset_rejects_a_layout_or_other_extras(client, auth_headers):
    body = {'expected_company_id': 1, 'expected_version': 0, 'layout': LAYOUT}
    assert client.post(RESET_URL, headers=auth_headers, json=body).status_code == 422


# --------------------------------------------------------------------------- lenient stored rows


def _placements(*ids_and_sizes):
    return [{'id': widget_id, 'size': size} for widget_id, size in ids_and_sizes]


STORED_ROWS = {
    'future_format': ({'format_version': 2, 'widgets': [{'id': 'attention'}]}, None),
    'boolean_format': ({'format_version': True, 'widgets': [{'id': 'attention'}]}, None),
    'float_format': ({'format_version': 1.0, 'widgets': [{'id': 'attention'}]}, None),
    'missing_format': ({'widgets': [{'id': 'attention'}]}, None),
    'list_root': (['attention'], None),
    'string_root': ('attention', None),
    'number_root': (7, None),
    'missing_widgets': ({'format_version': 1}, None),
    'widgets_not_a_list': ({'format_version': 1, 'widgets': {'id': 'attention'}}, None),
    'all_entries_invalid': (
        {'format_version': 1, 'widgets': [{'id': 'Attention'}, 'attention', 5, None, {'size': 'wide'}, {'id': 'a/b'}]},
        None,
    ),
    'bad_size_kept_without_its_size': (
        {
            'format_version': 1,
            'widgets': [{'id': 'capacity_overview', 'size': 'Enormous'}, {'id': 'presence', 'size': 7}],
        },
        {'format_version': 1, 'widgets': _placements(('capacity_overview', None), ('presence', None))},
    ),
    'unknown_entry_keys_ignored': (
        {'format_version': 1, 'widgets': [{'id': 'presence', 'size': 'narrow', 'pinned': True}]},
        {'format_version': 1, 'widgets': _placements(('presence', 'narrow'))},
    ),
    'unknown_root_keys_ignored': (
        {'format_version': 1, 'widgets': [{'id': 'presence'}], 'theme': 'dark'},
        {'format_version': 1, 'widgets': _placements(('presence', None))},
    ),
    'duplicates_keep_the_first': (
        {
            'format_version': 1,
            'widgets': [{'id': 'presence', 'size': 'narrow'}, {'id': 'presence', 'size': 'full'}, {'id': 'attention'}],
        },
        {'format_version': 1, 'widgets': _placements(('presence', 'narrow'), ('attention', None))},
    ),
    'invalid_entries_dropped_valid_kept': (
        {'format_version': 1, 'widgets': [5, {'id': 'presence'}, {'id': 'BAD'}, ['x']]},
        {'format_version': 1, 'widgets': _placements(('presence', None))},
    ),
    'over_the_cap_keeps_the_first_64': (
        {'format_version': 1, 'widgets': [{'id': 'w0'}] + _widgets(80)},
        {'format_version': 1, 'widgets': [{'id': f'w{index}', 'size': None} for index in range(64)]},
    ),
    'stored_empty_list_is_an_empty_dashboard': (
        {'format_version': 1, 'widgets': []},
        {'format_version': 1, 'widgets': []},
    ),
}


@pytest.mark.parametrize('raw,expected', list(STORED_ROWS.values()), ids=list(STORED_ROWS))
def test_normalizer_is_lenient_and_returns_the_input_contract(raw, expected):
    result = normalize_stored_layout(raw)
    if expected is None:
        assert result is None
    else:
        assert isinstance(result, DashboardLayoutValues)
        assert result.model_dump(mode='json') == expected


def test_normalizer_treats_the_reset_encoding_as_the_default():
    assert normalize_stored_layout({}) is None
    assert normalize_stored_layout(LAYOUT).model_dump(mode='json') == LAYOUT


@pytest.mark.parametrize('raw,expected', list(STORED_ROWS.values()), ids=list(STORED_ROWS))
def test_any_stored_row_reads_and_can_be_saved_over(client, auth_headers, db_session, test_user, raw, expected):
    store(db_session, test_user, raw, version=3)
    read = client.get(URL, headers=auth_headers)
    assert read.status_code == 200, read.text
    assert read.json()['version'] == 3 and read.json()['layout'] == expected

    saved = save(client, auth_headers, version=3)
    assert saved.status_code == 200, saved.text
    assert saved.json()['version'] == 4 and saved.json()['layout'] == LAYOUT
    entry = audits(db_session)[-1]
    # The audit row records what was actually stored, not the normalized view of it.
    assert entry.action == 'UPDATE' and entry.old_values == {'version': 3, 'layout': raw}
    db_session.expire_all()
    assert db_session.query(DashboardLayout).one().layout_json == LAYOUT


@pytest.mark.parametrize('raw,expected', list(STORED_ROWS.values()), ids=list(STORED_ROWS))
def test_any_stored_row_can_be_reset(client, auth_headers, db_session, test_user, raw, expected):
    store(db_session, test_user, raw, version=3)
    response = reset(client, auth_headers, version=3)
    assert response.status_code == 200, response.text
    assert response.json()['version'] == 4 and response.json()['layout'] is None
    entry = audits(db_session)[-1]
    assert entry.old_values == {'version': 3, 'layout': raw} and entry.new_values == {'version': 4, 'layout': {}}
    assert entry.extra_data['reset_to_default'] is True
    db_session.expire_all()
    assert db_session.query(DashboardLayout).one().layout_json == {}


def test_the_no_op_check_compares_the_raw_row_not_its_normalized_view(client, auth_headers, db_session, test_user):
    raw = {
        'format_version': 1,
        'widgets': [{'id': 'presence', 'size': 'narrow'}, {'id': 'presence', 'size': 'full'}, {'id': 'attention'}],
    }
    store(db_session, test_user, raw, version=2)
    shown = client.get(URL, headers=auth_headers).json()['layout']
    # Saving exactly what the GET showed still rewrites the row: the stored bytes differ.
    response = save(client, auth_headers, shown, version=2)
    assert response.status_code == 200, response.text
    assert response.json()['version'] == 3 and response.json()['layout'] == shown
    assert len(audits(db_session)) == 1
    db_session.expire_all()
    assert db_session.query(DashboardLayout).one().layout_json == shown


def test_reset_of_a_row_already_at_the_default_writes_nothing(client, auth_headers, db_session, test_user):
    store(db_session, test_user, {}, version=2)
    response = reset(client, auth_headers, version=2)
    assert response.status_code == 200, response.text
    assert response.json()['version'] == 2 and response.json()['layout'] is None
    assert audits(db_session) == []
    assert reset(client, auth_headers, version=1).status_code == 409  # still version-checked


# --------------------------------------------------------------------------- failure atomicity


@pytest.mark.parametrize('operation', ['create', 'update', 'reset'])
def test_required_audit_failure_rolls_back_and_answers_503(client, auth_headers, db_session, monkeypatch, operation):
    if operation != 'create':
        assert save(client, auth_headers).status_code == 200

    def fail(self, *args, **kwargs):
        raise AuditWriteError('unavailable')

    monkeypatch.setattr(AuditService, 'log_required', fail)
    if operation == 'reset':
        response = reset(client, auth_headers, version=1)
    else:
        response = save(client, auth_headers, OTHER_LAYOUT, version=int(operation == 'update'))
    assert response.status_code == 503, response.text
    assert response.json()['detail'] == AUDIT_FAILED_DETAIL
    db_session.rollback()
    db_session.expire_all()
    rows = db_session.query(DashboardLayout).all()
    if operation == 'create':
        assert rows == []
        assert audits(db_session) == []
    else:
        assert len(rows) == 1 and rows[0].version == 1 and rows[0].layout_json == LAYOUT
        assert len(audits(db_session)) == 1


def test_a_lost_first_create_race_is_a_409_not_a_500(client, auth_headers, db_session, test_user, monkeypatch):
    store(db_session, test_user, LAYOUT, version=1)
    # Simulate the race: the other request's INSERT committed after this one read "no row".
    monkeypatch.setattr('app.services.dashboard_layout_service._layout_row', lambda *args, **kwargs: None)
    response = save(client, auth_headers, OTHER_LAYOUT, version=0)
    assert response.status_code == 409, response.text
    assert response.json()['detail'] == STALE_VERSION_DETAIL
    monkeypatch.undo()
    db_session.expire_all()
    row = db_session.query(DashboardLayout).one()
    assert row.version == 1 and row.layout_json == LAYOUT
    assert audits(db_session) == []
