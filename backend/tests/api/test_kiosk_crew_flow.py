"""End-to-end crew-station flow: badge-minted operator tokens driving the
EXISTING shop-floor mutation endpoints.

The whole design bet of the crew kiosk is that the labor model already supports
multi-operator crews — so each badge scan mints a 5-minute ``scope="kiosk"``
operator token and the kiosk calls the SAME clock-in / clock-out / production /
complete endpoints a desktop session would, with the operator as
``current_user``. These tests prove the composed system end-to-end through the
real HTTP stack: station PIN → station token → badge token → labor mutation →
roster/tally read-back.

Covered invariants:
- Alice + Bob clock into the SAME operation concurrently (two open entries;
  ``started_by`` = the first); the G5-B qualification gate warns per operator.
- Bob's production report moves the shared tally with Bob as the audit actor.
- Charlie's COMPLETE auto-closes ALL open entries (per-entry durations) with
  ``completed_by`` = Charlie, and the response names who was auto-clocked-out
  (``closed_time_entries``).
- LEAVE is self-scoped: Alice's token cannot close Bob's entry (404).
- Double-JOIN on the same operation is a 400.
- A second COMPLETE is refused (the serialized twin of the concurrent-complete
  409; the true race is covered by test_completion_concurrency.py).
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.time_entry import TimeEntry
from app.models.work_order import OperationStatus, WorkOrderOperation
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    bearer,
    kiosk_token_for,
    make_kiosk_station,
    make_user,
    make_wo_with_operation,
    make_work_center,
    mint_badge_token,
    queue_url,
)

pytestmark = [pytest.mark.api, pytest.mark.integration, pytest.mark.requires_db]


@pytest.fixture
def crew(client: TestClient, db_session: Session):
    """A work center + station + queued operation + three badge operators,
    each holding a freshly badge-minted kiosk-scoped token."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    wo, op = make_wo_with_operation(db_session, company_id=COMPANY_A, work_center=wc, quantity_ordered=10)
    station_token = kiosk_token_for(station)

    operators = {}
    for name in ("Alice", "Bob", "Charlie"):
        user = make_user(db_session, company_id=COMPANY_A, first_name=name, last_name="Crew")
        minted = mint_badge_token(client, station_token, user.employee_id)
        assert minted.status_code == status.HTTP_200_OK, minted.text
        operators[name.lower()] = (user, minted.json()["access_token"])

    return {
        "wc": wc,
        "station": station,
        "station_token": station_token,
        "wo": wo,
        "op": op,
        **operators,
    }


def _clock_in(client: TestClient, token: str, crew_ctx: dict, entry_type: str = "run"):
    return client.post(
        "/api/v1/shop-floor/clock-in",
        headers=bearer(token),
        json={
            "work_order_id": crew_ctx["wo"].id,
            "operation_id": crew_ctx["op"].id,
            "work_center_id": crew_ctx["wc"].id,
            "entry_type": entry_type,
            "source": "kiosk",
        },
    )


def _roster(client: TestClient, crew_ctx: dict) -> list[dict]:
    resp = client.get(queue_url(crew_ctx["wc"].id), headers=bearer(crew_ctx["station_token"]))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    item = next(i for i in resp.json()["queue"] if i["operation_id"] == crew_ctx["op"].id)
    return item


def test_two_operators_concurrent_clock_in(client: TestClient, db_session: Session, crew):
    """Alice and Bob JOIN the same operation: two open entries, started_by is
    the FIRST joiner, the station roster shows both, and the G5-B
    qualification gate warns (never blocks) per operator."""
    alice, alice_token = crew["alice"]
    bob, bob_token = crew["bob"]

    r_alice = _clock_in(client, alice_token, crew)
    assert r_alice.status_code == status.HTTP_200_OK, r_alice.text
    r_bob = _clock_in(client, bob_token, crew, entry_type="setup")
    assert r_bob.status_code == status.HTTP_200_OK, r_bob.text

    # G5-B warn-and-record: neither operator has a SkillMatrix entry for this
    # WC, so each clock-in SUCCEEDS but carries its own qualification warning.
    for resp in (r_alice, r_bob):
        codes = {e["code"] for e in resp.json()["qualification_exceptions"]}
        assert "operator_not_skill_qualified" in codes

    open_entries = (
        db_session.query(TimeEntry).filter(TimeEntry.operation_id == crew["op"].id, TimeEntry.clock_out.is_(None)).all()
    )
    assert {e.user_id for e in open_entries} == {alice.id, bob.id}

    db_session.expire_all()
    op = db_session.query(WorkOrderOperation).filter(WorkOrderOperation.id == crew["op"].id).first()
    assert op.status == OperationStatus.IN_PROGRESS
    assert op.started_by == alice.id  # the FIRST joiner, not overwritten by Bob

    item = _roster(client, crew)
    assert {r["user_id"] for r in item["roster"]} == {alice.id, bob.id}


def test_double_join_rejected(client: TestClient, db_session: Session, crew):
    """JOINing an operation you're already clocked into is a 400 (the roster
    match on the kiosk should offer LEAVE instead)."""
    _, alice_token = crew["alice"]

    first = _clock_in(client, alice_token, crew)
    assert first.status_code == status.HTTP_200_OK, first.text

    dup = _clock_in(client, alice_token, crew)
    assert dup.status_code == status.HTTP_400_BAD_REQUEST, dup.text
    assert dup.json()["detail"] == "You are already clocked in to this operation."


