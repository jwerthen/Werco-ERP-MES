"""Explicit clearance profile upgrades without rewriting historical inputs."""

import copy
import hashlib
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.nesting_geometry_profile import geometry_profile_identity
from app.db.database import atomic_transaction
from app.models.audit_log import AuditLog
from app.models.quote_nesting_draft import QuoteNestingRevision
from app.models.quote_nesting_run import QuoteNestingRun, QuoteNestingRunCheckpoint
from app.schemas.quote_nesting_drafts import SavedProject
from app.services import quote_nesting_runs as service
from app.services.audit_service import AuditService
from app.services.quote_nesting_drafts import canonical_json, parse_estimate, save_revision
from tests.api.test_quote_nesting_drafts_contract import upload
from tests.services.test_quote_nesting_runs_service import RUNTIME, current_estimate, estimate, request_for

pytestmark = pytest.mark.integration


def store(db, user, source):
    with atomic_transaction(db):
        return save_revision(
            db,
            user,
            1,
            AuditService(db, user),
            content=json.dumps(source).encode(),
            request_key=str(uuid4()),
            expected_company_id=1,
        )


@pytest.mark.parametrize('project_version,quote_version', [(4, 3), (5, 3), (6, 7), (10, 9), (12, 11)])
def test_old_inputs_roundtrip_without_profile_or_hash_changes(client, admin_headers, project_version, quote_version):
    source = estimate()
    source['version'] = project_version
    source['groups'][0]['quote']['version'] = quote_version
    canonical_before = canonical_json(source)
    result = upload(client, admin_headers, source)
    assert result.status_code == 200, result.text
    saved = result.json()
    assert saved['estimate'] == source
    assert saved['content_sha256'] == hashlib.sha256(canonical_before.encode()).hexdigest()
    assert 'geometryProfile' not in saved['estimate']['groups'][0]['quote']
    assert saved['payload_schema_version'] == project_version and saved['status'] == 'DRAFT'


def test_profile_roundtrip_is_exact_unapproved_source_metadata(client, admin_headers, db_session):
    source = current_estimate()
    result = upload(client, admin_headers, source)
    assert result.status_code == 200, result.text
    saved = result.json()
    assert saved['estimate'] == source and saved['payload_schema_version'] == 15
    assert saved['status'] == 'DRAFT'
    assert any(issue['code'] == 'geometry_profile_not_shop_approval' for issue in saved['review_issues'])
    assert saved['content_sha256'] == hashlib.sha256(canonical_json(source).encode()).hexdigest()
    assert db_session.query(QuoteNestingRevision).count() == 1
    assert db_session.query(QuoteNestingRun).count() == 0


@pytest.mark.parametrize(
    'profile',
    [
        None,
        {},
        [],
        {'id': 'werco-compensated-v1'},
        {'id': 'unknown', 'sha256': 'a' * 64},
        {'id': 'werco-compensated-v1', 'sha256': 'a' * 64},
        {**geometry_profile_identity(), 'approved': True},
    ],
)
def test_current_profile_rejects_null_incomplete_unknown_and_extra_fields(profile, client, admin_headers, db_session):
    source = current_estimate()
    source['groups'][0]['quote']['geometryProfile'] = profile
    assert upload(client, admin_headers, source).status_code == 422
    assert db_session.query(QuoteNestingRevision).count() == db_session.query(AuditLog).count() == 0


def test_current_discriminator_requires_profile_and_legacy_discriminators_refuse_presence():
    source = current_estimate()
    source['groups'][0]['quote'].pop('geometryProfile')
    with pytest.raises(ValidationError, match='quote version 14'):
        SavedProject.model_validate(source)
    for version in (3, 7, 9, 11):
        source = current_estimate()
        source['groups'][0]['quote']['version'] = version
        with pytest.raises(ValidationError, match='quote version 14'):
            SavedProject.model_validate(source)
    for version in (4, 5, 6, 10, 12):
        source = current_estimate()
        source['version'] = version
        with pytest.raises(ValidationError, match='project version 15'):
            SavedProject.model_validate(source)


