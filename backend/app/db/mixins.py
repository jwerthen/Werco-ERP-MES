"""
Database mixins for common functionality.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer
from sqlalchemy.orm import declared_attr, relationship


class SoftDeleteMixin:
    """
    Mixin that adds soft delete support.

    Usage:
        class MyModel(Base, SoftDeleteMixin):
            __tablename__ = "my_table"
            ...

    Records are not physically deleted, but marked with:
    - is_deleted: Boolean flag
    - deleted_at: Timestamp when deleted
    - deleted_by: User ID who deleted (optional)

    Use filter(Model.is_deleted == False) in queries to exclude deleted records.
    """

    @declared_attr
    def is_deleted(cls):
        return Column(Boolean, nullable=False, default=False, server_default='false', index=True)

    @declared_attr
    def deleted_at(cls):
        return Column(DateTime(timezone=True), nullable=True)

    @declared_attr
    def deleted_by(cls):
        return Column(Integer, nullable=True)

    def soft_delete(self, user_id: int = None):
        """Mark record as deleted."""
        self.is_deleted = True
        self.deleted_at = datetime.utcnow()
        self.deleted_by = user_id

    def restore(self):
        """Restore a soft-deleted record."""
        self.is_deleted = False
        self.deleted_at = None
        self.deleted_by = None


class OptimisticLockMixin:
    """
    Mixin that adds a ``version`` column for optimistic locking.

    Usage:
        class MyModel(Base, OptimisticLockMixin):
            __tablename__ = "my_table"
            ...

    NOTE: this mixin only declares the column; it intentionally does NOT set
    ``__mapper_args__={'version_id_col': version}``. Enabling SQLAlchemy's native
    version_id_col globally here would change commit behavior (StaleDataError on
    concurrent writes) for every model that uses this mixin. Optimistic locking
    is instead enabled per-model on the contended write paths — ``WorkOrder``,
    ``WorkOrderOperation``, and ``TimeEntry`` map ``version_id_col`` directly (see
    those models). Other consumers of this mixin keep the column for
    application-managed comparisons without SQLAlchemy enforcement.
    """

    @declared_attr
    def version(cls):
        return Column(Integer, nullable=False, default=1, server_default='1')

    @declared_attr
    def updated_at(cls):
        return Column(
            DateTime(timezone=True),
            nullable=False,
            default=datetime.utcnow,
            onupdate=datetime.utcnow,
            server_default='now()',
        )


class TenantMixin:
    """
    Mixin that adds multi-company tenant isolation.

    Usage:
        class MyModel(Base, TenantMixin):
            __tablename__ = "my_table"
            ...

    Every record is scoped to a company. Use tenant_query() or
    tenant_filter() from app.db.tenant_filter to scope queries.
    """

    @declared_attr
    def company_id(cls):
        return Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)

    @declared_attr
    def company(cls):
        return relationship("Company")


class TimestampMixin:
    """
    Mixin that adds created_at and updated_at timestamps.
    """

    @declared_attr
    def created_at(cls):
        return Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, server_default='now()')

    @declared_attr
    def updated_at(cls):
        return Column(
            DateTime(timezone=True),
            nullable=False,
            default=datetime.utcnow,
            onupdate=datetime.utcnow,
            server_default='now()',
        )
