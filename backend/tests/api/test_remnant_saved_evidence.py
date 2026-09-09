"""Current authorization and source-currentness fences across saved evidence paths."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.db.database import atomic_transaction
from app.models.audit_log import AuditLog
from app.models.quote_nesting_draft import QuoteNestingRevision
from app.models.quote_nesting_run import QuoteNestingRunCheckpoint
from app.models.role_permission import RolePermission
from app.models.user import UserRole
from app.services import quote_nesting_runs as runs
from scripts.verify_remnant_planning_postgres import assert_json_presence, assert_source_header_lock
from tests.api.test_quote_nesting_drafts_contract import upload
from tests.services import test_nesting_remnant_protocol as fixtures
from tests.services.test_quote_nesting_runs_service import RUNTIME, request_for

observed = fixtures.observed
stock = fixtures.stock
project = fixtures.project
pytestmark = [pytest.mark.api, pytest.mark.integration]


@pytest.fixture
def saved(project, client, admin_headers, monkeypatch):
    from app.services import quote_nesting_run_outbox as outbox

    monkeypatch.setattr(outbox, 'enqueue_job_best_effort', lambda *a, **k: True)
    monkeypatch.setattr(
        'app.api.endpoints.quote_nesting_runs.runtime_status',
        AsyncMock(return_value={'available': True, 'identity': RUNTIME}),
    )
    key = str(uuid4())
    response = upload(client, admin_headers, project, key=key)
    assert response.status_code == 200, response.text
    revision = response.json()
    request = request_for(revision).model_dump(mode='json')
    response = client.post('/api/v1/quote-nesting/runs', json=request, headers=admin_headers)
    assert response.status_code == 200, response.text
    return revision, response.json(), key, request


def revoke(db):
    row = RolePermission(company_id=1, role=UserRole.ADMIN, permissions=['purchasing:view', 'purchasing:create'])
    db.add(row)
    db.commit()
    return row


def test_inventory_revocation_blocks_all_evidence_reads_and_exact_replays(
    client, admin_headers, project, saved, db_session
):
    revision, run, key, request = saved
    before = (db_session.query(QuoteNestingRevision).count(), db_session.query(AuditLog).count())
    override = revoke(db_session)
    drafts = '/api/v1/quote-nesting/drafts'
    history = f"{drafts}/{revision['draft_id']}/revisions"
    run_path = f"/api/v1/quote-nesting/runs/{run['id']}"
    paths = [
        drafts,
        history,
        history + '/1',
        history + '/1/sources',
        '/api/v1/quote-nesting/runs',
        run_path,
        run_path + '/report',
        run_path + '/checkpoints/1',
    ]
    for path in paths:
        response = client.get(path, headers=admin_headers)
        assert response.status_code == 403, (path, response.status_code, response.text)
    assert upload(client, admin_headers, project, key=key).status_code == 403
    assert client.post('/api/v1/quote-nesting/runs', json=request, headers=admin_headers).status_code == 403
    assert (
        client.post(
            run_path + '/cancel', json={'expected_company_id': 1, 'expected_version': 1}, headers=admin_headers
        ).status_code
        == 403
    )
    assert before == (db_session.query(QuoteNestingRevision).count(), db_session.query(AuditLog).count())
    override.permissions = ['purchasing:view', 'purchasing:create', 'inventory:view']
    db_session.commit()
    # Permission restoration allows the exact read/replay; no new history or audit.
    assert upload(client, admin_headers, project, key=key).json()['content_sha256'] == revision['content_sha256']
    assert client.post('/api/v1/quote-nesting/runs', json=request, headers=admin_headers).json()['id'] == run['id']
    assert before == (db_session.query(QuoteNestingRevision).count(), db_session.query(AuditLog).count())


@pytest.mark.parametrize('phase', ['claim', 'checkpoint'])
def test_worker_rechecks_inventory_permission_without_accepting_further_geometry(
    saved, project, db_session, admin_user, phase
):
    revision, run, _, _ = saved
    if phase == 'checkpoint':
        with atomic_transaction(db_session):
            payload, lease = runs.claim_run(db_session, 1, run['id'], runtime=RUNTIME)
        with atomic_transaction(db_session):
            runs.accept_hello(
                db_session,
                1,
                run['id'],
                lease,
                {
                    'protocol': 2,
                    **RUNTIME,
                    'geometry_profile': run['settings']['geometry_profile'],
                    'remnant_domain_profile': run['settings']['remnant_domain_profile'],
                }
                | {'protocol': 2},
            )
    revoke(db_session)
    if phase == 'claim':
        with atomic_transaction(db_session):
            assert runs.claim_run(db_session, 1, run['id'], runtime=RUNTIME) is None
        assert runs.get_run(db_session, 1, run['id']).error_code == 'actor_ineligible'
    else:
        with pytest.raises(ValueError, match='actor_ineligible'), atomic_transaction(db_session):
            runs.append_checkpoint(db_session, 1, run['id'], lease, {})
    assert db_session.query(QuoteNestingRunCheckpoint).count() == 0


def test_source_drift_blocks_new_save_but_does_not_block_historical_read_or_uuid_recovery(
    saved, stock, project, client, admin_headers, db_session
):
    revision, run, key, request = saved
    stock.quantity_on_hand -= 1
    db_session.commit()
    response = upload(client, admin_headers, project)
    assert response.status_code == 409, response.text
    assert upload(client, admin_headers, project, key=key).json()['content_sha256'] == revision['content_sha256']
    assert client.post('/api/v1/quote-nesting/runs', json=request, headers=admin_headers).json()['id'] == run['id']
    history = client.get(f"/api/v1/quote-nesting/drafts/{revision['draft_id']}/revisions/1", headers=admin_headers)
    assert history.status_code == 200 and history.json()['estimate'] == project


def test_postgres_presence_distinguishes_absent_explicit_null_and_object(db_session):
    if db_session.get_bind().dialect.name != 'postgresql':
        pytest.skip('PostgreSQL JSON expression is exercised by the isolated live-PG gate')
    assert_json_presence(db_session)


def test_postgres_save_source_header_lock_blocks_withdrawal_until_snapshot_commit(
    project, observed, db_session, admin_user
):
    if db_session.get_bind().dialect.name != 'postgresql':
        pytest.skip('FOR SHARE/UPDATE conflict requires the isolated PostgreSQL gate')
    assert_source_header_lock(project, observed[1]['piece_id'], db_session, admin_user)
