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
from app.models.user_workspace import TeamWorkspaceRecord, UserWorkspaceRecord
from app.schemas.user_workspace import TeamWorkspaceList, TeamWorkspaceWrite, WorkspaceResponse, WorkspaceWrite

router = APIRouter()
NAMESPACES = {"work-orders", "purchasing", "quality", "parts", "quotes", "inventory", "shipping"}


def require_workspace_access(db: Session, user: User, company_id: int, namespace: str):
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


def owner_query(db: Session, user: User, company_id: int, namespace: str, kind: str):
    require_workspace_access(db, user, company_id, namespace)
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


def can_manage_team(user: User) -> bool:
    return bool(
        not getattr(user, "_read_only_company_context", False)
        and (user.is_superuser or user.role in (UserRole.ADMIN, UserRole.MANAGER, UserRole.PLATFORM_ADMIN))
    )


def team_query(db: Session, user: User, company_id: int, namespace: str, *, write=False):
    require_workspace_access(db, user, company_id, namespace)
    if write and not can_manage_team(user):
        raise HTTPException(403, "Only managers and administrators can change team views")
    return db.query(TeamWorkspaceRecord).filter(
        TeamWorkspaceRecord.company_id == company_id,
        TeamWorkspaceRecord.namespace == namespace,
    )


@router.get("/team/{namespace}", response_model=TeamWorkspaceList)
def list_team_records(
    namespace: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    rows = team_query(db, user, company_id, namespace).order_by(TeamWorkspaceRecord.updated_at.desc()).all()
    return {"items": rows, "can_manage": can_manage_team(user)}


@router.put("/team/{namespace}/{key}", response_model=WorkspaceResponse)
def save_team_record(
    namespace: str,
    body: TeamWorkspaceWrite,
    key: str = Path(pattern=r"^[a-zA-Z0-9_-]{1,80}$"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    query = team_query(db, user, company_id, namespace, write=True)
    acquire_generator_lock(db, f"team-workspace:{namespace}", company_id)
    row = query.filter(TeamWorkspaceRecord.key == key).first()
    if row is None:
        if body.version != 0:
            raise HTTPException(409, "This team view was removed elsewhere. Reload before saving.")
        if query.count() >= 25:
            raise HTTPException(409, "This team workspace has 25 views. Remove one before adding another.")
        row = TeamWorkspaceRecord(
            company_id=company_id,
            namespace=namespace,
            key=key,
            name=body.name,
            data=body.data,
            version=1,
            updated_by=user.id,
        )
        db.add(row)
    else:
        updated = query.filter(TeamWorkspaceRecord.key == key, TeamWorkspaceRecord.version == body.version).update(
            {
                "name": body.name,
                "data": body.data,
                "version": body.version + 1,
                "updated_by": user.id,
                "updated_at": datetime.utcnow(),
            },
            synchronize_session=False,
        )
        if not updated:
            raise HTTPException(409, "This team view changed elsewhere. Reload before saving.")
    try:
        db.flush()
        db.refresh(row)
        response = WorkspaceResponse.model_validate(row)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "This team view changed elsewhere. Reload before saving.") from exc
    return response


@router.delete("/team/{namespace}/{key}", status_code=204)
def delete_team_record(
    namespace: str,
    key: str,
    version: int = Query(ge=1),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    query = team_query(db, user, company_id, namespace, write=True).filter(TeamWorkspaceRecord.key == key)
    if query.first() is None:
        return Response(status_code=204)
    if not query.filter(TeamWorkspaceRecord.version == version).delete(synchronize_session=False):
        raise HTTPException(409, "This team view changed elsewhere. Reload before removing it.")
    db.commit()
    return Response(status_code=204)
