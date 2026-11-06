"""Shared company-scoped NCR and CAR numbering.

Every NCR/CAR writer allocates its number here (the quality router, receiving, the
shop-floor quality hold, process-sheet holds, the AI action applier and the
customer-complaint escalation verbs), so each rule has exactly one copy. ``lock`` is
injectable only so a router can hand in its own module-level ``acquire_generator_lock``
reference, which tests spy on; every caller takes the same transaction-scoped
advisory lock per (namespace, company).
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.locks import acquire_generator_lock
from app.models.quality import CorrectiveActionRequest, NonConformanceReport


def generate_ncr_number(db: Session, company_id: int = None, *, lock=acquire_generator_lock) -> str:
    lock(db, "ncr_number", company_id)
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"NCR-{today}-"
    # Deliberately NO is_deleted filter: a voided NCR keeps its number for good, and
    # uq_ncrs_company_ncr_number has no partial predicate, so the scan must see
    # tombstones or it would reissue a voided number and 500 on the constraint.
    query = db.query(NonConformanceReport).filter(NonConformanceReport.ncr_number.like(f"{prefix}%"))
    if company_id is not None:
        query = query.filter(NonConformanceReport.company_id == company_id)
    last = query.order_by(NonConformanceReport.ncr_number.desc()).first()

    if last:
        num = int(last.ncr_number.split("-")[-1]) + 1
    else:
        num = 1
    return f"{prefix}{num:03d}"


def generate_car_number(db: Session, company_id: int = None, *, lock=acquire_generator_lock) -> str:
    lock(db, "car_number", company_id)
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"CAR-{today}-"
    query = db.query(CorrectiveActionRequest).filter(CorrectiveActionRequest.car_number.like(f"{prefix}%"))
    if company_id is not None:
        query = query.filter(CorrectiveActionRequest.company_id == company_id)
    last = query.order_by(CorrectiveActionRequest.car_number.desc()).first()

    if last:
        num = int(last.car_number.split("-")[-1]) + 1
    else:
        num = 1
    return f"{prefix}{num:03d}"
