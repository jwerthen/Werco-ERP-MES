from typing import List, Optional, Dict, Any
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import and_
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.custom_field import CustomFieldDefinition, CustomFieldValue, FieldType, EntityType
from app.schemas.custom_field import (
    CustomFieldDefinitionCreate, CustomFieldDefinitionUpdate, CustomFieldDefinitionResponse,
    CustomFieldValueSet, CustomFieldValueResponse, EntityCustomFields, BulkSetCustomFields
)

router = APIRouter()


# Field Definition endpoints
@router.get("/definitions", response_model=List[CustomFieldDefinitionResponse])
def list_field_definitions(
    entity_type: Optional[EntityType] = None,
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List custom field definitions, optionally filtered by entity type"""
    query = db.query(CustomFieldDefinition)
    
    if entity_type:
        query = query.filter(CustomFieldDefinition.entity_type == entity_type)
    
    if active_only:
        query = query.filter(CustomFieldDefinition.is_active == True)
    
    return query.order_by(CustomFieldDefinition.entity_type, CustomFieldDefinition.sort_order).all()


@router.post("/definitions", response_model=CustomFieldDefinitionResponse)
def create_field_definition(
    field_in: CustomFieldDefinitionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Create a new custom field definition"""
    # Check if field_key already exists for this entity type
    existing = db.query(CustomFieldDefinition).filter(
        and_(
            CustomFieldDefinition.field_key == field_in.field_key,
            CustomFieldDefinition.entity_type == field_in.entity_type
        )
    ).first()
    
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Field '{field_in.field_key}' already exists for {field_in.entity_type.value}"
        )
    
    field = CustomFieldDefinition(
        **field_in.model_dump(),
        created_by=current_user.id
    )
    db.add(field)
    db.commit()
    db.refresh(field)
    return field


@router.get("/definitions/{field_id}", response_model=CustomFieldDefinitionResponse)
def get_field_definition(
    field_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific field definition"""
    field = db.query(CustomFieldDefinition).filter(CustomFieldDefinition.id == field_id).first()
    if not field:
        raise HTTPException(status_code=404, detail="Field definition not found")
    return field


@router.put("/definitions/{field_id}", response_model=CustomFieldDefinitionResponse)
def update_field_definition(
    field_id: int,
    field_in: CustomFieldDefinitionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Update a field definition"""
    field = db.query(CustomFieldDefinition).filter(CustomFieldDefinition.id == field_id).first()
    if not field:
        raise HTTPException(status_code=404, detail="Field definition not found")
    
    update_data = field_in.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(field, key, value)
    
    db.commit()
    db.refresh(field)
    return field


@router.delete("/definitions/{field_id}")
def delete_field_definition(
    field_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN]))
):
    """Soft delete (deactivate) a field definition"""
    field = db.query(CustomFieldDefinition).filter(CustomFieldDefinition.id == field_id).first()
    if not field:
        raise HTTPException(status_code=404, detail="Field definition not found")
    
    field.is_active = False
    db.commit()
    return {"message": "Field definition deactivated"}


# Value endpoints
def parse_value(value: Any, field_type: FieldType) -> Dict[str, Any]:
    """Parse and validate value based on field type, return dict with appropriate column"""
    result = {
        "value_text": None,
        "value_number": None,
        "value_boolean": None,
        "value_date": None,
        "value_json": None
    }
    
    if value is None:
        return result
    
    if field_type in [FieldType.TEXT, FieldType.TEXTAREA, FieldType.URL, FieldType.EMAIL, FieldType.SELECT]:
        result["value_text"] = str(value)
    elif field_type in [FieldType.NUMBER, FieldType.DECIMAL]:
        result["value_number"] = float(value)
    elif field_type == FieldType.BOOLEAN:
        result["value_boolean"] = bool(value)
    elif field_type in [FieldType.DATE, FieldType.DATETIME]:
        if isinstance(value, str):
            result["value_date"] = datetime.fromisoformat(value.replace('Z', '+00:00'))
        else:
            result["value_date"] = value
    elif field_type == FieldType.MULTISELECT:
        result["value_json"] = value if isinstance(value, list) else [value]
    else:
        result["value_text"] = str(value)
    
    return result


def get_value(field_value: CustomFieldValue, field_type: FieldType) -> Any:
    """Extract value from CustomFieldValue based on field type"""
    if field_type in [FieldType.TEXT, FieldType.TEXTAREA, FieldType.URL, FieldType.EMAIL, FieldType.SELECT]:
        return field_value.value_text
    elif field_type in [FieldType.NUMBER, FieldType.DECIMAL]:
        return field_value.value_number
    elif field_type == FieldType.BOOLEAN:
        return field_value.value_boolean
    elif field_type in [FieldType.DATE, FieldType.DATETIME]:
        return field_value.value_date.isoformat() if field_value.value_date else None
    elif field_type == FieldType.MULTISELECT:
        return field_value.value_json
    return field_value.value_text


