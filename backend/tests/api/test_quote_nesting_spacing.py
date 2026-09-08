"""Development contracts for explicit spacing governance and saved-input claims."""

import copy
import hashlib
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.audit_log import AuditLog
from app.models.quote_nesting_spacing_policy import QuoteNestingSpacingEvent, QuoteNestingSpacingPolicy
from app.schemas.quote_nesting_spacing import SpacingContent, normalize_thickness, resolve_band
from app.services.audit_service import AuditService
from tests.services.test_quote_nesting_runs_service import RUNTIME, estimate

BASE = "/api/v1/quote-nesting/spacing-policies"


def content():
    return {
        "schema_version": 1,
        "units": "in",
        "name": "Synthetic quote allowances",
        "bands": [
            {
                "id": "steel",
                "material": "Carbon steel",
                "thickness_min_in": "0",
                "thickness_max_in": "4",
                "minimum_gap_in": "0.125",
                "gap_thickness_multiplier": "1",
                "minimum_margin_in": "0.375",
                "margin_thickness_multiplier": "2",
            }
        ],
    }


def command(version=0, **values):
    return {
        "expected_company_id": 1,
        "expected_version": version,
        "request_key": str(uuid4()),
        "reason": "Synthetic estimator review",
        **values,
    }


def created(client, headers):
    response = client.post(BASE + "/revisions", headers=headers, json=command(content=content()))
    assert response.status_code == 200, response.text
    return response.json()


