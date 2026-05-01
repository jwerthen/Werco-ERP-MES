from typing import Dict, List, Optional, Set

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, get_current_user
from app.db.database import get_db
from app.models.bom import BOM, BOMItem
from app.models.customer import Customer
from app.models.inventory import InventoryItem
from app.models.part import Part, PartType
from app.models.purchasing import Vendor
from app.models.routing import Routing, RoutingOperation
from app.models.user import User
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderStatus

router = APIRouter()


def _active_bom_for_part(db: Session, part_id: int, company_id: int) -> Optional[BOM]:
    return db.query(BOM).filter(
        BOM.part_id == part_id,
        BOM.company_id == company_id,
        BOM.is_active == True,
    ).first()


def _bom_has_released_component_routing(
    db: Session,
    bom: BOM,
    company_id: int,
    visited_part_ids: Optional[Set[int]] = None,
) -> bool:
    if visited_part_ids is None:
        visited_part_ids = {bom.part_id}

    items = db.query(BOMItem).filter(
        BOMItem.bom_id == bom.id,
        BOMItem.company_id == company_id,
    ).all()

    for item in items:
        if not item.component_part_id or item.component_part_id in visited_part_ids:
            continue

        routing_exists = db.query(Routing.id).filter(
            Routing.part_id == item.component_part_id,
            Routing.company_id == company_id,
            Routing.is_active == True,
            Routing.status == "released",
        ).first()
        if routing_exists:
            return True

        if (item.item_type or "").lower() == "buy":
            continue

        child_bom = _active_bom_for_part(db, item.component_part_id, company_id)
        if child_bom:
            next_visited = set(visited_part_ids)
            next_visited.add(item.component_part_id)
            if _bom_has_released_component_routing(db, child_bom, company_id, next_visited):
                return True

    return False


class SetupStep(BaseModel):
    key: str
    label: str
    status: str
    count: int = 0
    required_count: int = 1
    href: str
    reason: Optional[str] = None


class MasterDataIssue(BaseModel):
    key: str
    severity: str
    title: str
    detail: str
    count: int
    href: str


class SetupHealthResponse(BaseModel):
    progress: int
    counts: Dict[str, int]
    steps: List[SetupStep]
    issues: List[MasterDataIssue]


class ReadinessResponse(BaseModel):
    part_id: int
    ready: bool
    blockers: List[str]
    warnings: List[str]
    checks: Dict[str, str]


def _count(db: Session, query) -> int:
    return int(query.scalar() or 0)


def _step(key: str, label: str, count: int, href: str, reason: str, required_count: int = 1) -> SetupStep:
    return SetupStep(
        key=key,
        label=label,
        count=count,
        required_count=required_count,
        status="complete" if count >= required_count else "missing",
        href=href,
        reason=None if count >= required_count else reason,
    )


def _component_part_ids(db: Session, company_id: int):
    return (
        db.query(BOMItem.component_part_id)
        .join(BOM, BOM.id == BOMItem.bom_id)
        .filter(BOM.company_id == company_id, BOM.is_active == True)
    )


def _active_make_part_query(db: Session, company_id: int):
    return db.query(Part).filter(
        Part.company_id == company_id,
        Part.is_active == True,
        Part.is_deleted == False,
        Part.part_type.in_([PartType.MANUFACTURED, PartType.ASSEMBLY]),
    )


