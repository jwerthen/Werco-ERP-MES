"""Wallboard kpi_strip (Lean Phase 1, issue #88): trailing-30d floor KPIs on the TV.

Locks:
  * payload contract -- the five keys ride every wallboard read (user AND
    display-token callers),
  * honesty on empty data -- percentage legs are null (n/a), never a fake 0/100;
    open_wip_count is a real 0,
  * exact seeded values (ship OTD / FPY / scrap % / WIP count + age),
  * the ~5-min module TTL cache -- a second poll serves the cached strip until
    ``reset_kpi_strip_cache()`` (which tests MUST call between assertions),
  * best-effort -- a KPI compute failure yields kpi_strip=null, never a broken
    board.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrderStatus
from app.services.wallboard_service import reset_kpi_strip_cache
from tests.lean_phase1_helpers import (
    headers_for,
    make_op,
    make_part,
    make_shipment,
    make_user,
    make_wo,
    make_work_center,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

WALLBOARD_URL = "/api/v1/shop-floor/wallboard"
STRIP_KEYS = {"otd_ship_pct_30d", "fpy_pct_30d", "scrap_pct_30d", "open_wip_count", "avg_wip_age_days"}


@pytest.fixture(autouse=True)
def _fresh_kpi_cache():
    """The strip cache is module-level with a 5-minute TTL -- it outlives each
    test's dropped tables, so every test starts and ends from a clean cache."""
    reset_kpi_strip_cache()
    yield
    reset_kpi_strip_cache()


def test_kpi_strip_contract_and_null_on_empty_data(client: TestClient, db_session: Session):
    user = make_user(db_session)
    resp = client.get(WALLBOARD_URL, headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    strip = resp.json()["kpi_strip"]
    assert strip is not None
    assert set(strip.keys()) == STRIP_KEYS
    # No shipments/ops/WOs: percentage legs are honest n/a, count is a real 0.
    assert strip["otd_ship_pct_30d"] is None
    assert strip["fpy_pct_30d"] is None
    assert strip["scrap_pct_30d"] is None
    assert strip["avg_wip_age_days"] is None
    assert strip["open_wip_count"] == 0


def test_kpi_strip_exact_seeded_values(client: TestClient, db_session: Session):
    user = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    now = datetime.utcnow()

    # WIP: two open WOs released 4 and 2 days ago -> count 2, avg age 3.0d.
    make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, released_at=now - timedelta(days=4))
    make_wo(db_session, part, status_=WorkOrderStatus.RELEASED, released_at=now - timedelta(days=2))

    # Yield: one completed op 5 days ago -- 8 complete vs 2 scrap:
    # FPY = (8 - 0 - 2) / (8 + 2) = 60%; scrap = 2 / 10 = 20%.
    done = make_wo(db_session, part, status_=WorkOrderStatus.COMPLETE, released_at=now - timedelta(days=9))
    make_op(
        db_session,
        done,
        wc,
        status_=OperationStatus.COMPLETE,
        quantity_complete=8,
        quantity_scrapped=2,
        actual_end=now - timedelta(days=5),
    )

    # Delivery: the completed WO shipped in full 3 days ago vs promise yesterday.
    done.quantity_ordered = 8
    done.due_date = date.today() - timedelta(days=1)
    db_session.commit()
    make_shipment(db_session, done, ship_date=date.today() - timedelta(days=3), quantity_shipped=8)

    resp = client.get(WALLBOARD_URL, headers=headers_for(user))
    strip = resp.json()["kpi_strip"]
    assert strip["open_wip_count"] == 2
    assert strip["avg_wip_age_days"] == pytest.approx(3.0, abs=0.1)
    assert strip["fpy_pct_30d"] == pytest.approx(60.0)
    assert strip["scrap_pct_30d"] == pytest.approx(20.0)
    assert strip["otd_ship_pct_30d"] == pytest.approx(100.0)


def test_kpi_strip_is_ttl_cached_until_reset(client: TestClient, db_session: Session):
    """The 30s TV poll must NOT recompute analytics: within the TTL the strip is
    served from cache (stale by design); reset_kpi_strip_cache() refreshes it."""
    user = make_user(db_session)
    part = make_part(db_session)
    now = datetime.utcnow()
    make_wo(db_session, part, status_=WorkOrderStatus.RELEASED, released_at=now - timedelta(days=1))

    first = client.get(WALLBOARD_URL, headers=headers_for(user)).json()["kpi_strip"]
    assert first["open_wip_count"] == 1

    # New WIP lands, but the cached strip (same TTL window) still says 1.
    make_wo(db_session, part, status_=WorkOrderStatus.RELEASED, released_at=now - timedelta(days=1))
    cached = client.get(WALLBOARD_URL, headers=headers_for(user)).json()["kpi_strip"]
    assert cached["open_wip_count"] == 1

    reset_kpi_strip_cache()
    refreshed = client.get(WALLBOARD_URL, headers=headers_for(user)).json()["kpi_strip"]
    assert refreshed["open_wip_count"] == 2


def test_kpi_strip_compute_failure_omits_strip_not_the_board(client: TestClient, db_session: Session, monkeypatch):
    """An analytics blow-up must never take down the live board: kpi_strip is
    null for that poll (and NOT cached), the board payload stays intact."""
    import app.services.wallboard_service as wallboard_service

    def _boom(db, company_id):
        raise RuntimeError("analytics exploded")

    monkeypatch.setattr(wallboard_service, "_compute_kpi_strip", _boom)

    user = make_user(db_session)
    resp = client.get(WALLBOARD_URL, headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    payload = resp.json()
    assert payload["kpi_strip"] is None
    assert {"work_centers", "late_wos", "blocked_wos", "generated_at"} <= set(payload.keys())

    # Failures are not cached: once the compute recovers, the next poll has data.
    monkeypatch.undo()
    recovered = client.get(WALLBOARD_URL, headers=headers_for(user)).json()["kpi_strip"]
    assert recovered is not None


def test_kpi_strip_rides_the_display_token_read(client: TestClient, db_session: Session):
    """The scoped TV display token gets the strip on its one allowed endpoint --
    and remains fenced to that read-only surface."""
    admin = make_user(db_session, role=UserRole.ADMIN)
    issued = client.post("/api/v1/auth/display-token", json={"label": "KPI strip TV"}, headers=headers_for(admin))
    assert issued.status_code == status.HTTP_200_OK, issued.text
    display_headers = {
        "Authorization": f"Bearer {issued.json()['token']}",
        "X-Requested-With": "XMLHttpRequest",
    }

    resp = client.get(WALLBOARD_URL, headers=display_headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    strip = resp.json()["kpi_strip"]
    assert strip is not None
    assert set(strip.keys()) == STRIP_KEYS

    # Still display-scoped: any other read is refused (401), the token is not a session.
    assert client.get("/api/v1/shop-floor/dashboard", headers=display_headers).status_code == 401
