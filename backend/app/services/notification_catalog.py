"""Notification event catalog — the single registry of every notifiable event.

This is the source of truth described in ``docs/NOTIFICATIONS_PLAN.md`` §3.1/§4 and
``PR1_DESIGN_SPEC.md`` §A. One entry per ``event_key`` (dot notation, e.g.
``"wo.blocker_created"``) defines the label/description, category, severity, default
channels, the mandatory (forced-on) channel, SMS eligibility, the re-notify
(``recurring``) policy, the recipient spec, and the emitted ``OperationalEvent.event_type``
strings that map to it (``source_event_types``).

``SOURCE_EVENT_TYPE_TO_KEY`` is the reverse index the transactional-outbox tee uses to
decide whether a committed ``OperationalEvent`` should drive notifications. Emitted event
types with no catalog entry are deliberately ignored (visible future decisions, not silent
drops).

In PR 1 only the entries whose ``source_event_types`` are actually emitted today — or that
are wired via a repointed cron / direct dispatch — actually fire; the remaining entries are
dormant catalog rows so the settings matrix (PR 3) and later PRs already have them.

The recipient resolvers here are pure and MUST be tenant-scoped: every query filters by the
triggering event's ``company_id`` and ``User.is_active == True`` (compliance invariant §8).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from app.models.user import UserRole

# ---------------------------------------------------------------------------
# Channels & categories
# ---------------------------------------------------------------------------

CHANNEL_IN_APP = "in_app"
CHANNEL_EMAIL = "email"
CHANNEL_SMS = "sms"
CHANNEL_DIGEST = "digest"
ALL_CHANNELS = frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL, CHANNEL_SMS, CHANNEL_DIGEST})


class Category:
    PRODUCTION = "Production"
    QUALITY = "Quality"
    PURCHASING = "Purchasing & Inventory"
    SALES = "Sales"
    SHIPPING = "Shipping"
    ENGINEERING = "Engineering"
    MAINTENANCE = "Maintenance"
    SYSTEM = "System"


# Resolver signature: (db: Session, event: OperationalEvent, company_id: int) -> list[User]
RecipientResolver = Callable[..., List]


@dataclass(frozen=True)
class CatalogEntry:
    """One notifiable event type. Frozen: entries are static configuration."""

    event_key: str
    label: str
    description: str
    category: str
    severity: str  # "info" | "warning" | "critical"
    default_channels: frozenset = frozenset()
    mandatory_channel: Optional[str] = None
    sms_eligible: bool = False
    recurring: bool = False
    source_event_types: Tuple[str, ...] = ()
    # Declarative recipient spec (used by the outbox ``dispatch_for_event`` path).
    roles: Tuple[UserRole, ...] = ()
    departments: Tuple[str, ...] = ()
    resolver: Optional[RecipientResolver] = None


# ---------------------------------------------------------------------------
# Recipient resolvers (entity-derived, payload-conditional). Tenant-scoped.
# ---------------------------------------------------------------------------


def _active_user(db, user_id: Optional[int], company_id: int):
    if user_id is None:
        return None
    from app.models.user import User

    return db.query(User).filter(User.id == user_id, User.company_id == company_id, User.is_active.is_(True)).first()


def _users_by_department(db, departments: Tuple[str, ...], company_id: int) -> List:
    if not departments:
        return []
    from app.models.user import User

    return (
        db.query(User)
        .filter(
            User.company_id == company_id,
            User.is_active.is_(True),
            User.department.in_(list(departments)),
        )
        .all()
    )


def resolve_wo_creator(db, event, company_id: int) -> List:
    """The work order's creator (tenant-scoped, active)."""
    if not event.work_order_id:
        return []
    from app.models.work_order import WorkOrder

    wo = db.query(WorkOrder).filter(WorkOrder.id == event.work_order_id, WorkOrder.company_id == company_id).first()
    if wo is None:
        return []
    user = _active_user(db, getattr(wo, "created_by", None), company_id)
    return [user] if user else []


