"""
MRP Auto-Processing Service
Handles automatic creation of POs and WOs from MRP actions
"""
from datetime import datetime, date
from typing import List, Dict
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.mrp import MRPAction, PlanningAction
from app.models.part import Part, PartType
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine, POStatus
from app.models.vendor import Vendor
from app.models.user import User
from app.models.supplier_part import SupplierPartMapping
from app.services.audit_service import AuditService
from app.services.notification_service import NotificationService, NotificationEvent
import logging

logger = logging.getLogger(__name__)


class MRPAutoMode:
    """MRP Auto-processing modes"""
    REVIEW = "REVIEW"  # Generate actions, require manual approval
    AUTO_DRAFT = "AUTO_DRAFT"  # Auto-create POs/WOs in draft status
    AUTO_SUBMIT = "AUTO_SUBMIT"  # Auto-create and release POs/WOs (requires admin)


class MRPAutoService:
    """Service for auto-processing MRP actions"""

    def __init__(self, db: Session):
        self.db = db

    def process_actions(
        self,
        actions: List[MRPAction],
        mode: str = MRPAutoMode.REVIEW,
        user_id: int = None
    ) -> Dict[str, int]:
        """
        Process MRP actions based on mode

        Args:
            actions: List of MRP actions to process
            mode: Processing mode (REVIEW, AUTO_DRAFT, AUTO_SUBMIT)
            user_id: User ID for audit (None = system user)

        Returns:
            Dict with counts of processed actions
        """
        results = {
            "pos_created": 0,
            "wos_created": 0,
            "actions_processed": 0,
            "errors": 0
        }

        if mode == MRPAutoMode.REVIEW:
            # No auto-processing in review mode
            return results

        for action in actions:
            try:
                if action.action_type == PlanningAction.ORDER:
                    # Create purchase order
                    po = self._create_po_from_action(action, mode, user_id)
                    if po:
                        results["pos_created"] += 1
                        action.processed = True
                        action.processed_at = datetime.utcnow()
                        action.result_po_id = po.id

                elif action.action_type == PlanningAction.MANUFACTURE:
                    # Create work order
                    wo = self._create_wo_from_action(action, mode, user_id)
                    if wo:
                        results["wos_created"] += 1
                        action.processed = True
                        action.processed_at = datetime.utcnow()
                        action.result_wo_id = wo.id

                elif action.action_type == PlanningAction.EXPEDITE:
                    # Flag for manual expedite
                    self._flag_for_expedite(action, user_id)

                results["actions_processed"] += 1

            except Exception as e:
                logger.error(f"Failed to process MRP action {action.id}: {e}")
                results["errors"] += 1
                action.error_message = str(e)

        self.db.commit()
        return results

    def _create_po_from_action(
        self,
        action: MRPAction,
        mode: str,
        user_id: int = None
    ) -> PurchaseOrder:
        """Create purchase order from MRP action"""

        part = self.db.query(Part).filter(Part.id == action.part_id).first()
        if not part:
            raise ValueError(f"Part {action.part_id} not found")

        # Get preferred vendor
        vendor = self._get_preferred_vendor(action.part_id)
        if not vendor:
            raise ValueError(f"No vendor found for part {part.part_number}")

        # Generate PO number
        po_number = self._generate_po_number()

        # Determine status based on mode
        if mode == MRPAutoMode.AUTO_SUBMIT:
            status = POStatus.SENT
        else:
            status = POStatus.DRAFT

        # Create PO
        po = PurchaseOrder(
            po_number=po_number,
            vendor_id=vendor.id,
            status=status,
            order_date=date.today(),
            expected_date=action.required_date,
            created_by=user_id,
            notes=f"Auto-created by MRP run {action.mrp_run_id}"
        )
        self.db.add(po)
        self.db.flush()

        # Create PO line
        unit_cost = self._get_part_cost(action.part_id, vendor.id)
        po_line = PurchaseOrderLine(
            po_id=po.id,
            part_id=action.part_id,
            quantity=action.quantity,
            unit_cost=unit_cost,
            line_number=1
        )
        self.db.add(po_line)

        # Audit log
        self._create_audit_log(
            action="CREATE_PO",
            entity_type="PurchaseOrder",
            entity_id=po.id,
            details={
                "mrp_action_id": action.id,
                "mode": mode,
                "auto_created": True
            },
            user_id=user_id
        )

        logger.info(f"Created PO {po_number} from MRP action {action.id}")
        return po

    def _create_wo_from_action(
        self,
        action: MRPAction,
        mode: str,
        user_id: int = None
    ) -> WorkOrder:
        """Create work order from MRP action"""

        part = self.db.query(Part).filter(Part.id == action.part_id).first()
        if not part:
            raise ValueError(f"Part {action.part_id} not found")

        # Generate WO number
        wo_number = self._generate_wo_number()

        # Determine status based on mode
        if mode == MRPAutoMode.AUTO_SUBMIT:
            status = WorkOrderStatus.RELEASED
        else:
            status = WorkOrderStatus.DRAFT

        # Create WO
        wo = WorkOrder(
            wo_number=wo_number,
            part_id=action.part_id,
            quantity_ordered=action.quantity,
            due_date=action.required_date,
            status=status,
            priority=action.priority,
            created_by=user_id,
            notes=f"Auto-created by MRP run {action.mrp_run_id}"
        )
        self.db.add(wo)
        self.db.flush()

        # Audit log
        self._create_audit_log(
            action="CREATE_WO",
            entity_type="WorkOrder",
            entity_id=wo.id,
            details={
                "mrp_action_id": action.id,
                "mode": mode,
                "auto_created": True
            },
            user_id=user_id
        )

        logger.info(f"Created WO {wo_number} from MRP action {action.id}")
        return wo

    def _flag_for_expedite(self, action: MRPAction, user_id: int = None):
        """Flag action for manual expedite"""

        part = self.db.query(Part).filter(Part.id == action.part_id).first()
        if not part:
            return

        # Send notification to purchasing/production
        notification_service = NotificationService(self.db)

        from app.services.notification_service import get_notification_recipients
        recipients = get_notification_recipients(self.db, department="Purchasing")

        # Use enqueue_job to avoid blocking
        from app.core.queue import enqueue_job
        import asyncio

        asyncio.create_task(notification_service.send_notification(
            event_type=NotificationEvent.CAPACITY_OVERLOAD,
            users=recipients,
            subject=f"EXPEDITE REQUIRED: {part.part_number}",
            context={
                "part": part,
                "quantity": action.quantity,
                "required_date": action.required_date
            },
            template="expedite_required"
        ))

        action.processed = True
        action.processed_at = datetime.utcnow()

    def _get_preferred_vendor(self, part_id: int) -> Vendor:
        """
        Get preferred vendor for a part.
        
        Lookup priority:
        1. Check SupplierPartMapping for vendor associations
        2. Check recent purchase order history for most-used vendor
        3. Fall back to first active vendor
        
        Args:
            part_id: The part ID to find a vendor for
            
        Returns:
            Vendor object or None if no active vendors exist
        """
        # Priority 1: Check supplier part mappings for this part
        mapping = self.db.query(SupplierPartMapping).filter(
            SupplierPartMapping.part_id == part_id,
            SupplierPartMapping.is_active == True,
            SupplierPartMapping.vendor_id.isnot(None)
        ).join(Vendor).filter(Vendor.is_active == True).first()
        
        if mapping and mapping.vendor:
            logger.debug(f"Found preferred vendor {mapping.vendor.name} from supplier mapping for part {part_id}")
            return mapping.vendor
        
        # Priority 2: Check recent PO history to find most-used vendor for this part
        recent_vendor_query = (
            self.db.query(Vendor, func.count(PurchaseOrderLine.id).label('order_count'))
            .join(PurchaseOrder, PurchaseOrder.vendor_id == Vendor.id)
            .join(PurchaseOrderLine, PurchaseOrderLine.purchase_order_id == PurchaseOrder.id)
            .filter(
                PurchaseOrderLine.part_id == part_id,
                Vendor.is_active == True
            )
            .group_by(Vendor.id)
            .order_by(func.count(PurchaseOrderLine.id).desc())
            .first()
        )
        
        if recent_vendor_query:
            vendor = recent_vendor_query[0]
            logger.debug(f"Found vendor {vendor.name} from PO history for part {part_id}")
            return vendor
        
        # Priority 3: Fall back to first active vendor
        logger.debug(f"No specific vendor found for part {part_id}, using first active vendor")
        vendor = self.db.query(Vendor).filter(Vendor.is_active == True).first()
        return vendor

    def _get_part_cost(self, part_id: int, vendor_id: int) -> float:
        """
        Get part cost from vendor.
        
        Lookup priority:
        1. Check recent purchase orders from this vendor for actual pricing
        2. Fall back to part's standard cost
        
        Args:
            part_id: The part ID to get cost for
            vendor_id: The vendor ID (if known) to check pricing for
            
        Returns:
            Float representing unit cost for the part
        """
        # Priority 1: Check recent PO pricing from this vendor
        if vendor_id:
            recent_price = (
                self.db.query(PurchaseOrderLine.unit_price)
                .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
                .filter(
                    PurchaseOrderLine.part_id == part_id,
                    PurchaseOrder.vendor_id == vendor_id,
                    PurchaseOrder.status != POStatus.CANCELLED
                )
                .order_by(PurchaseOrder.created_at.desc())
                .first()
            )
            
            if recent_price and recent_price[0]:
                logger.debug(f"Using recent PO price ${recent_price[0]} for part {part_id} from vendor {vendor_id}")
                return recent_price[0]
        
        # Priority 2: Fall back to part standard cost
        part = self.db.query(Part).filter(Part.id == part_id).first()
        if part and part.standard_cost:
            logger.debug(f"Using standard cost ${part.standard_cost} for part {part_id}")
            return part.standard_cost
        
        logger.warning(f"No cost information found for part {part_id}, returning 0.0")
        return 0.0

    def _generate_po_number(self) -> str:
        """Generate PO number"""
        today = datetime.now().strftime("%Y%m%d")
        prefix = f"PO-{today}-"

        last_po = self.db.query(PurchaseOrder).filter(
            PurchaseOrder.po_number.like(f"{prefix}%")
        ).order_by(PurchaseOrder.po_number.desc()).first()

        if last_po:
            last_num = int(last_po.po_number.split("-")[-1])
            new_num = last_num + 1
        else:
            new_num = 1

        return f"{prefix}{new_num:03d}"

    def _generate_wo_number(self) -> str:
        """Generate WO number"""
        today = datetime.now().strftime("%Y%m%d")
        prefix = f"WO-{today}-"

        last_wo = self.db.query(WorkOrder).filter(
            WorkOrder.wo_number.like(f"{prefix}%")
        ).order_by(WorkOrder.wo_number.desc()).first()

        if last_wo:
            last_num = int(last_wo.wo_number.split("-")[-1])
            new_num = last_num + 1
        else:
            new_num = 1

        return f"{prefix}{new_num:03d}"

    def _create_audit_log(
        self,
        action: str,
        entity_type: str,
        entity_id: int,
        details: Dict,
        user_id: int = None
    ):
        """Create audit log entry"""
        user = None
        if user_id is not None:
            user = self.db.query(User).filter(User.id == user_id).first()

        resource_type_map = {
            "PurchaseOrder": "purchase_order",
            "WorkOrder": "work_order",
        }
        resource_type = resource_type_map.get(entity_type, entity_type)
        description = f"{action} {resource_type} {entity_id}"

        AuditService(self.db, user).log(
            action=action,
            resource_type=resource_type,
            resource_id=entity_id,
            description=description,
            extra_data=details
        )
