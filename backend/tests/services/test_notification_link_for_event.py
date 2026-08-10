"""Behavioral coverage of ``notification_dispatch._link_for_event`` -- one case per row of
the link table -- plus the ``_fan_out`` relative-path fence and the email-button guard.

There was ZERO coverage of this function before 2026-08-07, which is how six unresolvable
link shapes shipped green and a user clicking "Material received: RCV-20260807-008" in the
bell landed on the app's 404 screen.

Two halves, and both matter:

* THIS file pins the VALUES the builder returns (including the deliberate ``None``s and the
  ``work_order_id``-wins-first precedence);
* ``tests/test_notification_link_routes.py`` pins that those values RESOLVE against
  ``frontend/src/App.tsx``.

A value can be right here and still be a 404 without the other file, so neither is
redundant.

The builder cases are pure -- ``_link_for_event`` reads ``work_order_id`` / ``entity_type``
/ ``entity_id`` / ``event_payload`` and never touches the DB -- so they run against a
``SimpleNamespace`` fake with no fixtures. Only the fan-out fence test needs a database.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

import app.services.notification_dispatch as dispatch
from app.models.company import Company
from app.models.notification import DigestQueue, Notification
from app.models.user import User, UserRole
from app.services import notification_links as links
from app.services.notification_dispatch import _link_for_event, dispatch_direct


def _event(
    *,
    work_order_id=None,
    entity_type=None,
    entity_id=None,
    payload=None,
) -> SimpleNamespace:
    """The four attributes ``_link_for_event`` is allowed to read. Deliberately NOT an
    ``OperationalEvent`` row: if the builder ever starts re-querying the DB or reading a
    fifth column, these tests blow up with an AttributeError, which is the correct alarm."""
    return SimpleNamespace(
        work_order_id=work_order_id,
        entity_type=entity_type,
        entity_id=entity_id,
        event_payload=payload,
    )


# ---------------------------------------------------------------------------
# 1. Precedence: work_order_id wins over everything.
# ---------------------------------------------------------------------------


def test_work_order_id_wins_over_entity_type():
    """26 catalog keys carry BOTH a work_order_id and an entity_type (ncr.*, fai.*,
    car.created-from-ncr, downtime.*, shipment.*). The WO detail page is a genuine by-id
    route, so it is always the better landing -- and it is checked first."""
    assert _link_for_event(_event(work_order_id=7, entity_type="ncr", entity_id=3)) == "/work-orders/7"
    assert _link_for_event(_event(work_order_id=7, entity_type="fai", entity_id=3)) == "/work-orders/7"
    assert _link_for_event(_event(work_order_id=7, entity_type="downtime_event", entity_id=3)) == "/work-orders/7"


def test_work_order_link_uses_the_registry_template():
    assert _link_for_event(_event(work_order_id=1042)) == links.work_order_detail(1042)


# ---------------------------------------------------------------------------
# 2. The reported bug: po_receipt -> Purchasing, via the payload's po_id.
# ---------------------------------------------------------------------------


def test_po_receipt_with_po_id_lands_on_purchasing():
    """THE REPORTED BUG, PINNED. receipt.created / receipt.voided / receipt.corrected all
    emit entity_type="po_receipt" and never set work_order_id, so this branch fires 100% of
    the time. It used to emit /purchasing/{id}, which is not a route."""
    link = _link_for_event(_event(entity_type="po_receipt", entity_id=99, payload={"po_id": 12}))
    assert link == "/purchasing?po=12"
    assert link == links.purchase_order(12)
    # entity_id is the RECEIPT id, not the PO id -- it must not leak into the link.
    assert "99" not in link


def test_po_receipt_without_po_id_is_none():
    """The contract, not an accident: with no po_id there is no honest destination, so the
    row renders as a non-navigating button rather than guessing."""
    assert _link_for_event(_event(entity_type="po_receipt", entity_id=99, payload={})) is None
    assert _link_for_event(_event(entity_type="po_receipt", entity_id=99, payload=None)) is None


def test_inspection_failed_payload_deliberately_carries_no_po_id():
    """REGRESSION FENCE -- do not "fix" this by adding po_id to that payload.

    ``inspection.failed`` is the one ``po_receipt`` emitter whose payload omits ``po_id``
    (api/endpoints/receiving.py, the ``purchase_receipt_inspected`` emit). Adding it would
    make ``_link_for_event`` return ``/purchasing?po=...`` -- and that is a REGRESSION, not
    an improvement: the entry's audience is roles ``(QUALITY, MANAGER)``
    (notification_catalog.py), the ``quality`` role has no ``purchasing:view``
    (frontend/src/utils/permissions.ts), and ``/purchasing`` requires it
    (routeAccessRequirements in App.tsx). Quality inspectors -- the PRIMARY audience for a
    failed incoming inspection -- would be sent to ``/unauthorized``, which renders with no
    ``Layout``: the same chrome-less dead end as the 404 this whole change exists to remove.
    Today they get a harmless non-navigating row, which is strictly better.

    See notification_links.py rule 4 (recipients must be able to reach the route).
    """
    source = (Path(__file__).resolve().parents[2] / "app" / "api" / "endpoints" / "receiving.py").read_text()
    emit = source.split('event_type="purchase_receipt_inspected"', 1)
    assert len(emit) == 2, "the purchase_receipt_inspected emit moved -- re-point this fence"
    payload_block = emit[1].split("},", 1)[0]
    assert '"po_id"' not in payload_block, (
        'The purchase_receipt_inspected payload now carries "po_id", which gives '
        "inspection.failed a /purchasing link its QUALITY recipients cannot open. Either "
        "revert it, or first give the quality role purchasing:view / widen the route's "
        "permission -- see this test's docstring."
    )


def test_purchase_order_entity_lands_on_purchasing():
    """po.sent carries the PO id as entity_id and previously produced NO link at all."""
    assert _link_for_event(_event(entity_type="purchase_order", entity_id=9)) == "/purchasing?po=9"


# ---------------------------------------------------------------------------
# 3. Quality: FAI is record-bearing, NCR/CAR are deliberately record-less.
# ---------------------------------------------------------------------------


def test_fai_lands_on_the_fai_tab_with_the_report_id():
    """Record-bearing is honest here ONLY because Quality's openFaiDetail does a real
    GET /quality/fai/{id}. Do not copy this shape to a page that filters a loaded array."""
    assert _link_for_event(_event(entity_type="fai", entity_id=4)) == "/quality?tab=fai&fai=4"


def test_ncr_and_car_land_record_less_on_their_tab():
    """There is NO NCR or CAR detail view in the app. An id in the URL would be a promise
    the page cannot keep, so none is emitted -- and no entity_id is required to build it."""
    assert _link_for_event(_event(entity_type="ncr", entity_id=42)) == "/quality?tab=ncr"
    assert _link_for_event(_event(entity_type="ncr", entity_id=None)) == "/quality?tab=ncr"
    assert _link_for_event(_event(entity_type="car", entity_id=42)) == "/quality?tab=car"
    assert _link_for_event(_event(entity_type="car", entity_id=None)) == "/quality?tab=car"


def test_downtime_event_lands_record_less_on_the_downtime_list():
    """Downtime is normally reported against a work center with no WO, so this is the
    common case -- and it previously produced no link at all."""
    assert _link_for_event(_event(entity_type="downtime_event", entity_id=8)) == "/downtime"
    assert _link_for_event(_event(entity_type="downtime_event", entity_id=None)) == "/downtime"


# ---------------------------------------------------------------------------
# 4. The deleted shipment branch stays deleted.
# ---------------------------------------------------------------------------


def test_shipment_entity_emits_no_link():
    """REGRESSION FENCE for the removed ``/shipping/{id}`` branch. shipments.work_order_id
    is nullable=False, so a real shipment event always takes the work_order_id branch above
    and this one was unreachable dead code pointing at a non-existent route. If someone
    "restores" it, this test goes red."""
    assert _link_for_event(_event(entity_type="shipment", entity_id=5)) is None


# ---------------------------------------------------------------------------
# 5. Defaults.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entity_type",
    ["", None, "document", "quote", "calibration", "customer_complaint", "engineering_change"],
)
def test_unmatched_entity_types_emit_none(entity_type):
    """The correct default. The catalog has ~26 dormant entries; none of them may start
    emitting a guessed link when they are wired up."""
    assert _link_for_event(_event(entity_type=entity_type, entity_id=3)) is None


def test_entity_type_matching_is_case_insensitive():
    assert _link_for_event(_event(entity_type="NCR", entity_id=1)) == "/quality?tab=ncr"
    assert _link_for_event(_event(entity_type="PO_Receipt", payload={"po_id": 5})) == "/purchasing?po=5"


def test_every_emitted_link_is_a_registry_value():
    """Belt-and-braces on the no-inline-literals rule: every shape this builder can produce
    is either a registry constant or a registry builder's output."""
    produced = {
        _link_for_event(_event(work_order_id=1)),
        _link_for_event(_event(entity_type="po_receipt", payload={"po_id": 1})),
        _link_for_event(_event(entity_type="purchase_order", entity_id=1)),
        _link_for_event(_event(entity_type="fai", entity_id=1)),
        _link_for_event(_event(entity_type="ncr")),
        _link_for_event(_event(entity_type="car")),
        _link_for_event(_event(entity_type="downtime_event")),
    }
    expected = {
        links.work_order_detail(1),
        links.purchase_order(1),
        links.quality_fai_detail(1),
        links.QUALITY_NCR_LIST,
        links.QUALITY_CAR_LIST,
        links.DOWNTIME_LIST,
    }
    assert produced == expected


