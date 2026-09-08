"""Real authenticated policy governance races on isolated disposable PostgreSQL.

Invoked in a separate process by verify_operations_postgres before the E2E seed.
The process creates only a UUID-named schema, uses independent request sessions,
and removes that schema even after failed assertions. No geometry child runs.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor
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
        raise RuntimeError('API races require a local disposable PostgreSQL database')

    from fastapi.testclient import TestClient

    from app.core.security import create_access_token
    from app.db.database import Base, get_db
    from app.main import app
    from app.models.audit_log import AuditLog
    from app.models.company import Company
    from app.models.quote_nesting_draft import QuoteNestingDraft
    from app.models.quote_nesting_spacing_policy import QuoteNestingSpacingEvent as Event
    from app.models.quote_nesting_spacing_policy import QuoteNestingSpacingPolicy as Policy
    from app.models.user import User, UserRole

    schema = 'nest_policy_api_check_' + uuid4().hex
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
            'version': 6,
            'units': 'in',
            'currency': 'USD',
            'name': 'Synthetic API race',
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
        base_path = '/api/v1/quote-nesting/spacing-policies'
        content = {
            'schema_version': 1,
            'units': 'in',
            'name': 'Synthetic family allowances',
            'bands': [
                {
                    'id': 'carbon',
                    'material': 'Carbon steel',
                    'thickness_min_in': '0',
                    'thickness_max_in': '4',
                    'minimum_gap_in': '0.125',
                    'gap_thickness_multiplier': '1',
                    'minimum_margin_in': '0.375',
                    'margin_thickness_multiplier': '2',
                }
            ],
        }

        def command(version, **values):
            return {
                'expected_company_id': 1,
                'expected_version': version,
                'request_key': str(uuid4()),
                'reason': 'Synthetic independent API race',
                **values,
            }

        app.dependency_overrides[get_db] = request_db
        with TestClient(app) as client:
            assert client.get(base_path, headers=headers[1]).json()['policy'] is None

            def race(path, bodies):
                barrier = Barrier(2)

                def post(body):
                    barrier.wait(timeout=10)
                    response = client.post(base_path + path, headers=headers[1], json=body)
                    return response.status_code, response.json()

                with ThreadPoolExecutor(max_workers=2) as executor:
                    return list(executor.map(post, bodies))

            drafts = race('/revisions', [command(0, content=content), command(0, content=content)])
            assert sorted(status for status, _ in drafts) == [200, 409], drafts
            draft = next(value for status, value in drafts if status == 200)
            publication_args = {
                'revision_number': 1,
                'content_sha256': draft['revision']['content_sha256'],
                'effective_at': None,
            }
            publication_bodies = [command(1, **publication_args), command(1, **publication_args)]
            publications = race('/publications', publication_bodies)
            assert sorted(status for status, _ in publications) == [200, 409], publications
            winner_index = next(index for index, value in enumerate(publications) if value[0] == 200)
            publication = publications[winner_index][1]
            replay = race('/publications', [publication_bodies[winner_index]] * 2)
            assert [status for status, _ in replay] == [200, 200]
            assert replay[0][1] == replay[1][1] == publication
            target = publication['publication']['id']
            withdrawals = [command(2), command(2)]
            outcomes = race(f'/publications/{target}/withdraw', withdrawals)
            assert sorted(status for status, _ in outcomes) == [200, 409], outcomes
            winner_index = next(index for index, value in enumerate(outcomes) if value[0] == 200)
            retry = race(f'/publications/{target}/withdraw', [withdrawals[winner_index]] * 2)
            assert [status for status, _ in retry] == [200, 200]
            assert retry[0][1] == retry[1][1] == outcomes[winner_index][1]
            with patch('app.services.audit_service.AuditService.log', return_value=None):
                failed = client.post(base_path + '/revisions', headers=headers[1], json=command(3, content=content))
                assert failed.status_code == 503, failed.status_code
            assert client.get(base_path + '/revisions/1', headers=headers[2]).status_code == 404
            assert (
                client.post(
                    base_path + f'/publications/{target}/withdraw',
                    headers=headers[2],
                    json=command(0, expected_company_id=2),
                ).status_code
                == 404
            )
            assert (
                client.post(
                    base_path + '/revisions', headers=headers[1], json=command(True, content=content)
                ).status_code
                == 422
            )
            # Publication versus a new saved conformance claim must serialize.
            republished = client.post(
                base_path + '/publications', headers=headers[1], json=command(3, **publication_args)
            )
            assert republished.status_code == 200, republished.status_code
            resolved = client.post(
                base_path + '/resolve', headers=headers[1], json={'material': 'Carbon steel', 'thickness_in': '0.125'}
            ).json()['policy']
            estimate['version'] = 10
            estimate['groups'][0]['quote'].update(version=9, spacingMode='policy', spacingPolicy=resolved)
            barrier = Barrier(2)

            def save_or_withdraw(kind):
                barrier.wait(timeout=10)
                if kind == 'save':
                    response = client.post(
                        '/api/v1/quote-nesting/drafts',
                        headers=headers[1],
                        data={'request_key': str(uuid4()), 'expected_company_id': '1'},
                        files={'estimate': ('synthetic.json', json.dumps(estimate), 'application/json')},
                    )
                else:
                    response = client.post(
                        base_path + f"/publications/{resolved['publication_id']}/withdraw",
                        headers=headers[1],
                        json=command(4),
                    )
                return kind, response.status_code

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = dict(executor.map(save_or_withdraw, ('save', 'withdraw')))
            assert outcomes['withdraw'] == 200 and outcomes['save'] in (200, 409), outcomes
            with sessions() as db:
                assert db.query(Policy).one().version == 5
                assert db.query(Event).count() == 5
                assert db.query(AuditLog).filter(AuditLog.resource_type == 'quote_nesting_spacing_event').count() == 5
                assert db.query(QuoteNestingDraft).count() == (1 if outcomes['save'] == 200 else 0)
        print(
            'PostgreSQL policy JWT/API races passed: initial revision/publication/withdrawal each 200+409, '
            'same-key retries recover one event, audit failure rolls back, tenant404/validation422; '
            'save-versus-withdraw serialized with exactly five policy events/audits. No runtime or queue stubs.'
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
