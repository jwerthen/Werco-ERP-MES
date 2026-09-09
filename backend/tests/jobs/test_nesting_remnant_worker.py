"""Real pinned Node child and immutable protocol2 persistence, with synthetic stock."""

import copy
import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.remnant_evidence import evidence_sha256
from app.db.database import atomic_transaction
from app.jobs import quote_nesting_runs as jobs
from app.models.quote_nesting_run import QuoteNestingRunCheckpoint
from app.services import quote_nesting_run_outbox as outbox
from app.services import quote_nesting_runs as service
from app.services.audit_service import AuditService
from app.services.quote_nesting_drafts import save_revision
from tests.services import test_nesting_remnant_protocol as fixtures
from tests.services.test_quote_nesting_runs_service import request_for

observed = fixtures.observed
stock = fixtures.stock
project = fixtures.project
pytestmark = pytest.mark.integration


async def _noop(*args, **kwargs):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize('capacity_case', ['all', 'some', 'invalid_geometry'])
async def test_actual_child_partitions_original_instances_and_keeps_zero_credit(
    db_session, admin_user, project, monkeypatch, capacity_case
):
    bundle = Path(__file__).parents[2] / 'nesting-runtime/solver.cjs'
    if not bundle.exists():
        pytest.skip('Build the shared nesting worker before real child integration')
    node = shutil.which('node')
    assert node, 'The actual Node22 worker runtime is required'
    monkeypatch.setattr(jobs, 'NODE_PATH', Path(node))
    monkeypatch.setattr(jobs, 'BUNDLE_PATH', bundle)
    monkeypatch.setattr(jobs, 'MANIFEST_PATH', bundle.parent / 'manifest.json')
    monkeypatch.setattr(jobs, 'SessionLocal', sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr(outbox, 'enqueue_job_fire_and_forget_fastfail', _noop)
    runtime = await jobs.verify_runtime()
    # Change the immutable observed geometry before saving via an explicit test
    # source resolver seam; the actual Node topology/calculation stays untouched.
    if capacity_case != 'all':
        from app.services import remnant_planning

        evidence = project['remnantPlan']['snapshot']['evidence']
        evidence['geometry'] = (
            {'kind': 'rectangle', 'width': '3', 'height': '3'}
            if capacity_case == 'some'
            else {
                'kind': 'polygon',
                'outer': [{'x': '0', 'y': '0'}, {'x': '3', 'y': '3'}, {'x': '0', 'y': '3'}, {'x': '3', 'y': '0'}],
                'holes': [],
            }
        )
        encoded = service.canonical_json(evidence).encode()
        import hashlib

        snapshot = project['remnantPlan']['snapshot']
        snapshot['payloadSha256'] = hashlib.sha256(encoded).hexdigest()
        snapshot['payloadBytes'] = len(encoded)
        project['remnantPlan']['snapshotSha256'] = evidence_sha256(snapshot)
        monkeypatch.setattr(remnant_planning, 'verify_current_selection', lambda *a, **k: {'checked_at': 'synthetic'})
    with atomic_transaction(db_session):
        revision = save_revision(
            db_session,
            admin_user,
            1,
            AuditService(db_session, admin_user),
            content=json.dumps(project).encode(),
            request_key=str(uuid4()),
            expected_company_id=1,
        )
    with atomic_transaction(db_session):
        run = service.start_run(
            db_session, admin_user, 1, AuditService(db_session, admin_user), request_for(revision), runtime
        )
    outcome = await jobs.run_quote_nesting_task(company_id=1, run_id=run['id'])
    assert outcome == {'status': 'finished', 'code': None}
    db_session.expire_all()
    stored = service.get_run(db_session, 1, run['id'])
    assert stored.status == 'COMPLETED' and stored.evaluated_count == 3
    assert stored.completed_count == 2  # one baseline+one residual; never the physical piece
    checkpoints = db_session.query(QuoteNestingRunCheckpoint).order_by(QuoteNestingRunCheckpoint.sequence).all()
    assert [c.stock_option_id for c in checkpoints] == ['stage-01', 'stage-02', 'stage-03']
    recorded, residual = [c.result_json for c in checkpoints[1:]]
    nest = recorded['result']['nest']
    placed = {p['instance'] for p in nest['placements']} if nest else set()
    mapped = {i for row in residual['instance_map'] for i in row['originals']}
    assert placed.isdisjoint(mapped) and placed | mapped == {0, 1}
    assert len(placed) == {'all': 2, 'some': 1, 'invalid_geometry': 0}[capacity_case]
    if capacity_case == 'invalid_geometry':
        assert recorded['stock'] is None and recorded['result']['error']
        assert 'leftovers' not in recorded['result']
    else:
        report = recorded['result']['leftovers']
        assert report['version'] == 'werco-leftovers-v4' and report['creditUSD'] == 0
        assert report['sheets'][0]['excludedArea'] == 0
        from app.services.nesting_remnant_protocol import validate_stage
        from app.services.nesting_run_protocol import RunProtocolError

        for mutation in ('profile', 'protected', 'nominal', 'region_outside', 'credit'):
            counterfeit = copy.deepcopy(recorded)
            ledger = counterfeit['result']['leftovers']
            if mutation == 'profile':
                ledger['assumptions']['profile']['remnantDomainProfile']['sha256'] = '0' * 64
            elif mutation == 'protected':
                ledger['sheets'][0].pop('protectedArea')
            elif mutation == 'nominal':
                ledger['sheets'][0]['nominalPartArea'] += 1
            elif mutation == 'region_outside':
                ledger['sheets'][0]['regions'][0]['outer'][0]['x'] = counterfeit['stock']['width'] + 1
            else:
                ledger['creditUSD'] = 1
            with pytest.raises(RunProtocolError):
                validate_stage(counterfeit, stored.input_sha256, project, 2, [checkpoints[0].result_json])
    if capacity_case == 'all':
        assert residual['result'] is None and residual['stock'] is None and residual['requested'] == 0
    exported = service.export_report(db_session, 1, run['id'], user=admin_user)
    assert exported['estimate'] == project and exported['status'] == 'UNAPPROVED'
    assert exported['checkpoints'][1]['source_option_id'] is None
    assert exported['checkpoints'][2]['depends_on'] == 'stage-02'