@router.get("/values/{entity_type}/{entity_id}", response_model=EntityCustomFields)
def get_entity_custom_fields(
    entity_type: EntityType,
    entity_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all custom field values for an entity"""
    values = db.query(CustomFieldValue).join(CustomFieldDefinition).filter(
        and_(
            CustomFieldValue.entity_type == entity_type,
            CustomFieldValue.entity_id == entity_id,
            CustomFieldDefinition.is_active == True
        )
    ).all()
    
    fields = {}
    for val in values:
        field_def = val.field_definition
        fields[field_def.field_key] = get_value(val, field_def.field_type)
    
    return EntityCustomFields(
        entity_type=entity_type,
        entity_id=entity_id,
        fields=fields
    )


@router.post("/values/{entity_type}/{entity_id}")
def set_custom_field_value(
    entity_type: EntityType,
    entity_id: int,
    value_in: CustomFieldValueSet,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Set a custom field value for an entity"""
    # Find the field definition
    field_def = db.query(CustomFieldDefinition).filter(
        and_(
            CustomFieldDefinition.field_key == value_in.field_key,
            CustomFieldDefinition.entity_type == entity_type,
            CustomFieldDefinition.is_active == True
        )
    ).first()
    
    if not field_def:
        raise HTTPException(
            status_code=404,
            detail=f"Field '{value_in.field_key}' not found for {entity_type.value}"
        )
    
    # Parse the value
    parsed = parse_value(value_in.value, field_def.field_type)
    
    # Find existing value or create new
    existing = db.query(CustomFieldValue).filter(
        and_(
            CustomFieldValue.field_definition_id == field_def.id,
            CustomFieldValue.entity_type == entity_type,
            CustomFieldValue.entity_id == entity_id
        )
    ).first()
    
    if existing:
        for key, val in parsed.items():
            setattr(existing, key, val)
        existing.updated_by = current_user.id
        db.commit()
        return {"message": "Value updated", "field_key": value_in.field_key}
    else:
        new_value = CustomFieldValue(
            field_definition_id=field_def.id,
            entity_type=entity_type,
            entity_id=entity_id,
            updated_by=current_user.id,
            **parsed
        )
        db.add(new_value)
        db.commit()
        return {"message": "Value created", "field_key": value_in.field_key}


@router.post("/values/bulk")
def set_bulk_custom_fields(
    bulk_in: BulkSetCustomFields,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Set multiple custom field values at once"""
    results = []
    
    for field_key, value in bulk_in.values.items():
        try:
            field_def = db.query(CustomFieldDefinition).filter(
                and_(
                    CustomFieldDefinition.field_key == field_key,
                    CustomFieldDefinition.entity_type == bulk_in.entity_type,
                    CustomFieldDefinition.is_active == True
                )
            ).first()
            
            if not field_def:
                results.append({"field_key": field_key, "status": "error", "message": "Field not found"})
                continue
            
            parsed = parse_value(value, field_def.field_type)
            
            existing = db.query(CustomFieldValue).filter(
                and_(
                    CustomFieldValue.field_definition_id == field_def.id,
                    CustomFieldValue.entity_type == bulk_in.entity_type,
                    CustomFieldValue.entity_id == bulk_in.entity_id
                )
            ).first()
            
            if existing:
                for key, val in parsed.items():
                    setattr(existing, key, val)
                existing.updated_by = current_user.id
            else:
                new_value = CustomFieldValue(
                    field_definition_id=field_def.id,
                    entity_type=bulk_in.entity_type,
                    entity_id=bulk_in.entity_id,
                    updated_by=current_user.id,
                    **parsed
                )
                db.add(new_value)
            
            results.append({"field_key": field_key, "status": "success"})
            
        except Exception as e:
            results.append({"field_key": field_key, "status": "error", "message": str(e)})
    
    db.commit()
    return {"results": results}


@router.delete("/values/{entity_type}/{entity_id}/{field_key}")
def delete_custom_field_value(
    entity_type: EntityType,
    entity_id: int,
    field_key: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a specific custom field value"""
    field_def = db.query(CustomFieldDefinition).filter(
        and_(
            CustomFieldDefinition.field_key == field_key,
            CustomFieldDefinition.entity_type == entity_type
        )
    ).first()
    
    if not field_def:
        raise HTTPException(status_code=404, detail="Field not found")
    
    value = db.query(CustomFieldValue).filter(
        and_(
            CustomFieldValue.field_definition_id == field_def.id,
            CustomFieldValue.entity_type == entity_type,
            CustomFieldValue.entity_id == entity_id
        )
    ).first()
    
    if not value:
        raise HTTPException(status_code=404, detail="Value not found")
    
    db.delete(value)
    db.commit()
    return {"message": "Value deleted"}
