"""An unlocked crew kiosk selects its workstation within its own company."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token, create_kiosk_token, create_signin_token
from app.models.audit_log import AuditLog
from app.models.user import UserRole
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    COMPANY_B,
    STATION_LOGIN_URL,
    STATIONS_URL,
    bearer,
    kiosk_token_for,
    make_kiosk_station,
    make_user,
    make_work_center,
    queue_url,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

CHOICES_URL = f"{STATIONS_URL}/work-centers"
SELECTION_URL = f"{STATIONS_URL}/work-center"


def _selection_audits(db: Session, station_id: int):
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "kiosk_station",
            AuditLog.resource_id == station_id,
            AuditLog.action == "UPDATE",
        )
        .all()
    )


def test_choices_are_active_tenant_scoped_and_minimal(client: TestClient, db_session: Session):
    first = make_work_center(db_session, name="Weld")
    second = make_work_center(db_session, name="Assembly")
    inactive = make_work_center(db_session)
    inactive.is_active = False
    db_session.commit()
    make_work_center(db_session, company_id=COMPANY_B)
    station = make_kiosk_station(db_session, work_center=first)

    response = client.get(CHOICES_URL, headers=bearer(kiosk_token_for(station)))

    assert response.status_code == 200, response.text
    assert response.json() == {
        "work_centers": [
            {"id": wc.id, "code": wc.code, "name": wc.name, "work_center_type": "welding"} for wc in (first, second)
        ],
        "station": {
            "id": station.id,
            "label": station.label,
            "work_center_id": first.id,
            "work_center_code": first.code,
            "work_center_name": first.name,
        },
    }


def test_selection_persists_and_moves_queue_fence_with_same_token(client: TestClient, db_session: Session):
    old = make_work_center(db_session, name="Original workstation")
    selected = make_work_center(db_session, name="Selected workstation")
    station = make_kiosk_station(db_session, work_center=old, pin="4242")
    other_station = make_kiosk_station(db_session, work_center=old)
    headers = bearer(kiosk_token_for(station))
    assert client.get(queue_url(old.id), headers=headers).status_code == 200
    assert client.get(queue_url(selected.id), headers=headers).status_code == 403

    response = client.put(SELECTION_URL, headers=headers, json={"work_center_id": selected.id})

    assert response.status_code == 200, response.text
    expected_info = {
        "id": station.id,
        "label": station.label,
        "work_center_id": selected.id,
        "work_center_code": selected.code,
        "work_center_name": selected.name,
    }
    assert response.json() == expected_info
    db_session.refresh(station)
    db_session.refresh(other_station)
    assert station.work_center_id == selected.id
    assert other_station.work_center_id == old.id
    assert client.get(queue_url(old.id), headers=headers).status_code == 403
    assert client.get(queue_url(selected.id), headers=headers).status_code == 200
    choices = client.get(CHOICES_URL, headers=headers)
    assert choices.status_code == 200, choices.text
    assert choices.json()["station"] == expected_info

    # A fresh PIN unlock sees the persisted choice, without issuing any user token.
    login = client.post(STATION_LOGIN_URL, json={"station_id": station.id, "pin": "4242"})
    assert login.status_code == 200, login.text
    assert login.json()["station"] == expected_info

    audits = _selection_audits(db_session, station.id)
    assert len(audits) == 1
    assert audits[0].company_id == COMPANY_A
    assert audits[0].user_id is None
    assert audits[0].old_values == {"work_center_id": old.id}
    assert audits[0].new_values == {"work_center_id": selected.id}


def test_selecting_current_workstation_is_idempotent(client: TestClient, db_session: Session):
    station = make_kiosk_station(db_session)
    headers = bearer(kiosk_token_for(station))

    for _ in range(2):
        response = client.put(SELECTION_URL, headers=headers, json={"work_center_id": station.work_center_id})
        assert response.status_code == 200, response.text
        assert response.json()["work_center_id"] == station.work_center_id
    assert _selection_audits(db_session, station.id) == []


@pytest.mark.parametrize("target_kind", ["foreign", "inactive", "missing"])
def test_invalid_workstation_leaves_selection_unchanged(client: TestClient, db_session: Session, target_kind):
    station = make_kiosk_station(db_session)
    original_id = station.work_center_id
    if target_kind == "missing":
        target_id = 999999
    else:
        target = make_work_center(db_session, company_id=COMPANY_B if target_kind == "foreign" else COMPANY_A)
        target_id = target.id
        if target_kind == "inactive":
            target.is_active = False
            db_session.commit()

    response = client.put(SELECTION_URL, headers=bearer(kiosk_token_for(station)), json={"work_center_id": target_id})

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Active work center not found"
    db_session.refresh(station)
    assert station.work_center_id == original_id
    assert _selection_audits(db_session, station.id) == []


def test_selection_does_not_accept_client_station_identity(client: TestClient, db_session: Session):
    station = make_kiosk_station(db_session)
    other = make_kiosk_station(db_session)
    response = client.put(
        SELECTION_URL,
        headers=bearer(kiosk_token_for(station)),
        json={"work_center_id": station.work_center_id, "station_id": other.id},
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "credential_kind",
    ["missing", "malformed", "revoked", "foreign_claim", "missing_station", "expired", "user", "badge", "signin"],
)
def test_selection_endpoints_require_current_station_credentials(
    client: TestClient, db_session: Session, credential_kind
):
    station = make_kiosk_station(db_session)
    original_id = station.work_center_id
    selected = make_work_center(db_session)
    if credential_kind == "missing":
        headers = {}
    elif credential_kind == "malformed":
        headers = bearer("invalid-token")
    elif credential_kind == "revoked":
        headers = bearer(kiosk_token_for(station))
        station.revoked = True
        db_session.commit()
    elif credential_kind == "foreign_claim":
        headers = bearer(kiosk_token_for(station, company_id=COMPANY_B))
    elif credential_kind in ("missing_station", "expired"):
        headers = bearer(
            create_kiosk_token(
                station_id=999999 if credential_kind == "missing_station" else station.id,
                company_id=COMPANY_A,
                label=station.label,
                ttl_hours=-1 if credential_kind == "expired" else 24,
            )
        )
    elif credential_kind in ("user", "badge"):
        user = make_user(db_session, role=UserRole.ADMIN)
        headers = bearer(
            create_access_token(
                subject=user.id, company_id=COMPANY_A, scope="kiosk" if credential_kind == "badge" else None
            )
        )
    else:
        headers = bearer(create_signin_token(station_id=station.id, company_id=COMPANY_A, label="Lobby"))

    choices = client.get(CHOICES_URL, headers=headers)
    selection = client.put(SELECTION_URL, headers=headers, json={"work_center_id": selected.id})
    assert choices.status_code == 401, choices.text
    assert selection.status_code == 401, selection.text
    db_session.refresh(station)
    assert station.work_center_id == original_id
    assert _selection_audits(db_session, station.id) == []
