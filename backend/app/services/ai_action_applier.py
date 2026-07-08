"""Apply allowlisted ERP actions from AI recommendations (human accept or auto-execute).

Only allowlisted action types may mutate ERP records. Mutations go through existing
domain services / models + ``AuditService`` when available.

Controlled-record soft gates are **not** enforced here — auto-execute is allowed
for the allowlist (draft NCR, draft PO, priority, blocker escalate). Unknown
action types still raise.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, FrozenSet, Optional, Set

from sqlalchemy.orm import Session

from app.models.ai_learning import AIRecommendation
from app.models.part import Part
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.quality import NCRSource, NCRStatus, NonConformanceReport
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlockerSeverity, WorkOrderBlockerStatus
from app.schemas.work_order_blocker import WorkOrderBlockerUpdate
from app.services.audit_service import AuditService
from app.services.operational_event_service import OperationalEventService
from app.services.work_order_blocker_service import WorkOrderBlockerService

logger = logging.getLogger(__name__)

# Actions that may run on accept/auto-execute.
DEFAULT_APPLY_ALLOWLIST: FrozenSet[str] = frozenset(
    {
        "escalate_blocker",
        "acknowledge_blocker",
        "adjust_work_order_priority",
        "create_draft_ncr",
        "create_draft_po",
    }
)

ROLE_RANK = {
    UserRole.VIEWER: 0,
    UserRole.OPERATOR: 1,
    UserRole.SHIPPING: 1,
    UserRole.QUALITY: 2,
    UserRole.SUPERVISOR: 3,
    UserRole.MANAGER: 4,
    UserRole.ADMIN: 5,
    UserRole.PLATFORM_ADMIN: 5,
}

ACTION_MIN_ROLE: Dict[str, UserRole] = {
    "escalate_blocker": UserRole.SUPERVISOR,
    "acknowledge_blocker": UserRole.SUPERVISOR,
    "adjust_work_order_priority": UserRole.SUPERVISOR,
    "create_draft_ncr": UserRole.QUALITY,  # quality+ (rank 2); supervisor also ok
    "create_draft_po": UserRole.SUPERVISOR,
}


class AIActionApplyError(Exception):
    """User-facing apply failure (role, missing target, unsupported type)."""


class AIActionApplier:
    def __init__(
        self,
        db: Session,
        *,
        company_id: int,
        user: User,
        audit: Optional[AuditService] = None,
        allowlist: Optional[Set[str]] = None,
        bypass_role_checks: bool = False,
    ):
        self.db = db
        self.company_id = company_id
        self.user = user
        self.audit = audit
        self.allowlist = allowlist or set(DEFAULT_APPLY_ALLOWLIST)
        # System auto-execute actor (Claude agent) skips interactive role gates.
        self.bypass_role_checks = bypass_role_checks or bool(getattr(user, "is_superuser", False))

    def can_apply(self, recommendation: AIRecommendation) -> bool:
        action = recommendation.suggested_action or {}
        action_type = str(action.get("type") or "")
        if not action_type or action_type not in self.allowlist:
            return False
        if not self.bypass_role_checks and not self._role_ok(action_type):
            return False
        autonomy = str(action.get("autonomy") or "suggest_only")
        return autonomy in {"apply_on_accept", "execute_controlled", "auto_execute"}

    def apply(self, recommendation: AIRecommendation) -> Dict[str, Any]:
        """Execute the recommendation's suggested_action. Raises AIActionApplyError."""
        if recommendation.company_id != self.company_id:
            raise AIActionApplyError("Recommendation not found for this company")

        action = dict(recommendation.suggested_action or {})
        action_type = str(action.get("type") or "").strip()
        if not action_type:
            raise AIActionApplyError("Recommendation has no suggested action type")
        if action_type not in self.allowlist:
            raise AIActionApplyError(f"Action type '{action_type}' is not allowlisted for apply")
        if not self.bypass_role_checks and not self._role_ok(action_type):
            raise AIActionApplyError(f"Insufficient role to apply '{action_type}'")

        handlers = {
            "escalate_blocker": self._escalate_blocker,
            "acknowledge_blocker": self._acknowledge_blocker,
            "adjust_work_order_priority": self._adjust_priority,
            "create_draft_ncr": self._create_draft_ncr,
            "create_draft_po": self._create_draft_po,
        }
        handler = handlers.get(action_type)
        if not handler:
            raise AIActionApplyError(f"No applier registered for '{action_type}'")

        result = handler(recommendation, action)
        OperationalEventService(self.db).emit(
            company_id=self.company_id,
            event_type="ai_recommendation_applied",
            source_module=recommendation.source_module,
            entity_type=recommendation.target_entity_type,
            entity_id=recommendation.target_entity_id,
            work_order_id=action.get("work_order_id") if isinstance(action.get("work_order_id"), int) else None,
            user_id=self.user.id,
            severity="info",
            event_payload={
                "recommendation_id": recommendation.id,
                "action_type": action_type,
                "result": result,
            },
        )
        return {"action_type": action_type, **result}

    def _role_ok(self, action_type: str) -> bool:
        if self.user.is_superuser:
            return True
        min_role = ACTION_MIN_ROLE.get(action_type, UserRole.MANAGER)
        # Quality can create NCR; supervisors/managers/admins also can
        if action_type == "create_draft_ncr":
            return self.user.role in {
                UserRole.QUALITY,
                UserRole.SUPERVISOR,
                UserRole.MANAGER,
                UserRole.ADMIN,
                UserRole.PLATFORM_ADMIN,
            }
        user_rank = ROLE_RANK.get(self.user.role, 0)
        return user_rank >= ROLE_RANK.get(min_role, 99)

    def _escalate_blocker(self, recommendation: AIRecommendation, action: Dict[str, Any]) -> Dict[str, Any]:
        blocker_id = action.get("blocker_id") or (
            recommendation.target_entity_id if recommendation.target_entity_type == "work_order_blocker" else None
        )
        if not blocker_id:
            raise AIActionApplyError("escalate_blocker requires blocker_id")
        service = WorkOrderBlockerService(self.db)
        blocker = service.update_blocker(
            company_id=self.company_id,
            user=self.user,
            blocker_id=int(blocker_id),
            data=WorkOrderBlockerUpdate(
                status=WorkOrderBlockerStatus.ACKNOWLEDGED,
                severity=WorkOrderBlockerSeverity.HIGH,
            ),
            audit=self.audit,
        )
        # Bump linked WO priority toward expedite when still open
        wo_id = blocker.work_order_id
        if wo_id:
            wo = (
                self.db.query(WorkOrder)
                .filter(WorkOrder.id == wo_id, WorkOrder.company_id == self.company_id)
                .first()
            )
            if wo and wo.priority and wo.priority > 2:
                old = wo.priority
                wo.priority = 2
                wo.updated_at = datetime.utcnow()
                if self.audit:
                    self.audit.log_update(
                        resource_type="work_order",
                        resource_id=wo.id,
                        resource_identifier=wo.work_order_number,
                        old_values={"priority": old},
                        new_values={"priority": wo.priority},
                        description=f"Priority expedited by AI escalate_blocker on rec #{recommendation.id}",
                    )
        return {"blocker_id": blocker.id, "status": blocker.status, "severity": blocker.severity}

    def _acknowledge_blocker(self, recommendation: AIRecommendation, action: Dict[str, Any]) -> Dict[str, Any]:
        blocker_id = action.get("blocker_id") or (
            recommendation.target_entity_id if recommendation.target_entity_type == "work_order_blocker" else None
        )
        if not blocker_id:
            raise AIActionApplyError("acknowledge_blocker requires blocker_id")
        blocker = WorkOrderBlockerService(self.db).update_blocker(
            company_id=self.company_id,
            user=self.user,
            blocker_id=int(blocker_id),
            data=WorkOrderBlockerUpdate(status=WorkOrderBlockerStatus.ACKNOWLEDGED),
            audit=self.audit,
        )
        return {"blocker_id": blocker.id, "status": blocker.status}

    def _adjust_priority(self, recommendation: AIRecommendation, action: Dict[str, Any]) -> Dict[str, Any]:
        wo_id = action.get("work_order_id") or (
            recommendation.target_entity_id if recommendation.target_entity_type == "work_order" else None
        )
        if not wo_id:
            raise AIActionApplyError("adjust_work_order_priority requires work_order_id")
        new_priority = int(action.get("priority") or 2)
        new_priority = max(1, min(10, new_priority))
        wo = (
            self.db.query(WorkOrder)
            .filter(WorkOrder.id == int(wo_id), WorkOrder.company_id == self.company_id)
            .first()
        )
        if not wo:
            raise AIActionApplyError("Work order not found")
        if wo.status in {WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED}:
            raise AIActionApplyError("Cannot change priority on a terminal work order")
        old = wo.priority
        wo.priority = new_priority
        wo.updated_at = datetime.utcnow()
        if self.audit:
            self.audit.log_update(
                resource_type="work_order",
                resource_id=wo.id,
                resource_identifier=wo.work_order_number,
                old_values={"priority": old},
                new_values={"priority": new_priority},
                description=(
                    f"AI recommendation #{recommendation.id} set priority {old}→{new_priority}"
                ),
            )
        return {
            "work_order_id": wo.id,
            "work_order_number": wo.work_order_number,
            "old_priority": old,
            "new_priority": new_priority,
        }

    def _create_draft_ncr(self, recommendation: AIRecommendation, action: Dict[str, Any]) -> Dict[str, Any]:
        part_id = action.get("part_id") or (
            recommendation.target_entity_id if recommendation.target_entity_type == "part" else None
        )
        work_order_id = action.get("work_order_id")
        title = str(action.get("title") or recommendation.title)[:200]
        description = str(action.get("description") or recommendation.summary or recommendation.title)
        if len(description) < 5:
            description = f"{description} (opened from AI quality recommendation)"
        from app.api.endpoints.quality import generate_ncr_number

        ncr = NonConformanceReport(
            ncr_number=generate_ncr_number(self.db, self.company_id),
            part_id=int(part_id) if part_id else None,
            work_order_id=int(work_order_id) if work_order_id else None,
            quantity_affected=float(action.get("quantity_affected") or 1.0),
            source=NCRSource.IN_PROCESS,
            status=NCRStatus.OPEN,
            title=title if len(title) >= 5 else f"Quality review: {title}",
            description=description[:4000],
            detected_by=self.user.id,
            detected_date=date.today(),
            company_id=self.company_id,
        )
        self.db.add(ncr)
        self.db.flush()
        if self.audit:
            self.audit.log_create(
                resource_type="ncr",
                resource_id=ncr.id,
                resource_identifier=ncr.ncr_number,
                new_values=ncr,
                description=f"Draft NCR created from AI recommendation #{recommendation.id}",
            )
        return {"ncr_id": ncr.id, "ncr_number": ncr.ncr_number, "status": "open"}

    def _create_draft_po(self, recommendation: AIRecommendation, action: Dict[str, Any]) -> Dict[str, Any]:
        part_id = action.get("part_id") or (
            recommendation.target_entity_id if recommendation.target_entity_type == "part" else None
        )
        if not part_id:
            raise AIActionApplyError("create_draft_po requires part_id")
        part = (
            self.db.query(Part)
            .filter(Part.id == int(part_id), Part.company_id == self.company_id)
            .first()
        )
        if not part:
            raise AIActionApplyError("Part not found")
        vendor_id = action.get("vendor_id") or part.primary_supplier_id
        if not vendor_id:
            # Fall back to any active vendor for the tenant
            vendor = (
                self.db.query(Vendor)
                .filter(Vendor.company_id == self.company_id)
                .order_by(Vendor.id.asc())
                .first()
            )
            if not vendor:
                raise AIActionApplyError("No vendor available to create a draft PO")
            vendor_id = vendor.id
        else:
            vendor = (
                self.db.query(Vendor)
                .filter(Vendor.id == int(vendor_id), Vendor.company_id == self.company_id)
                .first()
            )
            if not vendor:
                raise AIActionApplyError("Vendor not found for this company")

        qty = float(action.get("suggested_qty") or part.reorder_quantity or 1.0)
        if qty <= 0:
            qty = 1.0
        unit_price = float(part.standard_cost or part.material_cost or 0.0)
        line_total = qty * unit_price

        today = datetime.utcnow().strftime("%Y%m%d")
        prefix = f"PO-{today}-"
        last = (
            self.db.query(PurchaseOrder)
            .filter(
                PurchaseOrder.company_id == self.company_id,
                PurchaseOrder.po_number.like(f"{prefix}%"),
            )
            .order_by(PurchaseOrder.po_number.desc())
            .first()
        )
        seq = 1
        if last and last.po_number:
            try:
                seq = int(last.po_number.rsplit("-", 1)[-1]) + 1
            except ValueError:
                seq = 1
        po_number = f"{prefix}{seq:03d}"

        po = PurchaseOrder(
            po_number=po_number,
            vendor_id=int(vendor_id),
            status=POStatus.DRAFT,
            order_date=date.today(),
            notes=f"Draft from AI inventory recommendation #{recommendation.id}",
            created_by=self.user.id,
            company_id=self.company_id,
            subtotal=line_total,
            total=line_total,
        )
        self.db.add(po)
        self.db.flush()
        line = PurchaseOrderLine(
            purchase_order_id=po.id,
            line_number=1,
            part_id=part.id,
            quantity_ordered=qty,
            unit_price=unit_price,
            line_total=line_total,
            company_id=self.company_id,
        )
        self.db.add(line)
        self.db.flush()
        if self.audit:
            self.audit.log_create(
                resource_type="purchase_order",
                resource_id=po.id,
                resource_identifier=po.po_number,
                new_values={"status": "draft", "part_id": part.id, "qty": qty},
                description=f"Draft PO created from AI recommendation #{recommendation.id}",
            )
        return {
            "purchase_order_id": po.id,
            "po_number": po.po_number,
            "part_id": part.id,
            "quantity": qty,
            "status": "draft",
        }
