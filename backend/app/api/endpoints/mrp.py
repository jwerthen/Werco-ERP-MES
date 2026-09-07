from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.core.time_utils import to_utc_iso
from app.db.database import atomic_transaction, get_db
from app.models.mrp import MRPAction, MRPRequirement, MRPRun, MRPRunStatus, PlanningAction
from app.models.purchasing import PurchaseOrder
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.schemas.mrp import (
    MRPActionResponse,
    MRPRequirementResponse,
    MRPRunCreate,
    MRPRunDetail,
    MRPRunResponse,
    MRPSupplyDraftRequest,
    MRPSupplyDraftResponse,
    PartSummary,
    ProcessActionResponse,
)
from app.services.audit_service import AuditService
from app.services.mrp_service import MRPService
from app.services.mrp_supply_service import MRPSupplyService

router = APIRouter()


def _supply_result(db: Session, action: MRPAction, company_id: int):
    """Resolve tenant-scoped new and legacy auto-draft associations."""
    if action.result_po_id:
        record = (
            db.query(PurchaseOrder)
            .filter(PurchaseOrder.id == action.result_po_id, PurchaseOrder.company_id == company_id)
            .first()
        )
        if record:
            return dict(
                action_id=action.id,
                mrp_run_id=action.mrp_run_id,
                kind="purchase_order",
                id=record.id,
                number=record.po_number,
                url=f"/purchasing?po={record.id}",
                status=record.status.value,
            )
    if action.result_wo_id:
        record = (
            db.query(WorkOrder).filter(WorkOrder.id == action.result_wo_id, WorkOrder.company_id == company_id).first()
        )
        if record:
            return dict(
                action_id=action.id,
                mrp_run_id=action.mrp_run_id,
                kind="work_order",
                id=record.id,
                number=record.work_order_number,
                url=f"/work-orders/{record.id}",
                status=record.status.value,
            )
    return None


