from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import TenantMixin


class LaserNestPackage(Base, TenantMixin):
    """Imported laser nest package tied to a parent assembly work order."""

    __tablename__ = "laser_nest_packages"

    id = Column(Integer, primary_key=True, index=True)
    parent_work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=False, index=True)
    child_work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True, index=True)
    package_name = Column(String(255), nullable=False)
    source_path = Column(String(1000), nullable=True)
    import_status = Column(String(50), default="imported", nullable=False, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    parent_work_order = relationship("WorkOrder", foreign_keys=[parent_work_order_id])
    child_work_order = relationship("WorkOrder", foreign_keys=[child_work_order_id])
    nests = relationship("LaserNest", back_populates="package", cascade="all, delete-orphan", order_by="LaserNest.id")


class LaserNest(Base, TenantMixin):
    """One nest sheet/run plan represented as one shop-floor operation."""

    __tablename__ = "laser_nests"
    __table_args__ = (
        UniqueConstraint("work_order_operation_id", name="uq_laser_nests_operation"),
        UniqueConstraint("package_id", "nest_name", "cnc_file_name", name="uq_laser_nests_package_file"),
    )

    id = Column(Integer, primary_key=True, index=True)
    package_id = Column(Integer, ForeignKey("laser_nest_packages.id"), nullable=False, index=True)
    work_order_operation_id = Column(Integer, ForeignKey("work_order_operations.id"), nullable=True, index=True)
    nest_name = Column(String(255), nullable=False)
    cnc_file_name = Column(String(255), nullable=False)
    cnc_file_path = Column(String(1000), nullable=True)
    planned_runs = Column(Integer, default=1, nullable=False)
    completed_runs = Column(Float, default=0.0, nullable=False)
    material = Column(String(100), nullable=True)
    thickness = Column(String(50), nullable=True)
    sheet_size = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    package = relationship("LaserNestPackage", back_populates="nests")
    operation = relationship("WorkOrderOperation", back_populates="laser_nest")

    @property
    def remaining_runs(self) -> float:
        return max(0.0, float(self.planned_runs or 0) - float(self.completed_runs or 0))
