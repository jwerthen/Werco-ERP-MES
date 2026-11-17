"""Comments with @mentions, and record watchers (docs/NOTIFICATIONS_PLAN.md section 3.6).

The tables are generic -- a comment or a watcher names its record as ``(entity_type,
entity_id)`` with no foreign key (``app/models/comment.py``) -- and this module is the ONE place
that decides which record types are live and who may see them: :data:`COMMENTABLE_TYPES`. v1
enables work orders only (owner decision, 2026-11-16); any other ``entity_type`` is a 422.
Adding a type later is a registry entry (a tenant-scoped live resolver, a ``can_view`` that
mirrors that domain's READ RBAC, a label and the notification identifier), never a migration.

The rules every verb here holds:

* **Tenant isolation.** A record is resolved through ``tenant_query`` against the active company
  and must be live (``is_deleted == False``); absent, foreign and soft-deleted records all answer
  the same 404. Entity ids are global integers, so a bare id is never trusted. A comment is
  resolved the same way and then RE-RESOLVES its record, so a comment on a since-deleted work
  order is a 404 on every verb.
* **People only.** The routes refuse every API token before calling in here
  (``app.core.credentials.API_TOKEN_COMMENT_WRITE``). The service functions are also the contract
  for a caller that acts on a person's behalf without the routes (the planned Hank ``add_note``
  task, not on this branch): such a caller would call :func:`create_comment` directly and could
  tag its evidence with ``audit_extra`` (e.g. ``{"via": "hank"}``).
* **Mentions are structured.** The caller sends user ids; each is re-checked here (same company,
  active, may view the record). A refusal names the ids only -- never a name, so a foreign id
  cannot be used to learn who it belongs to. Names are never parsed out of the text.
* **Audit.** Create / edit / delete write ``log_create`` / ``log_update`` / ``log_delete
  (soft_delete=True)`` on resource type ``comment``; watch / unwatch write ``log_create`` /
  ``log_delete`` on ``entity_watcher``. All are best-effort through the caller's
  ``AuditService``. :func:`create_comment` makes exactly ONE audit call, ``audit.log_create``, and
  nothing else: a caller that needs a required audit (the planned Hank ``add_note`` task) can
  pass a wrapper whose ``log_create`` is required (the ``hank_task_service._RequiredCreateAudit``
  shape), and that one call then decides the whole write. Auto-watches made by a comment are
  recorded on the comment's own audit row (``extra_data.auto_watched_user_ids``), not as rows of
  their own.
* **Flush, never commit.** Callers commit (one commit per request).
* **Every row lock, THEN the audit chain.** ``AuditService`` takes the GLOBAL audit-chain lock
  (``pg_advisory_xact_lock``, every tenant, held to commit) at a transaction's first audit row, so
  each write verb here takes all of its row locks first and writes its audit row LAST -- the order
  the floor's SFI-1 verbs hold (operations -> work order -> audit chain). A verb that waited on a
  row while holding the chain would stall every audited write in every company, and against a
  work-order-first writer about to write its own audit row it is a deadlock (40P01). Per verb:

  - create: the record row ``FOR KEY SHARE`` (``resolve_target(lock=True)``) -> the comment and
    mention inserts -> the auto-watch inserts (ascending user id) -> the notification events ->
    ``audit.log_create``;
  - edit: the comment row ``FOR UPDATE`` -> the record row ``FOR KEY SHARE`` -> the mention
    deletes / inserts and the comment ``UPDATE`` -> the auto-watch inserts (ascending user id) ->
    the events -> ``audit.log_update``;
  - delete: the comment row ``FOR UPDATE`` -> its soft-delete ``UPDATE`` -> ``audit.log_delete``
    (it inserts nothing that references the record, so it does not lock it);
  - watch: the watcher insert -> ``audit.log_create``; unwatch: the watcher ``DELETE`` ->
    ``audit.log_delete``.

  ``FOR KEY SHARE`` is exactly the lock the events' ``work_order_id`` foreign-key check takes;
  taking it up front makes it the verb's first lock on the work order instead of one taken
  mid-transaction (``comments`` has no foreign key to the record, so nothing else would). It
  conflicts only with ``FOR UPDATE``, a delete or a key change -- never with an ordinary header
  ``UPDATE`` -- so a comment queues only behind a work-order-first writer that holds ``FOR UPDATE``
  (an office operation completion, say), and it queues there holding nothing that writer needs.
  Comment rows are locked only by these verbs, always before the record, so the edit's
  comment -> record order meets no reverse order. Auto-watch inserts go in ascending user id so
  two first comments that auto-watch the same two people (X mentions Y while Y mentions X) take
  the unique-index entries in the same order: the later one waits and then loses cleanly
  (``_insert_watcher``'s ``IntegrityError``) instead of the two deadlocking -- a 40P01 is an
  ``OperationalError`` that savepoint does not catch, which would lose the comment with a 409.
* **Soft delete.** A comment is soft-deleted (``Comment.soft_delete``), never removed. Mention
  rows and watcher rows are not ``SoftDeleteMixin``: an edit that drops a mention PHYSICALLY
  deletes that mention row (the edit's audit row keeps the old and new mention sets, so nothing
  is lost), and unwatch physically deletes the watcher row (a watch is a preference, not a record).

Notifications (``comment.mention`` / ``comment.added`` in ``notification_catalog``) carry NO
comment text: who (``comment_author``, the author's name snapshotted at write time), where (the
record identifier, ``work_order_number``) and the link. See :func:`_emit_comment_events`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from fastapi import HTTPException, status
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query, Session, selectinload

from app.db.tenant_filter import tenant_query
from app.models.comment import Comment, CommentMention, EntityWatcher
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.schemas.comment import (
    CommentPerson,
    CommentResponse,
    MentionableUser,
    WatchStateResponse,
    normalize_comment_body,
    normalize_mention_ids,
)
from app.services.operational_event_service import OperationalEventService

logger = logging.getLogger(__name__)

COMMENT_RESOURCE_TYPE = "comment"
WATCHER_RESOURCE_TYPE = "entity_watcher"

# OperationalEvent types (wired in ``notification_catalog``: ``comment.mention`` and
# ``comment.added``). Being catalog source types makes both server-only automatically
# (``operational_events.SERVER_ONLY_EVENT_TYPES``): a client cannot post one.
COMMENT_MENTIONED_EVENT_TYPE = "comment_mentioned"
COMMENT_ADDED_EVENT_TYPE = "comment_added"
COMMENT_EVENT_SOURCE_MODULE = "comments"
# The events' ``entity`` is the COMMENT, not the record: notification delivery dedups for five
# minutes on (event_key, related_type, related_id, user, channel) with ``related_*`` taken from
# the event's entity, so a work-order entity would silently drop a second comment (or a second
# mention) on the same work order inside that window. The record is in ``work_order_id`` (which
# also makes the link) and in the payload.
COMMENT_EVENT_ENTITY_TYPE = "comment"

# The newest N comments of a thread are returned (oldest first). A thread longer than this is
# truncated from the old end; there is no pagination in v1.
COMMENT_LIST_LIMIT = 500

COMMENT_NOT_FOUND_DETAIL = "Comment not found"
EDIT_FORBIDDEN_DETAIL = "Only the person who wrote a comment can edit it."
DELETE_FORBIDDEN_DETAIL = "Only the person who wrote a comment, an Admin or a Manager can delete it."

# Roles that may delete anyone's comment (moderation). An Admin or Manager can REMOVE a comment,
# but never rewrite one: an edit is the author's alone.
_MODERATOR_ROLES = (UserRole.ADMIN, UserRole.MANAGER)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# The record-type registry
# ---------------------------------------------------------------------------


class RecordResolver(Protocol):
    """``resolve(db, company_id, entity_id, *, lock=False)`` -- see :class:`CommentableType`."""

    def __call__(self, db: Session, company_id: int, entity_id: int, *, lock: bool = False) -> Optional[Any]: ...


@dataclass(frozen=True)
class CommentableType:
    """One record type that carries a comment thread.

    ``resolve(db, company_id, entity_id, *, lock=False)`` returns the LIVE record in that company
    or ``None`` (absent, foreign and soft-deleted alike); ``lock=True`` reads it ``FOR KEY SHARE``,
    the lock a row referencing it (a notification event's foreign key) takes, so a write verb takes
    that lock FIRST rather than mid-transaction (see the module docstring's lock order).
    ``can_view(user)`` mirrors the domain's READ RBAC for a user already known to be an active
    member of the company; it gates the caller, every mention, and every notification recipient.
    ``notification_fields(record)`` returns the ``work_order_id`` for the event (the deep link) and
    the identifier payload keys that title the notification (``notification_dispatch._IDENTIFIER_KEYS``).
    """

    entity_type: str
    label: str
    not_found_detail: str
    resolve: RecordResolver
    can_view: Callable[[User], bool]
    identifier: Callable[[Any], str]
    notification_fields: Callable[[Any], Tuple[Optional[int], Dict[str, Any]]]


def _live_work_order_query(db: Session, company_id: int, entity_id: int, *, lock: bool = False) -> Query:
    query = tenant_query(db, WorkOrder, company_id).filter(WorkOrder.id == entity_id, WorkOrder.is_deleted.is_(False))
    if lock:
        # FOR KEY SHARE needs BOTH flags: ``key_share=True`` alone renders FOR NO KEY UPDATE, which
        # would queue a comment behind every work-order header save. SQLite renders no lock clause.
        query = query.with_for_update(read=True, key_share=True)
    return query


def _resolve_live_work_order(
    db: Session, company_id: int, entity_id: int, *, lock: bool = False
) -> Optional[WorkOrder]:
    return _live_work_order_query(db, company_id, entity_id, lock=lock).first()


def _work_order_viewable(user: User) -> bool:
    """Work-order READ access is every signed-in member of the company: ``GET /work-orders/{id}``
    depends only on ``get_current_user`` + ``get_current_company_id`` (all seven tenant roles,
    VIEWER included). Company membership and ``is_active`` are checked by the caller."""
    return True


def _work_order_notification_fields(record: WorkOrder) -> Tuple[Optional[int], Dict[str, Any]]:
    return record.id, {"work_order_number": record.work_order_number}


COMMENTABLE_TYPES: Dict[str, CommentableType] = {
    "work_order": CommentableType(
        entity_type="work_order",
        label="work order",
        not_found_detail="Work order not found",
        resolve=_resolve_live_work_order,
        can_view=_work_order_viewable,
        identifier=lambda record: record.work_order_number,
        notification_fields=_work_order_notification_fields,
    ),
}


def commentable_type(entity_type: str) -> CommentableType:
    """The registry entry for ``entity_type``; 422 for a type comments are not enabled on."""
    spec = COMMENTABLE_TYPES.get(entity_type) if isinstance(entity_type, str) else None
    if spec is None:
        enabled = ", ".join(sorted(COMMENTABLE_TYPES))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Comments are not available on this kind of record. Enabled entity_type values: {enabled}.",
        )
    return spec


def _bypasses_role_gates(user: User) -> bool:
    """``require_role`` waves superusers and platform admins through every role gate."""
    return bool(getattr(user, "is_superuser", False)) or user.role == UserRole.PLATFORM_ADMIN


def _assert_can_view(spec: CommentableType, actor: User) -> None:
    if _bypasses_role_gates(actor) or spec.can_view(actor):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"You don't have access to comments on this {spec.label}.",
    )


def resolve_target(
    db: Session, *, company_id: int, actor: User, entity_type: str, entity_id: int, lock: bool = False
) -> Tuple[CommentableType, Any]:
    """The enabled type and the LIVE record, tenant-scoped; 422 / 403 / 404.

    ``lock=True`` (a write that will insert a row referencing the record) reads the record
    ``FOR KEY SHARE`` -- call it before any other write, so it is the verb's first lock.
    """
    spec = commentable_type(entity_type)
    _assert_can_view(spec, actor)
    record = spec.resolve(db, company_id, entity_id, lock=lock)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=spec.not_found_detail)
    return spec, record


def _live_comment_or_404(
    db: Session, company_id: int, comment_id: int, *, lock: bool = False, lock_record: bool = False
) -> Tuple[Comment, CommentableType, Any]:
    """A live comment of this company AND its live record.

    Deliberate non-users of the ``is_deleted`` filter: none -- a deleted comment has no verb in
    v1 (no restore), so every by-id read goes through here. The row lock (edit / delete) keeps
    two concurrent edits of one comment from racing on its mention set. ``lock_record`` (the edit,
    which may emit an event referencing the record) then reads the record ``FOR KEY SHARE``:
    comment row first, record second, both before any write (module docstring, lock order).
    """
    query = tenant_query(db, Comment, company_id).filter(Comment.id == comment_id, Comment.is_deleted.is_(False))
    if lock:
        query = query.with_for_update()
    comment = query.first()
    if comment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=COMMENT_NOT_FOUND_DETAIL)
    spec = COMMENTABLE_TYPES.get(comment.entity_type)
    if spec is None:  # a type that was enabled once and is not now: nothing may act on it
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=COMMENT_NOT_FOUND_DETAIL)
    record = spec.resolve(db, company_id, comment.entity_id, lock=lock_record)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=spec.not_found_detail)
    return comment, spec, record


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _clean_body(body: str) -> str:
    try:
        return normalize_comment_body(body)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


def _clean_mention_ids(mentioned_user_ids: Optional[Sequence[int]]) -> List[int]:
    try:
        return normalize_mention_ids(mentioned_user_ids)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


def _mentionable_users(db: Session, company_id: int, spec: CommentableType, user_ids: Sequence[int]) -> List[User]:
    """The users behind ``user_ids`` (same order), each re-checked; 422 naming any refused id.

    Same company, active, and allowed to view the record. The refusal names ids only: a foreign
    or inactive id must not leak whose it is.
    """
    if not user_ids:
        return []
    candidates = tenant_query(db, User, company_id).filter(User.id.in_(list(user_ids)), User.is_active.is_(True)).all()
    allowed = {user.id: user for user in candidates if spec.can_view(user)}
    refused = [user_id for user_id in user_ids if user_id not in allowed]
    if refused:
        named = ", ".join(f"user {user_id}" for user_id in refused)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"These people can't be mentioned on this {spec.label}: {named}.",
        )
    return [allowed[user_id] for user_id in user_ids]


# ---------------------------------------------------------------------------
# Watchers
# ---------------------------------------------------------------------------


def _watcher_rows(
    db: Session, company_id: int, entity_type: str, entity_id: int, user_ids: Sequence[int]
) -> Dict[int, EntityWatcher]:
    rows = (
        tenant_query(db, EntityWatcher, company_id)
        .filter(
            EntityWatcher.entity_type == entity_type,
            EntityWatcher.entity_id == entity_id,
            EntityWatcher.user_id.in_(list(user_ids)),
        )
        .all()
    )
    return {row.user_id: row for row in rows}


def _insert_watcher(
    db: Session, company_id: int, entity_type: str, entity_id: int, user_id: int
) -> Optional[EntityWatcher]:
    """Insert one watcher row in its own savepoint; ``None`` when a concurrent request won.

    The unique constraint ``uq_entity_watchers_company_entity_user`` is the real guard: losing
    that race means the person is already watching, which is the outcome asked for, so it rolls
    back only the savepoint and the caller's transaction carries on.
    """
    savepoint = db.begin_nested()
    try:
        row = EntityWatcher(company_id=company_id, entity_type=entity_type, entity_id=entity_id, user_id=user_id)
        db.add(row)
        db.flush()
    except IntegrityError:
        savepoint.rollback()
        return None
    savepoint.commit()
    return row


def _ensure_watching(
    db: Session, company_id: int, entity_type: str, entity_id: int, user_ids: Sequence[int]
) -> List[EntityWatcher]:
    """Make each user a watcher of the record (idempotent); return the rows newly created.

    The rows are INSERTED in ascending user id, whatever order ``user_ids`` names them in: two
    transactions auto-watching the same people then take the unique-index entries in the same
    order, so one waits for the other and loses cleanly (``_insert_watcher``) instead of the two
    deadlocking. The rows are RETURNED in ``user_ids``' order (first occurrence), which is the
    order the audit row's ``auto_watched_user_ids`` lists them in.
    """
    wanted = list(dict.fromkeys(user_ids))
    if not wanted:
        return []
    existing = _watcher_rows(db, company_id, entity_type, entity_id, wanted)
    created: Dict[int, EntityWatcher] = {}
    for user_id in sorted(user_id for user_id in wanted if user_id not in existing):
        row = _insert_watcher(db, company_id, entity_type, entity_id, user_id)
        if row is not None:
            created[user_id] = row
    return [created[user_id] for user_id in wanted if user_id in created]


def _watch_state(db: Session, company_id: int, entity_type: str, entity_id: int, user_id: int) -> WatchStateResponse:
    """``watching`` = the caller has a row; ``watcher_count`` = ACTIVE members of the company
    watching (a deactivated person's row persists but is not counted, and is never notified)."""
    watching = (
        tenant_query(db, EntityWatcher, company_id)
        .filter(
            EntityWatcher.entity_type == entity_type,
            EntityWatcher.entity_id == entity_id,
            EntityWatcher.user_id == user_id,
        )
        .first()
        is not None
    )
    count = (
        tenant_query(db, EntityWatcher, company_id)
        .join(User, User.id == EntityWatcher.user_id)
        .filter(
            EntityWatcher.entity_type == entity_type,
            EntityWatcher.entity_id == entity_id,
            User.company_id == company_id,
            User.is_active.is_(True),
        )
        .count()
    )
    return WatchStateResponse(watching=watching, watcher_count=count)


def watch_state(db: Session, *, company_id: int, actor: User, entity_type: str, entity_id: int) -> WatchStateResponse:
    spec, record = resolve_target(db, company_id=company_id, actor=actor, entity_type=entity_type, entity_id=entity_id)
    return _watch_state(db, company_id, spec.entity_type, record.id, actor.id)


def watch(db: Session, *, company_id: int, actor: User, entity_type: str, entity_id: int, audit) -> WatchStateResponse:
    """Start watching (idempotent: already watching writes nothing). Flushes, never commits."""
    spec, record = resolve_target(db, company_id=company_id, actor=actor, entity_type=entity_type, entity_id=entity_id)
    created = _ensure_watching(db, company_id, spec.entity_type, record.id, [actor.id])
    if created:
        row = created[0]
        identifier = spec.identifier(record)
        audit.log_create(
            resource_type=WATCHER_RESOURCE_TYPE,
            resource_id=row.id,
            resource_identifier=f"{actor.full_name} watching {identifier}",
            new_values={"entity_type": spec.entity_type, "entity_id": record.id, "user_id": actor.id},
            description=f"Started watching {spec.label} {identifier}",
            extra_data={"entity_type": spec.entity_type, "entity_id": record.id, "entity_identifier": identifier},
        )
    return _watch_state(db, company_id, spec.entity_type, record.id, actor.id)


def unwatch(
    db: Session, *, company_id: int, actor: User, entity_type: str, entity_id: int, audit
) -> WatchStateResponse:
    """Stop watching (idempotent). The watcher row is PHYSICALLY deleted: it is a preference, not a
    record. Flushes, never commits."""
    spec, record = resolve_target(db, company_id=company_id, actor=actor, entity_type=entity_type, entity_id=entity_id)
    row = _watcher_rows(db, company_id, spec.entity_type, record.id, [actor.id]).get(actor.id)
    if row is not None:
        row_id = row.id
        identifier = spec.identifier(record)
        db.delete(row)
        db.flush()
        audit.log_delete(
            resource_type=WATCHER_RESOURCE_TYPE,
            resource_id=row_id,
            resource_identifier=f"{actor.full_name} watching {identifier}",
            old_values={"entity_type": spec.entity_type, "entity_id": record.id, "user_id": actor.id},
            description=f"Stopped watching {spec.label} {identifier}",
            extra_data={"entity_type": spec.entity_type, "entity_id": record.id, "entity_identifier": identifier},
            soft_delete=False,
        )
    return _watch_state(db, company_id, spec.entity_type, record.id, actor.id)


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


def _mention_ids(comment: Comment) -> List[int]:
    return [mention.user_id for mention in comment.mentions]


def _comment_identifier(comment: Comment, identifier: str) -> str:
    return f"Comment {comment.id} on {identifier}"


def _audit_extra(
    spec: CommentableType, record: Any, identifier: str, audit_extra: Optional[Dict[str, Any]], **fields: Any
) -> Dict[str, Any]:
    """The caller's ``audit_extra`` (e.g. a direct caller's ``{"via": "hank"}``) merged UNDER the
    service's own keys: a caller can add context to the evidence, never overwrite it."""
    extra: Dict[str, Any] = dict(audit_extra or {})
    extra.update({"entity_type": spec.entity_type, "entity_id": record.id, "entity_identifier": identifier})
    extra.update(fields)
    return extra


def create_comment(
    db: Session,
    *,
    company_id: int,
    actor: User,
    entity_type: str,
    entity_id: int,
    body: str,
    mentioned_user_ids: Optional[Sequence[int]],
    audit,
    audit_extra: Optional[Dict[str, Any]] = None,
) -> Comment:
    """Post a comment on a live record of the active company. Flushes, never commits.

    Validates the record (422 type / 403 / 404; read ``FOR KEY SHARE``, this verb's first lock),
    the body (422) and every mention (422 naming refused ids); inserts the comment and its mention
    rows; auto-watches the author and each mentioned person (idempotent, no backfill of older
    records); emits the ``comment_mentioned`` / ``comment_added`` notification events,
    best-effort; and LAST writes ONE ``audit.log_create`` (resource type ``comment``;
    ``extra_data`` carries the record, the mentioned and the newly auto-watched ids, with
    ``audit_extra`` merged in under them). The audit row comes last because it takes the global
    audit-chain lock, which no row lock may follow (module docstring, lock order).

    ``audit`` is anything with ``AuditService.log_create``'s signature. That is the only audit
    call made, so a caller that needs a required audit can pass a wrapper whose ``log_create`` is
    required; if it raises, the caller rolls back and the events go with the comment. ``actor``
    is the author -- always a person.
    """
    spec, record = resolve_target(
        db, company_id=company_id, actor=actor, entity_type=entity_type, entity_id=entity_id, lock=True
    )
    text = _clean_body(body)
    mention_ids = _clean_mention_ids(mentioned_user_ids)
    mentioned = _mentionable_users(db, company_id, spec, mention_ids)

    comment = Comment(
        company_id=company_id,
        entity_type=spec.entity_type,
        entity_id=record.id,
        author_id=actor.id,
        body=text,
    )
    db.add(comment)
    db.flush()
    for user in mentioned:
        db.add(CommentMention(company_id=company_id, comment_id=comment.id, user_id=user.id))
    db.flush()
    db.expire(comment, ["mentions"])

    auto_watched = _ensure_watching(
        db, company_id, spec.entity_type, record.id, [actor.id, *(user.id for user in mentioned)]
    )
    auto_watched_ids = [row.user_id for row in auto_watched]

    # The events BEFORE the audit row: the audit row takes the global audit-chain lock, and no row
    # lock may come after it (module docstring, lock order). The events need ``comment.id``, so the
    # comment and its mentions are flushed above.
    _emit_comment_events(
        db,
        company_id=company_id,
        actor=actor,
        spec=spec,
        record=record,
        comment=comment,
        notify_mention_ids=mention_ids,
        all_mention_ids=mention_ids,
        include_added=True,
    )

    identifier = spec.identifier(record)
    audit.log_create(
        resource_type=COMMENT_RESOURCE_TYPE,
        resource_id=comment.id,
        resource_identifier=_comment_identifier(comment, identifier),
        new_values={
            "entity_type": spec.entity_type,
            "entity_id": record.id,
            "author_id": actor.id,
            "body": text,
            "mentioned_user_ids": mention_ids,
        },
        description=f"Commented on {spec.label} {identifier}",
        extra_data=_audit_extra(
            spec,
            record,
            identifier,
            audit_extra,
            mentioned_user_ids=mention_ids,
            auto_watched_user_ids=auto_watched_ids,
        ),
    )
    return comment


def update_comment(
    db: Session,
    *,
    company_id: int,
    actor: User,
    comment_id: int,
    body: str,
    mentioned_user_ids: Optional[Sequence[int]],
    audit,
    audit_extra: Optional[Dict[str, Any]] = None,
) -> Comment:
    """Edit a comment: the AUTHOR only (403 otherwise -- an Admin cannot rewrite someone's words).

    ``body`` and ``mentioned_user_ids`` are the full new state. The mention set is REPLACED: new
    ids are validated like a create's (a mention kept from before is not re-checked, so a person
    deactivated since does not block an edit of the text) and inserted; dropped ids' rows are
    physically deleted (mention rows are not ``SoftDeleteMixin``; the ``log_update`` row keeps the
    old and new sets). Stamps ``edited_at``. Only NEWLY added people are notified, and each is
    auto-watched. An edit that changes neither the text nor the mention SET writes nothing (no
    ``edited_at``, no audit row). Locks the comment row, then the record ``FOR KEY SHARE``, before
    any write; emits the events and only then writes the ``log_update`` row (module docstring,
    lock order). Flushes, never commits.
    """
    comment, spec, record = _live_comment_or_404(db, company_id, comment_id, lock=True, lock_record=True)
    _assert_can_view(spec, actor)
    if comment.author_id != actor.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=EDIT_FORBIDDEN_DETAIL)
    text = _clean_body(body)
    requested_ids = _clean_mention_ids(mentioned_user_ids)

    old_body = comment.body
    old_ids = _mention_ids(comment)
    old_set, requested_set = set(old_ids), set(requested_ids)
    added_ids = [user_id for user_id in requested_ids if user_id not in old_set]
    removed_ids = [user_id for user_id in old_ids if user_id not in requested_set]
    added_users = _mentionable_users(db, company_id, spec, added_ids)

    if text == old_body and not added_ids and not removed_ids:
        return comment

    for mention in list(comment.mentions):
        if mention.user_id in removed_ids:
            db.delete(mention)
    for user in added_users:
        db.add(CommentMention(company_id=company_id, comment_id=comment.id, user_id=user.id))
    comment.body = text
    comment.edited_at = _utcnow()
    db.flush()
    db.expire(comment, ["mentions"])
    # The stored order: kept mentions in their original order, then the new ones as given.
    new_ids = [user_id for user_id in old_ids if user_id in requested_set] + added_ids

    auto_watched = _ensure_watching(db, company_id, spec.entity_type, record.id, added_ids)

    # The events BEFORE the audit row, as in ``create_comment`` (module docstring, lock order).
    _emit_comment_events(
        db,
        company_id=company_id,
        actor=actor,
        spec=spec,
        record=record,
        comment=comment,
        notify_mention_ids=added_ids,
        all_mention_ids=new_ids,
        include_added=False,
    )

    identifier = spec.identifier(record)
    audit.log_update(
        resource_type=COMMENT_RESOURCE_TYPE,
        resource_id=comment.id,
        resource_identifier=_comment_identifier(comment, identifier),
        old_values={"body": old_body, "mentioned_user_ids": old_ids},
        new_values={"body": text, "mentioned_user_ids": new_ids},
        description=f"Edited a comment on {spec.label} {identifier}",
        extra_data=_audit_extra(
            spec,
            record,
            identifier,
            audit_extra,
            added_mention_user_ids=added_ids,
            removed_mention_user_ids=removed_ids,
            auto_watched_user_ids=[row.user_id for row in auto_watched],
        ),
    )
    return comment


def _can_delete(comment: Comment, actor: User) -> bool:
    return comment.author_id == actor.id or _bypasses_role_gates(actor) or actor.role in _MODERATOR_ROLES


def delete_comment(
    db: Session,
    *,
    company_id: int,
    actor: User,
    comment_id: int,
    audit,
    audit_extra: Optional[Dict[str, Any]] = None,
) -> Comment:
    """Soft-delete a comment: its author, or an Admin / Manager (403 otherwise). The audit row
    snapshots the body, author, record and mentions, and is written after the comment row's lock
    and ``UPDATE`` (module docstring, lock order). Flushes, never commits."""
    comment, spec, record = _live_comment_or_404(db, company_id, comment_id, lock=True)
    _assert_can_view(spec, actor)
    if not _can_delete(comment, actor):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=DELETE_FORBIDDEN_DETAIL)
    identifier = spec.identifier(record)
    snapshot = {
        "entity_type": spec.entity_type,
        "entity_id": record.id,
        "author_id": comment.author_id,
        "body": comment.body,
        "mentioned_user_ids": _mention_ids(comment),
        "created_at": comment.created_at,
        "edited_at": comment.edited_at,
    }
    comment.soft_delete(actor.id)
    db.flush()
    audit.log_delete(
        resource_type=COMMENT_RESOURCE_TYPE,
        resource_id=comment.id,
        resource_identifier=_comment_identifier(comment, identifier),
        old_values=snapshot,
        description=f"Deleted a comment on {spec.label} {identifier}",
        extra_data=_audit_extra(
            spec, record, identifier, audit_extra, author_id=comment.author_id, moderated=comment.author_id != actor.id
        ),
        soft_delete=True,
    )
    return comment


def comment_response(comment: Comment, actor: User) -> CommentResponse:
    """The API shape of one comment, with the CALLER's edit / delete rights."""
    author = comment.author
    return CommentResponse(
        id=comment.id,
        entity_type=comment.entity_type,
        entity_id=comment.entity_id,
        author=CommentPerson(id=author.id, display_name=author.full_name),
        body=comment.body,
        created_at=comment.created_at,
        edited_at=comment.edited_at,
        mentions=[
            CommentPerson(id=mention.user.id, display_name=mention.user.full_name) for mention in comment.mentions
        ],
        can_edit=comment.author_id == actor.id,
        can_delete=_can_delete(comment, actor),
    )


def load_comment_response(db: Session, *, company_id: int, comment_id: int, actor: User) -> CommentResponse:
    """Re-read one comment (tenant-scoped, any state) for a write route's response."""
    comment = (
        tenant_query(db, Comment, company_id)
        .options(selectinload(Comment.author), selectinload(Comment.mentions).selectinload(CommentMention.user))
        .filter(Comment.id == comment_id)
        .one()
    )
    return comment_response(comment, actor)


def list_comments(
    db: Session, *, company_id: int, actor: User, entity_type: str, entity_id: int
) -> List[CommentResponse]:
    """A live record's live comments: the newest :data:`COMMENT_LIST_LIMIT`, oldest first."""
    spec, record = resolve_target(db, company_id=company_id, actor=actor, entity_type=entity_type, entity_id=entity_id)
    rows = (
        tenant_query(db, Comment, company_id)
        .options(selectinload(Comment.author), selectinload(Comment.mentions).selectinload(CommentMention.user))
        .filter(
            Comment.entity_type == spec.entity_type,
            Comment.entity_id == record.id,
            Comment.is_deleted.is_(False),
        )
        .order_by(Comment.created_at.desc(), Comment.id.desc())
        .limit(COMMENT_LIST_LIMIT)
        .all()
    )
    rows.reverse()
    return [comment_response(comment, actor) for comment in rows]


# ---------------------------------------------------------------------------
# Mention search
# ---------------------------------------------------------------------------


def _like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_mentionable_users(db: Session, *, company_id: int, q: str, limit: int) -> List[MentionableUser]:
    """Active people of the active company whose first name, last name or full name contains ``q``
    (case-insensitive). Returns ``{id, display_name}`` ONLY -- no email, role or phone."""
    term = " ".join((q or "").split())
    if not term:
        return []
    pattern = _like_pattern(term)
    full_name = User.first_name + " " + User.last_name
    users = (
        tenant_query(db, User, company_id)
        .filter(
            User.is_active.is_(True),
            or_(
                User.first_name.ilike(pattern, escape="\\"),
                User.last_name.ilike(pattern, escape="\\"),
                full_name.ilike(pattern, escape="\\"),
            ),
        )
        .order_by(func.lower(User.first_name), func.lower(User.last_name), User.id)
        .limit(limit)
        .all()
    )
    return [MentionableUser(id=user.id, display_name=user.full_name) for user in users]


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def _emit_comment_events(
    db: Session,
    *,
    company_id: int,
    actor: User,
    spec: CommentableType,
    record: Any,
    comment: Comment,
    notify_mention_ids: Sequence[int],
    all_mention_ids: Sequence[int],
    include_added: bool,
) -> None:
    """Emit ``comment_mentioned`` (when anyone new is mentioned) and ``comment_added`` (on create).

    BEST-EFFORT, IN ONE SAVEPOINT -- deliberately ``emit_best_effort`` inside ``begin_nested()``,
    the ``dispatch_service.emit_dispatch_notices`` pattern. A notification is a side signal of a
    person's comment and must never fail it: any failure (the event INSERT's flush included)
    rolls back ONLY the savepoint, so the caller's transaction stays usable -- which plain
    ``emit_best_effort`` alone cannot promise on PostgreSQL once a flush has failed. The
    notification outbox acts on the OUTERMOST transaction only: a savepoint release hands these
    events' ids to the request's transaction (whose real commit enqueues the fan-out) and a
    rollback drops exactly them, so no outbox handling is needed here.

    The payload carries ids and two strings -- the record identifier (titles the notification)
    and ``comment_author`` (the author's name, snapshotted now; the email detail line's "By:") --
    and NEVER the comment text. The text stays in the app, behind the record's read access.
    """
    if not notify_mention_ids and not include_added:
        return
    work_order_id, identifier_fields = spec.notification_fields(record)
    base_payload: Dict[str, Any] = {
        "comment_id": comment.id,
        "record_type": spec.entity_type,
        "record_id": record.id,
        **identifier_fields,
        "comment_author": actor.full_name,
    }
    emits: List[Tuple[str, Dict[str, Any]]] = []
    if notify_mention_ids:
        emits.append((COMMENT_MENTIONED_EVENT_TYPE, {**base_payload, "mentioned_user_ids": list(notify_mention_ids)}))
    if include_added:
        emits.append((COMMENT_ADDED_EVENT_TYPE, {**base_payload, "mentioned_user_ids": list(all_mention_ids)}))

    savepoint = db.begin_nested()
    try:
        events = OperationalEventService(db)
        for event_type, payload in emits:
            event = events.emit_best_effort(
                company_id=company_id,
                event_type=event_type,
                source_module=COMMENT_EVENT_SOURCE_MODULE,
                entity_type=COMMENT_EVENT_ENTITY_TYPE,
                entity_id=comment.id,
                work_order_id=work_order_id,
                user_id=actor.id,
                severity="info",
                event_payload=payload,
            )
            if event is None:
                raise RuntimeError(f"{event_type} emit failed for comment {comment.id}")
        savepoint.commit()
    except Exception:  # noqa: BLE001 - a notification must never fail the comment it describes
        logger.warning(
            "comment notification skipped (best-effort): company_id=%s comment_id=%s",
            company_id,
            comment.id,
            exc_info=True,
        )
        try:
            savepoint.rollback()
        except Exception:  # noqa: BLE001 - already failing; the outer transaction decides
            logger.warning("comment notification savepoint rollback failed", exc_info=True)


def _event_record(db: Session, event, company_id: int) -> Tuple[Optional[CommentableType], Optional[Any]]:
    """The event's record, re-resolved LIVE in the event's company; ``(None, None)`` when gone."""
    payload = event.event_payload or {}
    spec = COMMENTABLE_TYPES.get(payload.get("record_type"))
    record_id = payload.get("record_id")
    if spec is None or isinstance(record_id, bool) or not isinstance(record_id, int):
        return None, None
    record = spec.resolve(db, company_id, record_id)
    if record is None:
        return None, None
    return spec, record


def _payload_user_ids(event) -> List[int]:
    values = (event.event_payload or {}).get("mentioned_user_ids") or []
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, int) and not isinstance(value, bool)]


