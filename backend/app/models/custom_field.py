from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SQLEnum, Float, Text, ForeignKey, JSON
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.db.database import Base


class FieldType(str, enum.Enum):
    TEXT = "text"
    NUMBER = "number"
    DECIMAL = "decimal"
    DATE = "date"
    DATETIME = "datetime"
    BOOLEAN = "boolean"
    SELECT = "select"  # Single select from options
    MULTISELECT = "multiselect"  # Multiple select
    URL = "url"
    EMAIL = "email"
    TEXTAREA = "textarea"


class EntityType(str, enum.Enum):
    PART = "part"
    WORK_ORDER = "work_order"
    WORK_CENTER = "work_center"
    CUSTOMER = "customer"
    SUPPLIER = "supplier"
    INVENTORY = "inventory"
    BOM = "bom"


class CustomFieldDefinition(Base):
    """Definition of a custom field that can be added to entities"""
    __tablename__ = "custom_field_definitions"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # Field identification
    field_key = Column(String(100), nullable=False, index=True)  # Unique key like "customer_color_preference"
    display_name = Column(String(255), nullable=False)
    description = Column(Text)
    
    # What entity this field applies to
    entity_type = Column(SQLEnum(EntityType), nullable=False, index=True)
    
    # Field configuration
    field_type = Column(SQLEnum(FieldType), nullable=False)
    is_required = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    
    # Display order
    sort_order = Column(Integer, default=0)
    
    # For select/multiselect fields - JSON array of options
    # e.g., ["Red", "Blue", "Green"] or [{"value": "R", "label": "Red"}]
    options = Column(JSON, nullable=True)
    
    # Validation rules (JSON)
    # e.g., {"min": 0, "max": 100, "pattern": "^[A-Z]{3}$"}
    validation = Column(JSON, nullable=True)
    
    # Default value (stored as string, converted based on field_type)
    default_value = Column(String(500), nullable=True)
    
    # UI hints
    placeholder = Column(String(255))
    help_text = Column(Text)
    show_in_list = Column(Boolean, default=False)  # Show in list/table views
    show_in_filter = Column(Boolean, default=False)  # Allow filtering by this field
    
    # Grouping
    field_group = Column(String(100))  # Group related fields together
    
    # Audit
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = Column(Integer, nullable=True)
    
    # Unique constraint on field_key + entity_type
    __table_args__ = (
        # UniqueConstraint('field_key', 'entity_type', name='uq_field_entity'),
    )


class CustomFieldValue(Base):
    """Actual values stored for custom fields on entities"""
    __tablename__ = "custom_field_values"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # Which field definition this value belongs to
    field_definition_id = Column(Integer, ForeignKey("custom_field_definitions.id"), nullable=False)
    
    # Which entity this value belongs to (polymorphic)
    entity_type = Column(SQLEnum(EntityType), nullable=False, index=True)
    entity_id = Column(Integer, nullable=False, index=True)
    
    # The actual value - stored based on field type
    value_text = Column(Text)  # For text, textarea, url, email
    value_number = Column(Float)  # For number, decimal
    value_boolean = Column(Boolean)  # For boolean
    value_date = Column(DateTime)  # For date, datetime
    value_json = Column(JSON)  # For multiselect and complex values
    
    # Audit
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(Integer, nullable=True)
    
    # Relationships
    field_definition = relationship("CustomFieldDefinition")
    
    # Composite index for quick lookups
    __table_args__ = (
        # Index('ix_custom_field_entity', 'entity_type', 'entity_id'),
    )
