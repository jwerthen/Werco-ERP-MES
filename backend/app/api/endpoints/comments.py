"""Comments with @mentions, and watchers, on records (v1: work orders only).

Thin: validate, call ``app/services/comment_service.py``, commit once, return a schema. The
service owns the record-type registry, tenancy, the mention re-checks, auto-watch, audit and the
notification events.

Every WRITE (``POST``/``PATCH``/``DELETE /comments``, ``POST``/``DELETE /watchers``) is a person's:
it refuses ANY API token 403 (``API_TOKEN_COMMENT_WRITE``) in its first dependency, before the
body is validated or anything is read or written, and none of them is an MCP tool
(``app.mcp.catalog.COMMENT_WRITE_OPERATIONS``). The reads stay open to every credential the read
gates admit. A crew-station badge token never reaches this router (its path fence).

Route function names are MCP tool names (``app/mcp/naming.py``) and were chosen not to collide
with an existing family; keep them.
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user
from app.core.credentials import API_TOKEN_COMMENT_WRITE, is_api_token_credential
from app.db.database import get_db
from app.models.user import User
from app.schemas.comment import (
    ENTITY_TYPE_MAX_LENGTH,
    CommentCreate,
    CommentResponse,
    CommentUpdate,
    WatchRequest,
    WatchStateResponse,
)
from app.services import comment_service
from app.services.audit_service import AuditService

router = APIRouter()


def comment_writer(current_user: User = Depends(get_current_user)) -> User:
    """The signed-in PERSON for a comment / watcher write; 403 for any API token, whatever its role
    or scope. A dependency, so it answers before the request body is validated."""
    if is_api_token_credential(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=API_TOKEN_COMMENT_WRITE)
    return current_user


@router.get("/comments", response_model=List[CommentResponse])
def list_record_comments(
    entity_type: str = Query(..., min_length=1, max_length=ENTITY_TYPE_MAX_LENGTH, description="work_order"),
    entity_id: int = Query(..., gt=0, description="The record's id (the work order id)."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List the comments on a record, oldest first (the newest 500).

    Only ``entity_type=work_order`` is enabled. Each comment carries its author and the people it
    @mentions as ``{id, display_name}``, its timestamps, and whether the caller may edit it (the
    author only) or delete it (the author, an Admin or a Manager). Deleted comments are not
    listed. 404 when the work order does not exist in the active company or is deleted.
    """
    return comment_service.list_comments(
        db, company_id=company_id, actor=current_user, entity_type=entity_type, entity_id=entity_id
    )


@router.post("/comments", response_model=CommentResponse, status_code=status.HTTP_201_CREATED)
def create_record_comment(
    payload: CommentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(comment_writer),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Post a comment on a record (v1: a work order), optionally @mentioning people by user id.

    Anyone who can read the work order may comment. Each mentioned id must be an active person
    of the company who can read it (422 naming the refused ids). The author (when a member of the
    company, not a Platform Admin viewing it) and every mentioned person start watching the record.
    Mentioned people get a ``comment.mention`` notification and
    other watchers a ``comment.added`` one; neither carries the comment text. API tokens are
    refused (403): a comment is a person's own words.
    """
    comment = comment_service.create_comment(
        db,
        company_id=company_id,
        actor=current_user,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        body=payload.body,
        mentioned_user_ids=payload.mentioned_user_ids,
        audit=audit,
    )
    db.commit()
    return comment_service.load_comment_response(db, company_id=company_id, comment_id=comment.id, actor=current_user)


@router.patch("/comments/{comment_id}", response_model=CommentResponse)
def edit_record_comment(
    comment_id: int,
    payload: CommentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(comment_writer),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Edit your own comment: send the full new ``body`` and ``mentioned_user_ids``.

    Author only (403 for anyone else, Admins included). Only newly added people are notified.
    API tokens are refused (403).
    """
    comment = comment_service.update_comment(
        db,
        company_id=company_id,
        actor=current_user,
        comment_id=comment_id,
        body=payload.body,
        mentioned_user_ids=payload.mentioned_user_ids,
        audit=audit,
    )
    db.commit()
    return comment_service.load_comment_response(db, company_id=company_id, comment_id=comment.id, actor=current_user)


@router.delete("/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_record_comment(
    comment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(comment_writer),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Delete a comment (soft delete; it stops being listed). The author, an Admin or a Manager.

    Answers 204 with no body. API tokens are refused (403).
    """
    comment_service.delete_comment(db, company_id=company_id, actor=current_user, comment_id=comment_id, audit=audit)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/watchers", response_model=WatchStateResponse)
def get_record_watch_state(
    entity_type: str = Query(..., min_length=1, max_length=ENTITY_TYPE_MAX_LENGTH, description="work_order"),
    entity_id: int = Query(..., gt=0, description="The record's id (the work order id)."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Whether the caller watches a record (v1: a work order), and how many active people do.

    Watchers are notified of new comments on the record. Only members of the company are, so a
    caller viewing another company (a Platform Admin) is always ``watching: false``. 404 when the
    work order does not exist in the active company or is deleted.
    """
    return comment_service.watch_state(
        db, company_id=company_id, actor=current_user, entity_type=entity_type, entity_id=entity_id
    )


@router.post("/watchers", response_model=WatchStateResponse)
def watch_record(
    payload: WatchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(comment_writer),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Start watching a record (idempotent). API tokens are refused (403).

    409, writing nothing, when the caller is not a member of the active company (a Platform Admin
    viewing another company): notifications go to the company's members only.
    """
    state = comment_service.watch(
        db,
        company_id=company_id,
        actor=current_user,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        audit=audit,
    )
    db.commit()
    return state


@router.delete("/watchers", response_model=WatchStateResponse)
def unwatch_record(
    entity_type: str = Query(..., min_length=1, max_length=ENTITY_TYPE_MAX_LENGTH),
    entity_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(comment_writer),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Stop watching a record (idempotent). API tokens are refused (403)."""
    state = comment_service.unwatch(
        db, company_id=company_id, actor=current_user, entity_type=entity_type, entity_id=entity_id, audit=audit
    )
    db.commit()
    return state