def mention_recipients(db: Session, event, company_id: int) -> List[User]:
    """``comment.mention`` recipients: the payload's ``mentioned_user_ids``, RE-CHECKED at dispatch.

    Same company, active, and allowed to view the record -- which must still be live (a record
    deleted before the worker ran notifies nobody). The author is dropped by ``_fan_out``.
    """
    spec, _record = _event_record(db, event, company_id)
    user_ids = _payload_user_ids(event)
    if spec is None or not user_ids:
        return []
    users = (
        db.query(User)
        .filter(User.company_id == company_id, User.is_active.is_(True), User.id.in_(user_ids))
        .order_by(User.id)
        .all()
    )
    return [user for user in users if spec.can_view(user)]


def watcher_recipients(db: Session, event, company_id: int) -> List[User]:
    """``comment.added`` recipients: the record's watchers MINUS everyone mentioned in this comment
    (they get ``comment.mention`` instead) MINUS the author. Active members of the company who may
    view the record; nobody when the record is no longer live."""
    spec, record = _event_record(db, event, company_id)
    if spec is None:
        return []
    excluded = set(_payload_user_ids(event))
    if event.user_id is not None:
        excluded.add(event.user_id)
    users = (
        db.query(User)
        .join(EntityWatcher, EntityWatcher.user_id == User.id)
        .filter(
            EntityWatcher.company_id == company_id,
            EntityWatcher.entity_type == spec.entity_type,
            EntityWatcher.entity_id == record.id,
            User.company_id == company_id,
            User.is_active.is_(True),
        )
        .order_by(User.id)
        .all()
    )
    return [user for user in users if user.id not in excluded and spec.can_view(user)]
