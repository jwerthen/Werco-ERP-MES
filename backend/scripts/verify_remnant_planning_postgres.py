"""Run the two remnant PostgreSQL-only assertions in an isolated synthetic schema.

The normal pytest suite reuses these assertions but intentionally runs on SQLite.
The mandatory operations/E2E verifier invokes this child before seeding its tables.
No pytest plugins, Node runtime, external storage or production connection is used.
"""

import json
import os
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session


def assert_json_presence(db):
    """An explicit JSON null must still count as evidence field presence."""
    assert db.get_bind().dialect.name == 'postgresql', 'This assertion requires PostgreSQL'
    for value, expected in [({}, None), ({'remnantPlan': None}, 'null'), ({'remnantPlan': {}}, 'object')]:
        expression = sa.func.json_typeof(sa.cast(sa.literal(json.dumps(value)), sa.JSON)['remnantPlan'])
        assert db.scalar(sa.select(expression)) == expected


def assert_source_header_lock(project, piece_id, db, actor):
    """Withdrawal conflicts until the snapshot transaction commits, then invalidates it."""
    from fastapi import HTTPException

    from app.db.database import atomic_transaction
    from app.models.user import User
    from app.schemas.quote_nesting_drafts import SavedProject
    from app.schemas.stock_piece import WithdrawObservation
    from app.services.audit_service import AuditService
    from app.services.remnant_planning import verify_project_selection
    from app.services.stock_piece import save_observation

    assert db.get_bind().dialect.name == 'postgresql', 'This assertion requires PostgreSQL'
    body = WithdrawObservation(
        expected_company_id=1,
        expected_version=1,
        request_key=str(uuid4()),
        state='WITHDRAWN',
        reason='Explicit test withdrawal',
        observed_at='2026-09-08T13:00:00Z',
        observer_name='Synthetic inspector',
    )
    with atomic_transaction(db):
        verify_project_selection(db, actor, 1, SavedProject.model_validate(project), project)
        with Session(db.get_bind()) as concurrent:
            try:
                with atomic_transaction(concurrent):
                    concurrent.execute(sa.text("SET LOCAL lock_timeout = '150ms'"))
                    other = concurrent.get(User, actor.id)
                    other._active_company_id = 1
                    save_observation(concurrent, other, 1, AuditService(concurrent, other), body, piece_id=piece_id)
            except sa.exc.OperationalError as error:
                assert error.orig.pgcode == '55P03'
            else:
                raise AssertionError('Withdrawal bypassed the active planning snapshot header lock')
    with Session(db.get_bind()) as concurrent, atomic_transaction(concurrent):
        other = concurrent.get(User, actor.id)
        other._active_company_id = 1
        saved = save_observation(concurrent, other, 1, AuditService(concurrent, other), body, piece_id=piece_id)
        assert saved['state'] == 'WITHDRAWN' and saved['observation_number'] == 2
    try:
        with atomic_transaction(db):
            verify_project_selection(db, actor, 1, SavedProject.model_validate(project), project)
    except HTTPException as error:
        assert error.status_code == 409
    else:
        raise AssertionError('A withdrawn observation remained eligible for a new planning snapshot')


