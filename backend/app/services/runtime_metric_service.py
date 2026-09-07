"""Bounded first-party measurement storage and SQL percentile aggregation."""

from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.db.locks import acquire_generator_lock
from app.models.runtime_metric import RuntimeMetricSample as Sample
from app.models.runtime_metric import RuntimeMetricSetting
from app.schemas.runtime_metric import RuntimeMetricBatch

RETENTION_DAYS = 30
DAILY_SAMPLE_LIMIT = 5000
GOOD = {"LCP": 2500, "INP": 200, "CLS": 0.1}


def collection_enabled(db: Session, company_id: int) -> bool:
    setting = db.query(RuntimeMetricSetting).filter_by(company_id=company_id).first()
    return setting is None or bool(setting.enabled)


def prune_runtime_metrics(db: Session, company_id=None, now=None) -> int:
    query = db.query(Sample).filter(Sample.created_at < (now or datetime.utcnow()) - timedelta(days=RETENTION_DAYS))
    if company_id is not None:
        query = query.filter(Sample.company_id == company_id)
    return query.delete(synchronize_session=False)


def record_runtime_metrics(db: Session, company_id: int, batch: RuntimeMetricBatch):
    # Across replicas, serialize the quota, receipt updates and the admin off switch.
    acquire_generator_lock(db, "runtime_metrics", company_id)
    if not collection_enabled(db, company_id):
        return {"accepted": 0, "enabled": False}
    now = datetime.utcnow()
    prune_runtime_metrics(db, company_id, now)
    remaining = (
        DAILY_SAMPLE_LIMIT
        - db.query(Sample)
        .filter(
            Sample.company_id == company_id,
            Sample.created_at >= now.replace(hour=0, minute=0, second=0, microsecond=0),
        )
        .count()
    )
    accepted = 0
    for sample in batch.samples:
        row = db.query(Sample).filter_by(company_id=company_id, metric_id=sample.metric_id).first()
        if row is not None:
            identity = ("name", "route", "device", "navigation", "release")
            if any(getattr(row, field) != getattr(sample, field) for field in identity):
                raise HTTPException(409, "Metric identity changed")
            if sample.sequence > row.sequence:
                row.value, row.sequence = sample.value, sample.sequence
        elif remaining > 0:
            db.add(Sample(company_id=company_id, created_at=now, **sample.model_dump()))
            db.flush()
            remaining -= 1
        else:
            continue
        accepted += 1
    return {"accepted": accepted, "enabled": True}


def summarize_runtime_metrics(
    db: Session, company_id: int, days: int, device=None, release=None, route=None, *, limit=201, offset=0
):
    dimensions = [Sample.route, Sample.device, Sample.name, Sample.navigation, Sample.release]
    threshold = case(GOOD, value=Sample.name, else_=0)
    ranked = db.query(
        *dimensions,
        Sample.value,
        func.row_number().over(partition_by=dimensions, order_by=(Sample.value, Sample.id)).label("rank"),
        func.count().over(partition_by=dimensions).label("samples"),
        func.sum(case((Sample.value <= threshold, 1), else_=0)).over(partition_by=dimensions).label("good"),
    ).filter(
        Sample.company_id == company_id,
        Sample.created_at >= datetime.utcnow() - timedelta(days=days),
    )
    for column, value in [(Sample.device, device), (Sample.release, release), (Sample.route, route)]:
        if value:
            ranked = ranked.filter(column == value)
    subquery = ranked.subquery()
    # Nearest-rank p75, performed inside the database (no loading every event into Python).
    rows = (
        db.query(subquery)
        .filter(subquery.c.rank == func.ceil(subquery.c.samples * 0.75))
        .order_by(subquery.c.route, subquery.c.device, subquery.c.name, subquery.c.release, subquery.c.navigation)
        .limit(limit)
        .offset(offset)
        .all()
    )
    return [
        {
            "route": row.route,
            "device": row.device,
            "name": row.name,
            "navigation": row.navigation,
            "release": row.release,
            "samples": row.samples,
            "p75": row.value,
            "good_percent": round(100 * row.good / row.samples, 1),
        }
        for row in rows
    ]
