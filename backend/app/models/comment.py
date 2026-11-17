"""Comments with @mentions, and record watchers: the discussion thread on a record.

Generic by design (docs/NOTIFICATIONS_PLAN.md §3.6): a comment or a watcher names its target
as ``(entity_type, entity_id)``, a POLYMORPHIC reference with no foreign key. The database
therefore cannot vouch for the target, so every write resolves it through ``tenant_query()``
against the active company first (404 on a foreign or absent record) -- entity ids are global
integers and a bare id is never trusted. Which ``entity_type`` values are live is a service
decision (v1: ``work_order`` only), deliberately not a CHECK constraint, so enabling the next
record type needs no migration.

* ``Comment`` -- soft-deleted, never physically removed (invariant 3): every read filters
  ``is_deleted == False``. Editable by its author; ``edited_at`` marks an edit.
* ``CommentMention`` -- who a comment @mentions; one row per (company, comment, user).
  Written with the comment, never on its own.
* ``EntityWatcher`` -- a person's preference to hear about a record. A PREFERENCE, not a record:
  unwatch physically deletes the row. Watchers survive the watched record's soft delete, so a
  restore keeps them; recipient resolution skips deleted records.

Lock-step with migration ``125_work_order_comments``: every column, index and constraint here is
created there with the same name, and the ``after_create`` listeners below mirror its RLS /
REVOKE DDL for ``create_all`` bootstraps (``tests/test_migration_125_work_order_comments.py``).
"""

from datetime import datetime, timezone

from sqlalchemy import DDL, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import SoftDeleteMixin, TenantMixin


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Comment(Base, SoftDeleteMixin, TenantMixin):
    __tablename__ = 'comments'
    __table_args__ = (
        # The thread read: one record's comments in this company, oldest first.
        Index('ix_comments_entity', 'company_id', 'entity_type', 'entity_id', 'created_at'),
    )

    id = Column(Integer, primary_key=True)
    entity_type = Column(String(40), nullable=False)
    entity_id = Column(Integer, nullable=False)
    author_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    # Stored verbatim: there is no ingest-time sanitization, and the SPA renders it as text.
    body = Column(Text, nullable=False)
    edited_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)

    author = relationship('User', foreign_keys=[author_id])
    mentions = relationship('CommentMention', back_populates='comment', order_by='CommentMention.id')


class CommentMention(Base, TenantMixin):
    __tablename__ = 'comment_mentions'
    __table_args__ = (
        UniqueConstraint('company_id', 'comment_id', 'user_id', name='uq_comment_mentions_company_comment_user'),
    )

    id = Column(Integer, primary_key=True)
    # Plain FK, no ON DELETE: comments are soft-deleted, never physically removed.
    comment_id = Column(Integer, ForeignKey('comments.id'), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    comment = relationship('Comment', back_populates='mentions')
    user = relationship('User', foreign_keys=[user_id])


class EntityWatcher(Base, TenantMixin):
    __tablename__ = 'entity_watchers'
    __table_args__ = (
        # Also serves the (company, entity) "who watches this record?" lookup through its
        # leading columns; a separate index on them would duplicate it.
        UniqueConstraint(
            'company_id', 'entity_type', 'entity_id', 'user_id', name='uq_entity_watchers_company_entity_user'
        ),
    )

    id = Column(Integer, primary_key=True)
    entity_type = Column(String(40), nullable=False)
    entity_id = Column(Integer, nullable=False)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    user = relationship('User', foreign_keys=[user_id])


def security_sql(table: str) -> tuple[str, ...]:
    """The RLS / REVOKE DDL for one of these tables. Migration 125 carries its own frozen copy
    (a migration never imports the models it predates); a test pins the two byte-identical.

    Deny-by-default with zero policies (docs/SUPABASE_SECURITY.md): FastAPI owns authentication
    and tenancy, and neither Supabase client role gets a Data API path. The ``anon`` /
    ``authenticated`` REVOKEs sit under a ``pg_roles`` check because those roles exist only on
    Supabase, and on plain Postgres a statement naming a missing role aborts.
    """
    # B608 below: ``table`` is always one of this module's own table names, never input.
    return (
        f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY',
        f'REVOKE ALL ON TABLE {table} FROM PUBLIC',
        f'REVOKE ALL ON SEQUENCE {table}_id_seq FROM PUBLIC',
        f"""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE {table} FROM anon;
        REVOKE ALL ON SEQUENCE {table}_id_seq FROM anon;
      END IF;
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE {table} FROM authenticated;
        REVOKE ALL ON SEQUENCE {table}_id_seq FROM authenticated;
      END IF;
    END $$""",  # nosec B608
    )


# Mirror migration 125 for model-bootstrap (create_all) installations.
for _model in (Comment, CommentMention, EntityWatcher):
    for _statement in security_sql(_model.__tablename__):
        event.listen(_model.__table__, 'after_create', DDL(_statement).execute_if(dialect='postgresql'))
