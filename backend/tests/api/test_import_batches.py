from uuid import uuid4

import pytest

from app.models.import_batch import ImportBatchRow

pytestmark = [pytest.mark.api, pytest.mark.requires_db]
URL = '/api/v1/import/batches'


def test_batch_api_returns_paged_durable_receipt(client, admin_headers, db_session):
    response = client.post(
        f'{URL}/prepare',
        headers=admin_headers,
        data={'entity': 'customers', 'request_key': str(uuid4())},
        files={'file': ('customers.csv', b'name\nAPI Imported Customer\n', 'text/csv')},
    )
    assert response.status_code == 200, response.text
    review = response.json()
    assert review['counts'] == {'ready': 1}
    assert review['created_at'].endswith('Z')
    commit = client.post(
        f'{URL}/{review["id"]}/commit', headers=admin_headers, data={'expected_version': review['version']}
    )
    assert commit.status_code == 200, commit.text
    receipt = commit.json()
    assert receipt['counts'] == {'created': 1}
    assert receipt['rows'][0]['result']['record_id']
    assert client.get(f'{URL}/{review["id"]}', headers=admin_headers).json()['version'] == receipt['version']
    assert client.get(URL, headers=admin_headers).json()['batches'][0]['id'] == review['id']
    assert db_session.query(ImportBatchRow).one().status == 'created'


def test_upload_is_bounded_before_parsing(client, admin_headers, monkeypatch):
    import app.api.endpoints.import_batches as endpoint

    monkeypatch.setattr(endpoint, 'MAX_IMPORT_FILE_BYTES', 10)
    result = client.post(
        f'{URL}/prepare',
        headers=admin_headers,
        data={'entity': 'customers', 'request_key': str(uuid4())},
        files={'file': ('too-large.csv', b'name\n' + b'X' * 20, 'text/csv')},
    )
    assert result.status_code == 413


def test_operator_cannot_prepare_or_read_batches(client, operator_headers):
    assert client.get(URL, headers=operator_headers).status_code == 403
    result = client.post(
        f'{URL}/prepare',
        headers=operator_headers,
        data={'entity': 'parts', 'request_key': str(uuid4())},
        files={'file': ('parts.csv', b'part_number,name,part_type\nP-1,Test,manufactured', 'text/csv')},
    )
    assert result.status_code == 403


def test_failed_export_commits_metadata_only_audit_and_never_passwords(client, admin_headers, db_session):
    from app.models.audit_log import AuditLog

    secret = 'WeakPasswordNeverStoreMe'
    response = client.post(
        f'{URL}/prepare',
        headers=admin_headers,
        data={'entity': 'users', 'request_key': str(uuid4())},
        files={
            'file': (
                'employees.csv',
                f'employee_id,first_name,last_name,role,password\nEXPORT-1,Export,Person,manager,{secret}\n'.encode(),
                'text/csv',
            )
        },
    )
    assert response.status_code == 200, response.text
    batch_id = response.json()['id']
    exported = client.get(f'{URL}/{batch_id}/failed-rows.csv', headers=admin_headers)
    assert exported.status_code == 200
    assert secret not in exported.text
    assert 'password' not in exported.text.splitlines()[0]
    db_session.rollback()
    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == 'import_batch', AuditLog.resource_id == batch_id, AuditLog.action == 'EXPORT')
        .one()
    )
    assert audit.company_id == 1
    assert audit.extra_data == {'entity': 'users', 'failed_row_count': 1}
    assert audit.old_values is None and audit.new_values is None
    assert secret not in str(audit.extra_data) + str(audit.description)