def _project(db):
    """Seed the same reported rectangle/current-profile inputs used by the pytest case."""
    from app.core.nesting_geometry_profile import geometry_profile_identity
    from app.core.remnant_domain_profile import remnant_profile_identity
    from app.core.remnant_evidence import target_group_sha256
    from app.db.database import atomic_transaction
    from app.models.company import Company
    from app.models.inventory import InventoryItem
    from app.models.part import Part, PartType, UnitOfMeasure
    from app.models.user import User, UserRole
    from app.schemas.remnant_planning import PlanningSnapshotRequest
    from app.schemas.stock_piece import CreatePiece
    from app.services import remnant_planning, stock_piece
    from app.services.audit_service import AuditService

    with atomic_transaction(db):
        db.add(Company(id=1, name='Synthetic remnant check', slug='synthetic-remnant-check'))
        db.flush()
        actor = User(
            company_id=1,
            email='remnant@example.test',
            employee_id='SYN-REMNANT',
            first_name='Synthetic',
            last_name='Inspector',
            hashed_password='unused-synthetic-fixture',
            role=UserRole.ADMIN,
            is_active=True,
        )
        part = Part(
            company_id=1,
            part_number='SYN-REMNANT',
            name='Synthetic sheet',
            part_type=PartType.MANUFACTURED,
            unit_of_measure=UnitOfMeasure.EACH,
            is_active=True,
        )
        db.add_all([actor, part])
        db.flush()
        stock = InventoryItem(
            company_id=1, part_id=part.id, location='SYNTHETIC-RACK', quantity_on_hand=10, unit_cost=25
        )
        db.add(stock)
    actor._active_company_id = 1
    source = stock_piece.list_sources(db, 1, page=1, per_page=20, user=actor, inventory_item_id=stock.id)['items'][0]
    with atomic_transaction(db):
        recorded = stock_piece.save_observation(
            db,
            actor,
            1,
            AuditService(db, actor),
            CreatePiece(
                expected_company_id=1,
                request_key=str(uuid4()),
                state='RECORDED',
                label='Plaque café 😀',
                reason='Initial measured observation',
                observed_at='2026-09-08T12:00:00Z',
                observer_name='Synthetic observer',
                source_inventory_item_id=stock.id,
                source_part_id=part.id,
                expected_source_sha256=source['source_sha256'],
                evidence={
                    'version': 1,
                    'unit': 'in',
                    'measurement_method': 'Manual tape measurement',
                    'source_units': 'in',
                    'geometry': {'kind': 'rectangle', 'width': '24', 'height': '12'},
                    'unavailable_zones': [],
                    'thickness': '0.125',
                    'grade': 'A36',
                    'grain_axis': None,
                    'location_note': None,
                    'ownership_note': None,
                    'certification_note': None,
                },
            ),
        )
    resolved = remnant_planning.resolve_snapshot(
        db,
        actor,
        1,
        recorded['piece_id'],
        1,
        PlanningSnapshotRequest(
            expected_company_id=1,
            expected_payload_sha256=recorded['payload_sha256'],
            expected_source_sha256=recorded['source_sha256'],
        ),
    )
    quote = {
        'version': 14,
        'units': 'in',
        'currency': 'USD',
        'name': 'Sheet',
        'material': 'Carbon steel',
        'thickness': 0.125,
        'margin': 0.375,
        'gap': 0.125,
        'objective': 'area',
        'geometryProfile': geometry_profile_identity(),
        'options': [{'id': 's', 'width': 12, 'height': 8, 'enabled': True, 'price': 10}],
        'parts': [
            {
                'id': 'p',
                'name': 'Circle',
                'quantity': 2,
                'rotate': True,
                'color': 0,
                'loops': [{'type': 'circle', 'cx': 1, 'cy': 1, 'r': 1}],
            }
        ],
    }
    selection = {
        'version': 1,
        'groupId': 'group',
        'snapshot': resolved['snapshot'],
        'snapshotSha256': resolved['snapshot_sha256'],
        'assignment': {
            'version': 1,
            'basis': 'planner_declared_unverified',
            'family': 'Carbon steel',
            'requiredGrade': 'A36',
            'thicknessIn': '0.125',
            'reason': 'Planner declared requirement for this exact group',
            'targetGroupSha256': target_group_sha256('group', 'A36', quote),
        },
        'geometryProfile': remnant_profile_identity(),
        'zoneClearanceIn': '0.375',
        'capacity': 1,
        'planningOnly': True,
        'eligibilityVerified': False,
        'availabilityVerified': False,
    }
    return (
        {
            'version': 18,
            'units': 'in',
            'currency': 'USD',
            'name': 'Synthetic run',
            'activeGroupId': 'group',
            'groups': [{'id': 'group', 'quote': quote}],
            'remnantPlan': selection,
        },
        recorded['piece_id'],
        actor,
    )


def verify():
    url = sa.engine.make_url(os.environ['DATABASE_URL'])
    if (
        os.environ.get('ENVIRONMENT') != 'test'
        or url.get_backend_name() != 'postgresql'
        or url.host not in {'localhost', '127.0.0.1', 'postgres'}
        or bool(url.query)
    ):
        raise RuntimeError('Remnant checks require a local disposable PostgreSQL database')
    import app.models  # noqa: F401 - Register the same complete metadata used by E2E.
    from app.db.database import Base

    schema = 'remnant_check_' + uuid4().hex
    owner = sa.create_engine(url)
    scoped = sa.create_engine(
        url,
        connect_args={
            'options': f'-csearch_path={schema} -clock_timeout=10000 -cstatement_timeout=15000',
        },
    )
    quoted = owner.dialect.identifier_preparer.quote(schema)
    try:
        with owner.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA {quoted}')
        Base.metadata.create_all(scoped)
        with Session(scoped) as db:
            assert_json_presence(db)
            print('PASS remnant PostgreSQL absent/null/object JSON presence', flush=True)
            project, piece_id, actor = _project(db)
            assert_source_header_lock(project, piece_id, db, actor)
            print('PASS remnant PostgreSQL snapshot/withdrawal lock and post-commit refusal', flush=True)
    finally:
        scoped.dispose()
        try:
            with owner.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS {quoted} CASCADE')
        finally:
            owner.dispose()


if __name__ == '__main__':
    verify()
