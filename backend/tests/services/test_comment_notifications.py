"""``comment.mention`` and ``comment.added`` -- delivery of the work-order comment notifications.

Owner decisions (2026-11-16): a mention or comment notification carries NO comment text -- who
(the author's name), where (the work order number) and the link only. Pinned here, end to end
through ``dispatch_for_event`` (the outbox worker's entry point):

* recipients -- a mention reaches exactly the people the comment (newly) mentions, re-checked at
  dispatch (same company, active); ``comment.added`` reaches the record's watchers minus everyone
  mentioned in that comment minus the author; the author is never notified of their own comment;
  a soft-deleted work order notifies nobody;
* an edit notifies only the people it newly mentions;
* the 5-minute delivery dedup keys on the COMMENT, so a second comment on the same work order
  inside the window still notifies (the real ``_dedup_reserve`` against a fake Redis);
* content -- the payload, title and body never contain the comment text; ``comment_author`` is
  the email detail line's last key ("By: ...") and never an SMS key;
* email recipients -- ``comment.mention`` is not an Admin Settings email event, and a recipient
  list saved for it has no effect on who is emailed;
* the catalog wiring and the work-order deep link.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Dict, List, Optional
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

import app.services.notification_dispatch as dispatch
from app.models.comment import Comment, EntityWatcher
from app.models.notification import Notification, NotificationLog
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.quote_config import QuoteSettings
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.services import comment_service
from app.services import notification_links as links
from app.services.audit_service import AuditService
from app.services.notification_catalog import (
    CATALOG,
    CHANNEL_EMAIL,
    CHANNEL_IN_APP,
    SOURCE_EVENT_TYPE_TO_KEY,
    resolve_comment_mentioned_users,
    resolve_comment_watchers,
)
from app.services.notification_email_recipients import (
    EMAIL_EVENTS,
    EMAIL_RECIPIENT_EXCLUDED_EVENTS,
    email_recipient_ids,
    setting_key,
)
from tests.api.kiosk_test_helpers import COMPANY_A, COMPANY_B, ensure_company, make_user

pytestmark = [pytest.mark.requires_db]

SECRET = "Customer says the 0.250 flange bore is out -- rework before Friday"
_seq = itertools.count(1)


class FakeRedis:
    """``SET key value EX n NX`` semantics: the first reservation of a key wins until expiry."""

    def __init__(self) -> None:
        self.keys: Dict[str, str] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


@pytest.fixture
def redis(monkeypatch) -> FakeRedis:
    fake = FakeRedis()

    async def pool():
        return fake

    monkeypatch.setattr(dispatch, "get_redis_pool", pool)
    monkeypatch.setattr(dispatch, "enqueue_job", AsyncMock())
    return fake


def make_wo(db: Session, *, company_id: int = COMPANY_A) -> WorkOrder:
    ensure_company(db, company_id)
    n = next(_seq)
    part = Part(
        part_number=f"NTF-P-{company_id}-{n:04d}",
        name=f"Gusset {n}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"WO-NTF-{n:04d}",
        part_id=part.id,
        quantity_ordered=2,
        status="released",
        priority=3,
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def comment(db: Session, actor: User, wo: WorkOrder, body: str = SECRET, mentions: Optional[List[int]] = None):
    row = comment_service.create_comment(
        db,
        company_id=wo.company_id,
        actor=actor,
        entity_type="work_order",
        entity_id=wo.id,
        body=body,
        mentioned_user_ids=mentions or [],
        audit=AuditService(db, actor),
    )
    db.commit()
    return row


def watch(db: Session, user: User, wo: WorkOrder) -> None:
    comment_service.watch(
        db,
        company_id=wo.company_id,
        actor=user,
        entity_type="work_order",
        entity_id=wo.id,
        audit=AuditService(db, user),
    )
    db.commit()


def events(db: Session, event_type: str) -> List[OperationalEvent]:
    db.expire_all()
    return (
        db.query(OperationalEvent).filter(OperationalEvent.event_type == event_type).order_by(OperationalEvent.id).all()
    )


def deliver(db: Session, event: OperationalEvent) -> int:
    created = asyncio.run(dispatch.dispatch_for_event(db, event))
    db.commit()
    return created


def deliver_all(db: Session) -> None:
    for event_type in ("comment_mentioned", "comment_added"):
        for event in events(db, event_type):
            deliver(db, event)


def inbox(db: Session, user: User, event_key: Optional[str] = None) -> List[Notification]:
    db.expire_all()
    query = db.query(Notification).filter(Notification.user_id == user.id)
    if event_key:
        query = query.filter(Notification.event_key == event_key)
    return query.order_by(Notification.id).all()


def emails(db: Session, user: User, event_key: str) -> List[NotificationLog]:
    db.expire_all()
    return (
        db.query(NotificationLog)
        .filter(
            NotificationLog.user_id == user.id,
            NotificationLog.event_type == event_key,
            NotificationLog.channel == CHANNEL_EMAIL,
        )
        .all()
    )


@pytest.fixture
def crew(db_session: Session) -> Dict[str, User]:
    return {
        "author": make_user(db_session, role=UserRole.OPERATOR, first_name="Avery", last_name="Quill"),
        "mentioned": make_user(db_session, role=UserRole.SUPERVISOR, first_name="Blake", last_name="Marrow"),
        "watcher": make_user(db_session, role=UserRole.VIEWER, first_name="Casey", last_name="Fernhill"),
        "bystander": make_user(db_session, role=UserRole.MANAGER, first_name="Dana", last_name="Holloway"),
    }


# ---------------------------------------------------------------------------
# Catalog wiring
# ---------------------------------------------------------------------------


def test_the_catalog_entries_are_wired():
    mention, added = CATALOG["comment.mention"], CATALOG["comment.added"]
    assert SOURCE_EVENT_TYPE_TO_KEY["comment_mentioned"] == "comment.mention"
    assert SOURCE_EVENT_TYPE_TO_KEY["comment_added"] == "comment.added"
    assert mention.resolver is resolve_comment_mentioned_users
    assert added.resolver is resolve_comment_watchers
    assert mention.roles == () and mention.departments == ()
    assert added.roles == () and added.departments == ()
    assert mention.mandatory_channel == CHANNEL_IN_APP
    assert mention.default_channels == frozenset({CHANNEL_IN_APP, CHANNEL_EMAIL})
    assert added.default_channels == frozenset({CHANNEL_IN_APP})
    assert mention.sms_eligible is False and added.sms_eligible is False
    assert mention.recurring is False and added.recurring is False


def test_comment_author_is_the_last_email_detail_key_and_never_an_sms_key():
    assert dispatch._DETAIL_KEYS[-1] == ("comment_author", "By")
    assert "comment_author" not in dispatch._SMS_DETAIL_KEYS
    assert "comment_author" not in dispatch._IDENTIFIER_KEYS
    # The text never has a key to ride on: none of the comment payload keys is a detail or
    # identifier key except the two strings meant to be shown.
    shown = {key for key, _ in dispatch._DETAIL_KEYS} | set(dispatch._IDENTIFIER_KEYS)
    payload_keys = {
        "comment_id",
        "record_type",
        "record_id",
        "work_order_number",
        "comment_author",
        "mentioned_user_ids",
    }
    assert payload_keys & shown == {"work_order_number", "comment_author"}


# ---------------------------------------------------------------------------
# Content: who, where, link -- never the text
# ---------------------------------------------------------------------------


def test_payload_title_and_body_never_carry_the_comment_text(db_session, crew, redis):
    wo = make_wo(db_session)
    watch(db_session, crew["watcher"], wo)
    comment(db_session, crew["author"], wo, mentions=[crew["mentioned"].id])

    for event_type, event_key, recipient in (
        ("comment_mentioned", "comment.mention", crew["mentioned"]),
        ("comment_added", "comment.added", crew["watcher"]),
    ):
        (event,) = events(db_session, event_type)
        title, body = dispatch._content_for_event(CATALOG[event_key], event)
        assert title == f"{CATALOG[event_key].label}: {wo.work_order_number}"
        assert body == f"{CATALOG[event_key].description}\n\nBy: Avery Quill"
        for text in (json.dumps(event.event_payload), title, body):
            assert SECRET not in text
            assert "flange" not in text and "rework" not in text.lower()
        assert dispatch._payload_sms_detail(event.event_payload) is None
        assert dispatch._link_for_event(event) == links.work_order_detail(wo.id)

        deliver(db_session, event)
        (row,) = inbox(db_session, recipient, event_key)
        assert row.title == title and row.body == body
        assert row.link == links.work_order_detail(wo.id)
        assert row.related_type == "comment"

    (log,) = emails(db_session, crew["mentioned"], "comment.mention")
    assert SECRET not in (log.subject or "") and SECRET not in (log.body or "")
    assert "By: Avery Quill" in log.body


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------


def test_a_mention_reaches_only_the_people_mentioned(db_session, crew, redis):
    wo = make_wo(db_session)
    watch(db_session, crew["watcher"], wo)
    comment(db_session, crew["author"], wo, mentions=[crew["mentioned"].id])

    (event,) = events(db_session, "comment_mentioned")
    deliver(db_session, event)

    assert [n.user_id for n in inbox(db_session, crew["mentioned"], "comment.mention")] == [crew["mentioned"].id]
    for user in (crew["author"], crew["watcher"], crew["bystander"]):
        assert inbox(db_session, user, "comment.mention") == []
    # The mention's email leg goes to the mentioned person (catalog default + mandatory in-app).
    assert len(emails(db_session, crew["mentioned"], "comment.mention")) == 1


def test_comment_added_reaches_watchers_minus_the_mentioned_minus_the_author(db_session, crew, redis):
    wo = make_wo(db_session)
    for user in (crew["watcher"], crew["mentioned"]):
        watch(db_session, user, wo)
    comment(db_session, crew["author"], wo, mentions=[crew["mentioned"].id])

    deliver_all(db_session)

    assert len(inbox(db_session, crew["watcher"], "comment.added")) == 1
    assert inbox(db_session, crew["watcher"], "comment.mention") == []
    # The mentioned watcher gets the mention, not both.
    assert len(inbox(db_session, crew["mentioned"], "comment.mention")) == 1
    assert inbox(db_session, crew["mentioned"], "comment.added") == []
    # The author (auto-watching now) is never notified of their own comment.
    assert inbox(db_session, crew["author"]) == []
    assert inbox(db_session, crew["bystander"]) == []


def test_the_author_is_never_notified_even_when_mentioning_themselves(db_session, crew, redis):
    wo = make_wo(db_session)
    comment(db_session, crew["author"], wo, mentions=[crew["author"].id])
    deliver_all(db_session)
    assert inbox(db_session, crew["author"]) == []


def test_auto_watch_makes_the_next_comment_reach_the_earlier_participants(db_session, crew, redis):
    wo = make_wo(db_session)
    comment(db_session, crew["author"], wo, "first", mentions=[crew["mentioned"].id])
    comment(db_session, crew["watcher"], wo, "second")  # no mentions: everyone watching hears it

    _, second_added = events(db_session, "comment_added")
    deliver(db_session, second_added)

    assert len(inbox(db_session, crew["author"], "comment.added")) == 1
    assert len(inbox(db_session, crew["mentioned"], "comment.added")) == 1
    assert inbox(db_session, crew["watcher"], "comment.added") == []  # the second author


def test_an_edit_notifies_only_the_newly_mentioned(db_session, crew, redis):
    wo = make_wo(db_session)
    first = comment(db_session, crew["author"], wo, mentions=[crew["mentioned"].id])
    deliver_all(db_session)
    assert len(inbox(db_session, crew["mentioned"], "comment.mention")) == 1

    comment_service.update_comment(
        db_session,
        company_id=COMPANY_A,
        actor=crew["author"],
        comment_id=first.id,
        body="edited text",
        mentioned_user_ids=[crew["mentioned"].id, crew["watcher"].id],
        audit=AuditService(db_session, crew["author"]),
    )
    db_session.commit()
    edit_event = events(db_session, "comment_mentioned")[-1]
    deliver(db_session, edit_event)

    assert len(inbox(db_session, crew["mentioned"], "comment.mention")) == 1, "not re-notified"
    assert len(inbox(db_session, crew["watcher"], "comment.mention")) == 1
    assert len(events(db_session, "comment_added")) == 1, "an edit is not a new comment"


def test_a_person_deactivated_before_dispatch_is_not_notified(db_session, crew, redis):
    wo = make_wo(db_session)
    watch(db_session, crew["watcher"], wo)
    comment(db_session, crew["author"], wo, mentions=[crew["mentioned"].id])
    for key in ("mentioned", "watcher"):
        db_session.get(User, crew[key].id).is_active = False
    db_session.commit()

    deliver_all(db_session)

    assert inbox(db_session, crew["mentioned"]) == []
    assert inbox(db_session, crew["watcher"]) == []


def test_a_soft_deleted_work_order_notifies_nobody(db_session, crew, redis):
    wo = make_wo(db_session)
    watch(db_session, crew["watcher"], wo)
    comment(db_session, crew["author"], wo, mentions=[crew["mentioned"].id])
    db_session.get(WorkOrder, wo.id).soft_delete(crew["bystander"].id)
    db_session.commit()

    (added,) = events(db_session, "comment_added")
    (mentioned,) = events(db_session, "comment_mentioned")
    assert resolve_comment_watchers(db_session, added, COMPANY_A) == []
    assert resolve_comment_mentioned_users(db_session, mentioned, COMPANY_A) == []
    deliver_all(db_session)
    assert db_session.query(Notification).count() == 0


def test_resolvers_are_tenant_scoped(db_session, crew, redis):
    wo = make_wo(db_session)
    foreign = make_user(db_session, company_id=COMPANY_B)
    watch(db_session, crew["watcher"], wo)
    comment(db_session, crew["author"], wo, mentions=[crew["mentioned"].id])
    (added,) = events(db_session, "comment_added")
    (mentioned,) = events(db_session, "comment_mentioned")

    # A forged foreign id in the payload and a corrupt foreign watcher row reach nobody.
    mentioned.event_payload = {**mentioned.event_payload, "mentioned_user_ids": [crew["mentioned"].id, foreign.id]}
    db_session.add(EntityWatcher(company_id=COMPANY_A, entity_type="work_order", entity_id=wo.id, user_id=foreign.id))
    db_session.commit()

    assert [u.id for u in resolve_comment_mentioned_users(db_session, mentioned, COMPANY_A)] == [crew["mentioned"].id]
    assert foreign.id not in {u.id for u in resolve_comment_watchers(db_session, added, COMPANY_A)}
    # The event's company is the scope: resolved under another company, the record is not found.
    assert resolve_comment_watchers(db_session, added, COMPANY_B) == []
    assert resolve_comment_mentioned_users(db_session, mentioned, COMPANY_B) == []


# ---------------------------------------------------------------------------
# Dedup: keyed on the comment, so a second comment inside five minutes still notifies
# ---------------------------------------------------------------------------


def test_two_comments_on_one_work_order_within_five_minutes_both_notify(db_session, crew, redis):
    wo = make_wo(db_session)
    watch(db_session, crew["watcher"], wo)
    comment(db_session, crew["author"], wo, "first", mentions=[crew["mentioned"].id])
    comment(db_session, crew["author"], wo, "second", mentions=[crew["mentioned"].id])

    deliver_all(db_session)

    assert len(inbox(db_session, crew["mentioned"], "comment.mention")) == 2
    assert len(inbox(db_session, crew["watcher"], "comment.added")) == 2
    assert len(emails(db_session, crew["mentioned"], "comment.mention")) == 2
    assert all(key.startswith("werco:notify:dedup:comment.") for key in redis.keys)
    assert any(":comment:" in key for key in redis.keys)


def test_the_dedup_still_absorbs_a_redelivered_event(db_session, crew, redis):
    """The window still does its job: the SAME event dispatched twice (a retry) notifies once."""
    wo = make_wo(db_session)
    comment(db_session, crew["author"], wo, mentions=[crew["mentioned"].id])
    (event,) = events(db_session, "comment_mentioned")
    deliver(db_session, event)
    deliver(db_session, event)
    assert len(inbox(db_session, crew["mentioned"], "comment.mention")) == 1


# ---------------------------------------------------------------------------
# Email recipients: person-addressed, never a company-wide audience
# ---------------------------------------------------------------------------


def test_comment_events_are_not_admin_email_recipient_events():
    assert {"comment.mention", "comment.added"} <= EMAIL_RECIPIENT_EXCLUDED_EVENTS
    assert "comment.mention" not in EMAIL_EVENTS
    assert "comment.added" not in EMAIL_EVENTS


def test_a_saved_recipient_list_has_no_effect_on_mention_email(db_session, crew, redis):
    """A list saved for comment.mention (it was listed while dormant) must neither drop the
    mentioned person's email nor email the people on the list."""
    db_session.add(
        QuoteSettings(
            company_id=COMPANY_A,
            setting_key=setting_key("comment.mention"),
            setting_value=json.dumps([crew["bystander"].id]),
            setting_type="json",
        )
    )
    db_session.commit()
    assert email_recipient_ids(db_session, COMPANY_A, "comment.mention") is None

    wo = make_wo(db_session)
    comment(db_session, crew["author"], wo, mentions=[crew["mentioned"].id])
    (event,) = events(db_session, "comment_mentioned")
    deliver(db_session, event)

    assert len(emails(db_session, crew["mentioned"], "comment.mention")) == 1
    assert emails(db_session, crew["bystander"], "comment.mention") == []
    assert inbox(db_session, crew["bystander"]) == []


