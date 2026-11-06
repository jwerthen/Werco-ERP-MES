from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.core.time_utils import to_utc_iso
from app.db.database import get_db
from app.models.custom_field import CustomFieldDefinition, CustomFieldValue, EntityType, FieldType
from app.models.user import User, UserRole
from app.schemas.custom_field import (
    BulkSetCustomFields,
    CustomFieldDefinitionCreate,
    CustomFieldDefinitionResponse,
    CustomFieldDefinitionUpdate,
    CustomFieldValueSet,
    EntityCustomFields,
)
from app.services import custom_field_values
from app.services.audit_service import AuditService

router = APIRouter()

# Audit resource type for definition writes. A definition decides how every stored
# value under it is read (field_type / options) and whether it is shown at all
# (is_active hides its values from every read), so who changed one is evidence.
DEFINITION_RESOURCE_TYPE = "custom_field_definition"


def _definition_identifier(field: CustomFieldDefinition) -> str:
    entity_type = field.entity_type.value if isinstance(field.entity_type, EntityType) else str(field.entity_type)
    return f"{entity_type}:{field.field_key}"


# Field Definition endpoints
@router.get("/definitions", response_model=List[CustomFieldDefinitionResponse])
def list_field_definitions(
    entity_type: Optional[EntityType] = None,
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List custom field definitions, optionally filtered by entity type"""
    query = db.query(CustomFieldDefinition).filter(CustomFieldDefinition.company_id == company_id)

    if entity_type:
        query = query.filter(CustomFieldDefinition.entity_type == entity_type)

    if active_only:
        query = query.filter(CustomFieldDefinition.is_active == True)

    return query.order_by(CustomFieldDefinition.entity_type, CustomFieldDefinition.sort_order).all()


@router.post("/definitions", response_model=CustomFieldDefinitionResponse)
def create_field_definition(
    field_in: CustomFieldDefinitionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a new custom field definition. Writes an audit ``CREATE`` row in the same transaction."""
    # Check if field_key already exists for this entity type
    existing = (
        db.query(CustomFieldDefinition)
        .filter(
            and_(
                CustomFieldDefinition.company_id == company_id,
                CustomFieldDefinition.field_key == field_in.field_key,
                CustomFieldDefinition.entity_type == field_in.entity_type,
            )
        )
        .first()
    )

    if existing:
        raise HTTPException(
            status_code=400, detail=f"Field '{field_in.field_key}' already exists for {field_in.entity_type.value}"
        )

    field = CustomFieldDefinition(**field_in.model_dump(), created_by=current_user.id)
    field.company_id = company_id
    db.add(field)
    # Flush for the id and column defaults, and log BEFORE the commit so the audit
    # row commits atomically with the definition.
    db.flush()
    audit.log_create(DEFINITION_RESOURCE_TYPE, field.id, _definition_identifier(field), new_values=field)
    db.commit()
    db.refresh(field)
    return field


@router.get("/definitions/{field_id}", response_model=CustomFieldDefinitionResponse)
def get_field_definition(
    field_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get a specific field definition"""
    field = (
        db.query(CustomFieldDefinition)
        .filter(CustomFieldDefinition.id == field_id, CustomFieldDefinition.company_id == company_id)
        .first()
    )
    if not field:
        raise HTTPException(status_code=404, detail="Field definition not found")
    return field


@router.put("/definitions/{field_id}", response_model=CustomFieldDefinitionResponse)
def update_field_definition(
    field_id: int,
    field_in: CustomFieldDefinitionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update a field definition. Writes an audit ``UPDATE`` row (supplied fields, old and new)."""
    field = (
        db.query(CustomFieldDefinition)
        .filter(CustomFieldDefinition.id == field_id, CustomFieldDefinition.company_id == company_id)
        .first()
    )
    if not field:
        raise HTTPException(status_code=404, detail="Field definition not found")

    update_data = field_in.model_dump(exclude_unset=True)
    previous = {key: getattr(field, key) for key in update_data}
    for key, value in update_data.items():
        setattr(field, key, value)

    # Flush first so a database refusal surfaces here rather than inside the
    # best-effort audit call's autoflush; log before the commit (same transaction).
    db.flush()
    audit.log_update(
        DEFINITION_RESOURCE_TYPE,
        field.id,
        _definition_identifier(field),
        old_values=previous,
        new_values=update_data,
    )
    db.commit()
    db.refresh(field)
    return field


@router.delete("/definitions/{field_id}")
def delete_field_definition(
    field_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Soft delete (deactivate) a field definition.

    Writes an audit ``STATUS_CHANGE`` row (``active`` -> ``inactive``) when the
    definition was active; deactivating an already-inactive one changes and records
    nothing.
    """
    field = (
        db.query(CustomFieldDefinition)
        .filter(CustomFieldDefinition.id == field_id, CustomFieldDefinition.company_id == company_id)
        .first()
    )
    if not field:
        raise HTTPException(status_code=404, detail="Field definition not found")

    if field.is_active is not False:
        field.is_active = False
        db.flush()
        audit.log_status_change(
            DEFINITION_RESOURCE_TYPE,
            field.id,
            _definition_identifier(field),
            old_status="active",
            new_status="inactive",
        )
        db.commit()
    return {"message": "Field definition deactivated"}


# Value endpoints
#
# Every /values/* route is tenant-scoped end to end (custom_field_values module
# docstring): the definition is resolved by (company, field_key, entity_type), the
# target entity must exist in the active company (404 otherwise -- reads accept a
# soft-deleted entity, writes require a live one), and value rows are read through
# their own company_id. Writes and deletes are audited before the commit.
def parse_value(value: Any, field_type: FieldType) -> Dict[str, Any]:
    """Parse and validate value based on field type, return dict with appropriate column.

    Raises ``ValueError`` / ``TypeError`` for a value the type cannot hold."""
    result = {"value_text": None, "value_number": None, "value_boolean": None, "value_date": None, "value_json": None}

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
        elif isinstance(value, (datetime, date)):
            result["value_date"] = value
        else:
            # A JSON number / bool / object / array is not a date. Refuse it HERE so the
            # caller maps it to 400; passed through, it fails only at flush time (a 500,
            # and on an overwrite inside the best-effort audit call's autoflush).
            raise TypeError(f"{field_type.value} value must be an ISO-8601 string, not {type(value).__name__}")
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
        if field_value.value_date is None:
            return None
        # DATETIME values are true timestamps (emit UTC Z); DATE values are day-only (keep YYYY-MM-DD).
        if field_type == FieldType.DATETIME:
            return to_utc_iso(field_value.value_date)
        return field_value.value_date.isoformat()
    elif field_type == FieldType.MULTISELECT:
        return field_value.value_json
    return field_value.value_text


@router.get("/values/{entity_type}/{entity_id}", response_model=EntityCustomFields)
def get_entity_custom_fields(
    entity_type: EntityType,
    entity_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get all custom field values for an entity of the active company.

    404 when the entity is not one of the active company's records. Values of
    deactivated field definitions are omitted.
    """
    custom_field_values.require_entity(db, company_id, entity_type, entity_id, live=False)
    values = custom_field_values.entity_values(db, company_id, entity_type, entity_id)

    fields = {}
    for val in values:
        field_def = val.field_definition
        fields[field_def.field_key] = get_value(val, field_def.field_type)

    return EntityCustomFields(entity_type=entity_type, entity_id=entity_id, fields=fields)


def _parse_or_400(raw: Any, field_def: CustomFieldDefinition) -> Dict[str, Any]:
    try:
        return parse_value(raw, field_def.field_type)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid value for '{field_def.field_key}' ({field_def.field_type.value})"
        ) from exc


@router.post("/values/{entity_type}/{entity_id}")
def set_custom_field_value(
    entity_type: EntityType,
    entity_id: int,
    value_in: CustomFieldValueSet,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Set one custom field value on a live entity of the active company.

    404 when the entity is not a live record of the active company or the field is
    not an active definition there; 400 when the value does not fit the field type.
    Creates or overwrites the value and writes an audit row.
    """
    custom_field_values.require_entity(db, company_id, entity_type, entity_id, live=True)
    field_def = custom_field_values.find_definition(db, company_id, entity_type, value_in.field_key, active_only=True)
    if not field_def:
        raise HTTPException(status_code=404, detail=f"Field '{value_in.field_key}' not found for {entity_type.value}")

    parsed = _parse_or_400(value_in.value, field_def)
    outcome, _ = custom_field_values.upsert_value(
        db,
        audit,
        company_id=company_id,
        user=current_user,
        field_def=field_def,
        entity_type=entity_type,
        entity_id=entity_id,
        parsed=parsed,
    )
    db.commit()
    message = "Value updated" if outcome == "updated" else "Value created"
    return {"message": message, "field_key": value_in.field_key}


@router.post("/values/bulk")
def set_bulk_custom_fields(
    bulk_in: BulkSetCustomFields,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Set several custom field values on one live entity of the active company.

    404 (and nothing written) when the entity is not a live record of the active
    company. Otherwise each key is reported on its own: an unknown field or a value
    that does not fit its type is an ``error`` result and the rest still apply.
    Every value written is audited.
    """
    custom_field_values.require_entity(db, company_id, bulk_in.entity_type, bulk_in.entity_id, live=True)
    results = []

    for field_key, value in bulk_in.values.items():
        field_def = custom_field_values.find_definition(
            db, company_id, bulk_in.entity_type, field_key, active_only=True
        )
        if not field_def:
            results.append({"field_key": field_key, "status": "error", "message": "Field not found"})
            continue

        try:
            parsed = parse_value(value, field_def.field_type)
        except (TypeError, ValueError) as exc:
            results.append({"field_key": field_key, "status": "error", "message": str(exc)})
            continue

        custom_field_values.upsert_value(
            db,
            audit,
            company_id=company_id,
            user=current_user,
            field_def=field_def,
            entity_type=bulk_in.entity_type,
            entity_id=bulk_in.entity_id,
            parsed=parsed,
        )
        results.append({"field_key": field_key, "status": "success"})

    db.commit()
    return {"results": results}


@router.delete("/values/{entity_type}/{entity_id}/{field_key}")
def delete_custom_field_value(
    entity_type: EntityType,
    entity_id: int,
    field_key: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Delete one custom field value from a live entity of the active company.

    404 when the entity, the field or the value is not the active company's. The
    removed value is recorded in the audit row.
    """
    custom_field_values.require_entity(db, company_id, entity_type, entity_id, live=True)
    # Not active_only: a value of a since-deactivated field can still be removed.
    field_def = custom_field_values.find_definition(db, company_id, entity_type, field_key, active_only=False)
    if not field_def:
        raise HTTPException(status_code=404, detail="Field not found")

    value = custom_field_values.find_value(db, company_id, field_def, entity_type, entity_id)
    if not value:
        raise HTTPException(status_code=404, detail="Value not found")

    custom_field_values.delete_value(
        db, audit, field_def=field_def, value=value, entity_type=entity_type, entity_id=entity_id
    )
    db.commit()
    return {"message": "Value deleted"}