# ---------------------------------------------------------------------------
# 6. The _fan_out relative-path fence (needs a DB).
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
@pytest.mark.parametrize("hostile", ["//evil.example/x", "https://evil.example/x", "javascript:alert(1)"])
def test_fan_out_drops_a_non_relative_link(db_session: Session, monkeypatch, hostile):
    """``dispatch_direct(link=...)`` is an open kwarg. react-router renders a value with a
    scheme or a leading "//" as a plain external <a href> with the SPA click handler
    dropped, so a hostile value would become a live anchor in the bell popover. The fence
    drops it to None rather than persisting it -- on the Notification row AND on the
    DigestQueue copy."""
    monkeypatch.setattr(dispatch, "enqueue_job", AsyncMock())
    monkeypatch.setattr(dispatch, "_dedup_reserve", AsyncMock(return_value=True))

    if not db_session.query(Company).filter(Company.id == 1).first():
        db_session.add(Company(id=1, name="Co 1", slug="fence-co-1", is_active=True))
        db_session.commit()
    user = User(
        email=f"fence-{abs(hash(hostile)) % 100000}@co1.test",
        employee_id=f"FENCE-{abs(hash(hostile)) % 100000:05d}",
        first_name="Fence",
        last_name="User",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=UserRole.SUPERVISOR,
        is_active=True,
        company_id=1,
    )
    db_session.add(user)
    db_session.commit()

    asyncio.run(
        dispatch_direct(
            db_session,
            event_key="wo.late",
            company_id=1,
            recipients=[user],
            related_type="WorkOrder",
            related_id=4321,
            title="hostile link",
            link=hostile,
        )
    )
    db_session.flush()

    rows = db_session.query(Notification).filter(Notification.user_id == user.id).all()
    assert rows, "expected the notification to still be created -- the fence drops the link, not the row"
    assert all(row.link is None for row in rows), f"non-relative link {hostile!r} was persisted"
    for queued in db_session.query(DigestQueue).filter(DigestQueue.user_id == user.id).all():
        assert (queued.event_data or {}).get("link") is None


