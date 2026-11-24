"""Company-owned email audiences, independent of in-app/SMS routing.

An absent setting uses the catalog audience, except for Werco's two explicitly
restricted emails. An empty saved list disables that email. Invalid stored data
fails closed; it must never silently restore a broader audience.
"""

import json
import logging
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.quote_config import QuoteSettings
from app.models.user import User
from app.services.notification_catalog import CATALOG, CHANNEL_EMAIL
from app.services.user_identity import is_synthetic_email

logger = logging.getLogger(__name__)
SETTING_PREFIX = "notification_email_recipients."
WERCO_DEFAULT_EMAILS = ("awerthen@wercomfg.com", "jwerthen@wercomfg.com", "jmw@wercomfg.com")
WERCO_RESTRICTED_EVENTS = frozenset({"wo.completed", "receipt.created"})
# Person-addressed events: the audience IS the person a comment names (an @mention) or the
# people who chose to watch the record, so a company-wide email audience must never apply.
# Listed, a saved recipient list would break them both ways -- a non-empty list drops email
# for every mentioned person not on it and emails everyone on it for every mention anywhere,
# and an empty list switches mention email off company-wide. Excluded, they never appear under
# Admin Settings -> Email Recipients, the settings PUT refuses them, and a row saved for one
# before this exclusion (``comment.mention`` was listed while it was dormant) is ignored:
# ``email_recipient_ids`` returns None (the automatic audience) for any key not in EMAIL_EVENTS.
# ``comment.added`` has no email channel today; it is listed so gaining one cannot re-admit it.
EMAIL_RECIPIENT_EXCLUDED_EVENTS = frozenset({"comment.mention", "comment.added"})
# Account/security messages with a mandatory email channel keep their recipient.
EMAIL_EVENTS = {
    key: entry
    for key, entry in CATALOG.items()
    if CHANNEL_EMAIL in entry.default_channels
    and entry.mandatory_channel != CHANNEL_EMAIL
    and key not in EMAIL_RECIPIENT_EXCLUDED_EVENTS
}


def setting_key(event_key: str) -> str:
    return SETTING_PREFIX + event_key


def deliverable(user: User) -> bool:
    address = (user.email or "").strip()
    return bool(address) and not is_synthetic_email(address)


def _saved_ids(setting: QuoteSettings) -> list[int]:
    try:
        value = json.loads(setting.setting_value)
        if isinstance(value, list) and all(type(item) is int and item > 0 for item in value):
            return sorted(set(value))
    except (TypeError, ValueError):
        pass
    logger.error(
        "Invalid email recipient setting %s for company %s; email disabled", setting.setting_key, setting.company_id
    )
    return []


def default_emails(db: Session, company_id: int, event_key: str) -> tuple[str, ...]:
    if event_key in WERCO_RESTRICTED_EVENTS:
        company = db.query(Company).filter(Company.id == company_id).first()
        if company and company.slug == "werco":
            return WERCO_DEFAULT_EMAILS
    return ()


def email_recipient_ids(db: Session, company_id: int, event_key: str) -> Optional[set[int]]:
    """None = automatic audience; set (including empty) = exclusive selection."""
    if event_key not in EMAIL_EVENTS:
        return None
    setting = (
        db.query(QuoteSettings)
        .filter(QuoteSettings.company_id == company_id, QuoteSettings.setting_key == setting_key(event_key))
        .first()
    )
    if setting is not None:
        return set(_saved_ids(setting))
    emails = default_emails(db, company_id, event_key)
    if not emails:
        return None
    return {
        user.id
        for user in db.query(User)
        .filter(
            User.company_id == company_id,
            User.is_active.is_(True),
            func.lower(func.trim(User.email)).in_(emails),
        )
        .all()
    }


def email_recipient_allowed(db: Session, company_id: int, event_key: str, user_id: int) -> bool:
    ids = email_recipient_ids(db, company_id, event_key)
    return ids is None or user_id in ids