def resolve_blocker_created(db, event, company_id: int) -> List:
    """Material-shortage blockers additionally reach Purchasing / Inventory.

    The base roles (supervisors, managers) come from ``entry.roles``; this resolver
    only adds the by-blocker-type departments. ``operation_hold`` carries no
    ``category`` in its payload, so it never adds the material departments.
    """
    payload = event.event_payload or {}
    category = payload.get("category")
    if category == "material_missing":
        return _users_by_department(db, ("Purchasing", "Inventory"), company_id)
    return []


def resolve_po_creator(db, event, company_id: int) -> List:
    """The purchase order's creator (tenant-scoped, active)."""
    payload = event.event_payload or {}
    po_id = payload.get("po_id")
    if not po_id:
        return []
    from app.models.purchasing import PurchaseOrder

    po = db.query(PurchaseOrder).filter(PurchaseOrder.id == po_id, PurchaseOrder.company_id == company_id).first()
    if po is None:
        return []
    user = _active_user(db, getattr(po, "created_by", None), company_id)
    return [user] if user else []


# ---------------------------------------------------------------------------
# Transition gates (some emits fire on a broad action; only notify on the
# specific transition the catalog event means). Return True to FIRE.
# ---------------------------------------------------------------------------


def gate_blocker_resolved(event) -> bool:
    payload = event.event_payload or {}
    return str(payload.get("status", "")).lower() == "resolved"


def gate_ncr_closed(event) -> bool:
    payload = event.event_payload or {}
    return str(payload.get("status", "")).lower() == "closed"


def gate_fai_completed(event) -> bool:
    payload = event.event_payload or {}
    return str(payload.get("status", "")).lower() in {"passed", "failed", "conditional"}


def gate_inspection_failed(event) -> bool:
    payload = event.event_payload or {}
    try:
        return float(payload.get("quantity_rejected", 0) or 0) > 0
    except (TypeError, ValueError):
        return False


# Map event_key -> gate callable. Absent => always fire.
TRANSITION_GATES: Dict[str, Callable[..., bool]] = {
    "wo.blocker_resolved": gate_blocker_resolved,
    "ncr.closed": gate_ncr_closed,
    "fai.completed": gate_fai_completed,
    "inspection.failed": gate_inspection_failed,
}


# ---------------------------------------------------------------------------
# The v1 catalog
# ---------------------------------------------------------------------------

