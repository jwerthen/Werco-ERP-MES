"""Work-order comments -- the QA matrix the first-pass suite left unpinned.

``tests/api/test_work_order_comments.py`` pins the contract verb by verb with a fixed cast. This
file pins the rules ACROSS the axes that cast does not span:

* RBAC per role: every tenant role reads, posts, edits its own, watches itself and searches
  mentions (docs/RBAC_PERMISSIONS.md -> Comments and watchers); edit is the author's alone for
  every role, superuser and Platform Admin included; delete-any is Admin / Manager / superuser /
  Platform Admin and nobody else -- and the list's ``can_edit`` / ``can_delete`` flags agree with
  what each verb actually answers, role by role (the Comments panel renders its buttons from them).
* A Platform Admin switched into ANOTHER company's context: mention search returns that company's
  people, a comment and its mention / watcher / audit / event rows are that company's, a home-
  company person cannot be mentioned there, and a read-only switched context reads but writes
  nothing.
* Watching is the caller's own preference: a request naming another user id cannot watch or
  unwatch for them.
* A crew-station badge token is refused all five writes, and writes nothing.
* A soft-deleted work order: every verb 404s AND nothing is written -- no comment, no watcher,
  no audit row, no ``comment_added`` / ``comment_mentioned`` event.
* An edit keeps a mention of someone deactivated since (it is not re-checked), but cannot
  re-add that person once dropped; mention order is "given order", then "kept, then added".
* The body is stored and served verbatim (no ingest sanitization; CLAUDE.md).
* The thread returns the newest ``COMMENT_LIST_LIMIT`` comments, oldest first, ties by id.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.comment import Comment, EntityWatcher
from app.models.operational_event import OperationalEvent
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.services import comment_service
from tests.api.kiosk_test_helpers import COMPANY_A, COMPANY_B, bearer, make_user, user_headers
from tests.api.test_work_order_comments import (
    COMMENTS,
    MENTION_SEARCH,
    WATCHERS,
    _snapshot,
    _write_requests,
    audit_rows,
    list_comments,
    make_wo,
    post_comment,
    watcher_ids,
)

pytestmark = pytest.mark.api

TENANT_ROLES = [
    UserRole.ADMIN,
    UserRole.MANAGER,
    UserRole.SUPERVISOR,
    UserRole.OPERATOR,
    UserRole.QUALITY,
    UserRole.SHIPPING,
    UserRole.VIEWER,
]


def _params(wo: WorkOrder) -> Dict[str, object]:
    return {"entity_type": "work_order", "entity_id": wo.id}


@pytest.fixture
def cast(db_session: Session) -> Dict[str, User]:
    return {
        "author": make_user(db_session, role=UserRole.OPERATOR, first_name="Rowan", last_name="Penhallow"),
        "other": make_user(db_session, role=UserRole.SUPERVISOR, first_name="Sloane", last_name="Brightwater"),
        "third": make_user(db_session, role=UserRole.QUALITY, first_name="Tamsin", last_name="Okonkwo"),
        "fourth": make_user(db_session, role=UserRole.SHIPPING, first_name="Uriel", last_name="Vantongeren"),
    }


# ---------------------------------------------------------------------------
# RBAC per role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", TENANT_ROLES, ids=lambda role: role.value)
def test_every_tenant_role_posts_reads_edits_its_own_watches_and_searches(client, db_session, role):
    wo = make_wo(db_session)
    person = make_user(db_session, role=role, first_name="Rolecheck", last_name=role.value.title())
    headers = user_headers(person)

    created = post_comment(client, headers, wo, f"{role.value} note")
    assert created.status_code == 201, created.text
    assert created.json()["can_edit"] is True and created.json()["can_delete"] is True
    comment_id = created.json()["id"]

    listed = list_comments(client, headers, wo)
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()] == [comment_id]

    edited = client.patch(
        f"{COMMENTS}/{comment_id}", json={"body": "edited by its author", "mentioned_user_ids": []}, headers=headers
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["body"] == "edited by its author"

    # Posting auto-watched them; the toggle acts on the caller only.
    assert client.get(WATCHERS, params=_params(wo), headers=headers).json() == {"watching": True, "watcher_count": 1}
    assert client.delete(WATCHERS, params=_params(wo), headers=headers).json() == {
        "watching": False,
        "watcher_count": 0,
    }
    assert client.post(WATCHERS, json=_params(wo), headers=headers).json() == {"watching": True, "watcher_count": 1}

    search = client.get(MENTION_SEARCH, params={"q": "Rolecheck"}, headers=headers)
    assert search.status_code == 200, search.text
    assert search.json() == [{"id": person.id, "display_name": f"Rolecheck {role.value.title()}"}]

    assert client.delete(f"{COMMENTS}/{comment_id}", headers=headers).status_code == 204


# (label, role, is_superuser, may delete someone else's comment)
_MODERATION = [
    ("admin", UserRole.ADMIN, False, True),
    ("manager", UserRole.MANAGER, False, True),
    ("supervisor", UserRole.SUPERVISOR, False, False),
    ("operator", UserRole.OPERATOR, False, False),
    ("quality", UserRole.QUALITY, False, False),
    ("shipping", UserRole.SHIPPING, False, False),
    ("viewer", UserRole.VIEWER, False, False),
    ("superuser_operator", UserRole.OPERATOR, True, True),
    ("platform_admin", UserRole.PLATFORM_ADMIN, False, True),
]


@pytest.mark.parametrize(
    "role, superuser, may_delete",
    [row[1:] for row in _MODERATION],
    ids=[row[0] for row in _MODERATION],
)
def test_moderation_matrix_and_the_list_flags_agree_with_the_verbs(
    client, db_session, cast, role, superuser, may_delete
):
    """Someone else's comment: nobody but the author edits it (superuser and Platform Admin
    included -- no bypass rewrites a person's words); only Admin / Manager / superuser / Platform
    Admin delete it. The thread's ``can_edit`` / ``can_delete`` must say exactly what the verbs do,
    or the panel offers a button the server refuses (or hides one it would honor)."""
    wo = make_wo(db_session)
    actor = make_user(db_session, role=role, first_name="Moderator", last_name=role.value.title())
    if superuser:
        actor.is_superuser = True
        db_session.commit()
    comment_id = post_comment(client, user_headers(cast["author"]), wo, "the author's own words").json()["id"]
    headers = user_headers(actor)

    (row,) = list_comments(client, headers, wo).json()
    assert row["can_edit"] is False
    assert row["can_delete"] is may_delete

    patched = client.patch(
        f"{COMMENTS}/{comment_id}", json={"body": "rewritten", "mentioned_user_ids": []}, headers=headers
    )
    assert patched.status_code == 403, patched.text
    assert patched.json()["detail"] == comment_service.EDIT_FORBIDDEN_DETAIL

    deleted = client.delete(f"{COMMENTS}/{comment_id}", headers=headers)
    assert deleted.status_code == (204 if may_delete else 403), deleted.text
    if not may_delete:
        assert deleted.json()["detail"] == comment_service.DELETE_FORBIDDEN_DETAIL

    db_session.expire_all()
    stored = db_session.get(Comment, comment_id)
    assert stored.body == "the author's own words"
    assert stored.is_deleted is may_delete
    actions = [audit.action for audit in audit_rows(db_session, "comment", comment_id)]
    if may_delete:
        assert actions == ["CREATE", "DELETE"]
        delete_row = audit_rows(db_session, "comment", comment_id)[-1]
        assert delete_row.user_id == actor.id and delete_row.extra_data["moderated"] is True
    else:
        assert actions == ["CREATE"], "a refused verb wrote an audit row"


# ---------------------------------------------------------------------------
# A Platform Admin switched into another company's context
# ---------------------------------------------------------------------------


@pytest.fixture
def platform_admin(db_session: Session) -> User:
    return make_user(
        db_session, role=UserRole.PLATFORM_ADMIN, company_id=COMPANY_A, first_name="Plato", last_name="Oversight"
    )


class TestPlatformAdminInAnotherCompany:
    def test_mention_search_returns_the_active_companys_people(self, client, db_session, platform_admin):
        home = make_user(db_session, company_id=COMPANY_A, first_name="Harlan", last_name="Homeside")
        there = make_user(db_session, company_id=COMPANY_B, first_name="Harlan", last_name="Farside")

        viewing_b = user_headers(platform_admin, active_company_id=COMPANY_B)
        response = client.get(MENTION_SEARCH, params={"q": "harlan"}, headers=viewing_b)
        assert response.status_code == 200, response.text
        assert response.json() == [{"id": there.id, "display_name": "Harlan Farside"}]
        # The platform admin is not a member of the viewed company, so not mentionable there.
        assert client.get(MENTION_SEARCH, params={"q": "Plato"}, headers=viewing_b).json() == []

        at_home = client.get(MENTION_SEARCH, params={"q": "harlan"}, headers=user_headers(platform_admin))
        assert at_home.json() == [{"id": home.id, "display_name": "Harlan Homeside"}]

    def test_a_comment_in_the_viewed_company_is_wholly_that_companys(self, client, db_session, platform_admin):
        b_wo = make_wo(db_session, company_id=COMPANY_B)
        a_wo = make_wo(db_session, company_id=COMPANY_A)
        b_person = make_user(db_session, company_id=COMPANY_B, first_name="Imogen", last_name="Bayliss")
        a_person = make_user(db_session, company_id=COMPANY_A, first_name="Jasper", last_name="Homefield")
        viewing_b = user_headers(platform_admin, active_company_id=COMPANY_B)

        # A home-company person is not mentionable on the viewed company's work order...
        refused = post_comment(client, viewing_b, b_wo, "x", mentioned_user_ids=[a_person.id])
        assert refused.status_code == 422, refused.text
        assert "Jasper" not in refused.json()["detail"]
        # ...and the home company's work order is not reachable from the viewed context.
        assert post_comment(client, viewing_b, a_wo, "x").status_code == 404
        assert db_session.query(Comment).count() == 0

        created = post_comment(client, viewing_b, b_wo, "Oversight note", mentioned_user_ids=[b_person.id])
        assert created.status_code == 201, created.text
        comment_id = created.json()["id"]

        db_session.expire_all()
        row = db_session.get(Comment, comment_id)
        assert row.company_id == COMPANY_B and row.author_id == platform_admin.id
        assert {mention.company_id for mention in row.mentions} == {COMPANY_B}
        watchers = db_session.query(EntityWatcher).filter(EntityWatcher.entity_id == b_wo.id).all()
        assert {watcher.company_id for watcher in watchers} == {COMPANY_B}
        (create_audit,) = audit_rows(db_session, "comment", comment_id)
        assert create_audit.company_id == COMPANY_B and create_audit.user_id == platform_admin.id
        comment_events = db_session.query(OperationalEvent).filter(OperationalEvent.source_module == "comments").all()
        assert {event.event_type for event in comment_events} == {"comment_mentioned", "comment_added"}
        assert {event.company_id for event in comment_events} == {COMPANY_B}

        # The viewed company's own people read it; the home company cannot reach the work order.
        listed = list_comments(client, user_headers(b_person), b_wo)
        assert [entry["id"] for entry in listed.json()] == [comment_id]
        assert listed.json()[0]["author"] == {"id": platform_admin.id, "display_name": "Plato Oversight"}
        assert list_comments(client, user_headers(a_person), b_wo).status_code == 404
        assert list_comments(client, user_headers(platform_admin), b_wo).status_code == 404

    def test_moderation_in_the_viewed_company_is_audited_there(self, client, db_session, platform_admin):
        b_wo = make_wo(db_session, company_id=COMPANY_B)
        b_author = make_user(db_session, company_id=COMPANY_B, first_name="Kit", last_name="Marchbank")
        comment_id = post_comment(client, user_headers(b_author), b_wo, "B's words").json()["id"]
        viewing_b = user_headers(platform_admin, active_company_id=COMPANY_B)

        patched = client.patch(
            f"{COMMENTS}/{comment_id}", json={"body": "rewritten", "mentioned_user_ids": []}, headers=viewing_b
        )
        assert patched.status_code == 403
        # From the home context the comment does not exist.
        assert client.delete(f"{COMMENTS}/{comment_id}", headers=user_headers(platform_admin)).status_code == 404

        assert client.delete(f"{COMMENTS}/{comment_id}", headers=viewing_b).status_code == 204
        delete_row = audit_rows(db_session, "comment", comment_id)[-1]
        assert delete_row.action == "DELETE"
        assert delete_row.company_id == COMPANY_B
        assert delete_row.user_id == platform_admin.id and delete_row.extra_data["moderated"] is True

    def test_a_read_only_switched_context_reads_but_writes_nothing(self, client, db_session, platform_admin):
        b_wo = make_wo(db_session, company_id=COMPANY_B)
        b_author = make_user(db_session, company_id=COMPANY_B, first_name="Lark", last_name="Fenwick")
        comment_id = post_comment(client, user_headers(b_author), b_wo, "visible").json()["id"]
        read_only = bearer(create_access_token(subject=platform_admin.id, company_id=COMPANY_B, read_only=True))

        assert [entry["id"] for entry in list_comments(client, read_only, b_wo).json()] == [comment_id]
        assert client.get(WATCHERS, params=_params(b_wo), headers=read_only).status_code == 200
        assert client.get(MENTION_SEARCH, params={"q": "Lark"}, headers=read_only).status_code == 200

        before = _snapshot(db_session)
        for key, (method, url, kwargs) in _write_requests(b_wo, comment_id, b_author.id).items():
            response = getattr(client, method)(url, headers=read_only, **kwargs)
            assert response.status_code == 403, (key, response.text[:300])
            assert response.json()["detail"] == "Read-only company context cannot modify data", key
        assert _snapshot(db_session) == before


# ---------------------------------------------------------------------------
# Watching is the caller's own preference
# ---------------------------------------------------------------------------


def test_watch_and_unwatch_never_act_on_another_user(client, db_session, cast):
    wo = make_wo(db_session)
    caller, other = cast["author"], cast["other"]
    assert client.post(WATCHERS, json=_params(wo), headers=user_headers(other)).status_code == 200

    # Naming someone else in the body is refused outright (extra="forbid") and writes nothing.
    named = client.post(WATCHERS, json={**_params(wo), "user_id": caller.id}, headers=user_headers(other))
    assert named.status_code == 422
    assert watcher_ids(db_session, wo) == [other.id]

    # A stray ``user_id`` query parameter on unwatch is ignored: only the CALLER's row is targeted,
    # so the other person keeps watching.
    response = client.delete(WATCHERS, params={**_params(wo), "user_id": other.id}, headers=user_headers(caller))
    assert response.status_code == 200, response.text
    assert response.json() == {"watching": False, "watcher_count": 1}
    assert watcher_ids(db_session, wo) == [other.id]


def test_a_lost_watcher_insert_race_reads_as_already_watching_and_keeps_the_transaction(
    client, db_session, cast, monkeypatch
):
    """Two tabs (or two first comments) inserting the same watcher: the loser's pre-read missed the
    winner's uncommitted row, so its INSERT hits ``uq_entity_watchers_company_entity_user``.
    ``_insert_watcher`` must roll back only its savepoint -- the person IS watching, which is what
    was asked -- write no second audit row, and leave the request's transaction (a comment, say)
    intact."""
    wo = make_wo(db_session)
    author, other, third = cast["author"], cast["other"], cast["third"]
    assert client.post(WATCHERS, json=_params(wo), headers=user_headers(other)).status_code == 200
    watcher_audits = len(audit_rows(db_session, "entity_watcher"))

    # From here every pre-read misses existing rows, as a read taken before the winner committed would.
    monkeypatch.setattr(comment_service, "_watcher_rows", lambda *args, **kwargs: {})

    again = client.post(WATCHERS, json=_params(wo), headers=user_headers(other))
    assert again.status_code == 200, again.text
    assert again.json() == {"watching": True, "watcher_count": 1}
    assert len(audit_rows(db_session, "entity_watcher")) == watcher_audits, "the race loser wrote evidence"

    created = post_comment(client, user_headers(author), wo, "still saved", mentioned_user_ids=[other.id, third.id])
    assert created.status_code == 201, created.text
    comment_id = created.json()["id"]
    assert watcher_ids(db_session, wo) == sorted([author.id, other.id, third.id])
    (create_audit,) = audit_rows(db_session, "comment", comment_id)
    # The lost insert is not reported as an auto-watch this comment made.
    assert create_audit.extra_data["auto_watched_user_ids"] == [author.id, third.id]
    events = db_session.query(OperationalEvent).filter(OperationalEvent.entity_id == comment_id).all()
    assert {event.event_type for event in events} == {"comment_mentioned", "comment_added"}


# ---------------------------------------------------------------------------
# A crew-station badge token: every write refused, nothing written
# ---------------------------------------------------------------------------


def test_a_kiosk_badge_token_is_refused_all_five_writes_and_writes_nothing(client, db_session, cast):
    wo = make_wo(db_session)
    author = cast["author"]
    comment_id = post_comment(client, user_headers(author), wo, "a person's words").json()["id"]
    badge = bearer(create_access_token(subject=author.id, company_id=COMPANY_A, scope="kiosk"))
    assert client.get(WATCHERS, params=_params(wo), headers=badge).status_code == 403

    before = _snapshot(db_session)
    for key, (method, url, kwargs) in _write_requests(wo, comment_id, cast["other"].id).items():
        response = getattr(client, method)(url, headers=badge, **kwargs)
        assert response.status_code == 403, (key, response.text[:300])
        assert response.json()["detail"] == "Kiosk-scoped token cannot access this resource", key
    assert _snapshot(db_session) == before


# ---------------------------------------------------------------------------
# A soft-deleted work order: 404 everywhere AND nothing written
# ---------------------------------------------------------------------------


def test_after_the_work_order_is_soft_deleted_no_verb_writes_or_notifies(client, db_session, cast):
    wo = make_wo(db_session)
    author, other = cast["author"], cast["other"]
    comment_id = post_comment(client, user_headers(author), wo, "before the delete").json()["id"]
    assert client.post(WATCHERS, json=_params(wo), headers=user_headers(other)).status_code == 200
    db_session.expire_all()
    db_session.get(WorkOrder, wo.id).soft_delete(None)
    db_session.commit()
    events_before = db_session.query(OperationalEvent).count()
    before = _snapshot(db_session)

    admin = make_user(db_session, role=UserRole.ADMIN, first_name="Mira", last_name="Delacroix")
    for headers in (user_headers(author), user_headers(admin)):
        for key, (method, url, kwargs) in _write_requests(wo, comment_id, other.id).items():
            response = getattr(client, method)(url, headers=headers, **kwargs)
            assert response.status_code == 404, (key, response.text[:300])
        assert list_comments(client, headers, wo).status_code == 404
        assert client.get(WATCHERS, params=_params(wo), headers=headers).status_code == 404

    assert _snapshot(db_session) == before
    assert db_session.query(OperationalEvent).count() == events_before, "a 404 emitted a comment event"


# ---------------------------------------------------------------------------
# Edits: kept mentions are not re-checked; mention order
# ---------------------------------------------------------------------------


def test_an_edit_keeps_a_mention_of_someone_deactivated_since_but_cannot_re_add_them(client, db_session, cast):
    wo = make_wo(db_session)
    author, other, third = cast["author"], cast["other"], cast["third"]
    headers = user_headers(author)
    comment_id = post_comment(client, headers, wo, "v1", mentioned_user_ids=[other.id]).json()["id"]
    db_session.expire_all()
    db_session.get(User, other.id).is_active = False
    db_session.commit()

    # Fixing a typo must not be blocked by a mention that was valid when it was made.
    kept = client.patch(
        f"{COMMENTS}/{comment_id}", json={"body": "v1, typo fixed", "mentioned_user_ids": [other.id]}, headers=headers
    )
    assert kept.status_code == 200, kept.text
    assert [person["id"] for person in kept.json()["mentions"]] == [other.id]
    # Adding someone else alongside the kept mention is fine too.
    widened = client.patch(
        f"{COMMENTS}/{comment_id}",
        json={"body": "v2", "mentioned_user_ids": [other.id, third.id]},
        headers=headers,
    )
    assert widened.status_code == 200, widened.text
    # Dropped, the deactivated person is a NEW mention again -- and refused like one.
    assert (
        client.patch(
            f"{COMMENTS}/{comment_id}", json={"body": "v3", "mentioned_user_ids": [third.id]}, headers=headers
        ).status_code
        == 200
    )
    readded = client.patch(
        f"{COMMENTS}/{comment_id}",
        json={"body": "v4", "mentioned_user_ids": [third.id, other.id]},
        headers=headers,
    )
    assert readded.status_code == 422, readded.text
    assert readded.json()["detail"] == f"These people can't be mentioned on this work order: user {other.id}."
    db_session.expire_all()
    assert db_session.get(Comment, comment_id).body == "v3"


def test_mentions_keep_the_given_order_and_an_edit_lists_kept_then_added(client, db_session, cast):
    wo = make_wo(db_session)
    author, a, b, c = cast["author"], cast["other"], cast["third"], cast["fourth"]
    extra = make_user(db_session, role=UserRole.VIEWER, first_name="Wren", last_name="Ashby")
    assert a.id < b.id < c.id < extra.id
    headers = user_headers(author)

    # Given order, not id order.
    created = post_comment(client, headers, wo, "v1", mentioned_user_ids=[c.id, a.id])
    assert [person["id"] for person in created.json()["mentions"]] == [c.id, a.id]
    comment_id = created.json()["id"]

    # Kept (in their original order), then the newly added (as given).
    edited = client.patch(
        f"{COMMENTS}/{comment_id}", json={"body": "v2", "mentioned_user_ids": [extra.id, a.id, b.id]}, headers=headers
    )
    assert edited.status_code == 200, edited.text
    assert [person["id"] for person in edited.json()["mentions"]] == [a.id, extra.id, b.id]
    (listed,) = list_comments(client, headers, wo).json()
    assert [person["id"] for person in listed["mentions"]] == [a.id, extra.id, b.id]
    update = audit_rows(db_session, "comment", comment_id)[-1]
    assert update.new_values["mentioned_user_ids"] == [a.id, extra.id, b.id]
    assert update.extra_data["added_mention_user_ids"] == [extra.id, b.id]
    assert update.extra_data["removed_mention_user_ids"] == [c.id]


# ---------------------------------------------------------------------------
# Body verbatim; thread limit
# ---------------------------------------------------------------------------


def test_markup_in_a_body_is_stored_and_served_verbatim(client, db_session, cast):
    """No ingest sanitization (CLAUDE.md, bleach removed): ASME ``<REF>`` / ``<TYP>`` and anything
    that looks like HTML is a person's words and survives byte for byte; the SPA renders it as
    text and the notifications never carry it."""
    wo = make_wo(db_session)
    body = "Bore <REF> 0.250 <TYP> & per <b>note</b> <script>alert(1)</script>\n\n  indented line two"
    created = post_comment(client, user_headers(cast["author"]), wo, body)
    assert created.status_code == 201, created.text
    comment_id = created.json()["id"]

    assert created.json()["body"] == body
    (listed,) = list_comments(client, user_headers(cast["other"]), wo).json()
    assert listed["body"] == body
    db_session.expire_all()
    assert db_session.get(Comment, comment_id).body == body
    (create_audit,) = audit_rows(db_session, "comment", comment_id)
    assert create_audit.new_values["body"] == body


def test_a_thread_past_the_limit_returns_the_newest_oldest_first_ties_by_id(client, db_session, cast, monkeypatch):
    assert comment_service.COMMENT_LIST_LIMIT == 500
    monkeypatch.setattr(comment_service, "COMMENT_LIST_LIMIT", 3)
    wo = make_wo(db_session)
    t0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    stamps = [t0, t0 + timedelta(minutes=1), t0 + timedelta(minutes=2), t0 + timedelta(minutes=2), t0 + timedelta(3)]
    ids = []
    for index, stamp in enumerate(stamps):
        row = Comment(
            company_id=COMPANY_A,
            entity_type="work_order",
            entity_id=wo.id,
            author_id=cast["author"].id,
            body=f"comment {index}",
            created_at=stamp,
        )
        db_session.add(row)
        db_session.flush()
        ids.append(row.id)
    # A deleted comment newer than all of them does not take a slot.
    deleted = Comment(
        company_id=COMPANY_A,
        entity_type="work_order",
        entity_id=wo.id,
        author_id=cast["author"].id,
        body="deleted",
        created_at=t0 + timedelta(days=9),
    )
    deleted.soft_delete(cast["author"].id)
    db_session.add(deleted)
    db_session.commit()

    listed = list_comments(client, user_headers(cast["other"]), wo)
    assert listed.status_code == 200, listed.text
    # Newest three by (created_at, id) -- the tie at t0+2m broken by id -- then oldest first.
    assert [row["id"] for row in listed.json()] == [ids[2], ids[3], ids[4]]
