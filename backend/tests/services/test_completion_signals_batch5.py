"""Batch 5 / rank 8 — uniform completion signal set.

Compliance-gate coverage for the outbound completion signals:

* ``WebhookService.get_webhooks_for_event`` / ``dispatch_event`` are TENANT-SCOPED
  (EVT-3) — a completion in one company never matches another company's webhook,
  so completion data can't leak cross-tenant.
* ``create_webhook`` stamps the owning ``company_id`` so the scoped lookup can find
  it.
* The ARQ completion-signal task dispatches ONLY the owning tenant's webhooks.
* ``enqueue_job_best_effort`` swallows enqueue failures (a signal-enqueue failure
  must never fail an already-committed completion).

NOTE (notification pipeline PR 1): the wo.completed / wo.closed IN-APP + EMAIL
notification leg is now owned by the transactional-outbox notification pipeline
(driven by the emitted ``work_order_completed`` / ``work_order_closed``
OperationalEvents), NOT by this task — a second copy would double-fire. So the
completion-signal task now has ONLY the webhook leg; these tests assert that and
that it writes no in-app/log rows itself.

These exercise the tenant-isolation invariant (#1) on the signal surface.
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.core.queue import enqueue_job_best_effort
from app.models.company import Company
from app.models.notification import Notification, NotificationLog
from app.models.user import User, UserRole
from app.models.webhook import Webhook, WebhookDelivery
from app.models.work_order import WorkOrder, WorkOrderStatus, WorkOrderType
from app.services.webhook_service import WebhookService

# Module-level counter for globally-unique natural keys (tests run under -n auto
# against a shared per-worker SQLite file).
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _seed_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Co {company_id}", slug=f"sig-co-{company_id}", is_active=True))
        db.commit()


def _make_webhook(db: Session, *, company_id: int, events: list[str]) -> Webhook:
    _seed_company(db, company_id)
    return WebhookService(db).create_webhook(
        name=f"hook-{_next()}",
        url="https://example.test/hook",
        events=events,
        secret="s3cret",
        company_id=company_id,
    )


# --------------------------------------------------------------------------- #
# get_webhooks_for_event / dispatch_event tenant scoping (the compliance gate)
# --------------------------------------------------------------------------- #
@pytest.mark.requires_db
def test_get_webhooks_for_event_is_tenant_scoped(db_session: Session):
    a_hook = _make_webhook(db_session, company_id=1, events=["work_order.completed"])
    b_hook = _make_webhook(db_session, company_id=2, events=["work_order.completed"])

    a_matches = WebhookService(db_session).get_webhooks_for_event("work_order.completed", company_id=1)
    ids = {w.id for w in a_matches}

    assert a_hook.id in ids, "company 1's webhook should match its own completion event"
    assert b_hook.id not in ids, "company 2's webhook must NOT match when scoped to company 1"
    assert all(w.company_id == 1 for w in a_matches)


@pytest.mark.requires_db
def test_get_webhooks_for_event_filters_by_subscribed_event(db_session: Session):
    completed_hook = _make_webhook(db_session, company_id=1, events=["work_order.completed"])
    closed_hook = _make_webhook(db_session, company_id=1, events=["work_order.closed"])

    matches = WebhookService(db_session).get_webhooks_for_event("work_order.completed", company_id=1)
    ids = {w.id for w in matches}

    assert completed_hook.id in ids
    assert closed_hook.id not in ids, "a webhook not subscribed to the event must not match"


@pytest.mark.requires_db
def test_get_webhooks_for_event_excludes_inactive(db_session: Session):
    active = _make_webhook(db_session, company_id=1, events=["work_order.completed"])
    inactive = _make_webhook(db_session, company_id=1, events=["work_order.completed"])
    inactive.is_active = False
    db_session.commit()

    matches = WebhookService(db_session).get_webhooks_for_event("work_order.completed", company_id=1)
    ids = {w.id for w in matches}

    assert active.id in ids
    assert inactive.id not in ids


@pytest.mark.requires_db
def test_dispatch_event_only_enqueues_own_tenant_webhooks(db_session: Session):
    a_hook = _make_webhook(db_session, company_id=1, events=["work_order.completed"])
    _make_webhook(db_session, company_id=2, events=["work_order.completed"])

    enqueued: list[dict] = []

    async def _fake_enqueue(job_function, *args, **kwargs):
        enqueued.append(kwargs)

    import asyncio

    with patch("app.services.webhook_service.enqueue_job", new=AsyncMock(side_effect=_fake_enqueue)):
        asyncio.run(
            WebhookService(db_session).dispatch_event("work_order.completed", {"work_order_id": 99}, company_id=1)
        )

    dispatched_ids = {kw["webhook_id"] for kw in enqueued}
    assert dispatched_ids == {a_hook.id}, "dispatch must only reach the owning company's webhook"


@pytest.mark.requires_db
def test_dispatch_event_enqueues_with_company_id(db_session: Session):
    """Foot-gun follow-through: the enqueued send_webhook_job carries company_id so the
    delivery task can stamp a tenant-consistent WebhookDelivery row (e1)."""
    import asyncio

    _make_webhook(db_session, company_id=3, events=["work_order.completed"])

    enqueued: list[dict] = []

    async def _fake_enqueue(job_function, *args, **kwargs):
        enqueued.append(kwargs)

    with patch("app.services.webhook_service.enqueue_job", new=AsyncMock(side_effect=_fake_enqueue)):
        asyncio.run(
            WebhookService(db_session).dispatch_event("work_order.completed", {"work_order_id": 5}, company_id=3)
        )

    assert enqueued, "a subscribed webhook should be enqueued"
    assert all(kw.get("company_id") == 3 for kw in enqueued), "every enqueued delivery carries the owning company_id"


# --------------------------------------------------------------------------- #
# Foot-gun guard: an UNSCOPED dispatch is refused (no cross-tenant fan-out)
# --------------------------------------------------------------------------- #
@pytest.mark.requires_db
def test_dispatch_event_refuses_unscoped_dispatch(db_session: Session):
    """COMPLIANCE foot-gun: dispatch_event with no company_id (None) must be REFUSED
    rather than fanning one tenant's event out to every company's webhooks."""
    import asyncio

    _make_webhook(db_session, company_id=1, events=["work_order.completed"])

    with pytest.raises(ValueError):
        asyncio.run(
            WebhookService(db_session).dispatch_event("work_order.completed", {"work_order_id": 1}, company_id=None)
        )


# --------------------------------------------------------------------------- #
# record_delivery stamps WebhookDelivery.company_id (e1)
# --------------------------------------------------------------------------- #
@pytest.mark.requires_db
def test_record_delivery_stamps_company_id_from_webhook(db_session: Session):
    """WebhookDelivery is a TenantMixin (non-null company_id) row -- record_delivery
    MUST stamp it (derived from the owning webhook) or the INSERT fails on Postgres."""
    hook = _make_webhook(db_session, company_id=5, events=["work_order.completed"])

    delivery = WebhookService(db_session).record_delivery(
        webhook_id=hook.id,
        event="work_order.completed",
        payload={"work_order_id": 1},
        delivered=True,
    )

    assert delivery.company_id == 5, "delivery inherits the owning webhook's tenant"
    refreshed = db_session.query(WebhookDelivery).filter(WebhookDelivery.id == delivery.id).first()
    assert refreshed.company_id == 5


@pytest.mark.requires_db
def test_record_delivery_falls_back_to_passed_company_id(db_session: Session):
    """When the owning webhook can't be loaded, record_delivery stamps the explicitly
    threaded company_id so the (non-null) delivery row is still tenant-consistent."""
    # Use a webhook id that does not exist so the internal lookup misses.
    delivery = WebhookService(db_session).record_delivery(
        webhook_id=999999,
        event="work_order.completed",
        payload={"work_order_id": 1},
        error="Rate limit exceeded",
        delivered=False,
        company_id=8,
    )

    assert delivery.company_id == 8


# --------------------------------------------------------------------------- #
# create_webhook stamps company_id
# --------------------------------------------------------------------------- #
@pytest.mark.requires_db
def test_create_webhook_stamps_company_id(db_session: Session):
    hook = _make_webhook(db_session, company_id=7, events=["work_order.completed"])
    refreshed = db_session.query(Webhook).filter(Webhook.id == hook.id).first()
    assert refreshed.company_id == 7


# --------------------------------------------------------------------------- #
# enqueue_job_best_effort never raises
# --------------------------------------------------------------------------- #
def test_enqueue_job_best_effort_swallows_failures():
    """A Redis/enqueue failure must be swallowed so a completion can't fail on it."""
    with patch("app.core.queue.create_pool", side_effect=RuntimeError("redis down")):
        result = enqueue_job_best_effort("dispatch_work_order_completion_signals_job", work_order_id=1)
    assert result is False, "a failed enqueue returns False, not an exception"