def email_recipient_overrides(db: Session, company_id: int) -> dict[str, set[int]]:
    """Bulk policy read for the self-service channel matrix and daily digest."""
    overrides = {
        row.setting_key[len(SETTING_PREFIX) :]: set(_saved_ids(row))
        for row in db.query(QuoteSettings)
        .filter(
            QuoteSettings.company_id == company_id,
            QuoteSettings.setting_key.in_([setting_key(key) for key in EMAIL_EVENTS]),
        )
        .all()
    }
    default_keys = WERCO_RESTRICTED_EVENTS - overrides.keys()
    if default_keys and default_emails(db, company_id, "wo.completed"):
        ids = {
            user.id
            for user in db.query(User)
            .filter(
                User.company_id == company_id,
                User.is_active.is_(True),
                func.lower(func.trim(User.email)).in_(WERCO_DEFAULT_EMAILS),
            )
            .all()
        }
        overrides.update({key: ids for key in default_keys})
    return overrides


def _email_reach(db: Session, company_id: int, users: list):
    """Who each email event ACTUALLY reaches today, for the Admin Email Recipients preview.

    Mirrors ``notification_dispatch._fan_out``'s email leg: a selected list (saved, or Werco's
    shipped default) is exclusive and ignores personal preferences; otherwise the automatic
    audience is the catalog entry's roles OR departments (``catalog_audience``), each person's
    saved channel choice applied, and only deliverable addresses count. An entry with a
    record-derived resolver (e.g. the work order's creator) can also reach people this preview
    cannot list without a record: ``depends_on_record`` says so, and such an event is never
    flagged ``reaches_nobody``. Reads only; one query per table."""
    from app.models.notification import NotificationPreference
    from app.services.notification_dispatch import catalog_audience, channels_from_pref

    by_id = {user.id: user for user in users}
    prefs = {
        row.user_id: row
        for row in db.query(NotificationPreference).filter(NotificationPreference.company_id == company_id).all()
    }

    def reach(entry, selected_ids: Optional[list]) -> dict:
        if selected_ids is not None:
            people = [by_id[i] for i in selected_ids if i in by_id and by_id[i].is_active]
            source = "selected"
        else:
            people = [
                user
                for user in catalog_audience(db, entry, company_id)
                if CHANNEL_EMAIL in channels_from_pref(prefs.get(user.id), entry, email_deliverable=deliverable(user))
            ]
            source = "automatic"
        reaches = sorted(user.id for user in people if deliverable(user))
        depends_on_record = selected_ids is None and entry.resolver is not None
        return {
            "audience_source": source,
            "reaches_user_ids": reaches,
            "depends_on_record": depends_on_record,
            "reaches_nobody": not reaches and not depends_on_record,
        }

    return reach


def email_settings_response(db: Session, company_id: int) -> dict:
    # Load the company directory and settings once for the entire matrix.
    users = (
        db.query(User).filter(User.company_id == company_id).order_by(User.last_name, User.first_name, User.id).all()
    )
    settings = {
        row.setting_key: row
        for row in db.query(QuoteSettings)
        .filter(
            QuoteSettings.company_id == company_id,
            QuoteSettings.setting_key.in_([setting_key(key) for key in EMAIL_EVENTS]),
        )
        .all()
    }
    company = db.query(Company).filter(Company.id == company_id).first()
    reach = _email_reach(db, company_id, users)
    events = []
    for key, entry in EMAIL_EVENTS.items():
        saved = settings.get(setting_key(key))
        emails = WERCO_DEFAULT_EMAILS if company.slug == "werco" and key in WERCO_RESTRICTED_EVENTS else ()
        matched = [user for user in users if user.is_active and (user.email or "").strip().lower() in emails]
        ids = _saved_ids(saved) if saved else sorted(user.id for user in matched) if emails else None
        events.append(
            {
                **reach(entry, ids),
                "event_key": key,
                "label": entry.label,
                "description": entry.description,
                "category": entry.category,
                "user_ids": ids,
                "is_custom": saved is not None,
                "missing_default_emails": (
                    [email for email in emails if email not in {(user.email or "").strip().lower() for user in matched}]
                    if saved is None
                    else []
                ),
            }
        )
    return {
        "events": events,
        "users": [
            {
                "id": user.id,
                "name": user.full_name,
                "email": user.email,
                "is_active": user.is_active,
                "email_deliverable": deliverable(user),
            }
            for user in users
        ],
    }