@pytest.mark.requires_db
def test_fan_out_keeps_a_relative_link(db_session: Session, monkeypatch):
    """Control for the test above: a normal registry value survives untouched."""
    monkeypatch.setattr(dispatch, "enqueue_job", AsyncMock())
    monkeypatch.setattr(dispatch, "_dedup_reserve", AsyncMock(return_value=True))

    if not db_session.query(Company).filter(Company.id == 1).first():
        db_session.add(Company(id=1, name="Co 1", slug="fence-co-ok", is_active=True))
        db_session.commit()
    user = User(
        email="fence-ok@co1.test",
        employee_id="FENCE-OK001",
        first_name="Fence",
        last_name="Ok",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=UserRole.SUPERVISOR,
        is_active=True,
        company_id=1,
    )
    db_session.add(user)
    db_session.commit()

    asyncio.run(
        dispatch_direct(
            db_session,
            event_key="wo.late",
            company_id=1,
            recipients=[user],
            related_type="WorkOrder",
            related_id=4322,
            title="ok link",
            link=links.work_order_detail(4322),
        )
    )
    db_session.flush()

    rows = db_session.query(Notification).filter(Notification.user_id == user.id).all()
    assert [row.link for row in rows] == ["/work-orders/4322"]


# ---------------------------------------------------------------------------
# 7. The email "Open in Werco" button is only composed when it can be ABSOLUTE.
# ---------------------------------------------------------------------------


def _enqueued_email_context(monkeypatch, *, base_url: str, link) -> dict:
    spy = AsyncMock()
    monkeypatch.setattr(dispatch, "enqueue_job", spy)
    monkeypatch.setattr(dispatch.settings, "FRONTEND_BASE_URL", base_url)
    asyncio.run(
        dispatch._enqueue_email(
            user=SimpleNamespace(email="who@example.test"),
            title="t",
            body="b",
            link=link,
            template=None,
            context=None,
        )
    )
    assert spy.await_count == 1
    return spy.await_args.kwargs["context"]


def test_email_button_is_omitted_when_frontend_base_url_is_empty(monkeypatch):
    """``FRONTEND_BASE_URL`` defaults to "". Concatenating onto it produced
    ``<a href="/quality?tab=ncr">`` INSIDE AN EMAIL -- a relative href no mail client can
    resolve. base.html already guards the footer this way; the button now matches, which is
    also what makes docs/ENVIRONMENT_VARIABLES.md's claim true."""
    context = _enqueued_email_context(monkeypatch, base_url="", link=links.QUALITY_NCR_LIST)
    assert "notification_link" not in context, "a relative href was mailed as the Open-in-Werco button"


def test_email_button_is_absolute_when_frontend_base_url_is_set(monkeypatch):
    context = _enqueued_email_context(
        monkeypatch, base_url="https://werco-erp-mes.vercel.app", link=links.QUALITY_NCR_LIST
    )
    assert context["notification_link"] == "https://werco-erp-mes.vercel.app/quality?tab=ncr"


def test_email_button_is_omitted_when_there_is_no_link(monkeypatch):
    context = _enqueued_email_context(monkeypatch, base_url="https://werco-erp-mes.vercel.app", link=None)
    assert "notification_link" not in context
