"""Comments with @mentions and watchers on work orders -- the API (docs/NOTIFICATIONS_PLAN.md 3.6).

Pinned here:

* CRUD and moderation: any company member who can read a work order comments on it (VIEWER
  included); only the author edits (an Admin cannot rewrite someone's words); the author, an
  Admin or a Manager deletes; delete is soft and hides the comment.
* Tenant isolation: a foreign or soft-deleted work order is a 404 on every verb, a foreign
  comment id is a 404, and a foreign / inactive person cannot be mentioned -- the 422 names ids,
  never names.
* Audit rows for create / edit / delete (soft) and watch / unwatch, and the caller's
  ``audit_extra`` (Hank's ``{"via": "hank"}``) merged in under the service's own keys.
* People only: every write refuses every API token 403 (full, read-only, AI-external) and writes
  nothing; the reads stay open to the tokens the read gates admit; a kiosk badge token is fenced.
* Mention search returns ``{id, display_name}`` only, is reachable by a non-admin (route order
  above ``/users/{user_id}``), and excludes inactive and foreign people.
* Auto-watch: the author and every mentioned person start watching.
* The notification events are emitted with the COMMENT as their entity and never carry the text.

Notification delivery (recipients, dedup, content, email recipients) is pinned in
``tests/services/test_comment_notifications.py``.
"""

from __future__ import annotations

import itertools
from typing import Dict, List

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.credentials import API_TOKEN_COMMENT_WRITE, READ_ONLY_ROUTE_WRITES_DETAIL
from app.core.security import create_access_token
from app.mcp.catalog import COMMENT_WRITE_OPERATIONS
from app.models.audit_log import AuditLog
from app.models.comment import Comment, CommentMention, EntityWatcher
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.services import comment_service
from app.services.audit_service import AuditService
from tests.api.export_control_helpers import bearer, issue_token
from tests.api.kiosk_test_helpers import COMPANY_A, COMPANY_B, ensure_company, make_user, user_headers

pytestmark = pytest.mark.api

API = "/api/v1"
COMMENTS = f"{API}/comments"
WATCHERS = f"{API}/watchers"
MENTION_SEARCH = f"{API}/users/mention-search"

_seq = itertools.count(1)


def make_wo(db: Session, *, company_id: int = COMPANY_A, deleted: bool = False) -> WorkOrder:
    ensure_company(db, company_id)
    n = next(_seq)
    part = Part(
        part_number=f"CMT-P-{company_id}-{n:04d}",
        name=f"Bracket {n}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"WO-CMT-{company_id}-{n:04d}",
        part_id=part.id,
        quantity_ordered=5,
        status="draft",
        priority=3,
        company_id=company_id,
    )
    if deleted:
        wo.soft_delete(None)
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def post_comment(client: TestClient, headers: dict, wo: WorkOrder, body: str = "Check the weld prep.", **extra):
    return client.post(
        COMMENTS, json={"entity_type": "work_order", "entity_id": wo.id, "body": body, **extra}, headers=headers
    )


def list_comments(client: TestClient, headers: dict, wo: WorkOrder):
    return client.get(COMMENTS, params={"entity_type": "work_order", "entity_id": wo.id}, headers=headers)


def watcher_ids(db: Session, wo: WorkOrder) -> List[int]:
    db.expire_all()
    rows = (
        db.query(EntityWatcher)
        .filter(EntityWatcher.entity_type == "work_order", EntityWatcher.entity_id == wo.id)
        .order_by(EntityWatcher.user_id)
        .all()
    )
    return [row.user_id for row in rows]


def audit_rows(db: Session, resource_type: str, resource_id=None) -> List[AuditLog]:
    db.expire_all()
    query = db.query(AuditLog).filter(AuditLog.resource_type == resource_type)
    if resource_id is not None:
        query = query.filter(AuditLog.resource_id == resource_id)
    return query.order_by(AuditLog.id).all()


@pytest.fixture
def people(db_session: Session) -> Dict[str, User]:
    return {
        "author": make_user(db_session, role=UserRole.OPERATOR, first_name="Avery", last_name="Quill"),
        "colleague": make_user(db_session, role=UserRole.SUPERVISOR, first_name="Blake", last_name="Marrow"),
        "viewer": make_user(db_session, role=UserRole.VIEWER, first_name="Casey", last_name="Fernhill"),
        "admin": make_user(db_session, role=UserRole.ADMIN, first_name="Dana", last_name="Holloway"),
        "manager": make_user(db_session, role=UserRole.MANAGER, first_name="Emery", last_name="Stroud"),
        "other_operator": make_user(db_session, role=UserRole.OPERATOR, first_name="Finley", last_name="Ashgrove"),
        "inactive": make_user(
            db_session, role=UserRole.OPERATOR, first_name="Gray", last_name="Dormant", is_active=False
        ),
        "foreign": make_user(db_session, company_id=COMPANY_B, first_name="Harper", last_name="Elsewhere"),
    }


