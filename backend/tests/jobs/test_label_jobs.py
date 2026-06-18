"""Coverage for the auto-print-on-receipt ARQ job (``print_receiving_label_task``).

The job is the SOLE decider of whether an auto-print happens: it is a no-op unless
the company's ``CompanyPrintProfile`` exists, is active, has ``auto_print_on_receipt``
ON, AND has ``allow_print_egress`` ON. When all gates pass it prints (ProxyBox
mocked). A printer/tunnel failure is swallowed (the job NEVER raises out of the
worker) and surfaced as a ``receiving_label_print_failed`` operational event.

Setup: the job opens its own ``SessionLocal()`` (it runs outside a request), so we
patch ``app.jobs.label_jobs.SessionLocal`` to hand back the shared test session
(wrapped so the job's ``db.close()`` does not sever it for the assertions). The
ProxyBox network layer is mocked at ``app.services.print_service.ProxyBoxClient`` --
NO real outbound call is ever made.
"""

import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.jobs.label_jobs import print_receiving_label_task
from app.models.company import Company
from app.models.document import Document, DocumentType
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.print_profile import CompanyPrintProfile
from app.models.purchasing import (
    POReceipt,
    POStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    ReceiptStatus,
    Vendor,
)
from app.models.user import User, UserRole
from app.services.proxybox_client import ProxyBoxError

pytestmark = [pytest.mark.unit, pytest.mark.requires_db]

PROXYBOX_CLIENT_PATH = "app.services.print_service.ProxyBoxClient"
SESSION_PATH = "app.jobs.label_jobs.SessionLocal"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"job-co-{company_id}", is_active=True))
        db.commit()


