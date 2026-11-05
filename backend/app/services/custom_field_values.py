"""Tenant-scoped reads and writes of custom-field VALUES (``/custom-fields/values/*``).

A value row hangs off two things a caller names by id/key: a ``CustomFieldDefinition``
(by ``field_key`` + ``entity_type``) and a polymorphic target entity (``entity_type`` +
``entity_id``, no FK). Both are resolved in the ACTIVE company before anything is read or
written, and every value row is read through its own ``company_id`` too, so a caller can
never read, overwrite or delete another tenant's value by guessing an id or a key.

* The target entity is resolved through ``ENTITY_MODELS``. A miss -- absent, or another
  company's row -- is a 404, indistinguishable from "no such id".
* Reads accept a soft-deleted target (a historical record keeps its custom values); writes
  require a LIVE one (``is_deleted == False`` on the models that carry it), the same
  "historical reads retain tombstones, new writes validate live parents" rule the rest of
  the system follows.
* Every write is audited through ``AuditService`` (``log_create`` / ``log_update`` /
  ``log_delete``), logged before the caller's commit so the row commits atomically with
  the change. ``CustomFieldValue`` has no ``SoftDeleteMixin``; a delete stays physical and
  its ``log_delete`` row carries the removed value.

Nothing here commits: the router owns the transaction.
"""

from typing import Any, Dict, Optional, Tuple, Type

from fastapi import HTTPException
from sqlalchemy.orm import Session, contains_eager

from app.db.tenant_filter import tenant_query
from app.models.bom import BOM
from app.models.custom_field import CustomFieldDefinition, CustomFieldValue, EntityType
from app.models.customer import Customer
from app.models.inventory import InventoryItem
from app.models.part import Part
from app.models.purchasing import Vendor
from app.models.user import User
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder
from app.services.audit_service import AuditService

RESOURCE_TYPE = "custom_field_value"

# EntityType -> the tenant table its ``entity_id`` points at. ``supplier`` is the vendor
# master; ``inventory`` is a stock row (``inventory_items``), the record the Inventory
# page lists. Every member of EntityType must appear here -- a test enforces it -- so a
# new entity type cannot silently skip the existence check.
ENTITY_MODELS: Dict[EntityType, Type[Any]] = {
    EntityType.PART: Part,
    EntityType.WORK_ORDER: WorkOrder,
    EntityType.WORK_CENTER: WorkCenter,
    EntityType.CUSTOMER: Customer,
    EntityType.SUPPLIER: Vendor,
    EntityType.INVENTORY: InventoryItem,
    EntityType.BOM: BOM,
}

VALUE_COLUMNS = ("value_text", "value_number", "value_boolean", "value_date", "value_json")


def require_entity(db: Session, company_id: int, entity_type: EntityType, entity_id: int, *, live: bool) -> None:
    """404 unless ``(entity_type, entity_id)`` names a row of the active company.

    ``live=True`` (every write) additionally refuses a soft-deleted row.
    """
    model = ENTITY_MODELS[entity_type]
    query = tenant_query(db, model, company_id).filter(model.id == entity_id)
    if live and hasattr(model, "is_deleted"):
        query = query.filter(model.is_deleted == False)  # noqa: E712
    if query.with_entities(model.id).first() is None:
        raise HTTPException(status_code=404, detail=f"{entity_type.value} {entity_id} not found")


def find_definition(
    db: Session, company_id: int, entity_type: EntityType, field_key: str, *, active_only: bool
) -> Optional[CustomFieldDefinition]:
    """The active company's definition for ``field_key`` on ``entity_type``, if any."""
    query = tenant_query(db, CustomFieldDefinition, company_id).filter(
        CustomFieldDefinition.field_key == field_key,
        CustomFieldDefinition.entity_type == entity_type,
    )
    if active_only:
        query = query.filter(CustomFieldDefinition.is_active == True)  # noqa: E712
    return query.first()


def find_value(
    db: Session, company_id: int, field_def: CustomFieldDefinition, entity_type: EntityType, entity_id: int
) -> Optional[CustomFieldValue]:
    """The active company's value row for one definition on one entity, if any."""
    return (
        tenant_query(db, CustomFieldValue, company_id)
        .filter(
            CustomFieldValue.field_definition_id == field_def.id,
            CustomFieldValue.entity_type == entity_type,
            CustomFieldValue.entity_id == entity_id,
        )
        .first()
    )


def entity_values(db: Session, company_id: int, entity_type: EntityType, entity_id: int) -> list:
    """Value rows of ACTIVE definitions on one entity, both sides tenant-scoped.

    ``field_definition`` is populated from the join (``contains_eager``), so a caller
    reading ``value.field_definition`` issues no per-row lazy load.
    """
    return (
        tenant_query(db, CustomFieldValue, company_id)
        .join(CustomFieldValue.field_definition)
        .options(contains_eager(CustomFieldValue.field_definition))
        .filter(
            CustomFieldDefinition.company_id == company_id,
            CustomFieldDefinition.is_active == True,  # noqa: E712
            CustomFieldValue.entity_type == entity_type,
            CustomFieldValue.entity_id == entity_id,
        )
        .all()
    )


def _identifier(entity_type: EntityType, entity_id: int, field_key: str) -> str:
    return f"{entity_type.value}:{entity_id}:{field_key}"


def _snapshot(value: CustomFieldValue) -> Dict[str, Any]:
    return {column: getattr(value, column) for column in VALUE_COLUMNS}


def upsert_value(
    db: Session,
    audit: AuditService,
    *,
    company_id: int,
    user: User,
    field_def: CustomFieldDefinition,
    entity_type: EntityType,
    entity_id: int,
    parsed: Dict[str, Any],
) -> Tuple[str, CustomFieldValue]:
    """Create or overwrite one value row and audit it. Returns ``("created"|"updated", row)``.

    The caller has already resolved ``field_def`` in ``company_id`` and checked the
    entity with ``require_entity(..., live=True)``.
    """
    identifier = _identifier(entity_type, entity_id, field_def.field_key)
    existing = find_value(db, company_id, field_def, entity_type, entity_id)
    if existing is not None:
        previous = _snapshot(existing)
        for column, new_value in parsed.items():
            setattr(existing, column, new_value)
        existing.updated_by = user.id
        audit.log_update(
            RESOURCE_TYPE,
            existing.id,
            identifier,
            old_values=previous,
            new_values=_snapshot(existing),
            extra_data={"field_key": field_def.field_key, "entity_type": entity_type.value, "entity_id": entity_id},
        )
        return "updated", existing

    new_value = CustomFieldValue(
        field_definition_id=field_def.id,
        entity_type=entity_type,
        entity_id=entity_id,
        updated_by=user.id,
        **parsed,
    )
    new_value.company_id = company_id
    db.add(new_value)
    db.flush()
    audit.log_create(
        RESOURCE_TYPE,
        new_value.id,
        identifier,
        new_values=_snapshot(new_value),
        extra_data={"field_key": field_def.field_key, "entity_type": entity_type.value, "entity_id": entity_id},
    )
    return "created", new_value


def delete_value(
    db: Session,
    audit: AuditService,
    *,
    field_def: CustomFieldDefinition,
    value: CustomFieldValue,
    entity_type: EntityType,
    entity_id: int,
) -> None:
    """Physically remove one value row, recording what it held in the audit chain first."""
    audit.log_delete(
        RESOURCE_TYPE,
        value.id,
        _identifier(entity_type, entity_id, field_def.field_key),
        old_values=_snapshot(value),
        extra_data={"field_key": field_def.field_key, "entity_type": entity_type.value, "entity_id": entity_id},
    )
    db.delete(value)
