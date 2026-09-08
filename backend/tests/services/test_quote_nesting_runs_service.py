"""Development checks for exact-input runs and fail-closed audited lifecycle."""

import json
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.nesting_geometry_profile import geometry_profile_identity
from app.db.database import atomic_transaction
from app.models.api_token import ApiToken
from app.models.audit_log import AuditLog
from app.models.quote_nesting_run import QuoteNestingRun
from app.schemas.quote_nesting_runs import SOLVER_VERSION, StartRunRequest
from app.services import quote_nesting_run_outbox as outbox
from app.services import quote_nesting_runs as service
from app.services.audit_service import AuditService, AuditWriteError
from app.services.quote_nesting_drafts import save_revision

RUNTIME = {
    "release": "development",
    "protocol": 1,
    "solver_version": SOLVER_VERSION,
    "bundle_sha256": "a" * 64,
    "node_version": "v22.22.3",
}


def estimate():
    return {
        "version": 4,
        "units": "in",
        "currency": "USD",
        "name": "Synthetic run",
        "activeGroupId": "g",
        "groups": [
            {
                "id": "g",
                "quote": {
                    "version": 3,
                    "units": "in",
                    "currency": "USD",
                    "name": "Sheet",
                    "material": "Carbon steel",
                    "thickness": 0.125,
                    "margin": 0.375,
                    "gap": 0.125,
                    "objective": "area",
                    "options": [{"id": "s", "width": 12, "height": 8, "enabled": True, "price": 10}],
                    "parts": [
                        {
                            "id": "p",
                            "name": "Circle",
                            "quantity": 2,
                            "rotate": True,
                            "color": 0,
                            "loops": [{"type": "circle", "cx": 1, "cy": 1, "r": 1}],
                        }
                    ],
                },
            }
        ],
    }


def current_estimate(source=None):
    """Explicitly upgrade new-run fixtures; retain estimate() as historical input."""
    import copy

    value = copy.deepcopy(estimate() if source is None else source)
    value['version'] = 15
    for group in value['groups']:
        group['quote'].update(version=14, geometryProfile=geometry_profile_identity())
    return value


def saved(db, user):
    with atomic_transaction(db):
        return save_revision(
            db,
            user,
            1,
            AuditService(db, user),
            content=json.dumps(current_estimate()).encode(),
            request_key=str(uuid4()),
            expected_company_id=1,
        )


def request_for(revision):
    return StartRunRequest(
        draft_id=revision["draft_id"],
        revision_number=revision["revision_number"],
        input_sha256=revision["content_sha256"],
        expected_company_id=1,
        request_key=uuid4(),
    )


@pytest.fixture(autouse=True)
def no_real_queue(monkeypatch):
    monkeypatch.setattr(outbox, "enqueue_job_best_effort", lambda *a, **kw: True)


def test_start_is_exact_idempotent_and_runtime_down_still_recovers(db_session, admin_user, monkeypatch):
    dispatched = []
    monkeypatch.setattr(outbox, "enqueue_job_best_effort", lambda *args, **kwargs: dispatched.append(kwargs))
    revision = saved(db_session, admin_user)
    request = request_for(revision)
    with atomic_transaction(db_session):
        first = service.start_run(db_session, admin_user, 1, AuditService(db_session, admin_user), request, RUNTIME)
    with atomic_transaction(db_session):
        replay = service.start_run(db_session, admin_user, 1, AuditService(db_session, admin_user), request, None)
    assert replay == first
    assert len(dispatched) == 2
    assert dispatched[0]["_job_id"] == dispatched[1]["_job_id"]
    assert first["status"] == "QUEUED"
    assert first["settings"]["runtime"] == RUNTIME
    assert db_session.query(QuoteNestingRun).count() == 1
    assert db_session.query(AuditLog).filter(AuditLog.resource_type == "quote_nesting_run").count() == 1
    with pytest.raises(HTTPException) as error, atomic_transaction(db_session):
        service.start_run(
            db_session, admin_user, 1, AuditService(db_session, admin_user), request_for(revision), RUNTIME
        )
    assert error.value.status_code == 409