# --------------------------------------------------------------------------- #
# Completion-signal task: webhook-only, tenant-scoped (the notification leg moved
# to the transactional-outbox pipeline, PR 1)
# --------------------------------------------------------------------------- #
def _make_user(db: Session, *, company_id: int, role: UserRole) -> User:
    _seed_company(db, company_id)
    n = _next()
    user = User(
        email=f"sig-{n}@co{company_id}.test",
        employee_id=f"SIG-{n:05d}",
        first_name="Sig",
        last_name=f"C{company_id}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=role,
        is_active=True,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_work_order(db: Session, *, company_id: int) -> WorkOrder:
    from app.models.part import Part

    _seed_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"SIG-PART-{n}",
        name="Part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    wo = WorkOrder(
        work_order_number=f"SIG-WO-{n}",
        part_id=part.id,
        work_order_type=WorkOrderType.PRODUCTION,
        status=WorkOrderStatus.COMPLETE,
        quantity_ordered=10,
        quantity_complete=10,
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


@pytest.mark.requires_db
def test_completion_signal_task_scopes_webhook_and_has_no_notification_leg(db_session: Session, monkeypatch):
    """The ARQ task dispatches ONLY the owning company's webhooks and does NOT send any
    in-app/email notification itself (that leg moved to the outbox). It must not write
    Notification / NotificationLog rows."""
    import asyncio

    from app.jobs import completion_signal_jobs

    # Two tenants, each with a subscribed webhook.
    _make_user(db_session, company_id=1, role=UserRole.SUPERVISOR)
    hook_a = _make_webhook(db_session, company_id=1, events=["work_order.completed"])
    _make_webhook(db_session, company_id=2, events=["work_order.completed"])
    wo = _make_work_order(db_session, company_id=1)

    # The task opens its own SessionLocal; point it at the test session.
    monkeypatch.setattr(completion_signal_jobs, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    dispatched_webhook_ids: list[int] = []

    async def _fake_enqueue(job_function, *args, **kwargs):
        dispatched_webhook_ids.append(kwargs.get("webhook_id"))

    monkeypatch.setattr("app.services.webhook_service.enqueue_job", _fake_enqueue)

    result = asyncio.run(
        completion_signal_jobs.dispatch_work_order_completion_signals_task(
            work_order_id=wo.id, company_id=1, status="COMPLETE"
        )
    )

    # Webhook dispatch is tenant-scoped: company 1's hook only.
    assert dispatched_webhook_ids == [hook_a.id]
    assert result["webhook_dispatched"] is True
    # The notification leg is gone: the result no longer reports "notified", and the task
    # wrote no in-app inbox / delivery-log rows (the outbox owns those now).
    assert "notified" not in result
    assert db_session.query(Notification).count() == 0
    assert db_session.query(NotificationLog).count() == 0


@pytest.mark.requires_db
def test_completion_signal_webhook_payload_drops_customer_name(db_session: Session, monkeypatch):
    """CUI minimization (fix d): the OUTBOUND webhook payload egresses to an arbitrary
    external URL, so it must NOT carry customer_name (the clearest CUI) -- only the
    structured identifiers a subscriber needs."""
    import asyncio

    from app.jobs import completion_signal_jobs

    _make_webhook(db_session, company_id=1, events=["work_order.completed"])
    wo = _make_work_order(db_session, company_id=1)
    # Give the WO a customer_name so a leak would be detectable.
    wo.customer_name = "Lockheed Martin (CUI)"
    db_session.commit()

    monkeypatch.setattr(completion_signal_jobs, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    captured_payloads: list[dict] = []

    async def _fake_enqueue(job_function, *args, **kwargs):
        captured_payloads.append(kwargs.get("payload"))

    monkeypatch.setattr("app.services.webhook_service.enqueue_job", _fake_enqueue)

    asyncio.run(
        completion_signal_jobs.dispatch_work_order_completion_signals_task(
            work_order_id=wo.id, company_id=1, status="COMPLETE"
        )
    )

    assert captured_payloads, "the webhook leg should have enqueued a delivery"
    for payload in captured_payloads:
        assert "customer_name" not in payload, "outbound webhook payload must not carry customer_name (CUI)"
        # The structured identifiers a subscriber legitimately needs ARE present.
        assert payload["work_order_id"] == wo.id
        assert payload["status"] == "COMPLETE"
        assert payload["company_id"] == 1
        assert "completed_at" in payload
        # No free-text / customer fields leaked through.
        assert "Lockheed" not in str(payload)


@pytest.mark.requires_db
def test_completion_signal_task_closed_status_dispatches_closed_webhook(db_session: Session, monkeypatch):
    """status="CLOSED" (the mark_shipped path) dispatches the ``work_order.closed``
    webhook event, tenant-scoped: company 1's closed-hook is reached, company 2's is
    never targeted, and a company-1 hook subscribed only to ``completed`` is skipped."""
    import asyncio

    from app.jobs import completion_signal_jobs

    closed_a = _make_webhook(db_session, company_id=1, events=["work_order.closed"])
    _make_webhook(db_session, company_id=1, events=["work_order.completed"])  # wrong event -> skipped
    _make_webhook(db_session, company_id=2, events=["work_order.closed"])  # wrong tenant -> skipped
    wo = _make_work_order(db_session, company_id=1)

    monkeypatch.setattr(completion_signal_jobs, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    dispatched_webhook_ids: list[int] = []

    async def _fake_enqueue(job_function, *args, **kwargs):
        dispatched_webhook_ids.append(kwargs.get("webhook_id"))

    monkeypatch.setattr("app.services.webhook_service.enqueue_job", _fake_enqueue)

    result = asyncio.run(
        completion_signal_jobs.dispatch_work_order_completion_signals_task(
            work_order_id=wo.id, company_id=1, status="CLOSED"
        )
    )

    assert dispatched_webhook_ids == [closed_a.id], "only company 1's work_order.closed hook is dispatched"
    assert result["webhook_dispatched"] is True


@pytest.mark.requires_db
def test_completion_signal_task_survives_webhook_leg_failure(db_session: Session, monkeypatch):
    """Failure isolation (Point 5): if the (only remaining) webhook leg raises, the task
    does NOT propagate -- it completes and reports the leg as not dispatched. An outbound
    failure must never fail the already-committed completion."""
    import asyncio

    from app.jobs import completion_signal_jobs

    _make_webhook(db_session, company_id=1, events=["work_order.completed"])
    wo = _make_work_order(db_session, company_id=1)

    monkeypatch.setattr(completion_signal_jobs, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    async def _boom_dispatch(self, event, payload, company_id=None):
        raise RuntimeError("webhook subsystem down")

    monkeypatch.setattr("app.services.webhook_service.WebhookService.dispatch_event", _boom_dispatch)

    # Must NOT raise despite the webhook leg failing.
    result = asyncio.run(
        completion_signal_jobs.dispatch_work_order_completion_signals_task(
            work_order_id=wo.id, company_id=1, status="COMPLETE"
        )
    )

    assert result["webhook_dispatched"] is False, "the failed webhook leg is reported as not dispatched"


@pytest.mark.requires_db
def test_completion_signal_task_writes_no_notification_rows(db_session: Session, monkeypatch):
    """The notification leg is fully gone (it flows via the outbox now): running the task
    for a completed WO with recipients present writes ZERO in-app / delivery-log rows."""
    import asyncio

    from app.jobs import completion_signal_jobs

    _make_user(db_session, company_id=1, role=UserRole.SUPERVISOR)
    _make_user(db_session, company_id=1, role=UserRole.MANAGER)
    _make_webhook(db_session, company_id=1, events=["work_order.completed"])
    wo = _make_work_order(db_session, company_id=1)

    monkeypatch.setattr(completion_signal_jobs, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    async def _fake_enqueue(job_function, *args, **kwargs):
        return None

    monkeypatch.setattr("app.services.webhook_service.enqueue_job", _fake_enqueue)

    asyncio.run(
        completion_signal_jobs.dispatch_work_order_completion_signals_task(
            work_order_id=wo.id, company_id=1, status="COMPLETE"
        )
    )

    assert db_session.query(Notification).count() == 0
    assert db_session.query(NotificationLog).count() == 0


@pytest.mark.requires_db
def test_completion_signal_task_missing_work_order_is_safe_noop(db_session: Session, monkeypatch):
    """A stale / cross-tenant work_order_id is a safe no-op: the task loads the WO
    under (id, company_id) scope, finds nothing, and returns without dispatching -- it
    must not 500 or reach into another tenant."""
    import asyncio

    from app.jobs import completion_signal_jobs

    monkeypatch.setattr(completion_signal_jobs, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    result = asyncio.run(
        completion_signal_jobs.dispatch_work_order_completion_signals_task(
            work_order_id=999999, company_id=1, status="COMPLETE"
        )
    )

    assert result == {"webhook_dispatched": False, "reason": "work_order_not_found"}
