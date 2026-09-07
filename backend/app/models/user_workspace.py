from datetime import datetime

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)

from app.db.database import Base


class UserWorkspaceRecord(Base):
    """Private saved views and resumable input; never a posted business document."""

    __tablename__ = "user_workspace_records"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "user_id",
            "namespace",
            "kind",
            "key",
            name="uq_user_workspace_key",
        ),
        Index("ix_user_workspace_owner", "company_id", "user_id", "namespace", "kind"),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    namespace = Column(String(60), nullable=False)
    kind = Column(String(10), nullable=False)
    key = Column(String(80), nullable=False)
    name = Column(String(100), nullable=False)
    data = Column(JSON, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
