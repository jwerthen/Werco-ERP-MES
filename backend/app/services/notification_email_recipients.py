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
# Account/security messages with a mandatory email channel keep their recipient.
EMAIL_EVENTS = {
    key: entry
    for key, entry in CATALOG.items()
    if CHANNEL_EMAIL in entry.default_channels and entry.mandatory_channel != CHANNEL_EMAIL
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
    events = []
    for key, entry in EMAIL_EVENTS.items():
        saved = settings.get(setting_key(key))
        emails = WERCO_DEFAULT_EMAILS if company.slug == "werco" and key in WERCO_RESTRICTED_EVENTS else ()
        matched = [user for user in users if user.is_active and (user.email or "").strip().lower() in emails]
        ids = _saved_ids(saved) if saved else sorted(user.id for user in matched) if emails else None
        events.append(
            {
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
