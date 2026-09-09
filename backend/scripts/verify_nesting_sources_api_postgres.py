"""Real JWT/PostgreSQL/source-file race proof using only disposable synthetic originals."""

import copy
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker


def verify():
    url = sa.engine.make_url(os.environ['DATABASE_URL'])
    if (
        os.environ.get('ENVIRONMENT') != 'test'
        or url.get_backend_name() != 'postgresql'
        or url.host not in {'localhost', '127.0.0.1', 'postgres'}
    ):
        raise RuntimeError('Source API races require a local disposable PostgreSQL database')
    from fastapi.testclient import TestClient

    from app.core.security import create_access_token
    from app.db.database import Base, get_db
    from app.main import app
    from app.models.audit_log import AuditLog
    from app.models.company import Company
    from app.models.inventory import InventoryTransaction
    from app.models.quote_nesting_draft import QuoteNestingRevision
    from app.models.quote_nesting_source import QuoteNestingSourceAttempt as Attempt
    from app.models.quote_nesting_source import QuoteNestingSourceBinding as Binding
    from app.models.quote_nesting_source import QuoteNestingSourceIntent as Intent
    from app.models.quote_nesting_source import QuoteNestingSourceReceipt as Receipt
    from app.models.user import User, UserRole
    from app.services import storage_service
    from app.services.audit_service import AuditService, AuditWriteError

    schema = 'nest_source_api_' + uuid4().hex
    owner = sa.create_engine(url)
    scoped = sa.create_engine(
        url, connect_args={'options': f'-csearch_path={schema} -clock_timeout=10000 -cstatement_timeout=15000'}
    )
    quoted = owner.dialect.identifier_preparer.quote(schema)
    sessions = sessionmaker(bind=scoped)
    original_overrides = dict(app.dependency_overrides)
    original = b'\xef\xbb\xbf0\r\nSECTION\r\n999\r\ncaf\xc3\xa9\xff\r\n0\r\nENDSEC\r\n0\r\nEOF\r\n'
    source_hash = hashlib.sha256(original).hexdigest()

    def request_db():
        with sessions() as db:
            try:
                yield db
            except Exception:
                db.rollback()
                raise

    class TrackedStorage(storage_service.LocalStorageBackend):
        write_barrier = None

        def save(self, data, *, key):
            with sessions() as db:
                attempt = db.query(Attempt).filter(Attempt.storage_ref == key).one()
                assert (
                    db.query(AuditLog)
                    .filter(
                        AuditLog.resource_type == 'quote_nesting_source_attempt', AuditLog.resource_id == attempt.id
                    )
                    .count()
                    == 1
                )
            if self.write_barrier:
                self.write_barrier.wait(timeout=15)
            return super().save(data, key=key)

    try:
        with owner.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA {quoted}')
        Base.metadata.create_all(scoped)
        with sessions() as db:
            for idx in (1, 2):
                db.add(Company(id=idx, name=f'Synthetic source company {idx}', slug=f'source-{idx}'))
            db.flush()
            for idx, company in ((1, 1), (2, 2), (3, 1)):
                db.add(
                    User(
                        id=idx,
                        company_id=company,
                        email=f'source{idx}@example.test',
                        employee_id=f'SOURCE-{idx}',
                        first_name='Synthetic',
                        last_name='Estimator',
                        hashed_password='unused-synthetic-fixture',
                        role=UserRole.ADMIN,
                        is_active=True,
                    )
                )
            db.commit()
        headers = {
            idx: {
                'Authorization': f'Bearer {create_access_token(subject=idx, company_id=company)}',
                'X-Requested-With': 'XMLHttpRequest',
            }
            for idx, company in ((1, 1), (2, 2), (3, 1))
        }
        provenance = dict(
            version=1,
            sourceName='synthetic.dxf',
            sourceSha256=source_hash,
            sourceHashBasis='original-bytes',
            geometrySha256='1' * 64,
            geometryVersion='werco-geometry-v1',
            sourceUnits='in',
            resolvedUnits='in',
            unitDecision='declared',
            importerVersion='werco-dxf-v2',
            warnings=[],
        )
        parts = [
            dict(
                id=f'p{idx}',
                name=f'Synthetic profile {idx}',
                revision='A',
                quantity=1,
                rotate=True,
                color=0,
                loops=[dict(type='circle', cx=1, cy=1, r=1)],
                provenance=copy.deepcopy(provenance),
            )
            for idx in range(1, 5)
        ]
        project = {
            'version': 6,
            'units': 'in',
            'currency': 'USD',
            'name': 'Synthetic original source nest',
            'activeGroupId': 'group',
            'groups': [
                {
                    'id': 'group',
                    'quote': {
                        'version': 7,
                        'units': 'in',
                        'currency': 'USD',
                        'name': 'Synthetic carbon',
                        'material': 'Carbon steel',
                        'thickness': 0.125,
                        'margin': 0.375,
                        'gap': 0.125,
                        'objective': 'area',
                        'spacingMode': 'auto',
                        'grainAxis': 'x',
                        'parts': parts,
                        'options': [{'id': 'sheet', 'width': 96, 'height': 48, 'enabled': True, 'price': None}],
                    },
                }
            ],
        }
        app.dependency_overrides[get_db] = request_db
        with (
            TemporaryDirectory(prefix='nest-source-api-') as directory,
            patch.dict(os.environ, {'UPLOAD_DIR': directory}),
        ):
            storage = TrackedStorage()
            storage_service.override_storage(storage)
            with TestClient(app) as client:
                saved = client.post(
                    '/api/v1/quote-nesting/drafts',
                    headers=headers[1],
                    data={'request_key': str(uuid4()), 'expected_company_id': '1'},
                    files={'estimate': ('synthetic.json', json.dumps(project).encode(), 'application/json')},
                )
                assert saved.status_code == 200, saved.text
                revision = saved.json()
                path = f'/api/v1/quote-nesting/drafts/{revision["draft_id"]}/revisions/1/sources'

                def command(part_ids):
                    return dict(
                        expected_company_id=1,
                        request_key=str(uuid4()),
                        expected_input_sha256=revision['content_sha256'],
                        source_sha256=source_hash,
                        byte_count=len(original),
                        source_name='synthetic.dxf',
                        mime_type='application/dxf',
                        targets=[dict(group_id='group', part_id=name) for name in part_ids],
                    )

                def run_race(function, values):
                    barrier = Barrier(2)

                    def run(value):
                        barrier.wait(timeout=15)
                        response = function(value)
                        return response.status_code, response.json()

                    with ThreadPoolExecutor(max_workers=2) as pool:
                        return list(pool.map(run, values))

                def content(intent_id):
                    return client.post(
                        f'{path}/{intent_id}/content?expected_company_id=1',
                        content=original,
                        headers={**headers[1], 'Content-Type': 'application/octet-stream'},
                    )

                base = command(['p1', 'p2'])
                created = run_race(lambda body: client.post(path, json=body, headers=headers[1]), [base, base])
                assert [code for code, _ in created] == [200, 200], created
                assert created[0][1] == created[1][1]
                intent_id = created[0][1]['id']
                storage.write_barrier = Barrier(2)
                attached = run_race(content, [intent_id, intent_id])
                storage.write_barrier = None
                assert [code for code, _ in attached] == [200, 200], attached
                assert attached[0][1]['receipt']['id'] == attached[1][1]['receipt']['id']
                with sessions() as db:
                    assert db.query(Intent).count() == 1 and db.query(Attempt).count() == 2
                    assert db.query(Receipt).count() == 1 and db.query(Binding).count() == 2
                    assert db.query(AuditLog).filter(AuditLog.resource_type.like('quote_nesting_source_%')).count() == 4
                # Both bytes objects are tracked, including the late losing attempt.
                assert len(list(Path(directory).rglob('*.dxf'))) == 2
                recovered = client.post(
                    f'{path}/{intent_id}/finalize', json={'expected_company_id': 1}, headers=headers[1]
                )
                assert recovered.status_code == 200 and recovered.json()['receipt'] == attached[0][1]['receipt']
                download = client.get(f'{path}/{intent_id}/download', headers=headers[3])
                assert download.status_code == 200 and download.content == original
                assert download.headers['cache-control'] == 'private, no-store'
                assert client.get(path, headers=headers[2]).status_code == 404
                assert client.get(f'{path}/{intent_id}/download', headers=headers[2]).status_code == 404
                assert (
                    client.post(
                        f'{path}/{intent_id}/finalize', json={'expected_company_id': 1}, headers=headers[3]
                    ).status_code
                    == 403
                )
                assert client.post(path, json=base, headers=headers[3]).status_code == 409

                # Distinct intents for one saved part: complete one atomic receipt/binding only.
                competing = [client.post(path, json=command(['p3']), headers=headers[1]) for _ in range(2)]
                assert all(response.status_code == 200 for response in competing)
                storage.write_barrier = Barrier(2)
                results = run_race(content, [response.json()['id'] for response in competing])
                storage.write_barrier = None
                assert sorted(code for code, _ in results) == [200, 409], results
                with sessions() as db:
                    assert db.query(Receipt).count() == 2 and db.query(Binding).count() == 3

                # Storage survives a completion-audit failure; recovery uses it without another write.
                pending = client.post(path, json=command(['p4']), headers=headers[1]).json()
                audit = AuditService.log_required

                def audit_failure(self, *args, **kwargs):
                    if kwargs.get('resource_type') == 'quote_nesting_source_receipt':
                        raise AuditWriteError('Synthetic completion audit failure')
                    return audit(self, *args, **kwargs)

                with patch.object(AuditService, 'log_required', audit_failure):
                    failed = content(pending['id'])
                    assert failed.status_code == 503, failed.text
                with sessions() as db:
                    assert db.query(Receipt).count() == 2 and db.query(Binding).count() == 3
                with patch.object(storage, 'save', side_effect=AssertionError('Recovery must not write another blob')):
                    finished = client.post(
                        f'{path}/{pending["id"]}/finalize', json={'expected_company_id': 1}, headers=headers[1]
                    )
                    assert finished.status_code == 200, finished.text
                listing = client.get(path, headers=headers[1])
                assert listing.status_code == 200 and listing.json()['total'] == 4
                with sessions() as db:
                    assert db.query(Intent).count() == 4 and db.query(Attempt).count() == 5
                    assert db.query(Receipt).count() == 3 and db.query(Binding).count() == 4
                    assert (
                        db.query(AuditLog).filter(AuditLog.resource_type.like('quote_nesting_source_%')).count() == 12
                    )
                    assert db.query(QuoteNestingRevision).one().estimate_json == project
                    assert db.query(InventoryTransaction).count() == 0
                assert len(list(Path(directory).rglob('*.dxf'))) == 5
                print(
                    'PostgreSQL source JWT/API: exact original bytes, audited pre-write tracking, late-attempt/part races, tenant/actor fences and audit recovery passed; saved inputs and inventory unchanged.'
                )
    finally:
        storage_service.reset_storage()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original_overrides)
        scoped.dispose()
        with owner.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS {quoted} CASCADE')
        owner.dispose()


if __name__ == '__main__':
    verify()
