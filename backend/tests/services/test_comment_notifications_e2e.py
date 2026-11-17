"""``comment.mention`` / ``comment.added`` -- delivered all the way to the rendered email.

``tests/services/test_comment_notifications.py`` pins recipients, dedup and the title / body the
dispatcher composes. What it leaves unpinned is the last hop: the ``send_email_job`` the
dispatcher actually enqueues and the HTML the worker renders from it. Pinned here, through the
real ``dispatch_for_event`` and the real ``notification`` email template:

* exactly one email is enqueued per comment -- the mention's, to the mentioned person; the
  ``comment.added`` watcher leg is in-app only;
* its subject names the work order, its rendered HTML and plain text name the work order and
  the author ("By: ...") and carry the absolute work-order deep link -- and never the comment
  text;
* the author's name reaches the HTML escaped (it is the one person-supplied string the payload
  carries into email);
* who gets an in-app row, in one scenario: the mentioned person (the mention, not both), a
  watcher (``comment.added``); never the author, a watcher deactivated before the comment, or
  someone who unwatched;
* a comment a Platform Admin posts while viewing another company notifies that company's
  watchers and mentioned people, tenant-scoped to it -- and never the platform admin, who is
  not a member of that company even though the post auto-watched them.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Dict, List, Optional
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

import app.services.notification_dispatch as dispatch
from app.core.config import settings
from app.models.notification import Notification, NotificationLog
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.services import comment_service
from app.services import notification_links as links
from app.services.audit_service import AuditService
from app.services.email_service import EmailService
from app.services.notification_catalog import CATALOG, CHANNEL_EMAIL
from tests.api.kiosk_test_helpers import COMPANY_A, COMPANY_B, ensure_company, make_user, user_headers

pytestmark = [pytest.mark.requires_db]

SECRET = "Customer says the 0.250 flange bore is out -- rework before Friday"
BASE_URL = "https://erp.example.test"
_seq = itertools.count(1)


class FakeRedis:
    """``SET key value EX n NX`` semantics: the first reservation of a key wins."""

    def __init__(self) -> None:
        self.keys: Dict[str, str] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


@pytest.fixture
def mail(monkeypatch) -> AsyncMock:
    """The dispatcher's ``enqueue_job``, captured; dedup against a fake Redis."""
    fake = FakeRedis()

    async def pool():
        return fake

    enqueue = AsyncMock()
    monkeypatch.setattr(dispatch, "get_redis_pool", pool)
    monkeypatch.setattr(dispatch, "enqueue_job", enqueue)
    monkeypatch.setattr(settings, "FRONTEND_BASE_URL", BASE_URL)
    return enqueue


