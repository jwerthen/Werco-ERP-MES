"""
MRP (Material Requirements Planning) Service
Calculates material requirements based on:
- Work orders and their BOMs
- Current inventory levels
- Safety stock requirements
- Lead times
"""
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import and_, func

from app.models.part import Part, PartType
from app.models.bom import BOM, BOMItem, BOMItemType
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.models.inventory import InventoryItem
from app.models.mrp import MRPRun, MRPRequirement, MRPAction, MRPRunStatus, PlanningAction


class MRPService:
    def __init__(self, db: Session):
        self.db = db
    
    def generate_run_number(self) -> str:
        """Generate unique MRP run number"""
        today = datetime.now().strftime("%Y%m%d")
        prefix = f"MRP-{today}-"
        
        last_run = self.db.query(MRPRun).filter(
            MRPRun.run_number.like(f"{prefix}%")
        ).order_by(MRPRun.run_number.desc()).first()
        
        if last_run:
            last_num = int(last_run.run_number.split("-")[-1])
            new_num = last_num + 1
        else:
            new_num = 1
        
        return f"{prefix}{new_num:03d}"
    
    def get_inventory_summary(self, part_id: int) -> Tuple[float, float, float]:
        """Get inventory quantities for a part: (on_hand, allocated, on_order)"""
        # On hand from inventory
        on_hand_result = self.db.query(func.sum(InventoryItem.quantity_on_hand)).filter(
            InventoryItem.part_id == part_id,
            InventoryItem.is_active == True,
            InventoryItem.status == "available"
        ).scalar() or 0.0
        
        # Allocated from inventory
        allocated_result = self.db.query(func.sum(InventoryItem.quantity_allocated)).filter(
            InventoryItem.part_id == part_id,
            InventoryItem.is_active == True
        ).scalar() or 0.0
        
        # On order - would come from purchase orders (simplified for now)
        # In a full implementation, this would query open PO lines
        on_order = 0.0
        
        return float(on_hand_result), float(allocated_result), on_order
    
    def get_work_order_requirements(
        self, 
        horizon_end: date,
        include_allocated: bool = True
    ) -> List[Dict]:
        """Get material requirements from work orders"""
        requirements = []
        
        # Get active work orders within planning horizon
        work_orders = self.db.query(WorkOrder).filter(
            WorkOrder.status.in_([
                WorkOrderStatus.DRAFT, 
                WorkOrderStatus.RELEASED, 
                WorkOrderStatus.IN_PROGRESS
            ]),
            WorkOrder.due_date <= horizon_end
        ).all()
        
        for wo in work_orders:
            # Get the BOM for this work order's part
            bom = self.db.query(BOM).filter(
                BOM.part_id == wo.part_id,
                BOM.is_active == True
            ).first()
            
            if not bom:
                continue
            
            # Calculate remaining quantity needed
            qty_remaining = wo.quantity_ordered - wo.quantity_complete
            if qty_remaining <= 0:
                continue
            
            # Explode BOM to get all requirements
            bom_requirements = self.explode_bom_for_mrp(
                bom.id, 
                qty_remaining, 
                wo.due_date or date.today() + timedelta(days=7),
                0
            )
            
            for req in bom_requirements:
                req['source_type'] = 'work_order'
                req['source_id'] = wo.id
                req['source_number'] = wo.work_order_number
                requirements.append(req)
        
        return requirements
    
    def explode_bom_for_mrp(
        self, 
        bom_id: int, 
        parent_qty: float, 
        required_date: date,
        level: int,
        visited: set = None
    ) -> List[Dict]:
        """Recursively explode BOM for MRP requirements"""
        if visited is None:
            visited = set()
        
        if bom_id in visited:
            return []  # Prevent circular reference
        
        visited.add(bom_id)
        requirements = []
        
        bom = self.db.query(BOM).filter(BOM.id == bom_id).first()
        if not bom:
            return requirements
        
        for item in bom.items:
            if item.is_alternate:  # Skip alternates for now
                continue
            
            part = item.component_part
            if not part:
                continue
            
            # Calculate extended quantity with scrap
            ext_qty = item.quantity * parent_qty * (1 + item.scrap_factor)
            
            # Adjust required date by lead time offset
            item_required_date = required_date - timedelta(days=item.lead_time_offset)
            
            # Add requirement for this item
            requirements.append({
                'part_id': part.id,
                'part_number': part.part_number,
                'part_name': part.name,
                'part_type': part.part_type.value,
                'quantity': ext_qty,
                'required_date': item_required_date,
                'bom_level': level,
                'parent_part_id': bom.part_id,
                'item_type': item.item_type.value,
                'lead_time_days': part.lead_time_days
            })
            
            # If this is a MAKE item, recurse into its BOM
            if item.item_type == BOMItemType.MAKE:
                child_bom = self.db.query(BOM).filter(
                    BOM.part_id == part.id,
                    BOM.is_active == True
                ).first()
                
                if child_bom:
                    child_requirements = self.explode_bom_for_mrp(
                        child_bom.id,
                        ext_qty,
                        item_required_date - timedelta(days=part.lead_time_days),
                        level + 1,
                        visited.copy()
                    )
                    requirements.extend(child_requirements)
        
        return requirements
    
    def aggregate_requirements(self, requirements: List[Dict]) -> Dict[int, Dict]:
        """Aggregate requirements by part and date"""
        aggregated = {}
        
        for req in requirements:
            part_id = req['part_id']
            req_date = req['required_date']
            
            if part_id not in aggregated:
                aggregated[part_id] = {
                    'part_id': part_id,
                    'part_number': req['part_number'],
                    'part_name': req['part_name'],
                    'part_type': req['part_type'],
                    'lead_time_days': req.get('lead_time_days', 0),
                    'by_date': {},
                    'total_required': 0,
                    'sources': []
                }
            
            date_key = req_date.isoformat() if isinstance(req_date, date) else str(req_date)
            
            if date_key not in aggregated[part_id]['by_date']:
                aggregated[part_id]['by_date'][date_key] = 0
            
            aggregated[part_id]['by_date'][date_key] += req['quantity']
            aggregated[part_id]['total_required'] += req['quantity']
            
            if 'source_number' in req:
                aggregated[part_id]['sources'].append({
                    'type': req.get('source_type'),
                    'number': req.get('source_number'),
                    'quantity': req['quantity'],
                    'date': req_date
                })
        
        return aggregated
    
    def calculate_shortages_and_actions(
        self, 
        aggregated: Dict[int, Dict],
        include_safety_stock: bool = True
    ) -> Tuple[List[MRPRequirement], List[MRPAction]]:
        """Calculate shortages and generate recommended actions"""
        requirements_list = []
        actions_list = []
        
        for part_id, data in aggregated.items():
            part = self.db.query(Part).filter(Part.id == part_id).first()
            if not part:
                continue
            
            on_hand, allocated, on_order = self.get_inventory_summary(part_id)
            available = on_hand - allocated
            
            safety_stock = part.safety_stock if include_safety_stock else 0
            running_available = available + on_order
            
            # Process requirements by date
            sorted_dates = sorted(data['by_date'].keys())
            
            for date_str in sorted_dates:
                qty_required = data['by_date'][date_str]
                req_date = date.fromisoformat(date_str) if isinstance(date_str, str) else date_str
                
                # Calculate shortage
                shortage = max(0, qty_required + safety_stock - running_available)
                
                # Create requirement record
                req = MRPRequirement(
                    part_id=part_id,
                    required_date=req_date,
                    quantity_required=qty_required,
                    quantity_on_hand=on_hand,
                    quantity_on_order=on_order,
                    quantity_allocated=allocated,
                    quantity_available=available,
                    quantity_shortage=shortage,
                    source_type='aggregated',
                    bom_level=0
                )
                requirements_list.append(req)
                
                # Generate action if shortage
                if shortage > 0:
                    lead_time = part.lead_time_days or 0
                    order_date = req_date - timedelta(days=lead_time)
                    
                    # Determine action type based on part type
                    if part.part_type == PartType.PURCHASED:
                        action_type = PlanningAction.ORDER
                    else:
                        action_type = PlanningAction.MANUFACTURE
                    
                    # Check if order date is in the past
                    if order_date < date.today():
                        action_type = PlanningAction.EXPEDITE
                        order_date = date.today()
                    
                    action = MRPAction(
                        part_id=part_id,
                        action_type=action_type,
                        priority=1 if action_type == PlanningAction.EXPEDITE else 5,
                        quantity=shortage,
                        required_date=req_date,
                        suggested_order_date=order_date,
                        notes=f"Shortage of {shortage:.2f} units needed by {req_date}"
                    )
                    actions_list.append(action)
                
                # Update running available
                running_available -= qty_required
        
        return requirements_list, actions_list
    
    def run_mrp(
        self,
        user_id: int,
        planning_horizon_days: int = 90,
        include_safety_stock: bool = True,
        include_allocated: bool = True
    ) -> MRPRun:
        """Execute a full MRP run"""
        
        # Create MRP run record
        mrp_run = MRPRun(
            run_number=self.generate_run_number(),
            planning_horizon_days=planning_horizon_days,
            include_safety_stock=include_safety_stock,
            include_allocated=include_allocated,
            status=MRPRunStatus.RUNNING,
            started_at=datetime.utcnow(),
            created_by=user_id
        )
        self.db.add(mrp_run)
        self.db.flush()
        
        try:
            horizon_end = date.today() + timedelta(days=planning_horizon_days)
            
            # Get requirements from work orders
            requirements = self.get_work_order_requirements(horizon_end, include_allocated)
            
            # Aggregate by part
            aggregated = self.aggregate_requirements(requirements)
            
            # Calculate shortages and actions
            req_records, action_records = self.calculate_shortages_and_actions(
                aggregated, 
                include_safety_stock
            )
            
            # Link to MRP run and save
            for req in req_records:
                req.mrp_run_id = mrp_run.id
                self.db.add(req)
            
            for action in action_records:
                action.mrp_run_id = mrp_run.id
                self.db.add(action)
            
            # Update run statistics
            mrp_run.status = MRPRunStatus.COMPLETE
            mrp_run.completed_at = datetime.utcnow()
            mrp_run.total_parts_analyzed = len(aggregated)
            mrp_run.total_requirements = len(req_records)
            mrp_run.total_actions = len(action_records)
            
            self.db.commit()
            self.db.refresh(mrp_run)
            
        except Exception as e:
            mrp_run.status = MRPRunStatus.ERROR
            mrp_run.error_message = str(e)
            mrp_run.completed_at = datetime.utcnow()
            self.db.commit()
            raise
        
        return mrp_run
