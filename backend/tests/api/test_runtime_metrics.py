import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.security import create_access_token
from app.models.company import Company
from app.models.runtime_metric import RuntimeMetricSample, RuntimeMetricSetting
from app.models.user import User, UserRole
from app.services import runtime_metric_service

URL = "/api/v1/runtime-metrics"


def sample(**changes):
    return {
        "metric_id": str(uuid4()),
        "name": "LCP",
        "route": "/work-orders/:id",
        "device": "mobile",
        "navigation": "document",
        "release": "a" * 40,
        "value": 1000,
        "sequence": 1,
        **changes,
    }


def send(client, headers, *samples):
    return client.post(URL + "/samples", headers=headers, json={"samples": list(samples)})


def test_receipt_updates_are_ordered_and_percentiles_are_exact(client, admin_headers, db_session):
    first = sample()
    assert send(client, admin_headers, first).status_code == 202
    assert send(client, admin_headers, first).json()["accepted"] == 1
    assert send(client, admin_headers, {**first, "sequence": 3, "value": 5000}).status_code == 202
    assert send(client, admin_headers, {**first, "sequence": 2, "value": 9000}).status_code == 202
    assert send(client, admin_headers, sample(value=500), sample(value=2000), sample(value=3000)).status_code == 202
    assert db_session.query(RuntimeMetricSample).count() == 4
    summary = client.get(URL + "/summary", headers=admin_headers).json()
    assert summary["rows"][0]["p75"] == 3000
    assert summary["rows"][0]["samples"] == 4
    assert summary["rows"][0]["good_percent"] == 50
    assert client.get(URL + "/summary?device=desktop", headers=admin_headers).json()["rows"] == []
    assert client.get(URL + "/summary?route=/parts", headers=admin_headers).json()["rows"] == []
    assert client.get(URL + "/summary?release=" + "b" * 40, headers=admin_headers).json()["rows"] == []


def test_metric_identity_cannot_change_and_conflict_rolls_back_batch(client, admin_headers, db_session):
    first = sample()
    send(client, admin_headers, first)
    response = send(client, admin_headers, sample(), {**first, "route": "/parts", "sequence": 2})
    assert response.status_code == 409
    assert db_session.query(RuntimeMetricSample).count() == 1


def test_sample_quota_is_bounded_but_known_receipts_can_finish(client, admin_headers, db_session, monkeypatch):
    monkeypatch.setattr(runtime_metric_service, "DAILY_SAMPLE_LIMIT", 2)
    first = sample()
    assert send(client, admin_headers, first, sample(), sample()).json()["accepted"] == 2
    assert send(client, admin_headers, {**first, "sequence": 2, "value": 1234}, sample()).json()["accepted"] == 1
    assert db_session.query(RuntimeMetricSample).count() == 2


def test_admin_controls_and_tenant_isolation(client, admin_headers, manager_headers, db_session, admin_user):
    assert send(client, manager_headers, sample()).status_code == 202
    assert client.get(URL + "/summary", headers=manager_headers).status_code == 403
    assert client.put(URL + "/config", headers=manager_headers, json={"enabled": False}).status_code == 403
    assert client.delete(URL + "/samples", headers=manager_headers).status_code == 403
    db_session.add(Company(id=2, name="Metrics other company", slug="metrics-other", is_active=True))
    db_session.flush()
    other = User(
        company_id=2,
        employee_id="METRICS",
        email="metrics@example.test",
        first_name="Metrics",
        last_name="Other",
        hashed_password="unused",
        role=UserRole.ADMIN,
        is_active=True,
    )
    db_session.add(other)
    db_session.commit()
    headers = {"Authorization": "Bearer " + create_access_token(subject=other.id, company_id=2)}
    assert client.get(URL + "/summary", headers=headers).json()["rows"] == []
    assert send(client, headers, sample()).status_code == 202
    assert client.put(URL + "/config", headers=admin_headers, json={"enabled": False}).json()["enabled"] is False
    assert send(client, admin_headers, sample()).json() == {"accepted": 0, "enabled": False}
    assert client.get(URL + "/config", headers=headers).json()["enabled"] is True
    assert client.delete(URL + "/samples", headers=admin_headers).status_code == 204
    assert db_session.query(RuntimeMetricSample).count() == 1
    assert db_session.query(RuntimeMetricSetting).count() == 1