def published(client, headers, *, effective_at=None):
    draft = created(client, headers)
    body = command(1, revision_number=1, content_sha256=draft["revision"]["content_sha256"], effective_at=effective_at)
    response = client.post(BASE + "/publications", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json(), body


def resolved(client, headers, thickness="0.125"):
    return client.post(BASE + "/resolve", headers=headers, json={"material": "Carbon steel", "thickness_in": thickness})


def save(client, headers, source):
    return client.post(
        "/api/v1/quote-nesting/drafts",
        headers=headers,
        data={"expected_company_id": "1", "request_key": str(uuid4())},
        files={"estimate": ("synthetic.json", json.dumps(source).encode(), "application/json")},
    )


def policy_estimate(snapshot):
    source = estimate()
    source["version"] = 10
    quote = source["groups"][0]["quote"]
    quote.update(
        version=9,
        spacingMode="policy",
        spacingPolicy=snapshot,
        gap=float(snapshot["gap_in"]),
        margin=float(snapshot["margin_in"]),
    )
    return source


def test_decimal_profile_rounds_thickness_half_up_and_formula_upward():
    band = content()["bands"][0]
    band.update(minimum_gap_in="0", gap_thickness_multiplier="0.333333333")
    parsed = SpacingContent.model_validate({**content(), "bands": [band]})
    assert normalize_thickness("0.1250000005") == "0.125000001"
    assert normalize_thickness("0.1250000004") == "0.125"
    assert resolve_band(parsed.bands[0], "0.125") == ("0.041666667", "0.375")
    with pytest.raises(ValueError):
        normalize_thickness("0.0000000001")


@pytest.mark.parametrize(
    "change",
    [
        {"thickness_min_in": "00"},
        {"minimum_gap_in": "0.1250"},
        {"gap_thickness_multiplier": "101"},
        {"minimum_gap_in": "0", "gap_thickness_multiplier": "0"},
        {"thickness_max_in": "4.000000001"},
    ],
)
def test_content_refuses_noncanonical_or_out_of_profile_numbers(change):
    value = content()
    value["bands"][0].update(change)
    with pytest.raises(ValidationError):
        SpacingContent.model_validate(value)


def test_no_seed_and_explicit_admin_publication_with_same_key_retry(client, admin_headers, db_session):
    state = client.get(BASE, headers=admin_headers).json()
    assert state["policy"] is None and state["publications"] == []
    assert resolved(client, admin_headers).json()["status"] == "unavailable"
    assert db_session.query(QuoteNestingSpacingPolicy).count() == 0
    first, body = published(client, admin_headers)
    replay = client.post(BASE + "/publications", headers=admin_headers, json=body)
    assert replay.status_code == 200 and replay.json() == first
    result = resolved(client, admin_headers).json()
    assert result["status"] == "resolved" and result["policy"]["gap_in"] == "0.125"
    assert result["policy"]["publication_id"] == first["publication"]["id"]
    assert db_session.query(QuoteNestingSpacingEvent).count() == 2
    assert db_session.query(AuditLog).filter(AuditLog.resource_type == "quote_nesting_spacing_event").count() == 2


def test_withdrawal_never_falls_back_and_future_policy_does_not_apply_early(client, admin_headers):
    first, _ = published(client, admin_headers)
    rev = first["revision"]
    future = (datetime.utcnow() + timedelta(days=1)).isoformat() + "Z"
    response = client.post(
        BASE + "/publications",
        headers=admin_headers,
        json=command(2, revision_number=1, content_sha256=rev["content_sha256"], effective_at=future),
    )
    assert response.status_code == 200, response.text
    assert response.json()["publication"]["status"] == "scheduled"
    assert resolved(client, admin_headers).json()["policy"]["publication_id"] == first["publication"]["id"]
    cancelled = client.post(
        BASE + f"/publications/{first['publication']['id']}/withdraw", headers=admin_headers, json=command(3)
    )
    assert cancelled.status_code == 200, cancelled.text
    assert resolved(client, admin_headers).json()["status"] == "unavailable"


def test_audit_failure_rolls_back_header_revision_and_event(client, admin_headers, db_session, monkeypatch):
    monkeypatch.setattr(AuditService, "log", lambda *args, **kwargs: None)
    failed = client.post(BASE + "/revisions", headers=admin_headers, json=command(content=content()))
    assert failed.status_code == 503, failed.text
    assert db_session.query(QuoteNestingSpacingPolicy).count() == 0
    assert db_session.query(QuoteNestingSpacingEvent).count() == 0


def test_exact_snapshot_save_refuses_tampering_and_legacy_hash_is_unchanged(client, admin_headers):
    published(client, admin_headers)
    snapshot = resolved(client, admin_headers).json()["policy"]
    source = policy_estimate(snapshot)
    accepted = save(client, admin_headers, source)
    assert accepted.status_code == 200, accepted.text
    for mutate in (
        lambda q: q.update(gap=q["gap"] - 0.0000000001),
        lambda q: q["spacingPolicy"].update(company_id=2),
        lambda q: q.update(version=7),
    ):
        invalid = copy.deepcopy(source)
        mutate(invalid["groups"][0]["quote"])
        assert save(client, admin_headers, invalid).status_code in (409, 422)
    legacy = estimate()
    prior = save(client, admin_headers, legacy)
    assert prior.status_code == 200, prior.text
    canonical = json.dumps(legacy, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    assert prior.json()["estimate"] == legacy
    assert prior.json()["content_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()


def test_withdrawn_policy_blocks_new_save_and_run_but_queued_replay_is_preserved(client, admin_headers, monkeypatch):
    monkeypatch.setattr(
        "app.api.endpoints.quote_nesting_runs.runtime_status",
        AsyncMock(return_value={"available": True, "identity": RUNTIME}),
    )
    monkeypatch.setattr("app.services.quote_nesting_run_outbox.enqueue_job_best_effort", lambda *args, **kwargs: True)
    publication, _ = published(client, admin_headers)
    source = policy_estimate(resolved(client, admin_headers).json()["policy"])
    saved = save(client, admin_headers, source).json()
    run_body = {
        "draft_id": saved["draft_id"],
        "revision_number": 1,
        "input_sha256": saved["content_sha256"],
        "expected_company_id": 1,
        "request_key": str(uuid4()),
    }
    run = client.post("/api/v1/quote-nesting/runs", headers=admin_headers, json=run_body)
    assert run.status_code == 200, run.text
    withdrawal = client.post(
        BASE + f"/publications/{publication['publication']['id']}/withdraw", headers=admin_headers, json=command(2)
    )
    assert withdrawal.status_code == 200, withdrawal.text
    assert save(client, admin_headers, source).status_code == 409
    replay = client.post("/api/v1/quote-nesting/runs", headers=admin_headers, json=run_body)
    assert replay.status_code == 200 and replay.json()["id"] == run.json()["id"]
    cancelled = client.post(
        f"/api/v1/quote-nesting/runs/{run.json()['id']}/cancel",
        headers=admin_headers,
        json={"expected_company_id": 1, "expected_version": 1},
    )
    assert cancelled.status_code == 200
    assert (
        client.post(
            "/api/v1/quote-nesting/runs", headers=admin_headers, json={**run_body, "request_key": str(uuid4())}
        ).status_code
        == 409
    )
