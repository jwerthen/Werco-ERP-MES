"""Explicit, audited personal dashboard layouts scoped to the employee and active company.

A layout is presentation only: it chooses which dashboard widgets render, in what order and
at what width. It grants nothing -- every data endpoint a widget reads enforces its own
authorization -- so reads are open to any authenticated credential. Writes follow the Hank
preferences precedent: an interactive person (``is_interactive_person``) in a writable,
active company they belong to (or a platform principal), a compare-and-swap on ``version``,
and a REQUIRED audit row in the same transaction.

Stored rows are read only through ``normalize_stored_layout`` (lenient), and ``save`` never
validates the stored row at all: the no-op test and the audit ``old_values`` use the RAW
stored JSON, so a row written by another schema can always be saved over or reset. The no-op
test compares canonical JSON, not Python values (see ``_same_json``).
"""

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.core.credentials import is_interactive_person
from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_query
from app.models.company import Company
from app.models.dashboard_layout import DashboardLayout
from app.models.user import UserRole
from app.schemas.dashboard_layout import (
    DashboardLayoutCommand,
    DashboardLayoutResponse,
    DashboardLayoutSave,
    normalize_stored_layout,
)

AUDIT_RESOURCE_TYPE = 'dashboard_layout'
COMPANY_CHANGED_DETAIL = 'Active company changed. Reopen the dashboard in the intended company.'
NOT_EDITABLE_DETAIL = 'Save your dashboard layout from an interactive account in a writable company.'
STALE_VERSION_DETAIL = 'Your dashboard layout changed in another tab. Reload it before saving.'
# The stored encoding of "use the stock default"; only reset writes it.
DEFAULT_LAYOUT_JSON: dict[str, Any] = {}


def _same_json(stored: Any, payload: Any) -> bool:
    """Type-strict equality of two JSON values: True only when they serialize identically.

    Python ``==`` treats ``True == 1`` and ``1.0 == 1``, but ``normalize_stored_layout`` rejects
    a bool or float ``format_version``, so a value-equal row can read as the default. Treating it
    as "unchanged" would answer 200 for a save that wrote nothing. Anything that cannot be
    canonicalized counts as different, so the save overwrites it rather than trusting it.
    """
    try:
        return json.dumps(stored, sort_keys=True, separators=(',', ':')) == json.dumps(
            payload, sort_keys=True, separators=(',', ':')
        )
    except (TypeError, ValueError, RecursionError):
        return False


def _layout_row(db, company_id, user_id, *, locked=False):
    query = tenant_query(db, DashboardLayout, company_id).filter(DashboardLayout.user_id == user_id)
    if locked:
        query = query.with_for_update().populate_existing()
    return query.first()


class DashboardLayoutService:
    def __init__(self, db, user, company_id):
        self.db, self.user, self.company_id = db, user, company_id

    def can_edit(self) -> bool:
        elevated = self.user.is_superuser or self.user.role == UserRole.PLATFORM_ADMIN
        company_active = self.db.query(Company.is_active).filter(Company.id == self.company_id).scalar()
        return bool(
            company_active
            and self.user.is_active
            and (self.user.company_id == self.company_id or elevated)
            and is_interactive_person(self.user)
            and not getattr(self.user, '_read_only_company_context', False)
        )

    def response(self, row=None) -> DashboardLayoutResponse:
        return DashboardLayoutResponse(
            company_id=self.company_id,
            version=row.version if row is not None else 0,
            layout=normalize_stored_layout(row.layout_json, row_id=row.id) if row is not None else None,
            updated_at=row.updated_at if row is not None else None,
            can_edit=self.can_edit(),
        )

    def get(self) -> DashboardLayoutResponse:
        """Pure read: never creates a row."""
        return self.response(_layout_row(self.db, self.company_id, self.user.id))

    def save(self, command: DashboardLayoutCommand, audit, *, reset: bool = False) -> DashboardLayoutResponse:
        """Flush one CAS and its required audit row; the caller owns the only commit."""
        if reset:
            payload: dict[str, Any] = dict(DEFAULT_LAYOUT_JSON)
        elif isinstance(command, DashboardLayoutSave):
            payload = command.layout.model_dump(mode='json')
        else:
            raise TypeError('Saving a dashboard layout needs a DashboardLayoutSave command unless reset=True.')
        if command.expected_company_id != self.company_id:
            raise HTTPException(409, COMPANY_CHANGED_DETAIL)
        if not self.can_edit():
            raise HTTPException(403, NOT_EDITABLE_DETAIL)
        acquire_generator_lock(self.db, f'dashboard_layout:{self.user.id}', self.company_id)
        row = _layout_row(self.db, self.company_id, self.user.id, locked=True)
        current_version = row.version if row is not None else 0
        if command.expected_version != current_version:
            raise HTTPException(409, STALE_VERSION_DETAIL)
        # RAW stored JSON -- never validated here -- so a row another schema wrote can still be
        # compared, audited verbatim and overwritten.
        previous = row.layout_json if row is not None else None
        if row is None and reset:
            return self.response(None)
        if row is not None and _same_json(previous, payload):
            # Identical payload, or a reset of a row already at the default: no fake change.
            return self.response(row)
        now = datetime.now(timezone.utc)
        if row is None:
            row = DashboardLayout(
                company_id=self.company_id,
                user_id=self.user.id,
                version=1,
                layout_json=payload,
                created_at=now,
                updated_at=now,
            )
            self.db.add(row)
            self.db.flush()
            action = 'CREATE'
        else:
            count = (
                tenant_query(self.db, DashboardLayout, self.company_id)
                .filter(
                    DashboardLayout.id == row.id,
                    DashboardLayout.user_id == self.user.id,
                    DashboardLayout.version == command.expected_version,
                )
                .update(
                    {'version': current_version + 1, 'layout_json': payload, 'updated_at': now},
                    synchronize_session=False,
                )
            )
            if count != 1:
                raise HTTPException(409, STALE_VERSION_DETAIL)
            action = 'UPDATE'
            self.db.refresh(row)
        audit.log_required(
            action,
            AUDIT_RESOURCE_TYPE,
            resource_id=row.id,
            resource_identifier=f'Employee {self.user.id} dashboard layout',
            old_values={'version': current_version, 'layout': previous} if action == 'UPDATE' else None,
            new_values={'version': row.version, 'layout': payload},
            extra_data={'source': AUDIT_RESOURCE_TYPE, 'reset_to_default': reset},
        )
        self.db.flush()
        return self.response(row)
