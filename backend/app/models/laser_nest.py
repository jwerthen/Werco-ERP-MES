from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import SoftDeleteMixin, TenantMixin


class LaserNestPackage(Base, TenantMixin):
    """Imported laser nest package.

    Classic flow: tied to a parent assembly work order (``parent_work_order_id``
    set) with the nests built onto a LASER_CUTTING child WO. Standalone flow:
    the package is imported straight into a part-less laser-cutting WO with no
    parent, so ``parent_work_order_id`` is NULL and ``child_work_order_id``
    points at the standalone nest WO.
    """

    __tablename__ = "laser_nest_packages"

    id = Column(Integer, primary_key=True, index=True)
    # NULLABLE: standalone nest packages have no parent assembly work order.
    parent_work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True, index=True)
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


class LaserNest(Base, TenantMixin, SoftDeleteMixin):
    """One nest sheet/run plan represented as one shop-floor operation.

    A nest is either *imported* (parsed from an uploaded laser package, so it
    carries a ``cnc_file_name``) or *manual* (an operator-keyed program with no
    uploaded file, so ``cnc_file_name`` is NULL). The manual case is why
    ``cnc_file_name`` is nullable. ``SoftDeleteMixin`` lets a manually-created
    nest be (soft) deleted without breaking traceability or the package's run
    history -- queries against laser nests must filter ``is_deleted == False``.
    """

    __tablename__ = "laser_nests"
    __table_args__ = (
        UniqueConstraint("work_order_operation_id", name="uq_laser_nests_operation"),
        # Per-package uniqueness of (nest_name, cnc_file_name). On Postgres NULLs
        # are distinct, so manual nests (cnc_file_name IS NULL) never false-collide
        # with each other on this constraint even within the same package.
        UniqueConstraint("package_id", "nest_name", "cnc_file_name", name="uq_laser_nests_package_file"),
    )

    id = Column(Integer, primary_key=True, index=True)
    package_id = Column(Integer, ForeignKey("laser_nest_packages.id"), nullable=False, index=True)
    work_order_operation_id = Column(Integer, ForeignKey("work_order_operations.id"), nullable=True, index=True)
    nest_name = Column(String(255), nullable=False)
    # NULLABLE: manual nests have no uploaded CNC file. Imported nests still set it.
    cnc_file_name = Column(String(255), nullable=True)
    cnc_file_path = Column(String(1000), nullable=True)
    # Operator-/machine-facing program number keyed on the laser. NOT unique --
    # the same program number recurs across jobs/materials. Indexed for lookup.
    cnc_number = Column(String(100), nullable=True, index=True)
    # Optional reference PDF stored via the existing Document model ("documents").
    # Mirrors Shipment.label_document_id in app/models/shipping.py.
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=True, index=True)
    planned_runs = Column(Integer, default=1, nullable=False)
    completed_runs = Column(Float, default=0.0, nullable=False)
    material = Column(String(100), nullable=True)
    thickness = Column(String(50), nullable=True)
    sheet_size = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    package = relationship("LaserNestPackage", back_populates="nests")
    operation = relationship("WorkOrderOperation", back_populates="laser_nest")
    document = relationship("Document")

    @property
    def remaining_runs(self) -> float:
        return max(0.0, float(self.planned_runs or 0) - float(self.completed_runs or 0))