@router.get("/actions/{action_id}/supply-review")
def review_supply_draft(
    action_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Revalidate current shortage and preview a supply draft without writing."""
    return MRPSupplyService(db, company_id).review(action_id)


@router.post("/actions/{action_id}/supply-draft", response_model=MRPSupplyDraftResponse)
def create_supply_draft(
    action_id: int,
    payload: MRPSupplyDraftRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create an unissued draft and durable source/retry association atomically."""
    try:
        with atomic_transaction(db):
            result = MRPSupplyService(db, company_id).create(action_id, payload, current_user, audit)
    except IntegrityError as exc:
        raise HTTPException(
            409, "A supply draft conflicts with an existing record. Reload the review to open any existing draft."
        ) from exc
    return result


@router.get("/runs", response_model=List[MRPRunResponse])
def list_mrp_runs(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List MRP runs"""
    runs = (
        db.query(MRPRun)
        .filter(MRPRun.company_id == company_id)
        .order_by(MRPRun.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return runs


@router.post("/runs", response_model=MRPRunResponse)
def create_mrp_run(
    run_params: MRPRunCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Execute a new MRP run"""
    service = MRPService(db, company_id)

    try:
        mrp_run = service.run_mrp(
            user_id=current_user.id,
            planning_horizon_days=run_params.planning_horizon_days,
            include_safety_stock=run_params.include_safety_stock,
            include_allocated=run_params.include_allocated,
        )
        return mrp_run
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MRP run failed: {str(e)}")


@router.get("/runs/latest", response_model=Optional[MRPRunResponse])
def get_latest_mrp_run(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get the most recent completed MRP run"""
    run = (
        db.query(MRPRun)
        .filter(MRPRun.company_id == company_id, MRPRun.status == MRPRunStatus.COMPLETE)
        .order_by(MRPRun.completed_at.desc(), MRPRun.id.desc())
        .first()
    )

    return run


@router.get("/runs/{run_id}", response_model=MRPRunDetail)
def get_mrp_run(
    run_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get MRP run details with requirements and actions"""
    run = (
        db.query(MRPRun)
        .options(
            joinedload(MRPRun.requirements).joinedload(MRPRequirement.part),
            joinedload(MRPRun.actions).joinedload(MRPAction.part),
        )
        .filter(MRPRun.id == run_id, MRPRun.company_id == company_id)
        .first()
    )

    if not run:
        raise HTTPException(status_code=404, detail="MRP run not found")

    # Build response with part info
    requirements = []
    for req in run.requirements:
        req_response = MRPRequirementResponse(
            id=req.id,
            mrp_run_id=req.mrp_run_id,
            part_id=req.part_id,
            part=(
                PartSummary(
                    id=req.part.id,
                    part_number=req.part.part_number,
                    name=req.part.name,
                    part_type=req.part.part_type.value,
                )
                if req.part
                else None
            ),
            required_date=req.required_date,
            quantity_required=req.quantity_required,
            quantity_on_hand=req.quantity_on_hand,
            quantity_on_order=req.quantity_on_order,
            quantity_allocated=req.quantity_allocated,
            quantity_available=req.quantity_available,
            quantity_shortage=req.quantity_shortage,
            source_type=req.source_type,
            source_number=req.source_number,
            bom_level=req.bom_level,
        )
        requirements.append(req_response)

    actions = []
    for action in run.actions:
        action_response = MRPActionResponse(
            id=action.id,
            mrp_run_id=action.mrp_run_id,
            part_id=action.part_id,
            part=(
                PartSummary(
                    id=action.part.id,
                    part_number=action.part.part_number,
                    name=action.part.name,
                    part_type=action.part.part_type.value,
                )
                if action.part
                else None
            ),
            action_type=action.action_type,
            priority=action.priority,
            quantity=action.quantity,
            required_date=action.required_date,
            suggested_order_date=action.suggested_order_date,
            current_date=action.current_date,
            reference_type=action.reference_type,
            reference_number=action.reference_number,
            is_processed=action.is_processed,
            processed_at=action.processed_at,
            result_reference=(_supply_result(db, action, company_id) or {}).get("number"),
            supply_draft=_supply_result(db, action, company_id),
            notes=action.notes,
        )
        actions.append(action_response)

    return MRPRunDetail(
        id=run.id,
        run_number=run.run_number,
        planning_horizon_days=run.planning_horizon_days,
        include_safety_stock=run.include_safety_stock,
        include_allocated=run.include_allocated,
        status=run.status,
        started_at=run.started_at,
        completed_at=run.completed_at,
        error_message=run.error_message,
        total_parts_analyzed=run.total_parts_analyzed,
        total_requirements=run.total_requirements,
        total_actions=run.total_actions,
        created_at=run.created_at,
        requirements=requirements,
        actions=actions,
    )


@router.get("/runs/{run_id}/actions", response_model=List[MRPActionResponse])
def get_mrp_actions(
    run_id: int,
    action_type: Optional[PlanningAction] = None,
    unprocessed_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get actions from an MRP run with filtering"""
    query = (
        db.query(MRPAction)
        .options(joinedload(MRPAction.part))
        .filter(MRPAction.mrp_run_id == run_id, MRPAction.company_id == company_id)
    )

    if action_type:
        query = query.filter(MRPAction.action_type == action_type)

    if unprocessed_only:
        query = query.filter(MRPAction.is_processed == False)

    actions = query.order_by(MRPAction.priority, MRPAction.suggested_order_date).all()

    result = []
    for action in actions:
        result.append(
            MRPActionResponse(
                id=action.id,
                mrp_run_id=action.mrp_run_id,
                part_id=action.part_id,
                part=(
                    PartSummary(
                        id=action.part.id,
                        part_number=action.part.part_number,
                        name=action.part.name,
                        part_type=action.part.part_type.value,
                    )
                    if action.part
                    else None
                ),
                action_type=action.action_type,
                priority=action.priority,
                quantity=action.quantity,
                required_date=action.required_date,
                suggested_order_date=action.suggested_order_date,
                current_date=action.current_date,
                reference_type=action.reference_type,
                reference_number=action.reference_number,
                is_processed=action.is_processed,
                processed_at=action.processed_at,
                result_reference=(_supply_result(db, action, company_id) or {}).get("number"),
                supply_draft=_supply_result(db, action, company_id),
                notes=action.notes,
            )
        )

    return result


@router.get("/shortages")
def get_current_shortages(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get summary of current material shortages from latest MRP run"""
    # Get latest completed run
    latest_run = (
        db.query(MRPRun)
        .filter(MRPRun.company_id == company_id, MRPRun.status == MRPRunStatus.COMPLETE)
        .order_by(MRPRun.completed_at.desc(), MRPRun.id.desc())
        .first()
    )

    if not latest_run:
        return {"message": "No MRP run found. Run MRP to see shortages.", "shortages": []}

    # Get actions that indicate shortages
    shortage_actions = (
        db.query(MRPAction)
        .options(joinedload(MRPAction.part))
        .filter(
            MRPAction.mrp_run_id == latest_run.id,
            MRPAction.company_id == company_id,
            MRPAction.is_processed == False,
            MRPAction.action_type.in_([PlanningAction.ORDER, PlanningAction.MANUFACTURE, PlanningAction.EXPEDITE]),
        )
        .order_by(MRPAction.priority, MRPAction.suggested_order_date)
        .all()
    )

    shortages = []
    for action in shortage_actions:
        shortages.append(
            {
                "action_id": action.id,
                "supply_draft": _supply_result(db, action, company_id),
                "part_id": action.part_id,
                "part_number": action.part.part_number if action.part else None,
                "part_name": action.part.name if action.part else None,
                "action_type": action.action_type.value,
                "quantity": action.quantity,
                "required_date": action.required_date.isoformat(),
                "order_by_date": action.suggested_order_date.isoformat(),
                "priority": action.priority,
                "is_expedite": action.action_type == PlanningAction.EXPEDITE,
            }
        )

    return {
        "mrp_run_id": latest_run.id,
        "mrp_run_number": latest_run.run_number,
        "run_date": to_utc_iso(latest_run.completed_at),
        "total_shortages": len(shortages),
        "expedite_count": sum(1 for s in shortages if s['is_expedite']),
        "shortages": shortages,
    }


@router.post("/actions/{action_id}/process", response_model=ProcessActionResponse)
def process_mrp_action(
    action_id: int,
    notes: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Mark a recommendation reviewed, independently of supply draft creation."""
    action = db.query(MRPAction).filter(MRPAction.id == action_id, MRPAction.company_id == company_id).first()
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")

    if action.is_processed:
        raise HTTPException(status_code=400, detail="Action already processed")

    from datetime import datetime

    action.is_processed = True
    action.processed_at = datetime.utcnow()
    action.processed_by = current_user.id

    if notes:
        action.notes = (action.notes or "") + f"\nProcessed: {notes}"

    db.commit()

    return ProcessActionResponse(
        success=True,
        message="Recommendation marked reviewed. No supply document was created by this action.",
        created_reference=None,
    )


@router.delete("/runs/{run_id}")
def delete_mrp_run(
    run_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
):
    """Delete an MRP run and all its data"""
    run = db.query(MRPRun).filter(MRPRun.id == run_id, MRPRun.company_id == company_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="MRP run not found")

    if any(action.result_po_id or action.result_wo_id for action in run.actions):
        raise HTTPException(409, "This run is linked to supply documents and must be retained for traceability.")

    db.delete(run)
    db.commit()

    return {"message": "MRP run deleted"}
