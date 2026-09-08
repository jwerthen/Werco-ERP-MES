"""Exercise actual shared Node exclusion geometry through audited Python persistence."""

import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from app.db.database import atomic_transaction
from app.jobs import quote_nesting_runs as jobs
from app.models.quote_nesting_run import QuoteNestingRunCheckpoint
from app.services import quote_nesting_run_outbox as outbox
from app.services import quote_nesting_runs as service
from app.services.audit_service import AuditService
from app.services.quote_nesting_drafts import save_revision
from tests.api.test_quote_nesting_exclusions_contract import exclusion_estimate
from tests.services.test_quote_nesting_runs_service import request_for

pytestmark = pytest.mark.integration


async def no_queue(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_topology', [False, True])
async def test_real_node_exclusion_validation_precedes_any_accepted_checkpoint(
    db_session, admin_user, monkeypatch, invalid_topology
):
    bundle = Path(__file__).parents[2] / 'nesting-runtime/solver.cjs'
    node = shutil.which('node')
    if not bundle.exists() or not node:
        pytest.skip('Build the packaged Node22 nesting runtime before subprocess integration')
    monkeypatch.setattr(jobs, 'NODE_PATH', Path(node))
    monkeypatch.setattr(jobs, 'BUNDLE_PATH', bundle)
    monkeypatch.setattr(jobs, 'MANIFEST_PATH', bundle.parent / 'manifest.json')
    monkeypatch.setattr(jobs, 'SessionLocal', sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr(outbox, 'enqueue_job_best_effort', lambda *_args, **_kwargs: True)
    monkeypatch.setattr(outbox, 'enqueue_job_fire_and_forget_fastfail', no_queue)
    runtime = await jobs.verify_runtime()
    assert runtime['solver_version'] == 'werco-contour-v5'
    source = exclusion_estimate()
    region = source['groups'][0]['quote']['options'][0]['exclusions'][0]
    region['outline'] = {
        'type': 'poly',
        'points': (
            [{'x': 1, 'y': 1}, {'x': 3, 'y': 3}, {'x': 1, 'y': 3}, {'x': 3, 'y': 1}]
            if invalid_topology
            else [{'x': 0, 'y': 0}, {'x': 6, 'y': 0}, {'x': 6, 'y': 8}, {'x': 0, 'y': 8}]
        ),
    }
    with atomic_transaction(db_session):
        saved = save_revision(
            db_session,
            admin_user,
            1,
            AuditService(db_session, admin_user),
            content=json.dumps(source).encode(),
            request_key=str(uuid4()),
            expected_company_id=1,
        )
        run = service.start_run(
            db_session,
            admin_user,
            1,
            AuditService(db_session, admin_user),
            request_for(saved),
            runtime,
        )
    await jobs.run_quote_nesting_task(company_id=1, run_id=run['id'])
    db_session.expire_all()
    completed = service.get_run(db_session, 1, run['id'])
    if invalid_topology:
        assert completed.status == 'FAILED' and completed.error_code == 'invalid_geometry'
        assert completed.evaluated_count == 0
        assert db_session.query(QuoteNestingRunCheckpoint).count() == 0
        return
    assert completed.status == 'COMPLETED' and completed.evaluated_count == completed.completed_count == 1
    checkpoint = db_session.query(QuoteNestingRunCheckpoint).one()
    result = checkpoint.result_json['result']
    assert result['nest']['unplaced'] == [] and len(result['nest']['placements']) == 2
    boundary = (6 + 0.125 + 0.125 / 2) * 25.4 + 0.0008
    assert all(placement['x'] >= boundary - 1e-7 for placement in result['nest']['placements'])
    assert result['leftovers']['version'] == 'werco-leftovers-v2'
    assert result['leftovers']['creditUSD'] == 0 and result['leftovers']['sheets'][0]['excludedArea'] > 0
    report = service.export_report(db_session, 1, run['id'])
    assert report['status'] == 'UNAPPROVED' and report['estimate'] == source
    assert report['checkpoints'][0]['content_sha256'] == checkpoint.content_sha256
