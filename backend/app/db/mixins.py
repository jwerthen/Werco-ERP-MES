"""
Database mixins for common functionality.
"""
from sqlalchemy import Column, Integer, DateTime, event
from sqlalchemy.orm import declared_attr
from datetime import datetime


class OptimisticLockMixin:
    """
    Mixin that adds optimistic locking support via version column.
    
    Usage:
        class MyModel(Base, OptimisticLockMixin):
            __tablename__ = "my_table"
            ...
    
    The version column auto-increments on each update when using
    OptimisticLockService.update_with_lock()
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
            server_default='now()'
        )


class TimestampMixin:
    """
    Mixin that adds created_at and updated_at timestamps.
    """
    
    @declared_attr
    def created_at(cls):
        return Column(
            DateTime(timezone=True),
            nullable=False,
            default=datetime.utcnow,
            server_default='now()'
        )
    
    @declared_attr
    def updated_at(cls):
        return Column(
            DateTime(timezone=True),
            nullable=False,
            default=datetime.utcnow,
            onupdate=datetime.utcnow,
            server_default='now()'
        )
