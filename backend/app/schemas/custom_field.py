from pydantic import BaseModel
from typing import Optional, List, Any, Dict, Union
from datetime import datetime
from app.models.custom_field import FieldType, EntityType


class CustomFieldDefinitionBase(BaseModel):
    field_key: str
    display_name: str
    description: Optional[str] = None
    entity_type: EntityType
    field_type: FieldType
    is_required: bool = False
    sort_order: int = 0
    options: Optional[List[Any]] = None
    validation: Optional[Dict[str, Any]] = None
    default_value: Optional[str] = None
    placeholder: Optional[str] = None
    help_text: Optional[str] = None
    show_in_list: bool = False
    show_in_filter: bool = False
    field_group: Optional[str] = None


class CustomFieldDefinitionCreate(CustomFieldDefinitionBase):
    pass


class CustomFieldDefinitionUpdate(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    is_required: Optional[bool] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None
    options: Optional[List[Any]] = None
    validation: Optional[Dict[str, Any]] = None
    default_value: Optional[str] = None
    placeholder: Optional[str] = None
    help_text: Optional[str] = None
    show_in_list: Optional[bool] = None
    show_in_filter: Optional[bool] = None
    field_group: Optional[str] = None


class CustomFieldDefinitionResponse(CustomFieldDefinitionBase):
    id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class CustomFieldValueBase(BaseModel):
    field_definition_id: int
    entity_type: EntityType
    entity_id: int


class CustomFieldValueSet(BaseModel):
    """Set a value for a custom field"""
    field_key: str  # Can use field_key instead of ID
    value: Any  # Will be converted based on field type


class CustomFieldValueResponse(BaseModel):
    id: int
    field_definition_id: int
    field_key: str
    display_name: str
    field_type: FieldType
    entity_type: EntityType
    entity_id: int
    value: Any  # Parsed value based on field type
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class EntityCustomFields(BaseModel):
    """All custom field values for an entity"""
    entity_type: EntityType
    entity_id: int
    fields: Dict[str, Any]  # field_key -> value


class BulkSetCustomFields(BaseModel):
    """Set multiple custom field values at once"""
    entity_type: EntityType
    entity_id: int
    values: Dict[str, Any]  # field_key -> value
