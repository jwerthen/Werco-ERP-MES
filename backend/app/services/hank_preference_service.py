"""Explicit, audited personal settings scoped to the employee and active company."""

from datetime import datetime, timezone

from fastapi import HTTPException

from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_query
from app.models.company import Company
from app.models.hank_preferences import HankPreference
from app.models.user import UserRole
from app.schemas.hank_preferences import HankPreferenceResponse, HankPreferenceValues


def _preference_row(db, company_id, user_id, *, locked=False):
    query = tenant_query(db, HankPreference, company_id).filter(HankPreference.user_id == user_id)
    if locked:
        query = query.with_for_update().populate_existing()
    return query.first()


def get_hank_preference_values(db, company_id, user_id):
    """Read defaults without creating a row; callers supply already authorized identity."""
    row = _preference_row(db, company_id, user_id)
    return HankPreferenceValues.model_validate(row.preferences_json if row is not None else {})


class HankPreferenceService:
    def __init__(self, db, user, company_id):
        self.db, self.user, self.company_id = db, user, company_id

    def can_edit(self):
        elevated = self.user.is_superuser or self.user.role == UserRole.PLATFORM_ADMIN
        company_active = self.db.query(Company.is_active).filter(Company.id == self.company_id).scalar()
        return bool(
            company_active
            and self.user.is_active
            and (self.user.company_id == self.company_id or elevated)
            and getattr(self.user, '_api_token_id', None) is None
            and getattr(self.user, '_token_scope', None) not in ('kiosk', 'api')
            and not getattr(self.user, '_read_only_company_context', False)
        )

    def response(self, row=None):
        return HankPreferenceResponse(
            company_id=self.company_id,
            version=row.version if row is not None else 0,
            preferences=HankPreferenceValues.model_validate(row.preferences_json if row is not None else {}),
            updated_at=row.updated_at if row is not None else None,
            can_edit=self.can_edit(),
        )

    def get(self):
        return self.response(_preference_row(self.db, self.company_id, self.user.id))

    def save(self, command, audit, *, reset=False):
        """Flush one CAS and required audit; caller owns the only commit."""
        if command.expected_company_id != self.company_id:
            raise HTTPException(409, 'Active company changed. Reopen Hank preferences in the intended company.')
        if not self.can_edit():
            raise HTTPException(403, 'Save personal preferences from an interactive account in a writable company.')
        acquire_generator_lock(self.db, f'hank_preferences:{self.user.id}', self.company_id)
        row = _preference_row(self.db, self.company_id, self.user.id, locked=True)
        current_version = row.version if row is not None else 0
        if command.expected_version != current_version:
            raise HTTPException(409, 'Your Hank preferences changed. Refresh them before saving.')
        values = HankPreferenceValues() if reset else command.preferences
        payload = values.model_dump(mode='json')
        previous = HankPreferenceValues.model_validate(row.preferences_json if row is not None else {}).model_dump(
            mode='json'
        )
        if (reset and row is None) or (row is not None and previous == payload):
            return self.response(row)
        now = datetime.now(timezone.utc)
        if row is None:
            row = HankPreference(
                company_id=self.company_id,
                user_id=self.user.id,
                version=1,
                preferences_json=payload,
                created_at=now,
                updated_at=now,
            )
            self.db.add(row)
            self.db.flush()
            action = 'CREATE'
        else:
            count = (
                tenant_query(self.db, HankPreference, self.company_id)
                .filter(
                    HankPreference.id == row.id,
                    HankPreference.user_id == self.user.id,
                    HankPreference.version == command.expected_version,
                )
                .update(
                    {'version': current_version + 1, 'preferences_json': payload, 'updated_at': now},
                    synchronize_session=False,
                )
            )
            if count != 1:
                raise HTTPException(409, 'Your Hank preferences changed. Refresh them before saving.')
            action = 'UPDATE'
            self.db.refresh(row)
        audit.log_required(
            action,
            'hank_preference',
            resource_id=row.id,
            resource_identifier=f'Employee {self.user.id} Hank preferences',
            old_values={'version': current_version, 'preferences': previous} if action == 'UPDATE' else None,
            new_values={'version': row.version, 'preferences': payload},
            extra_data={'source': 'hank_preferences', 'reset_to_defaults': reset},
        )
        self.db.flush()
        return self.response(row)