def test_no_runtime_or_audit_never_creates_or_dispatches_run(db_session, admin_user, monkeypatch):
    revision = saved(db_session, admin_user)
    dispatch = []
    monkeypatch.setattr(outbox, "enqueue_job_best_effort", lambda *a, **kw: dispatch.append(kw))
    with pytest.raises(HTTPException) as error, atomic_transaction(db_session):
        service.start_run(db_session, admin_user, 1, AuditService(db_session, admin_user), request_for(revision))
    assert error.value.status_code == 503
    monkeypatch.setattr(AuditService, "log", lambda *a, **kw: None)
    with pytest.raises(AuditWriteError), atomic_transaction(db_session):
        service.start_run(
            db_session, admin_user, 1, AuditService(db_session, admin_user), request_for(revision), RUNTIME
        )
    assert db_session.query(QuoteNestingRun).count() == 0
    assert dispatch == []


def test_claim_binds_runtime_and_cancel_preserves_terminal(db_session, admin_user):
    revision = saved(db_session, admin_user)
    with atomic_transaction(db_session):
        first = service.start_run(
            db_session, admin_user, 1, AuditService(db_session, admin_user), request_for(revision), RUNTIME
        )
    with atomic_transaction(db_session):
        claimed = service.claim_run(db_session, 1, first["id"], runtime={**RUNTIME, "bundle_sha256": "b" * 64})
    assert claimed is None
    row = service.get_run(db_session, 1, first["id"])
    assert row.status == "FAILED" and row.error_code == "runtime_mismatch"
    before = row.version
    with atomic_transaction(db_session):
        terminal = service.cancel_run(db_session, admin_user, 1, AuditService(db_session, admin_user), row.id, 1, 1)
    assert terminal["status"] == "FAILED" and terminal["version"] == before


def test_queued_cancel_uses_cas_and_prevents_claim(db_session, admin_user):
    revision = saved(db_session, admin_user)
    with atomic_transaction(db_session):
        first = service.start_run(
            db_session, admin_user, 1, AuditService(db_session, admin_user), request_for(revision), RUNTIME
        )
    with pytest.raises(HTTPException), atomic_transaction(db_session):
        service.cancel_run(db_session, admin_user, 1, AuditService(db_session, admin_user), first["id"], 1, 2)
    with atomic_transaction(db_session):
        cancelled = service.cancel_run(
            db_session, admin_user, 1, AuditService(db_session, admin_user), first["id"], 1, 1
        )
    assert cancelled["status"] == "CANCELLED"
    with atomic_transaction(db_session):
        assert service.claim_run(db_session, 1, first["id"], runtime=RUNTIME) is None


def token_run(db, user):
    revision = saved(db, user)
    token = ApiToken(company_id=1, user_id=user.id, created_by=user.id, label="Synthetic runner", jti=str(uuid4()))
    db.add(token)
    db.commit()
    credential = {
        "kind": "api_token",
        "api_token_id": token.id,
        "label": token.label,
        "jti_prefix": token.jti_prefix,
    }
    user._api_token_id = token.id
    user._api_token_label = token.label
    user._api_token_jti_prefix = token.jti_prefix
    with atomic_transaction(db):
        run = service.start_run(db, user, 1, AuditService(db, user), request_for(revision), RUNTIME)
    return run, token, credential


def test_terminal_audit_restores_submission_credential_in_a_fresh_session(db_session, admin_user):
    run, _, credential = token_run(db_session, admin_user)
    with Session(db_session.get_bind()) as claim_db, atomic_transaction(claim_db):
        _, lease = service.claim_run(claim_db, 1, run["id"], runtime=RUNTIME)
    # A separate finish/lease-relay session has never authenticated the user.
    with Session(db_session.get_bind()) as finish_db, atomic_transaction(finish_db):
        row = service.get_run(finish_db, 1, run["id"], locked=True)
        service.finish_run(finish_db, row, "worker_lost", lease_token=lease)
    audits = db_session.query(AuditLog).filter(AuditLog.resource_type == "quote_nesting_run").all()
    assert len(audits) == 3
    assert all(row.extra_data["credential"] == credential for row in audits)


def test_revoked_submission_still_has_credential_evidence_when_claim_is_refused(db_session, admin_user):
    run, token, credential = token_run(db_session, admin_user)
    token.revoked = True
    db_session.commit()
    with Session(db_session.get_bind()) as worker_db, atomic_transaction(worker_db):
        assert service.claim_run(worker_db, 1, run["id"], runtime=RUNTIME) is None
        row = service.get_run(worker_db, 1, run["id"])
        assert row.status == "FAILED" and row.error_code == "actor_ineligible"
        assert row.started_at is None and row.lease_token is None
    audits = db_session.query(AuditLog).filter(AuditLog.resource_type == "quote_nesting_run").all()
    assert len(audits) == 2
    assert all(row.extra_data["credential"] == credential for row in audits)
