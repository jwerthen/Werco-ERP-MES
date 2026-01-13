from sqlalchemy import Column, Integer, String, DateTime, JSON, Enum as SQLEnum
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.db.database import Base
from app.models.user import UserRole


class RolePermission(Base):
    """
    Stores customized permissions for each role.
    Allows admins to override the default permission matrix.
    """
    __tablename__ = "role_permissions"
    
    id = Column(Integer, primary_key=True, index=True)
    role = Column(SQLEnum(UserRole), unique=True, nullable=False, index=True)
    permissions = Column(JSON, nullable=False, default=list)
    
    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(Integer, nullable=True)


# Default permissions matrix - same as frontend hardcoded values
DEFAULT_ROLE_PERMISSIONS = {
    UserRole.ADMIN: [
        'work_orders:view', 'work_orders:create', 'work_orders:edit', 'work_orders:delete', 'work_orders:release', 'work_orders:complete',
        'parts:view', 'parts:create', 'parts:edit', 'parts:delete',
        'boms:view', 'boms:create', 'boms:edit', 'boms:delete', 'boms:release',
        'routings:view', 'routings:create', 'routings:edit', 'routings:delete', 'routings:release',
        'inventory:view', 'inventory:adjust', 'inventory:transfer',
        'purchasing:view', 'purchasing:create', 'purchasing:approve',
        'receiving:view', 'receiving:create', 'receiving:inspect',
        'shipping:view', 'shipping:create', 'shipping:complete',
        'quality:view', 'quality:inspect', 'quality:approve', 'quality:calibration',
        'users:view', 'users:create', 'users:edit', 'users:delete', 'users:roles',
        'analytics:view', 'analytics:export',
        'admin:settings', 'admin:audit_logs', 'admin:system',
    ],
    UserRole.MANAGER: [
        'work_orders:view', 'work_orders:create', 'work_orders:edit', 'work_orders:delete', 'work_orders:release', 'work_orders:complete',
        'parts:view', 'parts:create', 'parts:edit',
        'boms:view', 'boms:create', 'boms:edit', 'boms:delete', 'boms:release',
        'routings:view', 'routings:create', 'routings:edit', 'routings:delete', 'routings:release',
        'inventory:view', 'inventory:adjust', 'inventory:transfer',
        'purchasing:view', 'purchasing:create', 'purchasing:approve',
        'receiving:view', 'receiving:create', 'receiving:inspect',
        'shipping:view', 'shipping:create', 'shipping:complete',
        'quality:view', 'quality:inspect', 'quality:approve', 'quality:calibration',
        'users:view', 'users:create', 'users:edit',
        'analytics:view', 'analytics:export',
        'admin:audit_logs',
    ],
    UserRole.SUPERVISOR: [
        'work_orders:view', 'work_orders:create', 'work_orders:edit', 'work_orders:release', 'work_orders:complete',
        'parts:view', 'parts:create', 'parts:edit',
        'boms:view', 'boms:create', 'boms:edit',
        'routings:view', 'routings:create', 'routings:edit',
        'inventory:view', 'inventory:adjust', 'inventory:transfer',
        'purchasing:view', 'purchasing:create',
        'receiving:view', 'receiving:create',
        'shipping:view', 'shipping:create', 'shipping:complete',
        'quality:view', 'quality:inspect',
        'users:view',
        'analytics:view',
    ],
    UserRole.OPERATOR: [
        'work_orders:view', 'work_orders:complete',
        'parts:view',
        'boms:view',
        'routings:view',
        'inventory:view',
        'quality:view',
        'analytics:view',
    ],
    UserRole.QUALITY: [
        'work_orders:view', 'work_orders:complete',
        'parts:view',
        'boms:view',
        'routings:view',
        'inventory:view',
        'receiving:view', 'receiving:inspect',
        'quality:view', 'quality:inspect', 'quality:approve', 'quality:calibration',
        'analytics:view',
    ],
    UserRole.SHIPPING: [
        'work_orders:view',
        'parts:view',
        'inventory:view',
        'shipping:view', 'shipping:create', 'shipping:complete',
        'analytics:view',
    ],
    UserRole.VIEWER: [
        'work_orders:view',
        'parts:view',
        'boms:view',
        'routings:view',
        'inventory:view',
        'purchasing:view',
        'receiving:view',
        'shipping:view',
        'quality:view',
        'analytics:view',
    ],
}

# All available permissions
ALL_PERMISSIONS = [
    # Work Orders
    'work_orders:view', 'work_orders:create', 'work_orders:edit', 'work_orders:delete', 'work_orders:release', 'work_orders:complete',
    # Parts
    'parts:view', 'parts:create', 'parts:edit', 'parts:delete',
    # BOMs
    'boms:view', 'boms:create', 'boms:edit', 'boms:delete', 'boms:release',
    # Routings
    'routings:view', 'routings:create', 'routings:edit', 'routings:delete', 'routings:release',
    # Inventory
    'inventory:view', 'inventory:adjust', 'inventory:transfer',
    # Purchasing
    'purchasing:view', 'purchasing:create', 'purchasing:approve',
    # Receiving
    'receiving:view', 'receiving:create', 'receiving:inspect',
    # Shipping
    'shipping:view', 'shipping:create', 'shipping:complete',
    # Quality
    'quality:view', 'quality:inspect', 'quality:approve', 'quality:calibration',
    # Users
    'users:view', 'users:create', 'users:edit', 'users:delete', 'users:roles',
    # Analytics
    'analytics:view', 'analytics:export',
    # Admin
    'admin:settings', 'admin:audit_logs', 'admin:system',
]

# Permission categories for UI grouping
PERMISSION_CATEGORIES = {
    'Work Orders': ['work_orders:view', 'work_orders:create', 'work_orders:edit', 'work_orders:delete', 'work_orders:release', 'work_orders:complete'],
    'Parts': ['parts:view', 'parts:create', 'parts:edit', 'parts:delete'],
    'BOMs': ['boms:view', 'boms:create', 'boms:edit', 'boms:delete', 'boms:release'],
    'Routings': ['routings:view', 'routings:create', 'routings:edit', 'routings:delete', 'routings:release'],
    'Inventory': ['inventory:view', 'inventory:adjust', 'inventory:transfer'],
    'Purchasing': ['purchasing:view', 'purchasing:create', 'purchasing:approve'],
    'Receiving': ['receiving:view', 'receiving:create', 'receiving:inspect'],
    'Shipping': ['shipping:view', 'shipping:create', 'shipping:complete'],
    'Quality': ['quality:view', 'quality:inspect', 'quality:approve', 'quality:calibration'],
    'Users': ['users:view', 'users:create', 'users:edit', 'users:delete', 'users:roles'],
    'Analytics': ['analytics:view', 'analytics:export'],
    'Admin': ['admin:settings', 'admin:audit_logs', 'admin:system'],
}