def test_kiosk_can_measure_but_cannot_administer(client, admin_user):
    headers = {
        "Authorization": "Bearer "
        + create_access_token(subject=admin_user.id, company_id=admin_user.company_id, scope="kiosk")
    }
    assert client.get(URL + "/config", headers=headers).status_code == 200
    assert send(client, headers, sample(route="/kiosk")).status_code == 202
    assert client.get(URL + "/summary", headers=headers).status_code == 403
    assert client.put(URL + "/config", headers=headers, json={"enabled": False}).status_code == 403
    assert client.delete(URL + "/samples", headers=headers).status_code == 403


@pytest.mark.parametrize(
    "change",
    [
        {"route": "/work-orders/123"},
        {"route": "/parts?customer=private"},
        {"route": "/parts#secret"},
        {"name": "EMAIL"},
        {"value": -1},
        {"name": "CLS", "value": 101},
        {"value": 300001},
        {"company_id": 2},
        {"user_id": 1},
        {"release": "customer-name"},
        {"sequence": 0},
        {"metric_id": "personal-information"},
        {"device": "phone model"},
        {"navigation": "http://private"},
    ],
)
def test_rejects_unbounded_and_sensitive_payloads(client, admin_headers, change):
    assert send(client, admin_headers, sample(**change)).status_code == 422


def test_auth_batch_bounds_and_retention(client, admin_headers, db_session, admin_user):
    assert client.get(URL + "/config").status_code == 401
    assert send(client, {}, sample()).status_code == 401
    assert send(client, admin_headers).status_code == 422
    assert send(client, admin_headers, *[sample() for _ in range(11)]).status_code == 422
    assert client.get(URL + "/summary?days=31", headers=admin_headers).status_code == 422
    assert client.get(URL + "/summary?route=/parts/123", headers=admin_headers).status_code == 422
    old = RuntimeMetricSample(
        company_id=admin_user.company_id, created_at=datetime.utcnow() - timedelta(days=31), **sample()
    )
    db_session.add(old)
    db_session.commit()
    db_session.expunge(old)
    assert client.get(URL + "/summary?days=30", headers=admin_headers).json()["rows"] == []
    assert send(client, admin_headers, sample()).status_code == 202
    assert db_session.query(RuntimeMetricSample).count() == 1
    assert runtime_metric_service.prune_runtime_metrics(db_session, now=datetime.utcnow() + timedelta(days=31)) == 1


def test_client_and_server_route_template_allowlists_match():
    root = Path(__file__).resolve().parents[3]
    assert json.loads((root / "frontend/src/data/runtimeMetricRoutes.json").read_text()) == json.loads(
        (root / "backend/app/data/runtime_metric_routes.json").read_text()
    )


def test_summary_pages_groups_without_truncating_group_samples(client, admin_headers, db_session, admin_user):
    db_session.add_all(
        RuntimeMetricSample(
            company_id=admin_user.company_id, created_at=datetime.utcnow(), **sample(release=f"{i:040x}")
        )
        for i in range(205)
    )
    db_session.commit()
    first = client.get(URL + "/summary", headers=admin_headers).json()
    second = client.get(URL + "/summary?page=2", headers=admin_headers).json()
    assert len(first["rows"]) == 200 and first["has_more"] and first["page"] == 1
    assert len(second["rows"]) == 5 and not second["has_more"] and second["page"] == 2
    assert len({row["release"] for row in first["rows"] + second["rows"]}) == 205
    assert all(row["samples"] == 1 and row["p75"] == 1000 for row in first["rows"] + second["rows"])
    assert client.get(URL + "/summary?page=0", headers=admin_headers).status_code == 422