def test_an_empty_saved_list_does_not_switch_mention_email_off(db_session, crew, redis):
    db_session.add(
        QuoteSettings(
            company_id=COMPANY_A,
            setting_key=setting_key("comment.mention"),
            setting_value=json.dumps([]),
            setting_type="json",
        )
    )
    db_session.commit()
    wo = make_wo(db_session)
    comment(db_session, crew["author"], wo, mentions=[crew["mentioned"].id])
    (event,) = events(db_session, "comment_mentioned")
    deliver(db_session, event)
    assert len(emails(db_session, crew["mentioned"], "comment.mention")) == 1


def test_the_admin_settings_screen_does_not_list_or_accept_the_comment_events(client, db_session, admin_headers):
    url = "/api/v1/admin/settings/email-recipients"
    listed = client.get(url, headers=admin_headers)
    assert listed.status_code == 200, listed.text
    keys = {event["event_key"] for event in listed.json()["events"]}
    assert "comment.mention" not in keys and "comment.added" not in keys
    for event_key in ("comment.mention", "comment.added"):
        response = client.put(f"{url}/{event_key}", json={"user_ids": []}, headers=admin_headers)
        assert response.status_code == 400, response.text
    assert db_session.query(QuoteSettings).count() == 0


# ---------------------------------------------------------------------------
# Best-effort: a failed notification never fails the comment
# ---------------------------------------------------------------------------


def test_a_failed_emit_rolls_back_only_its_savepoint(db_session, crew, monkeypatch):
    wo = make_wo(db_session)

    def boom(self, **kwargs):
        raise RuntimeError("event store down")

    monkeypatch.setattr(comment_service.OperationalEventService, "emit", boom)
    row = comment(db_session, crew["author"], wo, "still saved", mentions=[crew["mentioned"].id])

    db_session.expire_all()
    assert row.id is not None
    assert db_session.query(OperationalEvent).count() == 0
    assert db_session.get(Comment, row.id).body == "still saved"
    assert db_session.info.get("pending_notification_event_ids") in (None, [])
