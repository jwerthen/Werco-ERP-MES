from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta
import json

from app.db.database import get_db
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.quote_config import (
    QuoteMaterial, QuoteMachine, QuoteFinish, QuoteSettings,
    LaborRate, OutsideService, SettingsAuditLog,
    MaterialCategory, MachineType, ProcessType, CostUnit
)
from app.api.deps import get_current_user, require_role
from app.schemas.admin_settings import (
    MaterialCreate, MaterialUpdate, MaterialResponse,
    MachineCreate, MachineUpdate, MachineResponse,
    FinishCreate, FinishUpdate, FinishResponse,
    LaborRateCreate, LaborRateUpdate, LaborRateResponse,
    WorkCenterRateUpdate, WorkCenterRateResponse,
    OutsideServiceCreate, OutsideServiceUpdate, OutsideServiceResponse,
    SettingUpdate, SettingResponse,
    AuditLogResponse, AuditLogWithUser
)

router = APIRouter()

# Admin-only dependency
admin_only = require_role([UserRole.ADMIN])


def log_change(
    db: Session,
    entity_type: str,
    entity_id: int,
    entity_name: str,
    action: str,
    user_id: int,
    field_changed: str = None,
    old_value: any = None,
    new_value: any = None,
    ip_address: str = None
):
    """Log a settings change for audit purposes"""
    audit = SettingsAuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        action=action,
        field_changed=field_changed,
        old_value=json.dumps(old_value) if old_value is not None else None,
        new_value=json.dumps(new_value) if new_value is not None else None,
        changed_by=user_id,
        ip_address=ip_address
    )
    db.add(audit)


def get_client_ip(request: Request) -> str:
    """Get client IP from request"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ============ MATERIALS ============

@router.get("/materials", response_model=List[MaterialResponse])
def list_materials(
    include_inactive: bool = False,
    category: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """List all materials"""
    query = db.query(QuoteMaterial)
    if not include_inactive:
        query = query.filter(QuoteMaterial.is_active == True)
    if category:
        query = query.filter(QuoteMaterial.category == category)
    return query.order_by(QuoteMaterial.name).all()


@router.post("/materials", response_model=MaterialResponse)
def create_material(
    data: MaterialCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Create a new material"""
    material = QuoteMaterial(**data.model_dump())
    db.add(material)
    db.commit()
    db.refresh(material)
    
    log_change(db, "material", material.id, material.name, "create",
               current_user.id, ip_address=get_client_ip(request))
    db.commit()
    
    return material