def test_production_report_moves_tally_with_reporter_as_actor(client: TestClient, db_session: Session, crew):
    """Anyone on the crew reports quantities; the shared tally moves and the
    tamper-evident audit row is attributed to the REPORTER (Bob)."""
    _, alice_token = crew["alice"]
    bob, bob_token = crew["bob"]
    assert _clock_in(client, alice_token, crew).status_code == 200
    assert _clock_in(client, bob_token, crew).status_code == 200

    resp = client.post(
        f"/api/v1/shop-floor/operations/{crew['op'].id}/production",
        headers=bearer(bob_token),
        json={
            "quantity_complete_delta": 4,
            "quantity_scrapped_delta": 1,
            "scrap_reason": "porosity",
            "source": "kiosk",
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["operation"]["quantity_complete"] == 4
    assert resp.json()["operation"]["quantity_scrapped"] == 1

    # The station queue reflects the new tally for the whole crew.
    item = _roster(client, crew)
    assert item["quantity_complete"] == 4
    assert item["quantity_scrapped"] == 1

    db_session.expire_all()
    rows = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.action == "REPORT_OPERATION_PRODUCTION",
            AuditLog.resource_type == "work_order_operation",
            AuditLog.resource_id == crew["op"].id,
        )
        .all()
    )
    assert rows, "expected a REPORT_OPERATION_PRODUCTION audit row"
    assert all(r.user_id == bob.id for r in rows), "the audit actor must be the badge-identified reporter"


def test_leave_is_self_scoped(client: TestClient, db_session: Session, crew):
    """Alice's kiosk token can close only HER entry: Bob's entry id is a 404
    (tenant + user scoping on clock-out), her own is a 200."""
    alice, alice_token = crew["alice"]
    bob, bob_token = crew["bob"]
    assert _clock_in(client, alice_token, crew).status_code == 200
    assert _clock_in(client, bob_token, crew).status_code == 200

    entries = {
        e.user_id: e
        for e in db_session.query(TimeEntry)
        .filter(TimeEntry.operation_id == crew["op"].id, TimeEntry.clock_out.is_(None))
        .all()
    }

    stolen = client.post(
        f"/api/v1/shop-floor/clock-out/{entries[bob.id].id}",
        headers=bearer(alice_token),
        json={"quantity_produced": 0, "quantity_scrapped": 0, "source": "kiosk"},
    )
    assert stolen.status_code == status.HTTP_404_NOT_FOUND, stolen.text

    own = client.post(
        f"/api/v1/shop-floor/clock-out/{entries[alice.id].id}",
        headers=bearer(alice_token),
        json={"quantity_produced": 2, "quantity_scrapped": 0, "source": "kiosk"},
    )
    assert own.status_code == status.HTTP_200_OK, own.text

    # Bob is still on the roster; Alice is gone.
    item = _roster(client, crew)
    assert {r["user_id"] for r in item["roster"]} == {bob.id}


def test_complete_auto_closes_all_and_names_the_crew(client: TestClient, db_session: Session, crew):
    """Charlie's COMPLETE closes Alice's and Bob's open entries (per-entry
    durations), stamps completed_by=Charlie, and the response lists exactly
    who was auto-clocked-out so the kiosk can toast it."""
    alice, alice_token = crew["alice"]
    bob, bob_token = crew["bob"]
    charlie, charlie_token = crew["charlie"]
    assert _clock_in(client, alice_token, crew).status_code == 200
    assert _clock_in(client, bob_token, crew).status_code == 200

    resp = client.post(
        f"/api/v1/shop-floor/operations/{crew['op'].id}/complete",
        headers=bearer(charlie_token),
        json={"quantity_complete": 10, "source": "kiosk"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["is_fully_complete"] is True

    closed = body["closed_time_entries"]
    assert {c["user_id"] for c in closed} == {alice.id, bob.id}
    for c in closed:
        assert c["time_entry_id"]
        assert c["operator_name"] in ("Alice C.", "Bob C.")

    db_session.expire_all()
    op = db_session.query(WorkOrderOperation).filter(WorkOrderOperation.id == crew["op"].id).first()
    assert op.status == OperationStatus.COMPLETE
    assert op.completed_by == charlie.id

    # Every crew entry is closed with a real duration (the durable labor record).
    entries = db_session.query(TimeEntry).filter(TimeEntry.operation_id == crew["op"].id).all()
    assert len(entries) == 2
    for entry in entries:
        assert entry.clock_out is not None
        assert entry.duration_hours is not None and entry.duration_hours >= 0

    # The completed operation leaves the crew board.
    queue_resp = client.get(queue_url(crew["wc"].id), headers=bearer(crew["station_token"]))
    assert all(i["operation_id"] != crew["op"].id for i in queue_resp.json()["queue"])


def test_second_complete_rejected_409(client: TestClient, db_session: Session, crew):
    """Two crew members racing COMPLETE: the loser is refused with a 409 —
    here the serialized case (the winner's completion flipped the WO terminal,
    so the G6-A guard conflicts the second attempt; the mid-flight version-race
    409 is covered by test_completion_concurrency.py). The kiosk shows the
    server detail verbatim and refreshes."""
    alice, alice_token = crew["alice"]
    charlie, charlie_token = crew["charlie"]
    assert _clock_in(client, alice_token, crew).status_code == 200

    first = client.post(
        f"/api/v1/shop-floor/operations/{crew['op'].id}/complete",
        headers=bearer(charlie_token),
        json={"quantity_complete": 10, "source": "kiosk"},
    )
    assert first.status_code == status.HTTP_200_OK, first.text

    second = client.post(
        f"/api/v1/shop-floor/operations/{crew['op'].id}/complete",
        headers=bearer(alice_token),
        json={"quantity_complete": 10, "source": "kiosk"},
    )
    assert second.status_code == status.HTTP_409_CONFLICT, second.text
    assert second.json()["detail"] == "cannot complete operation: work order is complete"