@router.get("/health", response_model=SetupHealthResponse)
def get_setup_health(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Return onboarding progress plus master-data issues that can block production."""
    active_make_parts = _active_make_part_query(db, company_id).subquery()
    component_part_ids = _component_part_ids(db, company_id)

    counts = {
        "users": _count(db, db.query(func.count(User.id)).filter(User.company_id == company_id, User.is_active == True)),
        "employees": _count(
            db,
            db.query(func.count(User.id)).filter(
                User.company_id == company_id,
                User.is_active == True,
                User.employee_id.isnot(None),
            ),
        ),
        "work_centers": _count(db, db.query(func.count(WorkCenter.id)).filter(WorkCenter.company_id == company_id, WorkCenter.is_active == True)),
        "parts": _count(db, db.query(func.count(Part.id)).filter(Part.company_id == company_id, Part.is_active == True, Part.is_deleted == False)),
        "make_parts": _count(db, db.query(func.count(active_make_parts.c.id))),
        "top_level_make_parts": _count(
            db,
            db.query(func.count(Part.id)).filter(
                Part.company_id == company_id,
                Part.is_active == True,
                Part.is_deleted == False,
                Part.part_type.in_([PartType.MANUFACTURED, PartType.ASSEMBLY]),
                ~Part.id.in_(component_part_ids),
            ),
        ),
        "boms": _count(db, db.query(func.count(BOM.id)).filter(BOM.company_id == company_id, BOM.is_active == True)),
        "released_boms": _count(db, db.query(func.count(BOM.id)).filter(BOM.company_id == company_id, BOM.is_active == True, BOM.status == "released")),
        "routings": _count(db, db.query(func.count(Routing.id)).filter(Routing.company_id == company_id, Routing.is_active == True)),
        "released_routings": _count(db, db.query(func.count(Routing.id)).filter(Routing.company_id == company_id, Routing.is_active == True, Routing.status == "released")),
        "work_orders": _count(db, db.query(func.count(WorkOrder.id)).filter(WorkOrder.company_id == company_id)),
        "customers": _count(db, db.query(func.count(Customer.id)).filter(Customer.company_id == company_id, Customer.is_active == True, Customer.is_deleted == False)),
        "vendors": _count(db, db.query(func.count(Vendor.id)).filter(Vendor.company_id == company_id, Vendor.is_active == True)),
        "inventory_items": _count(db, db.query(func.count(InventoryItem.id)).filter(InventoryItem.company_id == company_id, InventoryItem.is_active == True)),
    }

    steps = [
        _step("company", "Company account", 1, "/admin/settings", "Company account is not initialized."),
        _step("employees", "Employees imported", counts["employees"], "/import-center?type=employees", "Import or add operator employees."),
        _step("work_centers", "Work centers configured", counts["work_centers"], "/work-centers", "Create at least one active work center."),
        _step("parts", "Parts loaded", counts["parts"], "/import-center?type=parts", "Import or create parts."),
        _step("boms", "BOMs created", counts["boms"], "/import-center?type=boms", "Create or import BOMs for assemblies."),
        _step("routings", "Routings created", counts["routings"], "/routing", "Create or generate routings for make parts."),
        _step("work_orders", "First work order", counts["work_orders"], "/work-orders/new", "Create the first work order."),
    ]
    progress = round(sum(1 for step in steps if step.status == "complete") / len(steps) * 100)

    make_part_ids_with_bom = db.query(BOM.part_id).filter(BOM.company_id == company_id, BOM.is_active == True)
    make_part_ids_with_routing = db.query(Routing.part_id).filter(Routing.company_id == company_id, Routing.is_active == True)

    parts_without_bom = _count(
        db,
        db.query(func.count(Part.id)).filter(
            Part.company_id == company_id,
            Part.is_active == True,
            Part.is_deleted == False,
            Part.part_type == PartType.ASSEMBLY,
            ~Part.id.in_(make_part_ids_with_bom),
        ),
    )
    parts_without_routing = _count(
        db,
        db.query(func.count(Part.id)).filter(
            Part.company_id == company_id,
            Part.is_active == True,
            Part.is_deleted == False,
            Part.part_type.in_([PartType.MANUFACTURED, PartType.ASSEMBLY]),
            ~Part.id.in_(component_part_ids),
            ~Part.id.in_(make_part_ids_with_routing),
        ),
    )
    inactive_routing_wcs = _count(
        db,
        db.query(func.count(RoutingOperation.id))
        .join(Routing, Routing.id == RoutingOperation.routing_id)
        .join(WorkCenter, WorkCenter.id == RoutingOperation.work_center_id)
        .filter(
            Routing.company_id == company_id,
            Routing.is_active == True,
            RoutingOperation.is_active == True,
            or_(WorkCenter.company_id != company_id, WorkCenter.is_active == False),
        ),
    )
    inactive_bom_components = _count(
        db,
        db.query(func.count(BOMItem.id))
        .join(BOM, BOM.id == BOMItem.bom_id)
        .join(Part, Part.id == BOMItem.component_part_id)
        .filter(
            BOM.company_id == company_id,
            BOM.is_active == True,
            or_(Part.company_id != company_id, Part.is_active == False, Part.is_deleted == True),
        ),
    )
    draft_boms = counts["boms"] - counts["released_boms"]
    draft_routings = counts["routings"] - counts["released_routings"]

    issues = [
        MasterDataIssue(
            key="assemblies_without_bom",
            severity="high",
            title="Assemblies without BOMs",
            detail="Assemblies need active BOMs before clean work order release.",
            count=parts_without_bom,
            href="/parts",
        ),
        MasterDataIssue(
            key="top_level_parts_without_routing",
            severity="high",
            title="Top-level make parts without routings",
            detail="Manufactured top-level parts need routings; BOM components stay under assemblies.",
            count=parts_without_routing,
            href="/routing",
        ),
        MasterDataIssue(
            key="draft_boms",
            severity="medium",
            title="BOMs not released",
            detail="Draft BOMs can be reviewed but should be released before production.",
            count=max(draft_boms, 0),
            href="/bom",
        ),
        MasterDataIssue(
            key="draft_routings",
            severity="medium",
            title="Routings not released",
            detail="Draft routings should be released before production scheduling.",
            count=max(draft_routings, 0),
            href="/routing",
        ),
        MasterDataIssue(
            key="inactive_routing_work_centers",
            severity="high",
            title="Routing operations using inactive work centers",
            detail="Update routing operations assigned to inactive or cross-company work centers.",
            count=inactive_routing_wcs,
            href="/routing",
        ),
        MasterDataIssue(
            key="inactive_bom_components",
            severity="high",
            title="BOM lines with inactive components",
            detail="Replace or reactivate inactive component parts used in active BOMs.",
            count=inactive_bom_components,
            href="/bom",
        ),
    ]

    return SetupHealthResponse(
        progress=progress,
        counts=counts,
        steps=steps,
        issues=[issue for issue in issues if issue.count > 0],
    )


@router.get("/readiness/part/{part_id}", response_model=ReadinessResponse)
def get_part_readiness(
    part_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Return release-readiness checks for creating or releasing work against a part."""
    part = db.query(Part).filter(Part.id == part_id, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")

    blockers: List[str] = []
    warnings: List[str] = []
    checks: Dict[str, str] = {}

    routing = db.query(Routing).filter(Routing.part_id == part_id, Routing.company_id == company_id, Routing.is_active == True).first()
    bom = _active_bom_for_part(db, part_id, company_id)

    if part.part_type == PartType.MANUFACTURED:
        if not routing:
            blockers.append("No active routing exists for this make part.")
            checks["routing"] = "missing"
        elif routing.status != "released":
            warnings.append(f"Routing exists but is {routing.status}.")
            checks["routing"] = "draft"
        else:
            checks["routing"] = "ready"

    if part.part_type == PartType.ASSEMBLY:
        has_component_routing = bool(bom and _bom_has_released_component_routing(db, bom, company_id))
        if not routing:
            if has_component_routing:
                warnings.append("No assembly routing exists; work order will use released BOM component routings.")
                checks["routing"] = "component_routings_ready"
            else:
                blockers.append("No active routing exists for this make part.")
                checks["routing"] = "missing"
        elif routing.status != "released":
            warnings.append(f"Routing exists but is {routing.status}.")
            checks["routing"] = "draft"
        else:
            checks["routing"] = "ready"

    if part.part_type == PartType.ASSEMBLY:
        if not bom:
            blockers.append("No active BOM exists for this assembly.")
            checks["bom"] = "missing"
        elif bom.status != "released":
            warnings.append(f"BOM exists but is {bom.status}.")
            checks["bom"] = "draft"
        elif not bom.items:
            warnings.append("BOM has no component lines.")
            checks["bom"] = "empty"
        else:
            checks["bom"] = "ready"

    if routing:
        inactive_ops = (
            db.query(func.count(RoutingOperation.id))
            .join(WorkCenter, WorkCenter.id == RoutingOperation.work_center_id)
            .filter(
                RoutingOperation.routing_id == routing.id,
                RoutingOperation.is_active == True,
                WorkCenter.is_active == False,
            )
            .scalar()
            or 0
        )
        if inactive_ops:
            blockers.append(f"{inactive_ops} routing operation(s) use inactive work centers.")
            checks["work_centers"] = "blocked"
        else:
            checks["work_centers"] = "ready"

    open_wos = (
        db.query(func.count(WorkOrder.id))
        .filter(
            WorkOrder.part_id == part_id,
            WorkOrder.company_id == company_id,
            WorkOrder.status.in_([WorkOrderStatus.DRAFT, WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.ON_HOLD]),
        )
        .scalar()
        or 0
    )
    if open_wos:
        warnings.append(f"{open_wos} open work order(s) already exist for this part.")
    checks["open_work_orders"] = "present" if open_wos else "none"

    return ReadinessResponse(
        part_id=part_id,
        ready=len(blockers) == 0,
        blockers=blockers,
        warnings=warnings,
        checks=checks,
    )