@router.put("/materials/{material_id}", response_model=MaterialResponse)
def update_material(
    material_id: int,
    data: MaterialUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Update a material"""
    material = db.query(QuoteMaterial).filter(QuoteMaterial.id == material_id).first()
    if not material:
        raise HTTPException(status_code=404, detail="Material not found")
    
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        old_value = getattr(material, field)
        if old_value != value:
            log_change(db, "material", material.id, material.name, "update",
                      current_user.id, field, old_value, value, get_client_ip(request))
        setattr(material, field, value)
    
    db.commit()
    db.refresh(material)
    return material


@router.delete("/materials/{material_id}")
def delete_material(
    material_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Soft-delete a material"""
    material = db.query(QuoteMaterial).filter(QuoteMaterial.id == material_id).first()
    if not material:
        raise HTTPException(status_code=404, detail="Material not found")
    
    material.is_active = False
    log_change(db, "material", material.id, material.name, "delete",
               current_user.id, ip_address=get_client_ip(request))
    db.commit()
    return {"status": "ok", "message": f"Material '{material.name}' deactivated"}


# ============ MACHINES ============

@router.get("/machines", response_model=List[MachineResponse])
def list_machines(
    include_inactive: bool = False,
    machine_type: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """List all machines"""
    query = db.query(QuoteMachine)
    if not include_inactive:
        query = query.filter(QuoteMachine.is_active == True)
    if machine_type:
        query = query.filter(QuoteMachine.machine_type == machine_type)
    return query.order_by(QuoteMachine.name).all()


@router.post("/machines", response_model=MachineResponse)
def create_machine(
    data: MachineCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Create a new machine"""
    machine = QuoteMachine(**data.model_dump())
    db.add(machine)
    db.commit()
    db.refresh(machine)
    
    log_change(db, "machine", machine.id, machine.name, "create",
               current_user.id, ip_address=get_client_ip(request))
    db.commit()
    
    return machine


@router.put("/machines/{machine_id}", response_model=MachineResponse)
def update_machine(
    machine_id: int,
    data: MachineUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Update a machine"""
    machine = db.query(QuoteMachine).filter(QuoteMachine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        old_value = getattr(machine, field)
        if old_value != value:
            log_change(db, "machine", machine.id, machine.name, "update",
                      current_user.id, field, old_value, value, get_client_ip(request))
        setattr(machine, field, value)
    
    db.commit()
    db.refresh(machine)
    return machine


@router.delete("/machines/{machine_id}")
def delete_machine(
    machine_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Soft-delete a machine"""
    machine = db.query(QuoteMachine).filter(QuoteMachine.id == machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    
    machine.is_active = False
    log_change(db, "machine", machine.id, machine.name, "delete",
               current_user.id, ip_address=get_client_ip(request))
    db.commit()
    return {"status": "ok", "message": f"Machine '{machine.name}' deactivated"}


# ============ FINISHES ============

@router.get("/finishes", response_model=List[FinishResponse])
def list_finishes(
    include_inactive: bool = False,
    category: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """List all finishes"""
    query = db.query(QuoteFinish)
    if not include_inactive:
        query = query.filter(QuoteFinish.is_active == True)
    if category:
        query = query.filter(QuoteFinish.category == category)
    return query.order_by(QuoteFinish.name).all()


@router.post("/finishes", response_model=FinishResponse)
def create_finish(
    data: FinishCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Create a new finish"""
    finish = QuoteFinish(**data.model_dump())
    db.add(finish)
    db.commit()
    db.refresh(finish)
    
    log_change(db, "finish", finish.id, finish.name, "create",
               current_user.id, ip_address=get_client_ip(request))
    db.commit()
    
    return finish


@router.put("/finishes/{finish_id}", response_model=FinishResponse)
def update_finish(
    finish_id: int,
    data: FinishUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Update a finish"""
    finish = db.query(QuoteFinish).filter(QuoteFinish.id == finish_id).first()
    if not finish:
        raise HTTPException(status_code=404, detail="Finish not found")
    
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        old_value = getattr(finish, field)
        if old_value != value:
            log_change(db, "finish", finish.id, finish.name, "update",
                      current_user.id, field, old_value, value, get_client_ip(request))
        setattr(finish, field, value)
    
    db.commit()
    db.refresh(finish)
    return finish


@router.delete("/finishes/{finish_id}")
def delete_finish(
    finish_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Soft-delete a finish"""
    finish = db.query(QuoteFinish).filter(QuoteFinish.id == finish_id).first()
    if not finish:
        raise HTTPException(status_code=404, detail="Finish not found")
    
    finish.is_active = False
    log_change(db, "finish", finish.id, finish.name, "delete",
               current_user.id, ip_address=get_client_ip(request))
    db.commit()
    return {"status": "ok", "message": f"Finish '{finish.name}' deactivated"}


# ============ LABOR RATES ============

@router.get("/labor-rates", response_model=List[LaborRateResponse])
def list_labor_rates(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """List all labor rates"""
    query = db.query(LaborRate)
    if not include_inactive:
        query = query.filter(LaborRate.is_active == True)
    return query.order_by(LaborRate.name).all()


@router.post("/labor-rates", response_model=LaborRateResponse)
def create_labor_rate(
    data: LaborRateCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Create a new labor rate"""
    labor_rate = LaborRate(**data.model_dump())
    db.add(labor_rate)
    db.commit()
    db.refresh(labor_rate)
    
    log_change(db, "labor_rate", labor_rate.id, labor_rate.name, "create",
               current_user.id, ip_address=get_client_ip(request))
    db.commit()
    
    return labor_rate


@router.put("/labor-rates/{rate_id}", response_model=LaborRateResponse)
def update_labor_rate(
    rate_id: int,
    data: LaborRateUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Update a labor rate"""
    labor_rate = db.query(LaborRate).filter(LaborRate.id == rate_id).first()
    if not labor_rate:
        raise HTTPException(status_code=404, detail="Labor rate not found")
    
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        old_value = getattr(labor_rate, field)
        if old_value != value:
            log_change(db, "labor_rate", labor_rate.id, labor_rate.name, "update",
                      current_user.id, field, old_value, value, get_client_ip(request))
        setattr(labor_rate, field, value)
    
    db.commit()
    db.refresh(labor_rate)
    return labor_rate


@router.delete("/labor-rates/{rate_id}")
def delete_labor_rate(
    rate_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Soft-delete a labor rate"""
    labor_rate = db.query(LaborRate).filter(LaborRate.id == rate_id).first()
    if not labor_rate:
        raise HTTPException(status_code=404, detail="Labor rate not found")
    
    labor_rate.is_active = False
    log_change(db, "labor_rate", labor_rate.id, labor_rate.name, "delete",
               current_user.id, ip_address=get_client_ip(request))
    db.commit()
    return {"status": "ok", "message": f"Labor rate '{labor_rate.name}' deactivated"}


# ============ WORK CENTER RATES ============

@router.get("/work-center-rates", response_model=List[WorkCenterRateResponse])
def list_work_center_rates(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """List all work centers with their rates"""
    query = db.query(WorkCenter)
    if not include_inactive:
        query = query.filter(WorkCenter.is_active == True)
    return query.order_by(WorkCenter.name).all()


@router.put("/work-center-rates/{work_center_id}", response_model=WorkCenterRateResponse)
def update_work_center_rate(
    work_center_id: int,
    data: WorkCenterRateUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Update a work center's hourly rate"""
    wc = db.query(WorkCenter).filter(WorkCenter.id == work_center_id).first()
    if not wc:
        raise HTTPException(status_code=404, detail="Work center not found")
    
    old_rate = wc.hourly_rate
    wc.hourly_rate = data.hourly_rate
    
    log_change(db, "work_center_rate", wc.id, wc.name, "update",
               current_user.id, "hourly_rate", old_rate, data.hourly_rate, get_client_ip(request))
    
    db.commit()
    db.refresh(wc)
    return wc


# ============ OUTSIDE SERVICES ============

@router.get("/outside-services", response_model=List[OutsideServiceResponse])
def list_outside_services(
    include_inactive: bool = False,
    process_type: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """List all outside services"""
    query = db.query(OutsideService)
    if not include_inactive:
        query = query.filter(OutsideService.is_active == True)
    if process_type:
        query = query.filter(OutsideService.process_type == process_type)
    return query.order_by(OutsideService.name).all()


@router.post("/outside-services", response_model=OutsideServiceResponse)
def create_outside_service(
    data: OutsideServiceCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Create a new outside service"""
    service = OutsideService(**data.model_dump())
    db.add(service)
    db.commit()
    db.refresh(service)
    
    log_change(db, "outside_service", service.id, service.name, "create",
               current_user.id, ip_address=get_client_ip(request))
    db.commit()
    
    return service


@router.put("/outside-services/{service_id}", response_model=OutsideServiceResponse)
def update_outside_service(
    service_id: int,
    data: OutsideServiceUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Update an outside service"""
    service = db.query(OutsideService).filter(OutsideService.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Outside service not found")
    
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        old_value = getattr(service, field)
        if old_value != value:
            log_change(db, "outside_service", service.id, service.name, "update",
                      current_user.id, field, old_value, value, get_client_ip(request))
        setattr(service, field, value)
    
    db.commit()
    db.refresh(service)
    return service


@router.delete("/outside-services/{service_id}")
def delete_outside_service(
    service_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Soft-delete an outside service"""
    service = db.query(OutsideService).filter(OutsideService.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Outside service not found")
    
    service.is_active = False
    log_change(db, "outside_service", service.id, service.name, "delete",
               current_user.id, ip_address=get_client_ip(request))
    db.commit()
    return {"status": "ok", "message": f"Outside service '{service.name}' deactivated"}


# ============ OVERHEAD/MARKUP SETTINGS ============

@router.get("/overhead")
def get_overhead_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Get all overhead/markup settings"""
    settings = db.query(QuoteSettings).all()
    result = {}
    for s in settings:
        result[s.setting_key] = {
            "value": s.setting_value,
            "type": s.setting_type,
            "description": s.description,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None
        }
    return result


@router.put("/overhead/{key}")
def update_overhead_setting(
    key: str,
    data: SettingUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Update an overhead/markup setting"""
    setting = db.query(QuoteSettings).filter(QuoteSettings.setting_key == key).first()
    
    old_value = setting.setting_value if setting else None
    
    if setting:
        setting.setting_value = data.value
        setting.setting_type = data.setting_type
        if data.description:
            setting.description = data.description
    else:
        setting = QuoteSettings(
            setting_key=key,
            setting_value=data.value,
            setting_type=data.setting_type,
            description=data.description
        )
        db.add(setting)
    
    log_change(db, "overhead", None, key, "update" if old_value else "create",
               current_user.id, "setting_value", old_value, data.value, get_client_ip(request))
    
    db.commit()
    return {"status": "ok", "key": key, "value": data.value}


# ============ AUDIT LOG ============

@router.get("/audit-log", response_model=List[AuditLogWithUser])
def get_audit_log(
    entity_type: Optional[str] = None,
    days: int = 30,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Get audit log entries (up to 1 year history)"""
    cutoff_date = datetime.utcnow() - timedelta(days=min(days, 365))
    
    query = db.query(
        SettingsAuditLog,
        User.first_name,
        User.last_name
    ).outerjoin(User, SettingsAuditLog.changed_by == User.id)
    
    query = query.filter(SettingsAuditLog.changed_at >= cutoff_date)
    
    if entity_type:
        query = query.filter(SettingsAuditLog.entity_type == entity_type)
    
    results = query.order_by(SettingsAuditLog.changed_at.desc()).limit(limit).all()
    
    response = []
    for audit, first_name, last_name in results:
        item = AuditLogWithUser(
            id=audit.id,
            entity_type=audit.entity_type,
            entity_id=audit.entity_id,
            entity_name=audit.entity_name,
            action=audit.action,
            field_changed=audit.field_changed,
            old_value=audit.old_value,
            new_value=audit.new_value,
            changed_by=audit.changed_by,
            changed_at=audit.changed_at,
            user_name=f"{first_name} {last_name}" if first_name else None
        )
        response.append(item)
    
    return response


# ============ SEED DEFAULT DATA ============

@router.post("/seed-labor-rates")
def seed_labor_rates(
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Seed default labor rates"""
    defaults = [
        ("General Shop Labor", 45.00, "Standard shop floor labor rate"),
        ("Welder", 55.00, "Certified welders"),
        ("CNC Machinist", 50.00, "CNC machine operators"),
        ("Assembler", 40.00, "Assembly technicians"),
        ("Painter/Coater", 45.00, "Paint and powder coat operators"),
        ("Quality Inspector", 48.00, "QC inspection labor"),
        ("Engineer", 85.00, "Engineering support"),
    ]
    
    created = 0
    for name, rate, desc in defaults:
        existing = db.query(LaborRate).filter(LaborRate.name == name).first()
        if not existing:
            db.add(LaborRate(name=name, rate_per_hour=rate, description=desc))
            created += 1
    
    db.commit()
    return {"status": "ok", "created": created}


@router.post("/seed-database")
async def seed_database(
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Seed database with initial data. Requires admin authentication."""
    
    from app.core.security import get_password_hash
    
    # Check if already seeded
    if db.query(User).first():
        return {"status": "already_seeded", "message": "Database already has users"}
    
    # Create admin user
    admin = User(
        employee_id="EMP001",
        email="admin@werco.com",
        hashed_password=get_password_hash("admin123"),
        first_name="System",
        last_name="Administrator",
        role=UserRole.ADMIN,
        department="IT",
        is_superuser=True
    )
    db.add(admin)
    
    # Create sample users
    users_data = [
        ("EMP002", "jsmith@werco.com", "John", "Smith", UserRole.MANAGER, "Production"),
        ("EMP003", "mjohnson@werco.com", "Mary", "Johnson", UserRole.SUPERVISOR, "Fabrication"),
        ("EMP004", "bwilliams@werco.com", "Bob", "Williams", UserRole.OPERATOR, "CNC"),
        ("EMP005", "sjones@werco.com", "Sarah", "Jones", UserRole.QUALITY, "Quality"),
    ]
    
    for emp_id, email, first, last, role, dept in users_data:
        user = User(
            employee_id=emp_id,
            email=email,
            hashed_password=get_password_hash("password123"),
            first_name=first,
            last_name=last,
            role=role,
            department=dept
        )
        db.add(user)
    
    db.commit()
    return {"status": "success", "message": "Database seeded with admin and sample users"}

@router.post("/seed-outside-services")
def seed_outside_services(
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Seed default outside services"""
    defaults = [
        ("Heat Treat - Stress Relieve", None, ProcessType.HEAT_TREAT, 3.50, CostUnit.PER_LB, 25.00, 5),
        ("Heat Treat - Harden", None, ProcessType.HEAT_TREAT, 5.00, CostUnit.PER_LB, 35.00, 7),
        ("Anodize Type II", None, ProcessType.PLATING, 6.00, CostUnit.PER_SQFT, 35.00, 5),
        ("Anodize Type III", None, ProcessType.PLATING, 15.00, CostUnit.PER_SQFT, 75.00, 7),
        ("Zinc Plating", None, ProcessType.PLATING, 4.00, CostUnit.PER_SQFT, 25.00, 3),
        ("Nickel Plating", None, ProcessType.PLATING, 10.00, CostUnit.PER_SQFT, 50.00, 5),
        ("NDT - Dye Penetrant", None, ProcessType.TESTING, 25.00, CostUnit.PER_PART, 50.00, 2),
        ("NDT - Mag Particle", None, ProcessType.TESTING, 35.00, CostUnit.PER_PART, 75.00, 3),
        ("Chem Film", None, ProcessType.COATING, 3.00, CostUnit.PER_SQFT, 20.00, 2),
        ("Passivation", None, ProcessType.COATING, 2.00, CostUnit.PER_PART, 15.00, 2),
    ]
    
    created = 0
    for name, vendor, ptype, cost, unit, minimum, days in defaults:
        existing = db.query(OutsideService).filter(OutsideService.name == name).first()
        if not existing:
            db.add(OutsideService(
                name=name,
                vendor_name=vendor,
                process_type=ptype,
                default_cost=cost,
                cost_unit=unit,
                minimum_charge=minimum,
                typical_lead_days=days
            ))
            created += 1
    
    db.commit()
    return {"status": "ok", "created": created}


# ============ ROLE PERMISSIONS ============

from app.models.role_permission import (
    RolePermission, DEFAULT_ROLE_PERMISSIONS, ALL_PERMISSIONS, PERMISSION_CATEGORIES
)

@router.get("/role-permissions")
def get_all_role_permissions(
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """
    Get permissions for all roles.
    Returns stored custom permissions or defaults if not customized.
    """
    stored = db.query(RolePermission).all()
    stored_map = {rp.role: rp.permissions for rp in stored}
    
    result = {}
    for role in UserRole:
        if role in stored_map:
            result[role.value] = stored_map[role]
        else:
            result[role.value] = DEFAULT_ROLE_PERMISSIONS.get(role, [])
    
    return {
        "role_permissions": result,
        "all_permissions": ALL_PERMISSIONS,
        "permission_categories": PERMISSION_CATEGORIES,
        "roles": [{"value": r.value, "label": r.value.title()} for r in UserRole]
    }


@router.get("/role-permissions/{role}")
def get_role_permissions(
    role: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Get permissions for a specific role"""
    try:
        user_role = UserRole(role)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid role: {role}")
    
    stored = db.query(RolePermission).filter(RolePermission.role == user_role).first()
    
    if stored:
        return {"role": role, "permissions": stored.permissions, "is_customized": True}
    else:
        return {
            "role": role,
            "permissions": DEFAULT_ROLE_PERMISSIONS.get(user_role, []),
            "is_customized": False
        }


@router.put("/role-permissions/{role}")
def update_role_permissions(
    role: str,
    request: Request,
    permissions: list[str],
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Update permissions for a specific role"""
    try:
        user_role = UserRole(role)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid role: {role}")
    
    # Validate permissions
    invalid = [p for p in permissions if p not in ALL_PERMISSIONS]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid permissions: {invalid}")
    
    # Get or create role permission record
    stored = db.query(RolePermission).filter(RolePermission.role == user_role).first()
    
    old_permissions = stored.permissions if stored else DEFAULT_ROLE_PERMISSIONS.get(user_role, [])
    
    if stored:
        stored.permissions = permissions
        stored.updated_by = current_user.id
    else:
        stored = RolePermission(
            role=user_role,
            permissions=permissions,
            updated_by=current_user.id
        )
        db.add(stored)
    
    # Log the change
    log_change(
        db, "role_permission", stored.id if stored.id else 0, role,
        "update", current_user.id,
        field_changed="permissions",
        old_value=old_permissions,
        new_value=permissions,
        ip_address=get_client_ip(request)
    )
    
    db.commit()
    db.refresh(stored)
    
    return {"role": role, "permissions": stored.permissions, "is_customized": True}


@router.post("/role-permissions/{role}/reset")
def reset_role_permissions(
    role: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(admin_only)
):
    """Reset a role's permissions to defaults"""
    try:
        user_role = UserRole(role)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid role: {role}")
    
    stored = db.query(RolePermission).filter(RolePermission.role == user_role).first()
    
    if stored:
        old_permissions = stored.permissions
        db.delete(stored)
        
        log_change(
            db, "role_permission", stored.id, role,
            "reset", current_user.id,
            field_changed="permissions",
            old_value=old_permissions,
            new_value=DEFAULT_ROLE_PERMISSIONS.get(user_role, []),
            ip_address=get_client_ip(request)
        )
        
        db.commit()
    
    return {
        "role": role,
        "permissions": DEFAULT_ROLE_PERMISSIONS.get(user_role, []),
        "is_customized": False
    }
