from typing import List, Optional
from datetime import date
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session, joinedload
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.mrp import MRPRun, MRPRequirement, MRPAction, MRPRunStatus, PlanningAction
from app.models.part import Part
from app.schemas.mrp import (
    MRPRunCreate, MRPRunResponse, MRPRunDetail,
    MRPRequirementResponse, MRPActionResponse,
    MRPPartAnalysis, ProcessActionRequest, ProcessActionResponse,
    PartSummary
)
from app.services.mrp_service import MRPService

router = APIRouter()


@router.get("/runs", response_model=List[MRPRunResponse])
def list_mrp_runs(
    skip: int = 0,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List MRP runs"""
    runs = db.query(MRPRun).order_by(MRPRun.created_at.desc()).offset(skip).limit(limit).all()
    return runs


@router.post("/runs", response_model=MRPRunResponse)
def create_mrp_run(
    run_params: MRPRunCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Execute a new MRP run"""
    service = MRPService(db)
    
    try:
        mrp_run = service.run_mrp(
            user_id=current_user.id,
            planning_horizon_days=run_params.planning_horizon_days,
            include_safety_stock=run_params.include_safety_stock,
            include_allocated=run_params.include_allocated
        )
        return mrp_run
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MRP run failed: {str(e)}")


@router.get("/runs/{run_id}", response_model=MRPRunDetail)
def get_mrp_run(
    run_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get MRP run details with requirements and actions"""
    run = db.query(MRPRun).options(
        joinedload(MRPRun.requirements).joinedload(MRPRequirement.part),
        joinedload(MRPRun.actions).joinedload(MRPAction.part)
    ).filter(MRPRun.id == run_id).first()
    
    if not run:
        raise HTTPException(status_code=404, detail="MRP run not found")
    
    # Build response with part info
    requirements = []
    for req in run.requirements:
        req_response = MRPRequirementResponse(
            id=req.id,
            mrp_run_id=req.mrp_run_id,
            part_id=req.part_id,
            part=PartSummary(
                id=req.part.id,
                part_number=req.part.part_number,
                name=req.part.name,
                part_type=req.part.part_type.value
            ) if req.part else None,
            required_date=req.required_date,
            quantity_required=req.quantity_required,
            quantity_on_hand=req.quantity_on_hand,
            quantity_on_order=req.quantity_on_order,
            quantity_allocated=req.quantity_allocated,
            quantity_available=req.quantity_available,
            quantity_shortage=req.quantity_shortage,
            source_type=req.source_type,
            source_number=req.source_number,
            bom_level=req.bom_level
        )
        requirements.append(req_response)
    
    actions = []
    for action in run.actions:
        action_response = MRPActionResponse(
            id=action.id,
            mrp_run_id=action.mrp_run_id,
            part_id=action.part_id,
            part=PartSummary(
                id=action.part.id,
                part_number=action.part.part_number,
                name=action.part.name,
                part_type=action.part.part_type.value
            ) if action.part else None,
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
            result_reference=action.result_reference,
            notes=action.notes
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
        actions=actions
    )


@router.get("/runs/{run_id}/actions", response_model=List[MRPActionResponse])
def get_mrp_actions(
    run_id: int,
    action_type: Optional[PlanningAction] = None,
    unprocessed_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get actions from an MRP run with filtering"""
    query = db.query(MRPAction).options(
        joinedload(MRPAction.part)
    ).filter(MRPAction.mrp_run_id == run_id)
    
    if action_type:
        query = query.filter(MRPAction.action_type == action_type)
    
    if unprocessed_only:
        query = query.filter(MRPAction.is_processed == False)
    
    actions = query.order_by(MRPAction.priority, MRPAction.suggested_order_date).all()
    
    result = []
    for action in actions:
        result.append(MRPActionResponse(
            id=action.id,
            mrp_run_id=action.mrp_run_id,
            part_id=action.part_id,
            part=PartSummary(
                id=action.part.id,
                part_number=action.part.part_number,
                name=action.part.name,
                part_type=action.part.part_type.value
            ) if action.part else None,
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
            result_reference=action.result_reference,
            notes=action.notes
        ))
    
    return result


@router.get("/runs/latest", response_model=Optional[MRPRunResponse])
def get_latest_mrp_run(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get the most recent completed MRP run"""
    run = db.query(MRPRun).filter(
        MRPRun.status == MRPRunStatus.COMPLETE
    ).order_by(MRPRun.completed_at.desc()).first()
    
    return run


@router.get("/shortages")
def get_current_shortages(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get summary of current material shortages from latest MRP run"""
    # Get latest completed run
    latest_run = db.query(MRPRun).filter(
        MRPRun.status == MRPRunStatus.COMPLETE
    ).order_by(MRPRun.completed_at.desc()).first()
    
    if not latest_run:
        return {
            "message": "No MRP run found. Run MRP to see shortages.",
            "shortages": []
        }
    
    # Get actions that indicate shortages
    shortage_actions = db.query(MRPAction).options(
        joinedload(MRPAction.part)
    ).filter(
        MRPAction.mrp_run_id == latest_run.id,
        MRPAction.is_processed == False,
        MRPAction.action_type.in_([PlanningAction.ORDER, PlanningAction.MANUFACTURE, PlanningAction.EXPEDITE])
    ).order_by(MRPAction.priority, MRPAction.suggested_order_date).all()
    
    shortages = []
    for action in shortage_actions:
        shortages.append({
            "action_id": action.id,
            "part_id": action.part_id,
            "part_number": action.part.part_number if action.part else None,
            "part_name": action.part.name if action.part else None,
            "action_type": action.action_type.value,
            "quantity": action.quantity,
            "required_date": action.required_date.isoformat(),
            "order_by_date": action.suggested_order_date.isoformat(),
            "priority": action.priority,
            "is_expedite": action.action_type == PlanningAction.EXPEDITE
        })
    
    return {
        "mrp_run_id": latest_run.id,
        "mrp_run_number": latest_run.run_number,
        "run_date": latest_run.completed_at.isoformat() if latest_run.completed_at else None,
        "total_shortages": len(shortages),
        "expedite_count": sum(1 for s in shortages if s['is_expedite']),
        "shortages": shortages
    }


@router.post("/actions/{action_id}/process", response_model=ProcessActionResponse)
def process_mrp_action(
    action_id: int,
    notes: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """
    Mark an MRP action as processed.
    In a full implementation, this would create the actual WO or PO.
    """
    action = db.query(MRPAction).filter(MRPAction.id == action_id).first()
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
    
    # In a full implementation, we would:
    # - For ORDER actions: Create a Purchase Order
    # - For MANUFACTURE actions: Create a Work Order
    # For now, we just mark it processed
    
    db.commit()
    
    return ProcessActionResponse(
        success=True,
        message=f"Action marked as processed. Create {action.action_type.value} manually.",
        created_reference=None
    )


@router.delete("/runs/{run_id}")
def delete_mrp_run(
    run_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN]))
):
    """Delete an MRP run and all its data"""
    run = db.query(MRPRun).filter(MRPRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="MRP run not found")
    
    db.delete(run)
    db.commit()
    
    return {"message": "MRP run deleted"}