_ENTRIES: List[CatalogEntry] = [
    # ---------------- Production ----------------
    CatalogEntry(
        event_key="wo.blocker_created",
        label="Work order blocked / on hold",
        description="A work order or operation was placed on hold or blocked.",
        category=Category.PRODUCTION,
        severity="critical",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        mandatory_channel=CHANNEL_IN_APP,
        sms_eligible=True,
        source_event_types=("work_order_blocker_created", "operation_hold"),
        roles=(UserRole.MANAGER, UserRole.SUPERVISOR),
        resolver=resolve_blocker_created,
    ),
    CatalogEntry(
        event_key="wo.blocker_escalated",
        label="Work order blocker escalated",
        description="An open blocker was escalated to management.",
        category=Category.PRODUCTION,
        severity="critical",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        sms_eligible=True,
        source_event_types=("work_order_blocker_escalated",),
        roles=(UserRole.MANAGER, UserRole.SUPERVISOR),
    ),
    CatalogEntry(
        event_key="wo.blocker_resolved",
        label="Work order blocker resolved",
        description="A blocker on a work order was resolved.",
        category=Category.PRODUCTION,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=("work_order_blocker_updated",),
        roles=(UserRole.MANAGER, UserRole.SUPERVISOR),
    ),
    CatalogEntry(
        event_key="wo.released",
        label="Work order released",
        description="A work order was released to the floor.",
        category=Category.PRODUCTION,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=("work_order_released",),
        roles=(UserRole.SUPERVISOR,),
    ),
    CatalogEntry(
        event_key="wo.started",
        label="Work order started",
        description="Production started on a work order.",
        category=Category.PRODUCTION,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=("work_order_started",),
    ),
    CatalogEntry(
        event_key="wo.completed",
        label="Work order completed",
        description="A work order reached completion.",
        category=Category.PRODUCTION,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        source_event_types=("work_order_completed",),
        roles=(UserRole.SUPERVISOR, UserRole.MANAGER),
        resolver=resolve_wo_creator,
    ),
    CatalogEntry(
        event_key="wo.closed",
        label="Work order closed",
        description="A work order was closed after shipment.",
        category=Category.PRODUCTION,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=("work_order_closed",),
        roles=(UserRole.MANAGER,),
    ),
    CatalogEntry(
        event_key="wo.late",
        label="Work order late",
        description="A released work order passed its due date.",
        category=Category.PRODUCTION,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        recurring=True,
        source_event_types=(),  # cron-driven via dispatch_direct
        roles=(UserRole.SUPERVISOR, UserRole.MANAGER),
    ),
    CatalogEntry(
        event_key="wo.deleted",
        label="Work order deleted",
        description="A work order was soft-deleted.",
        category=Category.PRODUCTION,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=(),  # PR 6 instrumentation
        roles=(UserRole.SUPERVISOR,),
    ),
    CatalogEntry(
        event_key="wo.priority_changed",
        label="Work order priority changed",
        description="A work order's priority was changed.",
        category=Category.PRODUCTION,
        severity="info",
        default_channels=frozenset(),  # off by default (available)
        source_event_types=("work_order_priority_updated",),
    ),
    CatalogEntry(
        event_key="op.completed",
        label="Operation completed",
        description="An operation was completed.",
        category=Category.PRODUCTION,
        severity="info",
        default_channels=frozenset(),  # off by default (available)
        source_event_types=("operation_completed",),
    ),
    CatalogEntry(
        event_key="op.ready",
        label="Operation ready",
        description="An operation became ready to run.",
        category=Category.PRODUCTION,
        severity="info",
        default_channels=frozenset(),  # off by default (available)
        source_event_types=("operation_ready",),
    ),
    CatalogEntry(
        event_key="scrap.recorded",
        label="Scrap recorded",
        description="Scrap was recorded against an operation.",
        category=Category.PRODUCTION,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=(),  # PR 6 instrumentation
        roles=(UserRole.QUALITY, UserRole.SUPERVISOR),
        departments=("Quality",),
    ),
    CatalogEntry(
        event_key="production.reduced",
        label="Production quantity reduced",
        description="An operator reduced a previously recorded production quantity.",
        category=Category.PRODUCTION,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=("operation_production_reduced",),
        roles=(UserRole.SUPERVISOR,),
    ),
    # ---------------- Quality ----------------
    CatalogEntry(
        event_key="ncr.created",
        label="NCR created",
        description="A non-conformance report was created.",
        category=Category.QUALITY,
        severity="critical",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        mandatory_channel=CHANNEL_IN_APP,
        sms_eligible=True,
        source_event_types=("ncr_created",),
        roles=(UserRole.QUALITY, UserRole.MANAGER),
        departments=("Quality",),
    ),
    CatalogEntry(
        event_key="ncr.closed",
        label="NCR closed",
        description="A non-conformance report was closed.",
        category=Category.QUALITY,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=("ncr_updated",),
        roles=(UserRole.QUALITY,),
        departments=("Quality",),
    ),
    CatalogEntry(
        event_key="ncr.voided",
        label="NCR voided",
        description="A non-conformance report was voided.",
        category=Category.QUALITY,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=(),  # PR 6 instrumentation
        roles=(UserRole.QUALITY, UserRole.MANAGER),
    ),
    CatalogEntry(
        event_key="quality.hold",
        label="Quality hold raised",
        description="A quality hold was raised on a step.",
        category=Category.QUALITY,
        severity="critical",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        mandatory_channel=CHANNEL_IN_APP,
        sms_eligible=True,
        # DORMANT (not wired) — deliberately, to avoid double-notifying the same users.
        # The sole quality-hold path (process_sheet_service.create_quality_hold) ALWAYS, in
        # one transaction, emits BOTH ``ncr_created`` (-> ncr.created, mandatory in-app to
        # Quality) AND ``work_order_blocker_created`` (-> wo.blocker_created, mandatory
        # in-app to supervisors/managers). Every recipient a quality.hold notification would
        # target (Quality via ncr.created; supervisors via wo.blocker_created) is therefore
        # already covered mandatorily by an event fired from the same action, so wiring this
        # would double-notify them. Revisit only if a quality-hold path appears that does NOT
        # also raise an NCR + blocker.
        source_event_types=(),
        roles=(UserRole.QUALITY, UserRole.SUPERVISOR),
        departments=("Quality",),
    ),
    CatalogEntry(
        event_key="inspection.failed",
        label="Incoming inspection failed",
        description="A receiving inspection rejected material.",
        category=Category.QUALITY,
        severity="critical",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        mandatory_channel=CHANNEL_IN_APP,
        sms_eligible=True,
        source_event_types=("purchase_receipt_inspected",),
        roles=(UserRole.QUALITY, UserRole.MANAGER),
        departments=("Quality", "Purchasing"),
    ),
    CatalogEntry(
        event_key="car.created",
        label="Corrective action created",
        description="A corrective action request was created.",
        category=Category.QUALITY,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        source_event_types=("car_created", "car_created_from_ncr"),
        roles=(UserRole.QUALITY,),
        departments=("Quality",),
    ),
    CatalogEntry(
        event_key="fai.created",
        label="First article inspection created",
        description="A first article inspection was created.",
        category=Category.QUALITY,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=("fai_created",),
        roles=(UserRole.QUALITY,),
        departments=("Quality",),
    ),
    CatalogEntry(
        event_key="fai.completed",
        label="First article inspection completed",
        description="A first article inspection was completed.",
        category=Category.QUALITY,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=("fai_updated",),
        roles=(UserRole.QUALITY,),
        departments=("Quality",),
    ),
    CatalogEntry(
        event_key="calibration.due",
        label="Calibration due",
        description="Equipment calibration is coming due.",
        category=Category.QUALITY,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_DIGEST}),
        recurring=True,
        source_event_types=(),  # cron-driven via dispatch_direct
        roles=(UserRole.QUALITY,),
        departments=("Quality",),
    ),
    CatalogEntry(
        event_key="cert.expiring",
        label="Certification expiring",
        description="An operator certification is expiring.",
        category=Category.QUALITY,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_DIGEST}),
        recurring=True,
        source_event_types=(),  # PR 6 instrumentation
        roles=(UserRole.SUPERVISOR, UserRole.QUALITY),
    ),
    CatalogEntry(
        event_key="cert.expired",
        label="Certification expired",
        description="An operator certification has expired.",
        category=Category.QUALITY,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_DIGEST}),
        source_event_types=(),  # PR 6 instrumentation
        roles=(UserRole.SUPERVISOR, UserRole.QUALITY),
    ),
    CatalogEntry(
        event_key="complaint.received",
        label="Customer complaint received",
        description="A customer complaint was received.",
        category=Category.QUALITY,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        source_event_types=(),  # PR 6 instrumentation
        roles=(UserRole.QUALITY, UserRole.MANAGER),
        departments=("Quality",),
    ),
    CatalogEntry(
        event_key="complaint.status_changed",
        label="Customer complaint updated",
        description="A customer complaint changed status.",
        category=Category.QUALITY,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        source_event_types=(),  # PR 6 instrumentation
        roles=(UserRole.QUALITY, UserRole.MANAGER),
        departments=("Quality",),
    ),
    CatalogEntry(
        event_key="rma.approved",
        label="RMA approved",
        description="A return material authorization was approved.",
        category=Category.QUALITY,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=(),  # PR 6 instrumentation
        roles=(UserRole.QUALITY,),
        departments=("Quality",),
    ),
    CatalogEntry(
        event_key="rma.received",
        label="RMA received",
        description="Returned material was received.",
        category=Category.QUALITY,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=(),  # PR 6 instrumentation
        roles=(UserRole.QUALITY,),
        departments=("Quality",),
    ),
    # ---------------- Purchasing & Inventory ----------------
    CatalogEntry(
        event_key="po.sent",
        label="Purchase order sent",
        description="A purchase order was sent to a vendor.",
        category=Category.PURCHASING,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=("purchase_order_sent",),
        departments=("Purchasing",),
        resolver=resolve_po_creator,
    ),
    CatalogEntry(
        event_key="po.deleted",
        label="Purchase order deleted",
        description="A purchase order was soft-deleted.",
        category=Category.PURCHASING,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=(),  # PR 6 instrumentation
        departments=("Purchasing",),
    ),
    CatalogEntry(
        event_key="receipt.created",
        label="Material received",
        description="Material was received against a purchase order.",
        category=Category.PURCHASING,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        source_event_types=("purchase_order_received",),
        departments=("Purchasing",),
        resolver=resolve_po_creator,
    ),
    CatalogEntry(
        event_key="receipt.voided",
        label="Receipt voided",
        description="A posted receipt was voided.",
        category=Category.PURCHASING,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=("receipt_voided",),
        roles=(UserRole.MANAGER, UserRole.QUALITY),
        departments=("Purchasing",),
    ),
    CatalogEntry(
        event_key="receipt.corrected",
        label="Receipt corrected",
        description="A posted receipt was corrected.",
        category=Category.PURCHASING,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=("receipt_corrected",),
        roles=(UserRole.MANAGER, UserRole.QUALITY),
        departments=("Purchasing",),
    ),
    CatalogEntry(
        event_key="vendor.deactivated",
        label="Vendor deactivated",
        description="A vendor was deactivated or deleted.",
        category=Category.PURCHASING,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=(),  # PR 6 instrumentation
        departments=("Purchasing", "Quality"),
    ),
    CatalogEntry(
        event_key="stock.low",
        label="Low stock",
        description="Inventory dropped below its reorder point.",
        category=Category.PURCHASING,
        severity="warning",
        default_channels=frozenset({CHANNEL_DIGEST}),
        recurring=True,
        source_event_types=(),  # cron-driven via dispatch_direct
        departments=("Purchasing", "Inventory"),
    ),
    CatalogEntry(
        event_key="mrp.expedite_required",
        label="Expedite required",
        description="MRP flagged a part that must be expedited.",
        category=Category.PURCHASING,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        source_event_types=(),  # mrp_auto via dispatch_direct
        departments=("Purchasing",),
    ),
    CatalogEntry(
        event_key="mrp.completed",
        label="MRP run completed",
        description="An MRP run finished and auto-processed actions.",
        category=Category.PURCHASING,
        severity="info",
        default_channels=frozenset({CHANNEL_EMAIL}),
        source_event_types=(),  # MRP jobs via dispatch_direct
        roles=(UserRole.MANAGER,),
    ),
    CatalogEntry(
        event_key="mrp.review_needed",
        label="MRP actions need review",
        description="An MRP run produced actions requiring review.",
        category=Category.PURCHASING,
        severity="info",
        default_channels=frozenset({CHANNEL_EMAIL}),
        source_event_types=(),  # MRP jobs via dispatch_direct
        roles=(UserRole.MANAGER,),
    ),
    CatalogEntry(
        event_key="capacity.overload",
        label="Capacity conflicts detected",
        description="Scheduling detected capacity conflicts.",
        category=Category.PURCHASING,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        source_event_types=(),  # scheduling jobs via dispatch_direct
        roles=(UserRole.MANAGER,),
    ),
    # ---------------- Sales / Shipping / Engineering / Maintenance / System ----------------
    CatalogEntry(
        event_key="quote.sent",
        label="Quote sent",
        description="A customer quote was sent.",
        category=Category.SALES,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=(),  # PR 6 instrumentation
        roles=(UserRole.MANAGER,),
        departments=("Sales",),
    ),
    CatalogEntry(
        event_key="quote.accepted",
        label="Quote accepted",
        description="A customer accepted a quote.",
        category=Category.SALES,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=(),  # PR 6 instrumentation
        roles=(UserRole.MANAGER,),
        departments=("Sales",),
    ),
    CatalogEntry(
        event_key="quote.expiring",
        label="Quote expiring",
        description="A sent quote is about to expire.",
        category=Category.SALES,
        severity="warning",
        default_channels=frozenset({CHANNEL_DIGEST}),
        recurring=True,
        source_event_types=(),  # cron-driven via dispatch_direct
        departments=("Sales",),
    ),
    CatalogEntry(
        event_key="shipment.shipped",
        label="Shipment shipped",
        description="A shipment was shipped.",
        category=Category.SHIPPING,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        source_event_types=("shipment_shipped",),
        roles=(UserRole.SHIPPING,),
        departments=("Sales", "Shipping"),
    ),
    CatalogEntry(
        event_key="shipment.delivery_exception",
        label="Delivery exception",
        description="A shipment hit a delivery exception.",
        category=Category.SHIPPING,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        source_event_types=(),  # PR 6 instrumentation
        roles=(UserRole.SHIPPING,),
        departments=("Sales", "Shipping"),
    ),
    CatalogEntry(
        event_key="coc.generation_failed",
        label="CoC generation failed",
        description="A certificate of conformance failed to generate.",
        category=Category.SHIPPING,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=("coc_generation_failed",),
        roles=(UserRole.SHIPPING, UserRole.QUALITY),
        departments=("Shipping", "Quality"),
    ),
    CatalogEntry(
        event_key="eco.submitted",
        label="ECO submitted",
        description="An engineering change order was submitted.",
        category=Category.ENGINEERING,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        source_event_types=(),  # PR 6 instrumentation
        roles=(UserRole.MANAGER,),
    ),
    CatalogEntry(
        event_key="eco.approved",
        label="ECO approved",
        description="An engineering change order was approved.",
        category=Category.ENGINEERING,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        source_event_types=(),  # PR 6 instrumentation
    ),
    CatalogEntry(
        event_key="eco.rejected",
        label="ECO rejected",
        description="An engineering change order was rejected.",
        category=Category.ENGINEERING,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        source_event_types=(),  # PR 6 instrumentation
    ),
    CatalogEntry(
        event_key="eco.implemented",
        label="ECO implemented",
        description="An engineering change order was implemented.",
        category=Category.ENGINEERING,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        source_event_types=(),  # PR 6 instrumentation
    ),
    CatalogEntry(
        event_key="maintenance.due",
        label="Maintenance due",
        description="Preventive maintenance is coming due.",
        category=Category.MAINTENANCE,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=(),  # PR 6 instrumentation
        roles=(UserRole.SUPERVISOR,),
        departments=("Maintenance",),
    ),
    CatalogEntry(
        event_key="maintenance.overdue",
        label="Maintenance overdue",
        description="Preventive maintenance is overdue.",
        category=Category.MAINTENANCE,
        severity="warning",
        default_channels=frozenset({CHANNEL_IN_APP}),
        recurring=True,
        source_event_types=(),  # PR 6 instrumentation
        roles=(UserRole.SUPERVISOR,),
        departments=("Maintenance",),
    ),
    CatalogEntry(
        event_key="downtime.started",
        label="Downtime started",
        description="A machine downtime event started.",
        category=Category.MAINTENANCE,
        severity="critical",
        default_channels=frozenset({CHANNEL_IN_APP}),
        sms_eligible=True,
        source_event_types=("downtime_started",),
        roles=(UserRole.SUPERVISOR, UserRole.MANAGER),
    ),
    CatalogEntry(
        event_key="downtime.resolved",
        label="Downtime resolved",
        description="A machine downtime event was resolved.",
        category=Category.MAINTENANCE,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=("downtime_resolved",),
        roles=(UserRole.SUPERVISOR, UserRole.MANAGER),
    ),
    CatalogEntry(
        event_key="comment.mention",
        label="You were mentioned",
        description="You were @mentioned in a comment.",
        category=Category.SYSTEM,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        mandatory_channel=CHANNEL_IN_APP,
        source_event_types=(),  # PR 5 comments
    ),
    CatalogEntry(
        event_key="comment.added",
        label="New comment",
        description="A comment was added to a record you watch.",
        category=Category.SYSTEM,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=(),  # PR 5 comments
    ),
    CatalogEntry(
        event_key="account.locked",
        label="Account locked",
        description="An account was locked after repeated failed logins.",
        category=Category.SYSTEM,
        severity="critical",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        mandatory_channel=CHANNEL_EMAIL,
        source_event_types=(),  # PR 6 instrumentation
        roles=(UserRole.ADMIN,),
    ),
    CatalogEntry(
        event_key="import.completed",
        label="Import completed",
        description="A data import finished.",
        category=Category.SYSTEM,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=(),  # PR 6 instrumentation
    ),
    CatalogEntry(
        event_key="import.failed",
        label="Import failed",
        description="A data import failed.",
        category=Category.SYSTEM,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP}),
        source_event_types=(),  # PR 6 instrumentation
    ),
    CatalogEntry(
        event_key="visitor.check_in",
        label="Visitor checked in",
        description="A visitor checked in and named you as host.",
        category=Category.SYSTEM,
        severity="info",
        default_channels=frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL}),
        source_event_types=(),  # not outbox-driven: dispatched directly via dispatch_notification_direct_job
        #                          from visitor_log_service._notify_host_best_effort (host gets in-app + CUI-safe email)
    ),
]


