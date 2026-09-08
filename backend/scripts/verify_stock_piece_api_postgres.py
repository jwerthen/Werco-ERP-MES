"""Real JWT, transaction and advisory-stock races in a disposable local schema."""

import copy
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
    from app.models.inventory import InventoryItem, InventoryTransaction
    from app.models.part import Part, PartType, UnitOfMeasure
    from app.models.stock_piece import StockPiece, StockPieceObservation
    from app.models.user import User, UserRole

    schema = 'stock_obs_api_' + uuid4().hex
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
                db.add(Company(id=idx, name=f'Synthetic company {idx}', slug=f'stock-test-{idx}'))
            db.flush()
            for idx, company in ((1, 1), (2, 2), (3, 1)):
                db.add(
                    User(
                        id=idx,
                        company_id=company,
                        email=f'synthetic{idx}@example.test',
                        employee_id=f'SYN-{idx}',
                        first_name='Synthetic',
                        last_name='Estimator',
                        hashed_password='unused-test-fixture',
                        role=UserRole.ADMIN,
                        is_active=True,
                    )
                )
            for idx in (1, 2):
                db.add(
                    Part(
                        id=idx,
                        company_id=idx,
                        part_number=f'SYN-{idx}',
                        name='Synthetic sheet stock',
                        part_type=PartType.RAW_MATERIAL,
                        unit_of_measure=UnitOfMeasure.EACH,
                    )
                )
            db.flush()
            for idx in (1, 2):
                db.add(
                    InventoryItem(
                        id=idx,
                        company_id=idx,
                        part_id=idx,
                        location='SYNTHETIC-RACK',
                        quantity_on_hand=10,
                        unit_cost=25,
                        lot_number=f'SYN-LOT-{idx}',
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
        path = '/api/v1/inventory/stock-pieces'
        source_path = '/api/v1/inventory/stock-piece-sources'
        evidence = {
            'version': 1,
            'unit': 'in',
            'measurement_method': 'Synthetic measured fixture',
            'source_units': 'in',
            'geometry': {'kind': 'rectangle', 'width': '24', 'height': '12'},
            'unavailable_zones': [],
            'thickness': '0.125',
            'grade': None,
            'grain_axis': None,
            'location_note': None,
            'ownership_note': None,
            'certification_note': None,
        }
        app.dependency_overrides[get_db] = request_db
        with TestClient(app) as client:
            source = client.get(source_path, headers=headers[1]).json()['items'][0]
            base = {
                'expected_company_id': 1,
                'request_key': str(uuid4()),
                'state': 'RECORDED',
                'label': 'SYN-CAS',
                'reason': 'Synthetic actual JWT race',
                'observed_at': '2026-09-08T12:00:00Z',
                'observer_name': 'Synthetic observer',
                'source_inventory_item_id': 1,
                'source_part_id': 1,
                'expected_source_sha256': source['source_sha256'],
                'evidence': evidence,
            }

            def race(target, bodies):
                barrier = Barrier(2)

                def post(body):
                    barrier.wait(timeout=10)
                    response = client.post(target, headers=headers[1], json=body)
                    return response.status_code, response.json()

                with ThreadPoolExecutor(max_workers=2) as executor:
                    return list(executor.map(post, bodies))

            # Two requests with one key serialize to one observation and one audit.
            created = race(path, [base, base])
            assert [status for status, _ in created] == [200, 200], created
            assert created[0][1] == created[1][1]
            first = created[0][1]
            piece_id = first['piece_id']
            history_path = f'{path}/{piece_id}/observations'
            corrections = []
            for width in ('23.999999999', '23.5'):
                command = copy.deepcopy(base)
                command.pop('label')
                command.update(expected_version=1, request_key=str(uuid4()))
                command['evidence']['geometry']['width'] = width
                corrections.append(command)
            changed = race(history_path, corrections)
            assert sorted(status for status, _ in changed) == [200, 409], changed
            winner = next(index for index, (status, _) in enumerate(changed) if status == 200)
            recovered = race(history_path, [corrections[winner]] * 2)
            assert [status for status, _ in recovered] == [200, 200], recovered
            assert recovered[0][1] == recovered[1][1] == changed[winner][1]
            # Same label, different requests: losing transaction cannot leave a header.
            labels = race(
                path,
                [
                    {**base, 'label': 'SYN-LABEL-RACE', 'request_key': str(uuid4())},
                    {**base, 'label': 'SYN-LABEL-RACE', 'request_key': str(uuid4())},
                ],
            )
            assert sorted(status for status, _ in labels) == [200, 409], labels
            with patch('app.services.audit_service.AuditService.log', return_value=None):
                failed = client.post(
                    path,
                    headers=headers[1],
                    json={
                        **base,
                        'label': 'SYN-AUDIT-FAILURE',
                        'request_key': str(uuid4()),
                    },
                )
                assert failed.status_code == 503, failed.text
            assert client.post(path, headers=headers[3], json=base).status_code == 409
            assert client.get(history_path, headers=headers[2]).status_code == 404
            assert client.get(history_path + '/1', headers=headers[2]).status_code == 404
            assert (
                client.post(
                    path,
                    headers=headers[2],
                    json={
                        **base,
                        'expected_company_id': 2,
                        'request_key': str(uuid4()),
                    },
                ).status_code
                == 404
            )

            # A test-only source edit surfaces drift; it is never an availability decision.
            with sessions() as db:
                db.query(InventoryItem).filter(InventoryItem.id == 1).update({'status': 'on_hold'})
                db.commit()
            drift = client.get(history_path + '/1', headers=headers[1]).json()
            assert drift['source_status'] == 'changed' and drift['source_sha256'] == first['source_sha256']
            replay = client.post(path, headers=headers[1], json=base)
            assert replay.status_code == 200 and replay.json()['payload_sha256'] == first['payload_sha256']
            stale = copy.deepcopy(corrections[winner])
            stale.update(expected_version=2, request_key=str(uuid4()))
            assert client.post(history_path, headers=headers[1], json=stale).status_code == 409
            withdrawals = [
                {
                    'expected_company_id': 1,
                    'request_key': str(uuid4()),
                    'expected_version': 2,
                    'state': 'WITHDRAWN',
                    'reason': 'Synthetic outdated observation',
                    'observed_at': '2026-09-08T13:00:00Z',
                    'observer_name': 'Synthetic observer',
                }
                for _ in range(2)
            ]
            withdrawn = race(history_path, withdrawals)
            assert sorted(status for status, _ in withdrawn) == [200, 409], withdrawn
            final = next(value for status, value in withdrawn if status == 200)
            assert final['payload_sha256'] == changed[winner][1]['payload_sha256']
            assert final['source_snapshot'] == first['source_snapshot']
            # Exercise batched list reads on PostgreSQL, preserving historical evidence and drift.
            register_response = client.get(path, headers=headers[1])
            assert register_response.status_code == 200, register_response.text
            register = register_response.json()
            assert register['total'] == 2 and len(register['items']) == 2
            history_response = client.get(history_path, headers=headers[1])
            assert history_response.status_code == 200, history_response.text
            history = history_response.json()
            assert history['total'] == 3
            assert [row['observation_number'] for row in history['items']] == [3, 2, 1]
            assert all(row['source_status'] == 'changed' for row in history['items'])
            assert all(row['source_sha256'] == first['source_sha256'] for row in history['items'])
            assert history['items'][0]['payload_sha256'] == final['payload_sha256']
            # All writes above created only two labels/four immutable observations and audits.
            with sessions() as db:
                assert db.query(StockPiece).count() == 2
                assert db.query(StockPieceObservation).count() == 4
                assert db.query(AuditLog).filter(AuditLog.resource_type == 'stock_piece_observation').count() == 4
                assert db.query(InventoryTransaction).count() == 0
                assert db.query(InventoryItem.id, InventoryItem.quantity_on_hand).order_by(InventoryItem.id).all() == [
                    (1, 10),
                    (2, 10),
                ]
                assert db.query(StockPiece).filter(StockPiece.label == 'SYN-AUDIT-FAILURE').count() == 0
        print(
            'PostgreSQL stock JWT/API: same-key create/replay, correction/withdrawal CAS, label race, '
            'tenant and actor isolation, source drift and audit rollback passed; balances and ledger unchanged.'
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
