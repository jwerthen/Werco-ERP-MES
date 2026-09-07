from datetime import datetime
from typing import List, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, get_current_user
from app.db.database import get_db
from app.db.locks import acquire_generator_lock
from app.models.role_permission import DEFAULT_ROLE_PERMISSIONS, RolePermission
from app.models.user import User, UserRole
from app.models.user_workspace import UserWorkspaceRecord
from app.schemas.user_workspace import WorkspaceResponse, WorkspaceWrite

router = APIRouter()
NAMESPACES = {"work-orders", "purchasing", "quality", "parts", "quotes"}


def owner_query(db: Session, user: User, company_id: int, namespace: str, kind: str):
    if namespace not in NAMESPACES:
        raise HTTPException(404, "Workspace not found")
    # Saved payloads may contain customer details. Re-check current module
    # access so a revoked permission also revokes access to old private drafts.
    if namespace != "quotes" and not user.is_superuser and user.role != UserRole.PLATFORM_ADMIN:
        override = (
            db.query(RolePermission)
            .filter(
                RolePermission.company_id == company_id,
                RolePermission.role == user.role,
            )
            .first()
        )
        permissions = set(override.permissions if override else DEFAULT_ROLE_PERMISSIONS.get(user.role, []))
        module = {"work-orders": "work_orders"}.get(namespace, namespace)
        if f"{module}:view" not in permissions:
            raise HTTPException(403, "You no longer have access to this workspace")
    return db.query(UserWorkspaceRecord).filter(
        UserWorkspaceRecord.company_id == company_id,
        UserWorkspaceRecord.user_id == user.id,
        UserWorkspaceRecord.namespace == namespace,
        UserWorkspaceRecord.kind == kind,
    )


@router.get("/{namespace}", response_model=List[WorkspaceResponse])
def list_records(
    namespace: str,
    kind: Literal["view", "draft"] = "view",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    return owner_query(db, user, company_id, namespace, kind).order_by(UserWorkspaceRecord.updated_at.desc()).all()


@router.put("/{namespace}/{key}", response_model=WorkspaceResponse)
def save_record(
    namespace: str,
    body: WorkspaceWrite,
    key: str = Path(pattern=r"^[a-zA-Z0-9_-]{1,80}$"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    query = owner_query(db, user, company_id, namespace, body.kind)
    # Serialize creates for this owner's namespace so the quota cannot race.
    acquire_generator_lock(db, f"workspace:{user.id}:{namespace}:{body.kind}", company_id)
    row = query.filter(UserWorkspaceRecord.key == key).first()
    if row is None:
        if body.version != 0:
            raise HTTPException(409, "This saved item was removed elsewhere. Reload before saving.")
        if query.count() >= 25:
            raise HTTPException(
                409,
                "This workspace has 25 saved items. Remove one before adding another.",
            )
        row = UserWorkspaceRecord(
            company_id=company_id,
            user_id=user.id,
            namespace=namespace,
            kind=body.kind,
            key=key,
            name=body.name,
            data=body.data,
            version=1,
        )
        db.add(row)
    else:
        updated = query.filter(UserWorkspaceRecord.key == key, UserWorkspaceRecord.version == body.version).update(
            {
                "name": body.name,
                "data": body.data,
                "version": body.version + 1,
                "updated_at": datetime.utcnow(),
            },
            synchronize_session=False,
        )
        if not updated:
            raise HTTPException(409, "This saved item changed in another tab. Reload before saving.")
    try:
        db.flush()
        db.refresh(row)
        response = WorkspaceResponse.model_validate(row)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "This saved item changed elsewhere. Reload before saving.") from exc
    return response


@router.delete("/{namespace}/{key}", status_code=204)
def delete_record(
    namespace: str,
    key: str,
    kind: Literal["view", "draft"] = "view",
    version: int = Query(ge=1),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    query = owner_query(db, user, company_id, namespace, kind).filter(UserWorkspaceRecord.key == key)
    if query.first() is None:
        return Response(status_code=204)
    if not query.filter(UserWorkspaceRecord.version == version).delete(synchronize_session=False):
        raise HTTPException(409, "This saved item changed elsewhere. Reload before removing it.")
    db.commit()
    return Response(status_code=204)
