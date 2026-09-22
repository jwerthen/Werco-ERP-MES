"""Shared company-scoped NCR numbering."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.locks import acquire_generator_lock
from app.models.quality import NonConformanceReport


def generate_ncr_number(db: Session, company_id: int = None, *, lock=acquire_generator_lock) -> str:
    lock(db, "ncr_number", company_id)
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"NCR-{today}-"
    query = db.query(NonConformanceReport).filter(NonConformanceReport.ncr_number.like(f"{prefix}%"))
    if company_id is not None:
        query = query.filter(NonConformanceReport.company_id == company_id)
    last = query.order_by(NonConformanceReport.ncr_number.desc()).first()

    if last:
        num = int(last.ncr_number.split("-")[-1]) + 1
    else:
        num = 1
    return f"{prefix}{num:03d}"