def make_receipt(db: Session, *, company_id: int) -> POReceipt:
    _ensure_company(db, company_id)
    n = _next()
    vendor = Vendor(code=f"V{n:05d}", name=f"Vendor {n}", is_active=True, is_approved=True, company_id=company_id)
    db.add(vendor)
    part = Part(
        part_number=f"P-{n:05d}",
        name=f"Part {n}",
        description="Job part",
        revision="A",
        part_type="purchased",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    po = PurchaseOrder(
        po_number=f"PO-{n:05d}",
        vendor_id=vendor.id,
        status=POStatus.PARTIAL,
        order_date=date.today(),
        company_id=company_id,
    )
    db.add(po)
    db.flush()
    line = PurchaseOrderLine(
        purchase_order_id=po.id,
        line_number=1,
        part_id=part.id,
        quantity_ordered=10,
        quantity_received=5,
        unit_price=5.0,
        is_closed=False,
        company_id=company_id,
    )
    db.add(line)
    db.flush()
    receiver = User(
        email=f"jobrcv{n}@co{company_id}.test",
        employee_id=f"JOB-{n:05d}",
        first_name="Job",
        last_name="Receiver",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=UserRole.ADMIN,
        is_active=True,
        company_id=company_id,
    )
    db.add(receiver)
    db.flush()
    receipt = POReceipt(
        receipt_number=f"RCV-JOB-{n:05d}",
        po_line_id=line.id,
        quantity_received=5,
        lot_number=f"LOT-{n:05d}",
        status=ReceiptStatus.ACCEPTED,
        requires_inspection=False,
        received_by=receiver.id,
        received_at=datetime.utcnow(),
        company_id=company_id,
    )
    receipt.company_id = company_id
    db.add(receipt)
    db.commit()
    db.refresh(receipt)
    receipt._receiver_id = receiver.id  # convenience for the caller
    return receipt


def make_profile(
    db: Session,
    *,
    company_id: int,
    auto_print: bool,
    allow_egress: bool,
    is_active: bool = True,
    api_key: str = "PBX_JOB_SECRET_5151",
) -> CompanyPrintProfile:
    _ensure_company(db, company_id)
    profile = CompanyPrintProfile(
        proxybox_base_url="https://pbx-test.pbxz.cloud/api/v1",
        proxybox_target="usb_sn_JOBPRINTER",
        default_paper_size="4x6",
        default_copies=1,
        auto_print_on_receipt=auto_print,
        allow_print_egress=allow_egress,
        is_active=is_active,
    )
    profile.company_id = company_id
    if api_key:
        profile.set_api_key(api_key)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _non_closing_factory(db: Session):
    """Return a SessionLocal stand-in that yields the shared test session.

    The job calls ``db.close()`` in its ``finally``; on the shared session that
    would detach the rows the test still needs to assert on. We no-op ``close`` for
    the duration of the job, then the real session continues for assertions.
    """

    def factory():
        original_close = db.close
        db.close = lambda: None  # type: ignore[method-assign]

        # Restore the real close after the job is done with the session. The job
        # only calls close() once (in finally), so wrap it to self-restore.
        def restoring_close():
            db.close = original_close  # type: ignore[method-assign]

        db.close = restoring_close  # type: ignore[method-assign]
        return db

    return factory


def _mock_proxybox():
    instance = MagicMock()
    instance.print_and_wait = AsyncMock(
        return_value={"job_id": "j1", "status": "done", "terminal": True, "succeeded": True, "raw": {}}
    )
    constructor = MagicMock(return_value=instance)
    return constructor, instance


def _run_job(db: Session, *, company_id: int, receipt_id: int, user_id: int) -> dict:
    with patch(SESSION_PATH, _non_closing_factory(db)):
        return asyncio.run(print_receiving_label_task(company_id=company_id, receipt_id=receipt_id, user_id=user_id))


# ===========================================================================
# No-op gates -- the job makes NO ProxyBox call unless every gate passes.
# ===========================================================================


def test_job_noop_when_no_profile(db_session: Session):
    receipt = make_receipt(db_session, company_id=1)  # no profile

    constructor, instance = _mock_proxybox()
    with patch(PROXYBOX_CLIENT_PATH, constructor):
        result = _run_job(db_session, company_id=1, receipt_id=receipt.id, user_id=receipt._receiver_id)

    assert result["printed"] is False
    assert result["reason"] == "no_profile"
    constructor.assert_not_called()
    instance.print_and_wait.assert_not_awaited()


def test_job_noop_when_profile_inactive(db_session: Session):
    receipt = make_receipt(db_session, company_id=1)
    make_profile(db_session, company_id=1, auto_print=True, allow_egress=True, is_active=False)

    constructor, instance = _mock_proxybox()
    with patch(PROXYBOX_CLIENT_PATH, constructor):
        result = _run_job(db_session, company_id=1, receipt_id=receipt.id, user_id=receipt._receiver_id)

    assert result["printed"] is False
    assert result["reason"] == "no_profile"
    constructor.assert_not_called()


def test_job_noop_when_auto_print_off(db_session: Session):
    receipt = make_receipt(db_session, company_id=1)
    make_profile(db_session, company_id=1, auto_print=False, allow_egress=True)

    constructor, instance = _mock_proxybox()
    with patch(PROXYBOX_CLIENT_PATH, constructor):
        result = _run_job(db_session, company_id=1, receipt_id=receipt.id, user_id=receipt._receiver_id)

    assert result["printed"] is False
    assert result["reason"] == "auto_print_off"
    constructor.assert_not_called()
    instance.print_and_wait.assert_not_awaited()


def test_job_noop_when_egress_off(db_session: Session):
    receipt = make_receipt(db_session, company_id=1)
    make_profile(db_session, company_id=1, auto_print=True, allow_egress=False)

    constructor, instance = _mock_proxybox()
    with patch(PROXYBOX_CLIENT_PATH, constructor):
        result = _run_job(db_session, company_id=1, receipt_id=receipt.id, user_id=receipt._receiver_id)

    assert result["printed"] is False
    assert result["reason"] == "egress_off"
    constructor.assert_not_called()
    instance.print_and_wait.assert_not_awaited()


# ===========================================================================
# Happy path -- all gates pass -> prints (ProxyBox mocked) + Document persisted.
# ===========================================================================


def test_job_prints_when_all_gates_pass(db_session: Session):
    receipt = make_receipt(db_session, company_id=1)
    make_profile(db_session, company_id=1, auto_print=True, allow_egress=True, api_key="PBX_JOB_SECRET_5151")

    constructor, instance = _mock_proxybox()
    with patch(PROXYBOX_CLIENT_PATH, constructor):
        result = _run_job(db_session, company_id=1, receipt_id=receipt.id, user_id=receipt._receiver_id)

    assert result["printed"] is True
    assert result["label_document_id"] is not None

    # ProxyBox constructed with the decrypted key + configured target; print awaited.
    constructor.assert_called_once()
    assert constructor.call_args.kwargs["api_key"] == "PBX_JOB_SECRET_5151"
    assert constructor.call_args.kwargs["target"] == "usb_sn_JOBPRINTER"
    instance.print_and_wait.assert_awaited_once()

    # A RECEIVING_LABEL Document was persisted for this tenant and linked onto the receipt.
    doc = db_session.query(Document).filter(Document.id == result["label_document_id"]).one()
    assert doc.company_id == 1
    assert doc.document_type == DocumentType.RECEIVING_LABEL
    db_session.refresh(receipt)
    assert receipt.label_document_id == doc.id


# ===========================================================================
# Failure handling -- printer/tunnel error is swallowed (job never raises).
# ===========================================================================


def test_job_swallows_proxybox_error_and_emits_failed_event(db_session: Session):
    receipt = make_receipt(db_session, company_id=1)
    make_profile(db_session, company_id=1, auto_print=True, allow_egress=True)

    instance = MagicMock()
    instance.print_and_wait = AsyncMock(side_effect=ProxyBoxError("tunnel down"))
    constructor = MagicMock(return_value=instance)

    with patch(PROXYBOX_CLIENT_PATH, constructor):
        # Must NOT raise -- a printer outage cannot crash the worker.
        result = _run_job(db_session, company_id=1, receipt_id=receipt.id, user_id=receipt._receiver_id)

    assert result["printed"] is False
    assert result["reason"] == "print_failed"
    instance.print_and_wait.assert_awaited_once()

    # The failure is surfaced as a (secret-free) operational event for this tenant.
    events = (
        db_session.query(OperationalEvent)
        .filter(
            OperationalEvent.company_id == 1,
            OperationalEvent.event_type == "receiving_label_print_failed",
            OperationalEvent.entity_id == receipt.id,
        )
        .all()
    )
    assert len(events) == 1
    assert events[0].severity == "warning"
    import json as _json

    assert "PBX_JOB_SECRET" not in _json.dumps(events[0].event_payload)

    # Record retention: the label Document was committed before the (failed) print POST.
    db_session.refresh(receipt)
    assert receipt.label_document_id is not None


def test_job_noop_for_missing_receipt(db_session: Session):
    """An enqueued receipt that no longer exists -> graceful no-op, no raise."""
    make_profile(db_session, company_id=1, auto_print=True, allow_egress=True)

    constructor, instance = _mock_proxybox()
    with patch(PROXYBOX_CLIENT_PATH, constructor):
        result = _run_job(db_session, company_id=1, receipt_id=999999, user_id=1)

    assert result["printed"] is False
    assert result["reason"] == "receipt_not_found"
    instance.print_and_wait.assert_not_awaited()
