"""Crew-queue scrap picker feed (Lean Phase 1): ``scrap_reason_codes`` on
``GET /shop-floor/work-center-queue/{id}``.

The crew station's badge tokens are path-fenced to /shop-floor and its station
token is honored only by this read + badge mint, so the kiosk CANNOT call
GET /quality/scrap-reason-codes -- the active codes ride the queue payload
instead. Locked here:
  * a station-token caller receives the company's ACTIVE codes only, sorted by
    display_order then code, with exactly the picker fields
    (id/code/name/category/display_order),
  * tenant scope comes from the station's DB row -- another company's codes
    never appear,
  * zero active codes -> an empty list (the kiosk falls back to its legacy
    hardcoded reasons),
  * a normal user session sees the same field on the queue read.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    COMPANY_B,
    bearer,
    kiosk_token_for,
    make_kiosk_station,
    make_user,
    make_work_center,
    queue_url,
    user_headers,
)
from tests.lean_phase1_helpers import make_scrap_code

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


def test_station_gets_active_codes_sorted_and_shaped_for_the_picker(client: TestClient, db_session: Session):
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)

    # display_order then code: (1, MAT) < (2, AAA) < (2, ZZZ).
    make_scrap_code(db_session, company_id=COMPANY_A, code="ZZZ", name="Handling", category="handling", display_order=2)
    make_scrap_code(db_session, company_id=COMPANY_A, code="MAT", name="Material", category="material", display_order=1)
    make_scrap_code(db_session, company_id=COMPANY_A, code="AAA", name="Setup", category="setup", display_order=2)
    # Retired and foreign codes never reach the picker.
    make_scrap_code(db_session, company_id=COMPANY_A, code="OLD", is_active=False)
    make_scrap_code(db_session, company_id=COMPANY_B, code="FRGN")

    resp = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station)))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    codes = resp.json()["scrap_reason_codes"]

    assert [c["code"] for c in codes] == ["MAT", "AAA", "ZZZ"]
    # Exactly the picker fields -- nothing extra leaks onto the station payload.
    assert all(set(c.keys()) == {"id", "code", "name", "category", "display_order"} for c in codes)
    assert codes[0]["name"] == "Material"
    assert codes[0]["category"] == "material"
    assert codes[0]["display_order"] == 1


def test_empty_when_company_has_no_active_codes(client: TestClient, db_session: Session):
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    make_scrap_code(db_session, company_id=COMPANY_A, code="RETIRED", is_active=False)

    resp = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station)))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["scrap_reason_codes"] == []


def test_user_session_queue_read_carries_the_same_codes(client: TestClient, db_session: Session):
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    make_scrap_code(db_session, company_id=COMPANY_A, code="OT", name="Out of tolerance")

    resp = client.get(queue_url(wc.id), headers=user_headers(operator))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert [c["code"] for c in resp.json()["scrap_reason_codes"]] == ["OT"]
