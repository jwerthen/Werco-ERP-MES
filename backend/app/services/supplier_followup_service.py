"""Supplier evidence and current follow-up, independent of requested PO dates."""

from datetime import datetime, timezone

from fastapi import HTTPException

from app.models.purchasing import POStatus, PurchaseOrder
from app.models.role_permission import DEFAULT_ROLE_PERMISSIONS, RolePermission
from app.models.user import User, UserRole


def require_module_write(db, user, company_id, module, action):
    if getattr(user, '_read_only_company_context', False):
        raise HTTPException(403, "This company context is read-only")
    if user.is_superuser or user.role == UserRole.PLATFORM_ADMIN:
        return
    override = (
        db.query(RolePermission)
        .filter(RolePermission.company_id == company_id, RolePermission.role == user.role)
        .first()
    )
    permissions = set(override.permissions if override else DEFAULT_ROLE_PERMISSIONS.get(user.role, []))
    if not {f'{module}:view', f'{module}:{action}'}.issubset(permissions):
        raise HTTPException(403, "You no longer have permission for this workflow")


def update_supplier_confirmation(db, user, company_id, po_id, body, audit):
    require_module_write(db, user, company_id, 'purchasing', 'create')
    po = (
        db.query(PurchaseOrder)
        .filter(PurchaseOrder.id == po_id, PurchaseOrder.company_id == company_id, PurchaseOrder.is_deleted.is_(False))
        .with_for_update()
        .populate_existing()
        .first()
    )
    if not po:
        raise HTTPException(404, "Purchase order not found")
    if po.status not in (POStatus.SENT, POStatus.PARTIAL):
        raise HTTPException(409, "Supplier follow-up is available for issued, outstanding purchase orders")

    def utc_version(value):
        if value is None:
            return None
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value

    actual_version = utc_version(po.updated_at)
    expected_version = utc_version(body.expected_updated_at)
    if actual_version != expected_version:
        raise HTTPException(409, "This purchase order changed. Reload before recording the supplier response.")
    if body.follow_up_owner_id:
        owner = (
            db.query(User)
            .filter(User.id == body.follow_up_owner_id, User.company_id == company_id, User.is_active.is_(True))
            .first()
        )
        if not owner:
            raise HTTPException(422, "Choose an active follow-up owner in this company")
        override = (
            db.query(RolePermission)
            .filter(RolePermission.company_id == company_id, RolePermission.role == owner.role)
            .first()
        )
        permissions = set(override.permissions if override else DEFAULT_ROLE_PERMISSIONS.get(owner.role, []))
        if not owner.is_superuser and 'purchasing:view' not in permissions:
            raise HTTPException(422, "Follow-up owner must have purchasing access")
    fields = [
        'supplier_confirmed_date',
        'supplier_acknowledged_at',
        'supplier_acknowledged_by',
        'supplier_confirmation_reference',
        'supplier_confirmation_note',
        'follow_up_owner_id',
        'follow_up_due_date',
    ]
    old = {field: getattr(po, field) for field in fields}
    for field in fields:
        if field not in ('supplier_acknowledged_at', 'supplier_acknowledged_by'):
            setattr(po, field, getattr(body, field))
    po.supplier_acknowledged_at = (po.supplier_acknowledged_at or datetime.utcnow()) if body.acknowledged else None
    po.supplier_acknowledged_by = (po.supplier_acknowledged_by or user.id) if body.acknowledged else None
    po.updated_at = datetime.utcnow()
    audit.log_update(
        'purchase_order',
        po.id,
        po.po_number,
        old,
        {field: getattr(po, field) for field in fields},
        description='Recorded supplier confirmation or follow-up; requested dates unchanged',
    )
    db.commit()
    db.refresh(po)
    return po