@pytest.mark.parametrize('populated_legacy', [False, True])
def test_whole_project_preflight_preserves_empty_legacy_but_blocks_populated_before_any_run(
    db_session, admin_user, monkeypatch, populated_legacy
):
    dispatched = []
    monkeypatch.setattr(
        'app.services.quote_nesting_run_outbox.enqueue_job_best_effort', lambda *a, **k: dispatched.append(k)
    )
    source = current_estimate()
    old_group = copy.deepcopy(estimate()['groups'][0])
    old_group.update(id='old')
    old_group['quote']['thickness'] = 0.25
    old_group['quote']['parts'] = old_group['quote']['parts'] if populated_legacy else []
    if populated_legacy:
        old_group['quote']['parts'][0]['id'] = 'old-part'
    source['groups'].append(old_group)
    saved = store(db_session, admin_user, source)
    audit_count = db_session.query(AuditLog).count()
    if populated_legacy:
        with pytest.raises(HTTPException, match='422') as error, atomic_transaction(db_session):
            service.start_run(
                db_session, admin_user, 1, AuditService(db_session, admin_user), request_for(saved), RUNTIME
            )
        assert error.value.status_code == 422 and 'Every populated' in error.value.detail
        assert db_session.query(QuoteNestingRun).count() == db_session.query(QuoteNestingRunCheckpoint).count() == 0
        assert db_session.query(AuditLog).count() == audit_count and dispatched == []
    else:
        with atomic_transaction(db_session):
            run = service.start_run(
                db_session, admin_user, 1, AuditService(db_session, admin_user), request_for(saved), RUNTIME
            )
        assert run['status'] == 'QUEUED' and run['settings']['geometry_profile'] == geometry_profile_identity()
        assert len(dispatched) == 1
    assert parse_estimate(json.dumps(source).encode())[0] == source


def test_old_request_retry_and_report_read_survive_but_old_claim_fails_release_before_profile(
    db_session, admin_user, monkeypatch
):
    monkeypatch.setattr('app.services.quote_nesting_run_outbox.enqueue_job_best_effort', lambda *a, **k: True)
    old_source = estimate()
    saved = store(db_session, admin_user, old_source)
    request = request_for(saved)
    old_runtime = {**RUNTIME, 'solver_version': 'werco-contour-v5'}
    old_settings = {**service.RUN_SETTINGS, 'solver_version': 'werco-contour-v5'}
    old_settings.pop('geometry_profile')
    # Seed through the previous release's contract; thereafter all operations use
    # the current unmodified service against the immutable old record.
    with monkeypatch.context() as old_release:
        old_release.setattr(service, 'RUN_SETTINGS', old_settings)
        old_release.setattr(service, 'require_current_geometry', lambda _project: None)
        with atomic_transaction(db_session):
            old = service.start_run(
                db_session, admin_user, 1, AuditService(db_session, admin_user), request, old_runtime
            )
    before = service.export_report(db_session, 1, old['id'])
    audits = db_session.query(AuditLog).count()
    with atomic_transaction(db_session):
        replay = service.start_run(db_session, admin_user, 1, AuditService(db_session, admin_user), request, None)
    assert replay == old
    assert service.export_report(db_session, 1, old['id']) == before
    assert before['estimate'] == old_source and before['run']['settings'] == {**old_settings, 'runtime': old_runtime}
    assert db_session.query(AuditLog).count() == audits
    with monkeypatch.context() as guard:
        guard.setattr(
            service, 'require_current_geometry', lambda _: pytest.fail('Old runtime must fail before new profile')
        )
        with atomic_transaction(db_session):
            assert service.claim_run(db_session, 1, old['id'], runtime=RUNTIME) is None
    row = service.get_run(db_session, 1, old['id'])
    assert row.error_code == 'runtime_mismatch' and row.status == 'FAILED'
    assert row.started_at is None and row.evaluated_count == 0
    assert db_session.query(QuoteNestingRevision).one().estimate_json == old_source