# ---------------------------------------------------------------------------
# CRUD + moderation
# ---------------------------------------------------------------------------


class TestCommentLifecycle:
    def test_a_viewer_comments_and_the_thread_lists_it(self, client, db_session, people):
        wo = make_wo(db_session)
        viewer = people["viewer"]

        response = post_comment(client, user_headers(viewer), wo, "  Drawing rev looks stale.  ")

        assert response.status_code == 201, response.text
        body = response.json()
        assert set(body) == {
            "id",
            "entity_type",
            "entity_id",
            "author",
            "body",
            "created_at",
            "edited_at",
            "mentions",
            "can_edit",
            "can_delete",
        }
        assert body["entity_type"] == "work_order" and body["entity_id"] == wo.id
        assert body["author"] == {"id": viewer.id, "display_name": "Casey Fernhill"}
        assert body["body"] == "Drawing rev looks stale."  # stripped
        assert body["edited_at"] is None
        assert body["created_at"].endswith("Z")
        assert body["mentions"] == []
        assert body["can_edit"] is True and body["can_delete"] is True

        listed = list_comments(client, user_headers(people["colleague"]), wo)
        assert listed.status_code == 200
        (row,) = listed.json()
        assert row["id"] == body["id"]
        # Another person sees it but may neither edit nor delete it.
        assert row["can_edit"] is False and row["can_delete"] is False

    def test_the_thread_is_oldest_first(self, client, db_session, people):
        wo = make_wo(db_session)
        headers = user_headers(people["author"])
        ids = [post_comment(client, headers, wo, f"note {i}").json()["id"] for i in range(3)]
        assert [row["id"] for row in list_comments(client, headers, wo).json()] == ids

    def test_mentions_are_returned_as_id_and_name_and_auto_watch(self, client, db_session, people):
        wo = make_wo(db_session)
        author, colleague, viewer = people["author"], people["colleague"], people["viewer"]

        response = post_comment(
            client, user_headers(author), wo, "Please look", mentioned_user_ids=[colleague.id, viewer.id]
        )

        assert response.status_code == 201, response.text
        assert response.json()["mentions"] == [
            {"id": colleague.id, "display_name": "Blake Marrow"},
            {"id": viewer.id, "display_name": "Casey Fernhill"},
        ]
        assert watcher_ids(db_session, wo) == sorted([author.id, colleague.id, viewer.id])

    def test_auto_watch_is_idempotent(self, client, db_session, people):
        wo = make_wo(db_session)
        headers = user_headers(people["author"])
        post_comment(client, headers, wo, "one", mentioned_user_ids=[people["colleague"].id])
        second = post_comment(client, headers, wo, "two", mentioned_user_ids=[people["colleague"].id])
        assert second.status_code == 201
        assert watcher_ids(db_session, wo) == sorted([people["author"].id, people["colleague"].id])

    def test_mentioning_yourself_is_allowed(self, client, db_session, people):
        wo = make_wo(db_session)
        author = people["author"]
        response = post_comment(client, user_headers(author), wo, "note to self", mentioned_user_ids=[author.id])
        assert response.status_code == 201, response.text
        assert response.json()["mentions"] == [{"id": author.id, "display_name": "Avery Quill"}]

    def test_the_author_edits_and_the_mention_set_is_replaced(self, client, db_session, people):
        wo = make_wo(db_session)
        author, colleague, viewer = people["author"], people["colleague"], people["viewer"]
        headers = user_headers(author)
        comment_id = post_comment(client, headers, wo, "first", mentioned_user_ids=[colleague.id]).json()["id"]

        response = client.patch(
            f"{COMMENTS}/{comment_id}", json={"body": "second", "mentioned_user_ids": [viewer.id]}, headers=headers
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["body"] == "second"
        assert body["edited_at"] is not None and body["edited_at"].endswith("Z")
        assert body["mentions"] == [{"id": viewer.id, "display_name": "Casey Fernhill"}]
        db_session.expire_all()
        # A dropped mention row is physically gone (mention rows are not SoftDeleteMixin).
        rows = db_session.query(CommentMention).filter(CommentMention.comment_id == comment_id).all()
        assert [row.user_id for row in rows] == [viewer.id]
        assert viewer.id in watcher_ids(db_session, wo)

    def test_an_unchanged_edit_writes_nothing(self, client, db_session, people):
        wo = make_wo(db_session)
        headers = user_headers(people["author"])
        comment_id = post_comment(client, headers, wo, "same", mentioned_user_ids=[people["colleague"].id]).json()["id"]
        before = len(audit_rows(db_session, "comment", comment_id))

        response = client.patch(
            f"{COMMENTS}/{comment_id}",
            json={"body": "  same ", "mentioned_user_ids": [people["colleague"].id]},
            headers=headers,
        )

        assert response.status_code == 200
        assert response.json()["edited_at"] is None
        assert len(audit_rows(db_session, "comment", comment_id)) == before

    @pytest.mark.parametrize("editor", ["colleague", "admin", "manager"])
    def test_nobody_but_the_author_edits(self, client, db_session, people, editor):
        wo = make_wo(db_session)
        comment_id = post_comment(client, user_headers(people["author"]), wo, "mine").json()["id"]

        response = client.patch(
            f"{COMMENTS}/{comment_id}",
            json={"body": "rewritten", "mentioned_user_ids": []},
            headers=user_headers(people[editor]),
        )

        assert response.status_code == 403
        assert response.json()["detail"] == comment_service.EDIT_FORBIDDEN_DETAIL
        db_session.expire_all()
        assert db_session.get(Comment, comment_id).body == "mine"

    @pytest.mark.parametrize("deleter", ["author", "admin", "manager"])
    def test_the_author_an_admin_or_a_manager_deletes(self, client, db_session, people, deleter):
        wo = make_wo(db_session)
        comment_id = post_comment(client, user_headers(people["author"]), wo, "remove me").json()["id"]

        response = client.delete(f"{COMMENTS}/{comment_id}", headers=user_headers(people[deleter]))

        assert response.status_code == 204, response.text
        assert response.content == b""
        db_session.expire_all()
        row = db_session.get(Comment, comment_id)
        assert row is not None, "a comment is soft-deleted, never removed"
        assert row.is_deleted is True and row.deleted_by == people[deleter].id
        assert list_comments(client, user_headers(people["author"]), wo).json() == []

    @pytest.mark.parametrize("deleter", ["other_operator", "colleague", "viewer"])
    def test_anyone_else_cannot_delete(self, client, db_session, people, deleter):
        wo = make_wo(db_session)
        comment_id = post_comment(client, user_headers(people["author"]), wo, "stays").json()["id"]

        response = client.delete(f"{COMMENTS}/{comment_id}", headers=user_headers(people[deleter]))

        assert response.status_code == 403
        assert response.json()["detail"] == comment_service.DELETE_FORBIDDEN_DETAIL
        db_session.expire_all()
        assert db_session.get(Comment, comment_id).is_deleted is False

    def test_a_deleted_comment_is_404_to_edit_or_delete_again(self, client, db_session, people):
        wo = make_wo(db_session)
        headers = user_headers(people["author"])
        comment_id = post_comment(client, headers, wo, "gone").json()["id"]
        assert client.delete(f"{COMMENTS}/{comment_id}", headers=headers).status_code == 204

        assert client.delete(f"{COMMENTS}/{comment_id}", headers=headers).status_code == 404
        response = client.patch(
            f"{COMMENTS}/{comment_id}", json={"body": "x", "mentioned_user_ids": []}, headers=headers
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize(
        "payload",
        [
            {"body": "   "},
            {"body": ""},
            {"body": "x" * 5001},
            {"body": "nul\x00byte"},
            {"body": "ok", "mentioned_user_ids": [1, 1]},
            {"body": "ok", "mentioned_user_ids": [0]},
            {"body": "ok", "mentioned_user_ids": list(range(1, 22))},
            {"body": "ok", "mentioned_user_ids": [True]},
            {"body": "ok", "surprise": 1},
        ],
    )
    def test_bad_bodies_are_422_and_write_nothing(self, client, db_session, people, payload):
        wo = make_wo(db_session)
        response = client.post(
            COMMENTS,
            json={"entity_type": "work_order", "entity_id": wo.id, **payload},
            headers=user_headers(people["author"]),
        )
        assert response.status_code == 422, response.text
        assert db_session.query(Comment).count() == 0

    def test_a_5000_character_body_is_accepted(self, client, db_session, people):
        wo = make_wo(db_session)
        assert post_comment(client, user_headers(people["author"]), wo, "y" * 5000).status_code == 201

    @pytest.mark.parametrize("entity_type", ["purchase_order", "ncr", "WORK_ORDER", "work_orders"])
    def test_only_work_orders_are_enabled(self, client, db_session, people, entity_type):
        wo = make_wo(db_session)
        headers = user_headers(people["author"])
        response = client.post(
            COMMENTS, json={"entity_type": entity_type, "entity_id": wo.id, "body": "x"}, headers=headers
        )
        assert response.status_code == 422
        assert (
            client.get(COMMENTS, params={"entity_type": entity_type, "entity_id": wo.id}, headers=headers).status_code
            == 422
        )
        assert (
            client.post(WATCHERS, json={"entity_type": entity_type, "entity_id": wo.id}, headers=headers).status_code
            == 422
        )

    def test_patch_requires_both_fields(self, client, db_session, people):
        wo = make_wo(db_session)
        headers = user_headers(people["author"])
        comment_id = post_comment(client, headers, wo).json()["id"]
        assert client.patch(f"{COMMENTS}/{comment_id}", json={"body": "only body"}, headers=headers).status_code == 422


# ---------------------------------------------------------------------------
# Tenant isolation + soft-deleted records
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    def test_a_foreign_work_order_is_404_on_every_verb(self, client, db_session, people):
        foreign_wo = make_wo(db_session, company_id=COMPANY_B)
        headers = user_headers(people["author"])
        params = {"entity_type": "work_order", "entity_id": foreign_wo.id}

        assert post_comment(client, headers, foreign_wo).status_code == 404
        assert client.get(COMMENTS, params=params, headers=headers).status_code == 404
        assert client.get(WATCHERS, params=params, headers=headers).status_code == 404
        assert client.post(WATCHERS, json=params, headers=headers).status_code == 404
        assert client.delete(WATCHERS, params=params, headers=headers).status_code == 404
        assert db_session.query(Comment).count() == 0
        assert db_session.query(EntityWatcher).count() == 0

    def test_a_foreign_comment_id_is_404(self, client, db_session, people):
        foreign_wo = make_wo(db_session, company_id=COMPANY_B)
        foreign_author = people["foreign"]
        comment_id = post_comment(client, user_headers(foreign_author), foreign_wo, "theirs").json()["id"]

        for headers in (user_headers(people["author"]), user_headers(people["admin"])):
            assert client.delete(f"{COMMENTS}/{comment_id}", headers=headers).status_code == 404
            response = client.patch(
                f"{COMMENTS}/{comment_id}", json={"body": "mine now", "mentioned_user_ids": []}, headers=headers
            )
            assert response.status_code == 404
        db_session.expire_all()
        row = db_session.get(Comment, comment_id)
        assert row.body == "theirs" and row.is_deleted is False

    def test_a_foreign_or_inactive_person_cannot_be_mentioned_and_no_name_leaks(self, client, db_session, people):
        wo = make_wo(db_session)
        foreign, inactive = people["foreign"], people["inactive"]
        missing_id = 999_999

        response = post_comment(
            client,
            user_headers(people["author"]),
            wo,
            "hello",
            mentioned_user_ids=[people["colleague"].id, foreign.id, inactive.id, missing_id],
        )

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail == (
            f"These people can't be mentioned on this work order: user {foreign.id}, user {inactive.id}, "
            f"user {missing_id}."
        )
        for leaked in ("Harper", "Elsewhere", "Gray", "Dormant", "Blake"):
            assert leaked not in detail
        assert db_session.query(Comment).count() == 0
        assert db_session.query(EntityWatcher).count() == 0

    def test_an_edit_cannot_add_a_foreign_mention(self, client, db_session, people):
        wo = make_wo(db_session)
        headers = user_headers(people["author"])
        comment_id = post_comment(client, headers, wo).json()["id"]
        response = client.patch(
            f"{COMMENTS}/{comment_id}",
            json={"body": "edited", "mentioned_user_ids": [people["foreign"].id]},
            headers=headers,
        )
        assert response.status_code == 422
        db_session.expire_all()
        assert db_session.get(Comment, comment_id).edited_at is None

    def test_the_thread_never_lists_another_companys_comment(self, client, db_session, people):
        wo = make_wo(db_session)
        post_comment(client, user_headers(people["author"]), wo, "ours")
        # A foreign row pointing at OUR work order id (forged directly) must not surface.
        db_session.add(
            Comment(
                company_id=COMPANY_B,
                entity_type="work_order",
                entity_id=wo.id,
                author_id=people["foreign"].id,
                body="theirs",
            )
        )
        db_session.commit()
        bodies = [row["body"] for row in list_comments(client, user_headers(people["author"]), wo).json()]
        assert bodies == ["ours"]

    def test_a_soft_deleted_work_order_is_404_on_every_verb(self, client, db_session, people):
        wo = make_wo(db_session)
        headers = user_headers(people["author"])
        comment_id = post_comment(client, headers, wo, "before the delete").json()["id"]
        assert (
            client.post(WATCHERS, json={"entity_type": "work_order", "entity_id": wo.id}, headers=headers).status_code
            == 200
        )
        db_session.expire_all()
        live = db_session.get(WorkOrder, wo.id)
        live.soft_delete(people["admin"].id)
        db_session.commit()
        params = {"entity_type": "work_order", "entity_id": wo.id}

        assert post_comment(client, headers, wo).status_code == 404
        assert client.get(COMMENTS, params=params, headers=headers).status_code == 404
        assert (
            client.patch(
                f"{COMMENTS}/{comment_id}", json={"body": "x", "mentioned_user_ids": []}, headers=headers
            ).status_code
            == 404
        )
        assert client.delete(f"{COMMENTS}/{comment_id}", headers=user_headers(people["admin"])).status_code == 404
        assert client.get(WATCHERS, params=params, headers=headers).status_code == 404
        assert client.post(WATCHERS, json=params, headers=headers).status_code == 404
        assert client.delete(WATCHERS, params=params, headers=headers).status_code == 404
        db_session.expire_all()
        assert db_session.get(Comment, comment_id).is_deleted is False
        assert watcher_ids(db_session, wo) == [people["author"].id], "watchers survive the record's soft delete"

    def test_an_unknown_work_order_is_404(self, client, db_session, people):
        response = client.post(
            COMMENTS,
            json={"entity_type": "work_order", "entity_id": 987_654, "body": "x"},
            headers=user_headers(people["author"]),
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "Work order not found"


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class TestAudit:
    def test_create_edit_delete_are_audited(self, client, db_session, people):
        wo = make_wo(db_session)
        author, colleague, viewer = people["author"], people["colleague"], people["viewer"]
        headers = user_headers(author)
        comment_id = post_comment(client, headers, wo, "first words", mentioned_user_ids=[colleague.id]).json()["id"]
        client.patch(
            f"{COMMENTS}/{comment_id}",
            json={"body": "second words", "mentioned_user_ids": [viewer.id]},
            headers=headers,
        )
        client.delete(f"{COMMENTS}/{comment_id}", headers=user_headers(people["manager"]))

        create, update, delete = audit_rows(db_session, "comment", comment_id)

        assert create.action == "CREATE" and create.user_id == author.id and create.company_id == COMPANY_A
        assert create.new_values["body"] == "first words"
        assert create.new_values["mentioned_user_ids"] == [colleague.id]
        assert create.extra_data["entity_type"] == "work_order" and create.extra_data["entity_id"] == wo.id
        assert create.extra_data["entity_identifier"] == wo.work_order_number
        assert create.extra_data["mentioned_user_ids"] == [colleague.id]
        assert sorted(create.extra_data["auto_watched_user_ids"]) == sorted([author.id, colleague.id])

        assert update.action == "UPDATE"
        assert update.old_values == {"body": "first words", "mentioned_user_ids": [colleague.id]}
        assert update.new_values == {"body": "second words", "mentioned_user_ids": [viewer.id]}
        assert update.extra_data["added_mention_user_ids"] == [viewer.id]
        assert update.extra_data["removed_mention_user_ids"] == [colleague.id]
        assert update.extra_data["auto_watched_user_ids"] == [viewer.id]

        assert delete.action == "DELETE" and delete.user_id == people["manager"].id
        assert delete.extra_data["soft_delete"] is True
        assert delete.extra_data["moderated"] is True
        assert delete.old_values["body"] == "second words"
        assert delete.old_values["author_id"] == author.id
        assert delete.old_values["mentioned_user_ids"] == [viewer.id]

    def test_watch_and_unwatch_are_audited(self, client, db_session, people):
        wo = make_wo(db_session)
        headers = user_headers(people["colleague"])
        params = {"entity_type": "work_order", "entity_id": wo.id}

        client.post(WATCHERS, json=params, headers=headers)
        client.post(WATCHERS, json=params, headers=headers)  # idempotent: no second row
        client.delete(WATCHERS, params=params, headers=headers)
        client.delete(WATCHERS, params=params, headers=headers)  # idempotent: no second row

        rows = audit_rows(db_session, "entity_watcher")
        assert [row.action for row in rows] == ["CREATE", "DELETE"]
        assert rows[0].new_values == {
            "entity_type": "work_order",
            "entity_id": wo.id,
            "user_id": people["colleague"].id,
        }
        assert rows[1].extra_data["soft_delete"] is False

    def test_the_via_extra_is_merged_but_cannot_overwrite_the_evidence(self, db_session, people):
        wo = make_wo(db_session)
        author = people["author"]
        audit = AuditService(db_session, author)

        comment = comment_service.create_comment(
            db_session,
            company_id=COMPANY_A,
            actor=author,
            entity_type="work_order",
            entity_id=wo.id,
            body="Logged by Hank",
            mentioned_user_ids=[],
            audit=audit,
            audit_extra={"via": "hank", "entity_id": -1},
        )
        db_session.commit()

        (row,) = audit_rows(db_session, "comment", comment.id)
        assert row.extra_data["via"] == "hank"
        assert row.extra_data["entity_id"] == wo.id

    def test_create_comment_calls_log_create_exactly_once(self, db_session, people):
        """Hank's PR 4 hands in a wrapper that makes ``log_create`` a REQUIRED write; the service
        must route its only audit call through that method."""
        wo = make_wo(db_session)
        calls = []

        class Recorder:
            def log_create(self, **kwargs):
                calls.append(("log_create", kwargs))
                return object()

            def __getattr__(self, name):  # any other audit method is a contract break
                raise AssertionError(f"create_comment called audit.{name}")

        comment_service.create_comment(
            db_session,
            company_id=COMPANY_A,
            actor=people["author"],
            entity_type="work_order",
            entity_id=wo.id,
            body="one call",
            mentioned_user_ids=[people["colleague"].id],
            audit=Recorder(),
        )
        assert [name for name, _ in calls] == ["log_create"]
        assert calls[0][1]["resource_type"] == "comment"


# ---------------------------------------------------------------------------
# Watchers
# ---------------------------------------------------------------------------


class TestWatchers:
    def test_watch_state_watch_and_unwatch(self, client, db_session, people):
        wo = make_wo(db_session)
        params = {"entity_type": "work_order", "entity_id": wo.id}
        headers = user_headers(people["colleague"])

        assert client.get(WATCHERS, params=params, headers=headers).json() == {"watching": False, "watcher_count": 0}
        assert client.post(WATCHERS, json=params, headers=headers).json() == {"watching": True, "watcher_count": 1}
        assert client.post(WATCHERS, json=params, headers=headers).json() == {"watching": True, "watcher_count": 1}
        other = user_headers(people["viewer"])
        assert client.get(WATCHERS, params=params, headers=other).json() == {"watching": False, "watcher_count": 1}
        assert client.delete(WATCHERS, params=params, headers=headers).json() == {"watching": False, "watcher_count": 0}
        assert client.delete(WATCHERS, params=params, headers=headers).json() == {"watching": False, "watcher_count": 0}
        # Unwatch physically deletes the preference row.
        assert db_session.query(EntityWatcher).count() == 0

    def test_a_deactivated_watcher_is_not_counted(self, client, db_session, people):
        wo = make_wo(db_session)
        params = {"entity_type": "work_order", "entity_id": wo.id}
        client.post(WATCHERS, json=params, headers=user_headers(people["colleague"]))
        client.post(WATCHERS, json=params, headers=user_headers(people["viewer"]))
        db_session.expire_all()
        db_session.get(User, people["viewer"].id).is_active = False
        db_session.commit()
        state = client.get(WATCHERS, params=params, headers=user_headers(people["colleague"])).json()
        assert state == {"watching": True, "watcher_count": 1}


# ---------------------------------------------------------------------------
# People only: API tokens and kiosk badges
# ---------------------------------------------------------------------------


def _write_requests(wo: WorkOrder, comment_id: int, mention_id: int):
    """One request per COMMENT_WRITE_OPERATIONS route, keyed like the catalog set."""
    params = {"entity_type": "work_order", "entity_id": wo.id}
    return {
        ("POST", "/api/v1/comments"): (
            "post",
            COMMENTS,
            {"json": {**params, "body": "bot words", "mentioned_user_ids": [mention_id]}},
        ),
        ("PATCH", "/api/v1/comments/{comment_id}"): (
            "patch",
            f"{COMMENTS}/{comment_id}",
            {"json": {"body": "bot edit", "mentioned_user_ids": []}},
        ),
        ("DELETE", "/api/v1/comments/{comment_id}"): ("delete", f"{COMMENTS}/{comment_id}", {}),
        ("POST", "/api/v1/watchers"): ("post", WATCHERS, {"json": params}),
        ("DELETE", "/api/v1/watchers"): ("delete", WATCHERS, {"params": params}),
    }


def _snapshot(db: Session):
    db.expire_all()
    return (
        [(c.id, c.body, c.is_deleted) for c in db.query(Comment).order_by(Comment.id)],
        [(w.user_id, w.entity_id) for w in db.query(EntityWatcher).order_by(EntityWatcher.id)],
        db.query(AuditLog).filter(AuditLog.resource_type.in_(["comment", "entity_watcher"])).count(),
        db.query(OperationalEvent).count(),
    )


class TestPeopleOnly:
    @pytest.mark.parametrize(
        "token_kwargs, detail",
        [
            ({}, API_TOKEN_COMMENT_WRITE),
            ({"scope": "read_only", "ai_external": False}, READ_ONLY_ROUTE_WRITES_DETAIL),
            ({"scope": "read_only", "ai_external": True}, None),
        ],
        ids=["full", "read_only", "ai_external"],
    )
    def test_every_api_token_is_refused_every_write_and_nothing_changes(
        self, client, db_session, admin_user, admin_headers, people, token_kwargs, detail
    ):
        wo = make_wo(db_session)
        author = people["author"]
        # The token is bound to the comment's AUTHOR, an Admin-minted credential: even the
        # author's own token may not speak for them.
        comment_id = post_comment(client, user_headers(author), wo, "a person's words").json()["id"]
        client.post(WATCHERS, json={"entity_type": "work_order", "entity_id": wo.id}, headers=user_headers(author))
        _id, token = issue_token(client, admin_headers, author, **token_kwargs)
        requests = _write_requests(wo, comment_id, people["colleague"].id)
        assert set(requests) == set(COMMENT_WRITE_OPERATIONS)
        before = _snapshot(db_session)

        for key, (method, url, kwargs) in requests.items():
            response = getattr(client, method)(url, headers=bearer(token), **kwargs)
            assert response.status_code == 403, (key, response.text[:300])
            if detail is not None:
                assert response.json()["detail"] == detail, key

        assert _snapshot(db_session) == before, "a refused token wrote something"

    def test_an_admin_bound_full_token_is_refused_before_the_body_is_validated(
        self, client, db_session, admin_user, admin_headers
    ):
        _id, token = issue_token(client, admin_headers, admin_user)
        response = client.post(COMMENTS, json={"nonsense": True}, headers=bearer(token))
        assert response.status_code == 403
        assert response.json()["detail"] == API_TOKEN_COMMENT_WRITE

    @pytest.mark.parametrize(
        "token_kwargs", [{}, {"scope": "read_only", "ai_external": False}], ids=["full", "read_only"]
    )
    def test_the_thread_and_watch_state_stay_readable_by_a_token(
        self, client, db_session, admin_headers, people, token_kwargs
    ):
        wo = make_wo(db_session)
        post_comment(client, user_headers(people["author"]), wo, "visible to the bot")
        _id, token = issue_token(client, admin_headers, people["colleague"], **token_kwargs)
        params = {"entity_type": "work_order", "entity_id": wo.id}

        listed = client.get(COMMENTS, params=params, headers=bearer(token))
        assert listed.status_code == 200, listed.text
        assert [row["body"] for row in listed.json()] == ["visible to the bot"]
        state = client.get(WATCHERS, params=params, headers=bearer(token))
        assert state.status_code == 200 and state.json() == {"watching": False, "watcher_count": 1}

    def test_mention_search_is_readable_by_a_full_token_but_not_a_read_only_one(
        self, client, db_session, admin_headers, people
    ):
        """The route lives under ``/users`` (tag "Users"), which ``READ_ONLY_DENY_TAG_FLOOR``
        denies to every read-only token. Pinned so a change to either side is a decision."""
        _full_id, full = issue_token(client, admin_headers, people["colleague"])
        _ro_id, read_only = issue_token(client, admin_headers, people["viewer"], scope="read_only", ai_external=False)
        assert client.get(MENTION_SEARCH, params={"q": "Avery"}, headers=bearer(full)).status_code == 200
        assert client.get(MENTION_SEARCH, params={"q": "Avery"}, headers=bearer(read_only)).status_code == 403

    def test_an_ai_external_token_reads_none_of_it(self, client, db_session, admin_headers, people):
        """Comment bodies are free text an operator types -- exactly what the AI-external
        redaction cannot vouch for -- so none of these routes is on the AI-external allowlist."""
        wo = make_wo(db_session)
        _id, token = issue_token(client, admin_headers, people["colleague"], scope="read_only", ai_external=True)
        params = {"entity_type": "work_order", "entity_id": wo.id}
        assert client.get(COMMENTS, params=params, headers=bearer(token)).status_code == 403
        assert client.get(WATCHERS, params=params, headers=bearer(token)).status_code == 403
        assert client.get(MENTION_SEARCH, params={"q": "Avery"}, headers=bearer(token)).status_code == 403

    def test_a_kiosk_badge_token_is_fenced_out(self, client, db_session, people):
        wo = make_wo(db_session)
        token = create_access_token(subject=people["author"].id, company_id=COMPANY_A, scope="kiosk")
        params = {"entity_type": "work_order", "entity_id": wo.id}
        assert client.get(COMMENTS, params=params, headers=bearer(token)).status_code == 403
        assert (
            client.post(COMMENTS, json={**params, "body": "from the kiosk"}, headers=bearer(token)).status_code == 403
        )
        assert client.get(MENTION_SEARCH, params={"q": "a"}, headers=bearer(token)).status_code == 403
        assert db_session.query(Comment).count() == 0


# ---------------------------------------------------------------------------
# Mention search
# ---------------------------------------------------------------------------


class TestMentionSearch:
    def test_a_non_admin_gets_id_and_display_name_only(self, client, db_session, people):
        response = client.get(MENTION_SEARCH, params={"q": "blake"}, headers=user_headers(people["viewer"]))
        assert response.status_code == 200, response.text
        assert response.json() == [{"id": people["colleague"].id, "display_name": "Blake Marrow"}]

    @pytest.mark.parametrize("q", ["Avery", "quill", "avery q", "Avery  Quill", "VERY QU"])
    def test_first_last_and_full_name_match_case_insensitively(self, client, db_session, people, q):
        response = client.get(MENTION_SEARCH, params={"q": q}, headers=user_headers(people["viewer"]))
        assert [row["id"] for row in response.json()] == [people["author"].id]

    def test_inactive_and_foreign_people_are_excluded(self, client, db_session, people):
        headers = user_headers(people["viewer"])
        assert client.get(MENTION_SEARCH, params={"q": "Dormant"}, headers=headers).json() == []
        assert client.get(MENTION_SEARCH, params={"q": "Elsewhere"}, headers=headers).json() == []

    def test_like_wildcards_are_literal(self, client, db_session, people):
        headers = user_headers(people["viewer"])
        assert client.get(MENTION_SEARCH, params={"q": "%"}, headers=headers).json() == []
        assert client.get(MENTION_SEARCH, params={"q": "_"}, headers=headers).json() == []

    def test_limit_and_bounds(self, client, db_session, people):
        headers = user_headers(people["viewer"])
        for i in range(12):
            make_user(db_session, first_name="Robin", last_name=f"Tester{i:02d}")
        assert len(client.get(MENTION_SEARCH, params={"q": "robin"}, headers=headers).json()) == 10
        assert len(client.get(MENTION_SEARCH, params={"q": "robin", "limit": 3}, headers=headers).json()) == 3
        assert client.get(MENTION_SEARCH, params={"q": "robin", "limit": 26}, headers=headers).status_code == 422
        assert client.get(MENTION_SEARCH, params={"q": ""}, headers=headers).status_code == 422
        assert client.get(MENTION_SEARCH, params={"q": "x" * 101}, headers=headers).status_code == 422


# ---------------------------------------------------------------------------
# Notification events (delivery is pinned in tests/services/test_comment_notifications.py)
# ---------------------------------------------------------------------------


class TestNotificationEvents:
    def test_create_emits_both_events_on_the_comment_without_its_text(self, client, db_session, people):
        wo = make_wo(db_session)
        author, colleague = people["author"], people["colleague"]
        secret = "Rework the flange per the customer's note"

        comment_id = post_comment(client, user_headers(author), wo, secret, mentioned_user_ids=[colleague.id]).json()[
            "id"
        ]

        db_session.expire_all()
        events = {
            event.event_type: event
            for event in db_session.query(OperationalEvent).filter(OperationalEvent.source_module == "comments")
        }
        assert set(events) == {"comment_mentioned", "comment_added"}
        for event in events.values():
            assert event.company_id == COMPANY_A
            assert event.entity_type == "comment" and event.entity_id == comment_id
            assert event.work_order_id == wo.id
            assert event.user_id == author.id
            payload = event.event_payload
            assert payload["work_order_number"] == wo.work_order_number
            assert payload["comment_author"] == "Avery Quill"
            assert payload["record_type"] == "work_order" and payload["record_id"] == wo.id
            assert secret not in repr(payload)
            assert "Rework" not in repr(payload)
        assert events["comment_mentioned"].event_payload["mentioned_user_ids"] == [colleague.id]

    def test_an_edit_emits_a_mention_only_for_people_newly_added(self, client, db_session, people):
        wo = make_wo(db_session)
        headers = user_headers(people["author"])
        comment_id = post_comment(client, headers, wo, "v1", mentioned_user_ids=[people["colleague"].id]).json()["id"]
        client.patch(
            f"{COMMENTS}/{comment_id}",
            json={"body": "v2", "mentioned_user_ids": [people["colleague"].id, people["viewer"].id]},
            headers=headers,
        )
        client.patch(
            f"{COMMENTS}/{comment_id}",
            json={"body": "v3", "mentioned_user_ids": [people["viewer"].id]},
            headers=headers,
        )
        db_session.expire_all()
        mentions = (
            db_session.query(OperationalEvent)
            .filter(OperationalEvent.event_type == "comment_mentioned")
            .order_by(OperationalEvent.id)
            .all()
        )
        assert [event.event_payload["mentioned_user_ids"] for event in mentions] == [
            [people["colleague"].id],
            [people["viewer"].id],
        ]
        assert db_session.query(OperationalEvent).filter(OperationalEvent.event_type == "comment_added").count() == 1

    @pytest.mark.parametrize("event_type", ["comment_mentioned", "comment_added", " Comment_Added "])
    def test_a_client_cannot_post_the_comment_events(self, client, db_session, people, event_type):
        response = client.post(
            f"{API}/operational-events/",
            json={"event_type": event_type, "source_module": "comments", "event_payload": {}},
            headers=user_headers(people["admin"]),
        )
        assert response.status_code == 400, response.text
