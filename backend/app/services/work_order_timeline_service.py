"""Bounded, read-only job history. No audit payloads, rates, costs or auth context.

Each source query takes at most page_size + 1 rows using the same global cursor.
The merge therefore remains bounded even when one job has years of history.
Supplemental quality telemetry is labelled separately and does not fill holes
in authoritative history. Actor lookup is tenant-scoped, including historically mis-parented FKs.
"""

import base64
import json
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import and_, literal, or_, select

from app.db.ledger_filter import work_order_ledger_filter
from app.db.tenant_filter import tenant_query
from app.models.audit_log import AuditLog
from app.models.inventory import InventoryTransaction
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.process_sheet import OperationStepRecord
from app.models.production_receipt import ProductionReceipt
from app.models.quality import FirstArticleInspection, NonConformanceReport
from app.models.time_entry import TimeEntry
from app.models.user import User
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.models.work_order_blocker import WorkOrderBlocker


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _value(value):
    return value.value if hasattr(value, "value") else value


def list_work_order_timeline(
    db, company_id, work_order_id, *, category=None, actor_id=None, start_at=None, end_at=None, cursor=None, limit=30
):
    wo = (
        tenant_query(db, WorkOrder, company_id)
        .filter(WorkOrder.id == work_order_id, WorkOrder.is_deleted.is_(False))
        .first()
    )
    if wo is None:
        raise HTTPException(404, "Work order not found")
    if actor_id is not None and tenant_query(db, User, company_id).filter(User.id == actor_id).first() is None:
        return {"items": [], "next_cursor": None}
    after = None
    if cursor:
        try:
            after = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            after = (_utc(datetime.fromisoformat(after[0])), str(after[1]), int(after[2]))
        except (ValueError, TypeError, IndexError, KeyError, UnicodeError, OverflowError):
            raise HTTPException(422, "Invalid timeline cursor")
    if start_at and end_at and _utc(start_at) > _utc(end_at):
        raise HTTPException(422, "Start must precede end")
    op_ids = select(WorkOrderOperation.id).where(
        WorkOrderOperation.company_id == company_id, WorkOrderOperation.work_order_id == wo.id
    )
    time_ids = select(TimeEntry.id).where(TimeEntry.company_id == company_id, TimeEntry.work_order_id == wo.id)
    ncr_ids = select(NonConformanceReport.id).where(
        NonConformanceReport.company_id == company_id,
        NonConformanceReport.work_order_id == wo.id,
        NonConformanceReport.is_deleted.is_(False),
    )
    audit_ncr_ids = select(NonConformanceReport.id).where(
        NonConformanceReport.company_id == company_id, NonConformanceReport.work_order_id == wo.id
    )
    fai_ids = select(FirstArticleInspection.id).where(
        FirstArticleInspection.company_id == company_id, FirstArticleInspection.work_order_id == wo.id
    )
    blocker_ids = select(WorkOrderBlocker.id).where(
        WorkOrderBlocker.company_id == company_id, WorkOrderBlocker.work_order_id == wo.id
    )
    url = f"/work-orders/{wo.id}"
    entries = []

    def add(model, source, kind, at, actor, filters, render, evidence="business_record"):
        if category and category != kind:
            return
        query = tenant_query(db, model, company_id).filter(at.isnot(None), *filters)
        if actor_id is not None:
            query = query.filter(actor == actor_id)

        def bound(value):
            utc = _utc(value)
            return utc if getattr(at.type, "timezone", False) else utc.replace(tzinfo=None)

        if start_at:
            query = query.filter(at >= bound(start_at))
        if end_at:
            query = query.filter(at <= bound(end_at))
        if after:
            stamp, prefix, row_id = after
            query = query.filter(
                or_(
                    at < bound(stamp),
                    and_(
                        at == bound(stamp),
                        or_(literal(source) < prefix, and_(literal(source) == prefix, model.id < row_id)),
                    ),
                )
            )
        for row in query.order_by(at.desc(), model.id.desc()).limit(limit + 1).all():
            title, detail, source_label, source_url = render(row)
            entries.append(
                {
                    "id": f"{source}:{row.id}",
                    "occurred_at": _utc(getattr(row, at.key)),
                    "category": kind,
                    "evidence": evidence,
                    "title": title,
                    "detail": detail,
                    "actor_id": getattr(row, actor.key) if hasattr(actor, "class_") else None,
                    "actor_name": None,
                    "source_label": source_label,
                    "source_url": source_url,
                    "_source": source,
                    "_row_id": row.id,
                    "_part_id": row.part_id if model is InventoryTransaction else None,
                }
            )

    add(
        WorkOrder,
        "job-created",
        "job",
        WorkOrder.created_at,
        WorkOrder.created_by,
        [WorkOrder.id == wo.id],
        lambda row: ("Work order created", row.work_order_number, "Work order", url),
    )
    add(
        WorkOrderOperation,
        "operation-start",
        "production",
        WorkOrderOperation.actual_start,
        literal(None),
        [WorkOrderOperation.work_order_id == wo.id],
        lambda row: (
            "Operation started",
            f"{row.operation_number or row.sequence} · {row.name}",
            "Operation",
            url + f"#operation-{row.id}",
        ),
    )
    add(
        WorkOrderOperation,
        "operation-end",
        "production",
        WorkOrderOperation.actual_end,
        literal(None),
        [WorkOrderOperation.work_order_id == wo.id],
        lambda row: (
            "Operation finished",
            f"{row.operation_number or row.sequence} · {row.name}",
            "Operation",
            url + f"#operation-{row.id}",
        ),
    )
    add(
        TimeEntry,
        "clock-in",
        "labor",
        TimeEntry.clock_in,
        TimeEntry.user_id,
        [TimeEntry.work_order_id == wo.id],
        lambda row: ("Clocked in", f"{_value(row.entry_type)} session", "Labor record", url + "#job-timeline"),
    )
    add(
        TimeEntry,
        "clock-out",
        "labor",
        TimeEntry.clock_out,
        TimeEntry.user_id,
        [TimeEntry.work_order_id == wo.id],
        lambda row: (
            "Clocked out",
            (
                f"Recorded session: {row.duration_hours:g} hours"
                if row.duration_hours is not None
                else "Duration not recorded"
            ),
            "Labor record",
            url + "#job-timeline",
        ),
    )
    add(
        ProductionReceipt,
        "production-receipt",
        "production",
        ProductionReceipt.created_at,
        ProductionReceipt.operator_id,
        [ProductionReceipt.operation_id.in_(op_ids)],
        lambda row: (
            "Production report accepted",
            "Recorded totals: "
            + ", ".join(
                f"{label} {row.response.get('operation', {}).get(key, 'unknown')}"
                for key, label in [("quantity_complete", "good"), ("quantity_scrapped", "scrap")]
            ),
            "Production receipt",
            url + f"#operation-{row.operation_id}",
        ),
    )
    add(
        InventoryTransaction,
        "material",
        "material",
        InventoryTransaction.created_at,
        InventoryTransaction.created_by,
        [work_order_ledger_filter(wo.id, company_id)],
        lambda row: (
            f"Material {_value(row.transaction_type)}",
            f"{row.quantity:+g} · lot {row.lot_number or 'not recorded'}",
            "Stock movement",
            f"/warehouse?tab=inventory&inventory_tab=movements&work_order_id={wo.id}",
        ),
    )
    add(
        WorkOrderBlocker,
        "blocker-open",
        "blocker",
        WorkOrderBlocker.reported_at,
        WorkOrderBlocker.reported_by,
        [WorkOrderBlocker.work_order_id == wo.id],
        lambda row: ("Blocker reported", row.title, "Blocker", url + "#job-blockers"),
    )
    add(
        WorkOrderBlocker,
        "blocker-resolved",
        "blocker",
        WorkOrderBlocker.resolved_at,
        WorkOrderBlocker.resolved_by,
        [WorkOrderBlocker.work_order_id == wo.id],
        lambda row: ("Blocker resolved", row.title, "Blocker", url + "#job-blockers"),
    )
    add(
        NonConformanceReport,
        "ncr-created",
        "quality",
        NonConformanceReport.created_at,
        NonConformanceReport.detected_by,
        [NonConformanceReport.work_order_id == wo.id, NonConformanceReport.is_deleted.is_(False)],
        lambda row: ("Nonconformance recorded", row.ncr_number, "NCR", f"/quality?ncr={row.id}"),
    )
    add(
        FirstArticleInspection,
        "fai-created",
        "quality",
        FirstArticleInspection.created_at,
        literal(None),
        [FirstArticleInspection.work_order_id == wo.id],
        lambda row: ("First article inspection created", row.fai_number, "FAI", f"/quality?tab=fai&fai={row.id}"),
    )

    add(
        OperationStepRecord,
        "step-record",
        "quality",
        OperationStepRecord.recorded_at,
        OperationStepRecord.recorded_by,
        [OperationStepRecord.work_order_operation_id.in_(op_ids)],
        lambda row: (
            "Process-step evidence recorded",
            (
                "Superseded by a correction"
                if row.superseded_by_id
                else (
                    "Conforming measurement"
                    if row.is_conforming is True
                    else "Nonconforming measurement" if row.is_conforming is False else "Recorded step evidence"
                )
            ),
            "Operation evidence",
            url + f"#operation-{row.work_order_operation_id}",
        ),
    )
    # Some older quality edits emit only operational events. They are useful
    # context, explicitly supplemental, and never substituted for audit evidence.
    add(
        OperationalEvent,
        "quality-event",
        "quality",
        OperationalEvent.occurred_at,
        OperationalEvent.user_id,
        [
            OperationalEvent.work_order_id == wo.id,
            OperationalEvent.source_module == "quality",
            OperationalEvent.event_type.in_(["ncr_updated", "fai_updated"]),
            or_(
                and_(OperationalEvent.entity_type == "ncr", OperationalEvent.entity_id.in_(ncr_ids)),
                and_(OperationalEvent.entity_type == "fai", OperationalEvent.entity_id.in_(fai_ids)),
            ),
        ],
        lambda row: (
            "Quality update reported",
            "Supplemental event; inspect the quality record for its current state.",
            "Quality record",
            f"/quality?{row.entity_type}={row.entity_id}",
        ),
        evidence="telemetry",
    )

    def render_audit(row):
        # Free-form descriptions/JSON may contain customer prices, payroll, IPs,
        # or private notes. Expose only the action and reviewed public field names.
        allowed = {
            "status",
            "scheduled_start",
            "scheduled_end",
            "due_date",
            "priority",
            "quantity_complete",
            "quantity_scrapped",
            "quantity_reworked",
        }
        changed = (
            sorted(allowed.intersection((row.new_values or {}).keys())) if isinstance(row.new_values, dict) else []
        )
        return (
            row.action.replace("_", " ").capitalize(),
            (
                "Updated " + ", ".join(key.replace("_", " ") for key in changed)
                if changed
                else "Recorded action on this job or its related record"
            ),
            f"Audit #{row.id}",
            url + f"#event-audit:{row.id}",
        )

    add(
        AuditLog,
        "audit",
        "audit",
        AuditLog.timestamp,
        AuditLog.user_id,
        [
            AuditLog.success == "true",
            or_(
                and_(AuditLog.resource_type == "work_order", AuditLog.resource_id == wo.id),
                and_(AuditLog.resource_type == "work_order_operation", AuditLog.resource_id.in_(op_ids)),
                and_(AuditLog.resource_type == "time_entry", AuditLog.resource_id.in_(time_ids)),
                and_(AuditLog.resource_type == "ncr", AuditLog.resource_id.in_(audit_ncr_ids)),
                and_(AuditLog.resource_type == "fai", AuditLog.resource_id.in_(fai_ids)),
                and_(AuditLog.resource_type == "work_order_blocker", AuditLog.resource_id.in_(blocker_ids)),
            ),
        ],
        render_audit,
        evidence="audit",
    )
    entries.sort(key=lambda row: (row["occurred_at"], row["_source"], row["_row_id"]), reverse=True)
    has_next = len(entries) > limit
    entries = entries[:limit]
    actor_ids = {row["actor_id"] for row in entries if row["actor_id"]}
    part_ids = {row["_part_id"] for row in entries if row["_part_id"]}
    parts = {row.id: row.part_number for row in tenant_query(db, Part, company_id).filter(Part.id.in_(part_ids)).all()}
    actors = {row.id: row.full_name for row in tenant_query(db, User, company_id).filter(User.id.in_(actor_ids)).all()}
    next_cursor = None
    if has_next:
        last = entries[-1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps([last["occurred_at"].isoformat(), last["_source"], last["_row_id"]]).encode()
        ).decode()
    for row in entries:
        row["actor_name"] = actors.get(row["actor_id"])
        if row["actor_name"] is None:
            row["actor_id"] = None
        if row["_part_id"] is not None:
            row["detail"] = "{} · {}".format(parts.get(row["_part_id"], "Unavailable material"), row["detail"])
        row.pop("_part_id")
        row.pop("_source")
        row.pop("_row_id")
    return {"items": entries, "next_cursor": next_cursor}
