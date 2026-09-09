"""Real JWT/API races on isolated PostgreSQL; runtime readiness and queue are stubbed.

Invoked in a separate process by verify_operations_postgres before the E2E seed.
The process creates only a UUID-named schema, uses independent request sessions,
and removes that schema even after failed assertions. No geometry child runs.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import AsyncMock, Mock, patch
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
        raise RuntimeError('API races require a local disposable PostgreSQL database')

    from fastapi.testclient import TestClient

    from app.core.nesting_geometry_profile import geometry_profile_identity
    from app.core.security import create_access_token
    from app.db.database import Base, get_db
    from app.main import app
    from app.models.audit_log import AuditLog
    from app.models.company import Company
    from app.models.quote_nesting_run import QuoteNestingRun
    from app.models.user import User, UserRole
    from app.schemas.quote_nesting_runs import SOLVER_VERSION

    schema = 'nest_api_check_' + uuid4().hex
    owner = sa.create_engine(url)
    scoped = sa.create_engine(
        url, connect_args={'options': f'-csearch_path={schema} -clock_timeout=10000 -cstatement_timeout=15000'}
    )
    quoted = owner.dialect.identifier_preparer.quote(schema)
    sessions = sessionmaker(bind=scoped)
    original_overrides = dict(app.dependency_overrides)

    def request_db():
        with sessions() as db:
            try:
                yield db
            except Exception:
                db.rollback()
                raise

    try:
        with owner.begin() as db:
            db.exec_driver_sql(f'CREATE SCHEMA {quoted}')
        Base.metadata.create_all(scoped)
        with sessions() as db:
            for idx in (1, 2):
                db.add(Company(id=idx, name=f'Synthetic company {idx}', slug=f'synthetic-run-{idx}'))
            db.flush()
            for idx in (1, 2):
                db.add(
                    User(
                        id=idx,
                        company_id=idx,
                        email=f'synthetic{idx}@example.test',
                        employee_id=f'SYN-{idx}',
                        first_name='Synthetic',
                        last_name='Estimator',
                        hashed_password='unused-test-fixture',
                        role=UserRole.ADMIN,
                        is_active=True,
                    )
                )
            db.commit()
        headers = {
            idx: {
                'Authorization': f'Bearer {create_access_token(subject=idx, company_id=idx)}',
                'X-Requested-With': 'XMLHttpRequest',
            }
            for idx in (1, 2)
        }
        estimate = {
            'version': 15,
            'units': 'in',
            'currency': 'USD',
            'name': 'Synthetic API race',
            'activeGroupId': 'group',
            'groups': [
                {
                    'id': 'group',
                    'quote': {
                        'version': 14,
                        'geometryProfile': geometry_profile_identity(),
                        'units': 'in',
                        'currency': 'USD',
                        'name': 'Synthetic carbon',
                        'material': 'Carbon steel',
                        'thickness': 0.125,
                        'margin': 0.375,
                        'gap': 0.125,
                        'objective': 'area',
                        'spacingMode': 'auto',
                        'parts': [
                            {
                                'id': 'disk',
                                'name': 'Synthetic disk',
                                'quantity': 1,
                                'rotate': False,
                                'color': 0,
                                'rotationMode': 'fixed',
                                'loops': [{'type': 'circle', 'cx': 1, 'cy': 1, 'r': 1}],
                            }
                        ],
                        'options': [{'id': 'sheet', 'width': 96, 'height': 48, 'enabled': True, 'price': None}],
                    },
                }
            ],
        }
        runtime = dict(
            release='synthetic-release',
            protocol=1,
            solver_version=SOLVER_VERSION,
            bundle_sha256='a' * 64,
            node_version='v22.20.0',
        )
        app.dependency_overrides[get_db] = request_db
        with (
            patch(
                'app.api.endpoints.quote_nesting_runs.runtime_status',
                AsyncMock(return_value={'available': True, 'identity': runtime}),
            ),
            patch('app.services.quote_nesting_run_outbox.enqueue_job_best_effort', Mock()),
            patch('app.services.quote_nesting_run_outbox.enqueue_job_fire_and_forget_fastfail', AsyncMock()),
            TestClient(app) as client,
        ):
            saved_response = client.post(
                '/api/v1/quote-nesting/drafts',
                headers=headers[1],
                data={'request_key': str(uuid4()), 'expected_company_id': '1'},
                files={'estimate': ('synthetic.json', json.dumps(estimate), 'application/json')},
            )
            assert saved_response.status_code == 200, saved_response.status_code
            saved = saved_response.json()
            base = dict(
                draft_id=saved['draft_id'],
                revision_number=1,
                input_sha256=saved['content_sha256'],
                expected_company_id=1,
            )

            def race(bodies):
                barrier = Barrier(2)

                def post(body):
                    barrier.wait(timeout=10)
                    response = client.post('/api/v1/quote-nesting/runs', headers=headers[1], json=body)
                    return response.status_code, response.json()

                with ThreadPoolExecutor(max_workers=2) as executor:
                    return list(executor.map(post, bodies))

            outcomes = race([{**base, 'request_key': str(uuid4())} for _ in range(2)])
            assert sorted(status for status, _ in outcomes) == [200, 409], outcomes
            first = next(value for status, value in outcomes if status == 200)
            cancelled = client.post(
                f"/api/v1/quote-nesting/runs/{first['id']}/cancel",
                headers=headers[1],
                json={'expected_company_id': 1, 'expected_version': first['version']},
            )
            assert cancelled.status_code == 200, cancelled.status_code
            same = {**base, 'request_key': str(uuid4())}
            replay = race([same, same])
            assert [status for status, _ in replay] == [200, 200], replay
            assert replay[0][1] == replay[1][1]
            latest = replay[0][1]
            for suffix in ('', '/report', '/checkpoints/1'):
                response = client.get(f"/api/v1/quote-nesting/runs/{latest['id']}{suffix}", headers=headers[2])
                assert response.status_code == 404
            malformed = client.post(
                '/api/v1/quote-nesting/runs',
                headers=headers[1],
                json={**base, 'request_key': str(uuid4()), 'approved': True},
            )
            assert malformed.status_code == 422
            with sessions() as db:
                assert db.query(QuoteNestingRun).count() == 2
                assert db.query(AuditLog).filter(AuditLog.resource_type == 'quote_nesting_run').count() == 3
        print(
            'PostgreSQL real JWT/API races passed: different keys 200/409, same key 200/200 identical; '
            'tenant 404, validation 422, exactly two runs and three lifecycle audits. Runtime/queue stubbed.'
        )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original_overrides)
        scoped.dispose()
        with owner.begin() as db:
            db.exec_driver_sql(f'DROP SCHEMA IF EXISTS {quoted} CASCADE')
        owner.dispose()


if __name__ == '__main__':
    verify()