# ---------------------------------------------------------------------------
# Registry + reverse index
# ---------------------------------------------------------------------------

CATALOG: Dict[str, CatalogEntry] = {entry.event_key: entry for entry in _ENTRIES}

# Reverse index: emitted OperationalEvent.event_type -> event_key. Built from every
# entry's source_event_types. A source event type must map to exactly one key.
SOURCE_EVENT_TYPE_TO_KEY: Dict[str, str] = {}
for _entry in _ENTRIES:
    for _source in _entry.source_event_types:
        if _source in SOURCE_EVENT_TYPE_TO_KEY:  # pragma: no cover - config guard
            raise RuntimeError(
                f"Duplicate source_event_type mapping: {_source!r} -> "
                f"{SOURCE_EVENT_TYPE_TO_KEY[_source]!r} and {_entry.event_key!r}"
            )
        SOURCE_EVENT_TYPE_TO_KEY[_source] = _entry.event_key


def get_entry(event_key: str) -> Optional[CatalogEntry]:
    return CATALOG.get(event_key)


def entry_for_event_type(event_type: str) -> Optional[CatalogEntry]:
    key = SOURCE_EVENT_TYPE_TO_KEY.get(event_type)
    return CATALOG.get(key) if key else None


def should_fire(entry: CatalogEntry, event) -> bool:
    """Apply the transition gate for an outbox event. True => fan out."""
    gate = TRANSITION_GATES.get(entry.event_key)
    if gate is None:
        return True
    try:
        return bool(gate(event))
    except Exception:  # pragma: no cover - a bad payload must not fan out
        return False