def make_wo(db: Session, *, company_id: int = COMPANY_A) -> WorkOrder:
    ensure_company(db, company_id)
    n = next(_seq)
    part = Part(
        part_number=f"E2E-P-{company_id}-{n:04d}",
        name=f"Gusset {n}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"WO-E2E-{company_id}-{n:04d}",
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


def post(db: Session, actor: User, wo: WorkOrder, body: str = SECRET, mentions: Optional[List[int]] = None):
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


def set_watch(db: Session, user: User, wo: WorkOrder, *, on: bool) -> None:
    verb = comment_service.watch if on else comment_service.unwatch
    verb(
        db,
        company_id=wo.company_id,
        actor=user,
        entity_type="work_order",
        entity_id=wo.id,
        audit=AuditService(db, user),
    )
    db.commit()


def deliver_all(db: Session) -> None:
    db.expire_all()
    for event in (
        db.query(OperationalEvent)
        .filter(OperationalEvent.source_module == "comments")
        .order_by(OperationalEvent.id)
        .all()
    ):
        asyncio.run(dispatch.dispatch_for_event(db, event))
        db.commit()


def inbox(db: Session, user: User) -> List[Notification]:
    db.expire_all()
    return db.query(Notification).filter(Notification.user_id == user.id).order_by(Notification.id).all()


def email_jobs(mail: AsyncMock) -> List[dict]:
    return [call.kwargs for call in mail.await_args_list if call.args and call.args[0] == "send_email_job"]


def render(job: dict) -> str:
    return EmailService()._render_template(job["template"], job["context"])


@pytest.fixture
def crew(db_session: Session) -> Dict[str, User]:
    return {
        "author": make_user(db_session, role=UserRole.OPERATOR, first_name="Avery", last_name="Quill"),
        "mentioned": make_user(db_session, role=UserRole.SUPERVISOR, first_name="Blake", last_name="Marrow"),
        "watcher": make_user(db_session, role=UserRole.VIEWER, first_name="Casey", last_name="Fernhill"),
        "lapsed": make_user(db_session, role=UserRole.QUALITY, first_name="Devon", last_name="Lapsley"),
        "unwatched": make_user(db_session, role=UserRole.SHIPPING, first_name="Ellis", last_name="Unwin"),
        "bystander": make_user(db_session, role=UserRole.MANAGER, first_name="Frankie", last_name="Holt"),
    }


def test_one_comment_end_to_end_who_is_notified_and_what_the_email_says(db_session, crew, mail):
    wo = make_wo(db_session)
    for key in ("watcher", "lapsed", "unwatched"):
        set_watch(db_session, crew[key], wo, on=True)
    set_watch(db_session, crew["unwatched"], wo, on=False)
    db_session.get(User, crew["lapsed"].id).is_active = False  # deactivated BEFORE the comment
    db_session.commit()

    post(db_session, crew["author"], wo, mentions=[crew["mentioned"].id])
    deliver_all(db_session)

    # In-app: the mention to the mentioned, comment.added to the remaining active watcher.
    (mention_row,) = inbox(db_session, crew["mentioned"])
    assert mention_row.event_key == "comment.mention"
    (added_row,) = inbox(db_session, crew["watcher"])
    assert added_row.event_key == "comment.added"
    for key in ("author", "lapsed", "unwatched", "bystander"):
        assert inbox(db_session, crew[key]) == [], key
    for row in (mention_row, added_row):
        assert row.company_id == COMPANY_A
        assert row.link == links.work_order_detail(wo.id)
        assert wo.work_order_number in row.title
        assert SECRET not in f"{row.title}\n{row.body}"

    # Email: exactly one job -- the mention's. comment.added is in-app only.
    (job,) = email_jobs(mail)
    mentioned = crew["mentioned"]
    assert job["to"] == mentioned.email
    assert job["user_id"] == mentioned.id and job["company_id"] == COMPANY_A
    assert job["subject"] == f"{CATALOG['comment.mention'].label}: {wo.work_order_number}"
    assert job["template"] == "notification"
    assert job["context"]["notification_link"] == f"{BASE_URL}/work-orders/{wo.id}"
    assert SECRET not in repr(job)

    html = render(job)
    plain = EmailService()._html_to_plain(html)
    for text in (html, plain):
        assert wo.work_order_number in text
        assert "By: Avery Quill" in text
        assert CATALOG["comment.mention"].description in text
        assert SECRET not in text
        assert "flange bore" not in text
    assert f'href="{BASE_URL}/work-orders/{wo.id}"' in html

    # The audit trail of the send: the log row matches the job and links the in-app row.
    db_session.expire_all()
    (log,) = (
        db_session.query(NotificationLog)
        .filter(NotificationLog.user_id == mentioned.id, NotificationLog.channel == CHANNEL_EMAIL)
        .all()
    )
    assert log.subject == job["subject"]
    assert log.notification_id == mention_row.id
    assert job["notification_log_id"] == log.id
    assert db_session.query(NotificationLog).filter(NotificationLog.channel == CHANNEL_EMAIL).count() == 1


def test_the_authors_name_reaches_the_email_escaped(db_session, crew, mail):
    wo = make_wo(db_session)
    author = make_user(db_session, role=UserRole.OPERATOR, first_name="Ola <b>Bold</b>", last_name="O'Neil & Sons")
    post(db_session, author, wo, "plain words", mentions=[crew["mentioned"].id])
    deliver_all(db_session)

    (job,) = email_jobs(mail)
    html = render(job)
    assert "<b>Bold</b>" not in html
    assert "By: Ola &lt;b&gt;Bold&lt;/b&gt; O&#39;Neil &amp; Sons" in html


def test_a_platform_admins_comment_in_another_company_notifies_that_company_only(client, db_session, mail):
    platform_admin = make_user(
        db_session, role=UserRole.PLATFORM_ADMIN, company_id=COMPANY_A, first_name="Plato", last_name="Oversight"
    )
    b_wo = make_wo(db_session, company_id=COMPANY_B)
    b_watcher = make_user(db_session, company_id=COMPANY_B, role=UserRole.VIEWER, first_name="Gale", last_name="Ward")
    b_mentioned = make_user(db_session, company_id=COMPANY_B, first_name="Hollis", last_name="Reed")
    set_watch(db_session, b_watcher, b_wo, on=True)
    viewing_b = user_headers(platform_admin, active_company_id=COMPANY_B)

    created = client.post(
        "/api/v1/comments",
        json={
            "entity_type": "work_order",
            "entity_id": b_wo.id,
            "body": SECRET,
            "mentioned_user_ids": [b_mentioned.id],
        },
        headers=viewing_b,
    )
    assert created.status_code == 201, created.text
    deliver_all(db_session)

    (mention_row,) = inbox(db_session, b_mentioned)
    (added_row,) = inbox(db_session, b_watcher)
    assert (mention_row.event_key, added_row.event_key) == ("comment.mention", "comment.added")
    for row in (mention_row, added_row):
        assert row.company_id == COMPANY_B
        assert row.body.endswith("By: Plato Oversight")
        assert SECRET not in f"{row.title}\n{row.body}"
    (job,) = email_jobs(mail)
    assert job["to"] == b_mentioned.email and job["company_id"] == COMPANY_B

    # A later comment by a company-B person: the platform admin's post auto-watched them, but a
    # notification is delivered only to members of the event's company.
    post(db_session, b_watcher, b_wo, "follow-up")
    deliver_all(db_session)
    assert inbox(db_session, platform_admin) == []
    assert {row.company_id for row in db_session.query(Notification).all()} == {COMPANY_B}
