"""AS9100D 8.7 disposition evidence on an NCR (migration ``127_ncr_car_records``).

Covers ``app/services/quality_record_service.py`` through the ``/api/v1/quality`` routes:

* the disposition stamp -- ``dispositioned_by`` / ``dispositioned_at`` are set by the server
  when the disposition changes to anything but PENDING, cleared when it goes back to
  PENDING, left alone by an unchanged re-send, never taken from a body, and recorded old ->
  new on the request's one audit row;
* the closure gate, judged on the MERGED record (the request's value, else the stored one):
  a disposition, ``action_taken``, a concession reference for use-as-is / repair and a
  STORED conforming re-verification for rework / repair, named in that order (422, nothing
  written) -- including a disposition change in the closing request itself, which clears
  the stored re-verification and so can never close on it;
* ``concession_date`` later than today (Central) is 422;
* a standing API token may not CHANGE the disposition or any ``concession_*`` field (403,
  nothing written, no audit row), may re-send them unchanged with another edit, and may not
  record a re-verification at all;
* ``POST /quality/ncr/{id}/reverification``: Admin / Manager / Quality only, rework / repair
  only, never on a CLOSED or VOID NCR (409), re-recordable with old -> new on the audit row,
  required audit (503 and nothing saved), tenant-isolated (404);
* changing the disposition clears a stored re-verification, keeping the cleared values on
  the audit row;
* ``dispositioned_by_name`` / ``reverified_by_name`` resolve tenant-scoped (a platform
  principal acting in the company is named; another company's ordinary user never is).

Audit rows are read back through a SECOND connection (``export_audit_helpers.fresh_session``)
so a write that logged but never committed fails here.
"""

import itertools
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import inspect

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.operational_event import OperationalEvent
from app.models.quality import (
    CorrectiveActionRequest,
    NCRDisposition,
    NCRSource,
    NCRStatus,
    NonConformanceReport,
)
from app.models.user import User, UserRole
from app.services import quality_record_service
from app.services.audit_service import AuditService
from app.services.quality_record_service import API_TOKEN_NCR_JUDGMENT, API_TOKEN_NCR_REVERIFICATION
from tests.api.export_audit_helpers import fresh_session

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

BASE = "/api/v1/quality"
API_TOKENS_URL = "/api/v1/api-tokens/"
PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens minted directly; never used for login
# The schema-level closure check still wants a root cause of at least 20 characters.
ROOT_CAUSE = "Fixture wear let the edge burr through deburr."
ACTION_TAKEN = "Deburred all five brackets by hand and re-inspected the edge."
NOTES = "Re-inspected all five brackets to drawing rev C; edge break within 0.010 max."
STAMPED_AT = datetime(2026, 10, 30, 15, 0, 0)

MISSING_DISPOSITION = "a disposition"
MISSING_ACTION = "action taken"
MISSING_CONCESSION = "a customer concession reference (use-as-is and repair)"
MISSING_REVERIFICATION = "a conforming re-verification (rework and repair)"

_seq = itertools.count(1)


# --------------------------------------------------------------------------- builders


def _company(db, company_id=1):
    company = db.query(Company).filter(Company.id == company_id).first()
    if company is None:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"ncr-ev-co-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def _user(db, role=UserRole.QUALITY, company_id=1, first_name="Quinn"):
    _company(db, company_id)
    n = next(_seq)
    user = User(
        email=f"ncr-evidence-{n}@co{company_id}.test",
        employee_id=f"NCRE-{n:05d}",
        first_name=first_name,
        last_name=f"Inspector{n}",
        hashed_password=PASSWORD_HASH,
        role=role,
        is_active=True,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers(user, company_id=None):
    token = create_access_token(subject=user.id, company_id=company_id or user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _token_headers(client, db, user):
    """A standing API token bound to ``user``, minted the way an Admin mints one."""
    admin = _user(db, role=UserRole.ADMIN, company_id=user.company_id, first_name="Ash")
    issued = client.post(API_TOKENS_URL, json={"user_id": user.id, "label": "ncr probe"}, headers=_headers(admin))
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}


def _ncr(db, company_id=1, **extra):
    _company(db, company_id)
    n = next(_seq)
    fields = {
        "ncr_number": f"NCR-E{company_id}-{n:04d}",
        "source": NCRSource.IN_PROCESS,
        "title": "Edge burr on bracket",
        "description": "Burr found on the outside edge at final inspection.",
        "quantity_affected": 5,
        "quantity_rejected": 0,
        "status": NCRStatus.OPEN,
        "disposition": NCRDisposition.PENDING,
        "company_id": company_id,
    }
    fields.update(extra)
    ncr = NonConformanceReport(**fields)
    db.add(ncr)
    db.commit()
    db.refresh(ncr)
    return ncr


def _reverified(db, verifier, result="conforming", **extra):
    """An NCR carrying a stored re-verification by ``verifier``."""
    extra.setdefault("disposition", NCRDisposition.REWORK)
    return _ncr(
        db,
        reverification_result=result,
        reverification_notes=NOTES,
        reverified_by=verifier.id,
        reverified_at=STAMPED_AT,
        **extra,
    )


def _close_body(**extra):
    body = {"version": 0, "status": "closed", "root_cause": ROOT_CAUSE}
    body.update(extra)
    return body


# --------------------------------------------------------------------------- readers


def _committed(resource_id, action=None):
    """Committed NCR audit rows, read on a separate connection."""
    with fresh_session() as verify:
        query = verify.query(AuditLog).filter(AuditLog.resource_type == "ncr", AuditLog.resource_id == resource_id)
        if action is not None:
            query = query.filter(AuditLog.action == action)
        return [
            {
                "action": row.action,
                "user_id": row.user_id,
                "company_id": row.company_id,
                "description": row.description,
                "old_values": row.old_values,
                "new_values": row.new_values,
                "extra_data": row.extra_data,
            }
            for row in query.order_by(AuditLog.id).all()
        ]


def _snapshot(db):
    """Every NCR / CAR / audit / event row, for "nothing changed" assertions."""
    db.expire_all()
    return {
        model.__tablename__: [
            {column.key: getattr(row, column.key) for column in inspect(model).columns}
            for row in db.query(model).order_by(model.id).all()
        ]
        for model in (NonConformanceReport, CorrectiveActionRequest, AuditLog, OperationalEvent)
    }


def _stored(db, ncr_id):
    db.expire_all()
    return db.get(NonConformanceReport, ncr_id)


def _assert_recent_utc(value: str):
    """A served stamp: UTC with a trailing ``Z``, written just now."""
    assert value.endswith("Z"), value
    stamped = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert abs(datetime.now(timezone.utc) - stamped) < timedelta(minutes=5)


# ===================================================================== disposition stamp


class TestDispositionStamp:
    def test_a_non_pending_disposition_stamps_the_caller_and_the_instant(self, client, db_session):
        user = _user(db_session, first_name="Rowan")
        ncr = _ncr(db_session)

        resp = client.put(f"{BASE}/ncr/{ncr.id}", headers=_headers(user), json={"version": 0, "disposition": "scrap"})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dispositioned_by"] == user.id
        assert body["dispositioned_by_name"] == user.full_name
        _assert_recent_utc(body["dispositioned_at"])
        rows = _committed(ncr.id)
        assert [row["action"] for row in rows] == ["UPDATE"]
        assert rows[0]["old_values"] == {"disposition": "pending", "dispositioned_by": None, "dispositioned_at": None}
        assert rows[0]["new_values"]["dispositioned_by"] == user.id
        assert rows[0]["new_values"]["dispositioned_at"] is not None
        assert rows[0]["extra_data"]["changes"]["dispositioned_by"] == {"old": None, "new": user.id}

    def test_a_new_disposition_restamps_with_the_new_decider_old_to_new(self, client, db_session):
        first = _user(db_session, first_name="Rowan")
        second = _user(db_session, first_name="Sage")
        ncr = _ncr(
            db_session, disposition=NCRDisposition.REWORK, dispositioned_by=first.id, dispositioned_at=STAMPED_AT
        )

        resp = client.put(f"{BASE}/ncr/{ncr.id}", headers=_headers(second), json={"version": 0, "disposition": "scrap"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["dispositioned_by"] == second.id
        assert resp.json()["dispositioned_by_name"] == second.full_name
        row = _committed(ncr.id)[0]
        assert row["old_values"] == {
            "disposition": "rework",
            "dispositioned_by": first.id,
            "dispositioned_at": STAMPED_AT.isoformat(),
        }
        assert row["new_values"]["dispositioned_by"] == second.id
        assert row["new_values"]["dispositioned_at"] != STAMPED_AT.isoformat()

    def test_back_to_pending_clears_the_stamp(self, client, db_session):
        decider = _user(db_session, first_name="Rowan")
        ncr = _ncr(
            db_session, disposition=NCRDisposition.SCRAP, dispositioned_by=decider.id, dispositioned_at=STAMPED_AT
        )

        resp = client.put(
            f"{BASE}/ncr/{ncr.id}", headers=_headers(_user(db_session)), json={"version": 0, "disposition": "pending"}
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dispositioned_by"] is None and body["dispositioned_at"] is None
        assert body["dispositioned_by_name"] is None
        row = _committed(ncr.id)[0]
        assert row["old_values"] == {
            "disposition": "scrap",
            "dispositioned_by": decider.id,
            "dispositioned_at": STAMPED_AT.isoformat(),
        }
        assert row["new_values"] == {"disposition": "pending", "dispositioned_by": None, "dispositioned_at": None}

    def test_an_unchanged_disposition_resent_with_another_edit_keeps_the_stamp(self, client, db_session):
        decider = _user(db_session, first_name="Rowan")
        ncr = _ncr(
            db_session, disposition=NCRDisposition.REWORK, dispositioned_by=decider.id, dispositioned_at=STAMPED_AT
        )

        resp = client.put(
            f"{BASE}/ncr/{ncr.id}",
            headers=_headers(_user(db_session, first_name="Sage")),
            json={"version": 0, "disposition": "rework", "containment_action": "Quarantined lot 7"},
        )

        assert resp.status_code == 200, resp.text
        stored = _stored(db_session, ncr.id)
        assert (stored.dispositioned_by, stored.dispositioned_at) == (decider.id, STAMPED_AT)
        row = _committed(ncr.id)[0]
        assert row["new_values"] == {"containment_action": "Quarantined lot 7"}
        assert "disposition_change" not in row["extra_data"]

    def test_a_put_body_cannot_set_the_stamps_or_the_reverification(self, client, db_session):
        ncr = _ncr(db_session)
        resp = client.put(
            f"{BASE}/ncr/{ncr.id}",
            headers=_headers(_user(db_session)),
            json={
                "version": 0,
                "containment_action": "Quarantined lot 7",
                "dispositioned_by": 1,
                "dispositioned_at": "2026-01-01T00:00:00Z",
                "reverification_result": "conforming",
                "reverification_notes": "Sent in a body.",
                "reverified_by": 1,
                "reverified_at": "2026-01-01T00:00:00Z",
            },
        )

        assert resp.status_code == 200, resp.text
        stored = _stored(db_session, ncr.id)
        assert stored.containment_action == "Quarantined lot 7"
        for field in (
            "dispositioned_by",
            "dispositioned_at",
            "reverification_result",
            "reverification_notes",
            "reverified_by",
            "reverified_at",
        ):
            assert getattr(stored, field) is None, field


# ========================================================================= closure gate


class TestClosureGate:
    def test_the_service_names_a_missing_disposition_first(self):
        # The schema-level check on NCRUpdate refuses a closing request without a final
        # disposition before the service sees it, so the service's own first branch is
        # exercised directly: it judges the merged record, in the contract's order.
        for disposition in (None, NCRDisposition.PENDING):
            ncr = NonConformanceReport(ncr_number="NCR-UNIT-1", disposition=disposition)
            with pytest.raises(HTTPException) as refused:
                quality_record_service._check_ncr_closure(ncr, {"status": NCRStatus.CLOSED})
            assert refused.value.status_code == 422
            assert refused.value.detail == f"Closure requires {MISSING_DISPOSITION}, {MISSING_ACTION}"

    def test_a_pending_disposition_in_the_closing_request_is_422_and_nothing_is_written(self, client, db_session):
        ncr = _ncr(db_session, action_taken=ACTION_TAKEN)
        before = _snapshot(db_session)
        resp = client.put(
            f"{BASE}/ncr/{ncr.id}", headers=_headers(_user(db_session)), json=_close_body(disposition="pending")
        )
        assert resp.status_code == 422, resp.text
        assert _snapshot(db_session) == before

    def test_every_missing_item_is_named_in_order(self, client, db_session):
        ncr = _ncr(db_session, disposition=NCRDisposition.REPAIR)
        before = _snapshot(db_session)
        resp = client.put(
            f"{BASE}/ncr/{ncr.id}", headers=_headers(_user(db_session)), json=_close_body(disposition="repair")
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == (
            f"Closure requires {MISSING_ACTION}, {MISSING_CONCESSION}, {MISSING_REVERIFICATION}"
        )
        assert _snapshot(db_session) == before

    # ---------------------------------------------------------------- action taken

    def test_missing_action_taken_is_422_and_nothing_is_written(self, client, db_session):
        ncr = _ncr(db_session, disposition=NCRDisposition.SCRAP)
        before = _snapshot(db_session)
        resp = client.put(
            f"{BASE}/ncr/{ncr.id}", headers=_headers(_user(db_session)), json=_close_body(disposition="scrap")
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == f"Closure requires {MISSING_ACTION}"
        assert _snapshot(db_session) == before

    def test_a_stored_action_taken_satisfies_the_gate(self, client, db_session):
        ncr = _ncr(db_session, disposition=NCRDisposition.SCRAP, action_taken=ACTION_TAKEN)
        resp = client.put(
            f"{BASE}/ncr/{ncr.id}", headers=_headers(_user(db_session)), json=_close_body(disposition="scrap")
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "closed"

    def test_action_taken_supplied_in_the_closing_request_satisfies_the_gate(self, client, db_session):
        ncr = _ncr(db_session, disposition=NCRDisposition.SCRAP)
        resp = client.put(
            f"{BASE}/ncr/{ncr.id}",
            headers=_headers(_user(db_session)),
            json=_close_body(disposition="scrap", action_taken=ACTION_TAKEN),
        )
        assert resp.status_code == 200, resp.text
        assert _stored(db_session, ncr.id).action_taken == ACTION_TAKEN
        row = _committed(ncr.id)[0]
        assert row["action"] == "STATUS_CHANGE" and row["new_values"]["action_taken"] == ACTION_TAKEN

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blanking_the_stored_action_taken_in_the_closing_request_is_422(self, client, db_session, blank):
        ncr = _ncr(db_session, disposition=NCRDisposition.SCRAP, action_taken=ACTION_TAKEN)
        before = _snapshot(db_session)
        resp = client.put(
            f"{BASE}/ncr/{ncr.id}",
            headers=_headers(_user(db_session)),
            json=_close_body(disposition="scrap", action_taken=blank),
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == f"Closure requires {MISSING_ACTION}"
        assert _snapshot(db_session) == before

    # ---------------------------------------------------------------- concession

    @staticmethod
    def _concession_candidate(db, disposition, verifier, **extra):
        """An NCR whose only closure gap is the concession (repair also carries its re-verification)."""
        if disposition == "repair":
            return _reverified(db, verifier, disposition=NCRDisposition.REPAIR, action_taken=ACTION_TAKEN, **extra)
        return _ncr(db, disposition=NCRDisposition(disposition), action_taken=ACTION_TAKEN, **extra)

    @pytest.mark.parametrize("disposition", ["use_as_is", "repair"])
    def test_use_as_is_and_repair_need_a_concession_reference(self, client, db_session, disposition):
        user = _user(db_session)
        ncr = self._concession_candidate(db_session, disposition, user)
        before = _snapshot(db_session)
        resp = client.put(f"{BASE}/ncr/{ncr.id}", headers=_headers(user), json=_close_body(disposition=disposition))
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == f"Closure requires {MISSING_CONCESSION}"
        assert _snapshot(db_session) == before

    @pytest.mark.parametrize("disposition", ["use_as_is", "repair"])
    def test_a_stored_concession_satisfies_the_gate(self, client, db_session, disposition):
        user = _user(db_session)
        ncr = self._concession_candidate(db_session, disposition, user, concession_reference="DEV-2041")
        resp = client.put(f"{BASE}/ncr/{ncr.id}", headers=_headers(user), json=_close_body(disposition=disposition))
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "closed"

    @pytest.mark.parametrize("disposition", ["use_as_is", "repair"])
    def test_a_concession_supplied_in_the_closing_request_satisfies_the_gate(self, client, db_session, disposition):
        user = _user(db_session)
        ncr = self._concession_candidate(db_session, disposition, user)
        resp = client.put(
            f"{BASE}/ncr/{ncr.id}",
            headers=_headers(user),
            json=_close_body(
                disposition=disposition,
                concession_reference="DEV-2041",
                concession_authorized_by="Robin Buyer",
                concession_date="2026-10-29",
            ),
        )
        assert resp.status_code == 200, resp.text
        stored = _stored(db_session, ncr.id)
        assert (stored.concession_reference, stored.concession_authorized_by, stored.concession_date) == (
            "DEV-2041",
            "Robin Buyer",
            date(2026, 10, 29),
        )

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blanking_a_stored_concession_in_the_closing_request_is_422(self, client, db_session, blank):
        ncr = _ncr(
            db_session, disposition=NCRDisposition.USE_AS_IS, action_taken=ACTION_TAKEN, concession_reference="DEV-1"
        )
        before = _snapshot(db_session)
        resp = client.put(
            f"{BASE}/ncr/{ncr.id}",
            headers=_headers(_user(db_session)),
            json=_close_body(disposition="use_as_is", concession_reference=blank),
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == f"Closure requires {MISSING_CONCESSION}"
        assert _snapshot(db_session) == before

    def test_a_disposition_decided_in_the_closing_request_is_judged_and_stamped(self, client, db_session):
        user = _user(db_session)
        ncr = _ncr(db_session)
        resp = client.put(
            f"{BASE}/ncr/{ncr.id}",
            headers=_headers(user),
            json=_close_body(disposition="use_as_is", action_taken=ACTION_TAKEN, concession_reference="DEV-2041"),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "closed" and body["dispositioned_by"] == user.id
        row = _committed(ncr.id)[0]
        assert row["action"] == "STATUS_CHANGE"
        assert row["new_values"]["concession_reference"] == "DEV-2041"
        assert row["new_values"]["dispositioned_by"] == user.id

    @pytest.mark.parametrize("disposition", ["rework", "scrap", "return_to_vendor"])
    def test_other_dispositions_need_no_concession(self, client, db_session, disposition):
        user = _user(db_session)
        if disposition == "rework":
            ncr = _reverified(db_session, user, action_taken=ACTION_TAKEN)
        else:
            ncr = _ncr(db_session, disposition=NCRDisposition(disposition), action_taken=ACTION_TAKEN)
        resp = client.put(f"{BASE}/ncr/{ncr.id}", headers=_headers(user), json=_close_body(disposition=disposition))
        assert resp.status_code == 200, resp.text

    # ---------------------------------------------------------------- re-verification

    @staticmethod
    def _rework_or_repair(db, disposition, **extra):
        concession = {"concession_reference": "DEV-2041"} if disposition == "repair" else {}
        return _ncr(db, disposition=NCRDisposition(disposition), action_taken=ACTION_TAKEN, **concession, **extra)

    @pytest.mark.parametrize("disposition", ["rework", "repair"])
    def test_rework_and_repair_need_a_reverification(self, client, db_session, disposition):
        ncr = self._rework_or_repair(db_session, disposition)
        before = _snapshot(db_session)
        resp = client.put(
            f"{BASE}/ncr/{ncr.id}", headers=_headers(_user(db_session)), json=_close_body(disposition=disposition)
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == f"Closure requires {MISSING_REVERIFICATION}"
        assert _snapshot(db_session) == before

    @pytest.mark.parametrize("disposition", ["rework", "repair"])
    def test_a_nonconforming_reverification_does_not_close(self, client, db_session, disposition):
        verifier = _user(db_session)
        ncr = self._rework_or_repair(
            db_session,
            disposition,
            reverification_result="nonconforming",
            reverification_notes=NOTES,
            reverified_by=verifier.id,
            reverified_at=STAMPED_AT,
        )
        before = _snapshot(db_session)
        resp = client.put(f"{BASE}/ncr/{ncr.id}", headers=_headers(verifier), json=_close_body(disposition=disposition))
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == f"Closure requires {MISSING_REVERIFICATION}"
        assert _snapshot(db_session) == before

    @pytest.mark.parametrize("disposition", ["rework", "repair"])
    def test_a_stored_conforming_reverification_closes_and_stays_on_the_record(self, client, db_session, disposition):
        verifier = _user(db_session)
        ncr = self._rework_or_repair(
            db_session,
            disposition,
            reverification_result="conforming",
            reverification_notes=NOTES,
            reverified_by=verifier.id,
            reverified_at=STAMPED_AT,
        )
        resp = client.put(f"{BASE}/ncr/{ncr.id}", headers=_headers(verifier), json=_close_body(disposition=disposition))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "closed" and body["reverification_result"] == "conforming"
        row = _committed(ncr.id)[0]
        assert not any(key.startswith("reverif") for key in row["new_values"])

    # ------------------------------------------------- same-request disposition change

    def test_changing_the_disposition_in_the_closing_request_cannot_close_on_the_old_reverification(
        self, client, db_session
    ):
        # A conforming re-verification of the REWORK says nothing about a repair: the
        # disposition change clears it, and the gate judges that clearing in this request.
        verifier = _user(db_session)
        ncr = _reverified(db_session, verifier, action_taken=ACTION_TAKEN)
        before = _snapshot(db_session)
        resp = client.put(
            f"{BASE}/ncr/{ncr.id}",
            headers=_headers(verifier),
            json=_close_body(disposition="repair", concession_reference="DEV-2041"),
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == f"Closure requires {MISSING_REVERIFICATION}"
        # Refused before any write: the stored re-verification is untouched.
        assert _snapshot(db_session) == before
        assert _stored(db_session, ncr.id).reverification_result == "conforming"

    def test_repair_changed_to_rework_in_the_closing_request_is_refused_too(self, client, db_session):
        verifier = _user(db_session)
        ncr = _reverified(
            db_session,
            verifier,
            disposition=NCRDisposition.REPAIR,
            action_taken=ACTION_TAKEN,
            concession_reference="DEV-2041",
        )
        resp = client.put(f"{BASE}/ncr/{ncr.id}", headers=_headers(verifier), json=_close_body(disposition="rework"))
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == f"Closure requires {MISSING_REVERIFICATION}"

    def test_changing_to_scrap_in_the_closing_request_closes_and_records_the_cleared_reverification(
        self, client, db_session
    ):
        verifier = _user(db_session)
        closer = _user(db_session, first_name="Sage")
        ncr = _reverified(db_session, verifier, action_taken=ACTION_TAKEN)
        resp = client.put(f"{BASE}/ncr/{ncr.id}", headers=_headers(closer), json=_close_body(disposition="scrap"))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "closed" and body["disposition"] == "scrap"
        assert body["reverification_result"] is None and body["reverified_by"] is None
        row = _committed(ncr.id)[0]
        assert row["action"] == "STATUS_CHANGE"
        assert row["old_values"]["reverification_result"] == "conforming"
        assert row["old_values"]["reverified_by"] == verifier.id
        assert row["new_values"]["reverification_result"] is None
        assert row["new_values"]["dispositioned_by"] == closer.id


# ========================================================================= concession date


class TestConcessionDate:
    def test_a_future_concession_date_is_422_and_today_is_accepted(self, client, db_session, monkeypatch):
        monkeypatch.setattr(quality_record_service, "central_today", lambda: date(2026, 11, 13))
        headers = _headers(_user(db_session))
        ncr = _ncr(db_session)
        before = _snapshot(db_session)

        future = client.put(
            f"{BASE}/ncr/{ncr.id}", headers=headers, json={"version": 0, "concession_date": "2026-11-14"}
        )

        assert future.status_code == 422, future.text
        assert future.json()["detail"] == "The concession date cannot be in the future."
        assert _snapshot(db_session) == before
        today = client.put(
            f"{BASE}/ncr/{ncr.id}", headers=headers, json={"version": 0, "concession_date": "2026-11-13"}
        )
        assert today.status_code == 200, today.text
        assert today.json()["concession_date"] == "2026-11-13"

    def test_a_future_date_in_a_closing_request_is_422_before_the_close(self, client, db_session, monkeypatch):
        monkeypatch.setattr(quality_record_service, "central_today", lambda: date(2026, 11, 13))
        ncr = _ncr(db_session, disposition=NCRDisposition.USE_AS_IS, action_taken=ACTION_TAKEN)
        before = _snapshot(db_session)
        resp = client.put(
            f"{BASE}/ncr/{ncr.id}",
            headers=_headers(_user(db_session)),
            json=_close_body(disposition="use_as_is", concession_reference="DEV-2041", concession_date="2026-12-01"),
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == "The concession date cannot be in the future."
        assert _snapshot(db_session) == before

    def test_an_unchanged_stored_date_is_not_rechecked(self, client, db_session, monkeypatch):
        monkeypatch.setattr(quality_record_service, "central_today", lambda: date(2026, 11, 13))
        ncr = _ncr(db_session, concession_date=date(2026, 11, 20))
        resp = client.put(
            f"{BASE}/ncr/{ncr.id}",
            headers=_headers(_user(db_session)),
            json={"version": 0, "concession_date": "2026-11-20", "containment_action": "Quarantined lot 7"},
        )
        assert resp.status_code == 200, resp.text


# ============================================================================= API tokens


class TestApiTokenJudgment:
    @staticmethod
    def _decided(db):
        person = _user(db, first_name="Rowan")
        ncr = _ncr(
            db,
            disposition=NCRDisposition.USE_AS_IS,
            dispositioned_by=person.id,
            dispositioned_at=STAMPED_AT,
            concession_reference="DEV-1",
            concession_authorized_by="Robin Buyer",
            concession_date=date(2026, 10, 30),
        )
        return person, ncr

    @pytest.mark.parametrize(
        "change",
        [
            {"disposition": "scrap"},
            {"disposition": "pending"},
            {"concession_reference": "DEV-2"},
            {"concession_reference": None},
            {"concession_authorized_by": "Casey Planner"},
            {"concession_date": "2026-10-31"},
        ],
    )
    def test_a_token_cannot_change_the_disposition_or_concession(self, client, db_session, change):
        _, ncr = self._decided(db_session)
        headers = _token_headers(client, db_session, _user(db_session, role=UserRole.QUALITY))
        before = _snapshot(db_session)

        resp = client.put(
            f"{BASE}/ncr/{ncr.id}", headers=headers, json={"version": 0, "containment_action": "Tagged", **change}
        )

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_NCR_JUDGMENT
        assert _snapshot(db_session) == before
        assert _committed(ncr.id) == []

    def test_a_token_cannot_record_a_first_disposition(self, client, db_session):
        ncr = _ncr(db_session)
        headers = _token_headers(client, db_session, _user(db_session, role=UserRole.QUALITY))
        before = _snapshot(db_session)
        resp = client.put(f"{BASE}/ncr/{ncr.id}", headers=headers, json={"version": 0, "disposition": "rework"})
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_NCR_JUDGMENT
        assert _snapshot(db_session) == before

    def test_a_token_may_resend_the_stored_judgment_unchanged_with_another_edit(self, client, db_session):
        person, ncr = self._decided(db_session)
        holder = _user(db_session, role=UserRole.QUALITY)
        headers = _token_headers(client, db_session, holder)

        resp = client.put(
            f"{BASE}/ncr/{ncr.id}",
            headers=headers,
            json={
                "version": 0,
                "disposition": "use_as_is",
                "concession_reference": "DEV-1",
                "concession_authorized_by": "Robin Buyer",
                "concession_date": "2026-10-30",
                "action_taken": ACTION_TAKEN,
            },
        )

        assert resp.status_code == 200, resp.text
        stored = _stored(db_session, ncr.id)
        assert stored.action_taken == ACTION_TAKEN
        # The stamp still names the person who decided it, not the token holder.
        assert (stored.dispositioned_by, stored.dispositioned_at) == (person.id, STAMPED_AT)
        rows = _committed(ncr.id)
        assert len(rows) == 1 and rows[0]["new_values"] == {"action_taken": ACTION_TAKEN}
        assert rows[0]["user_id"] == holder.id

    def test_a_token_cannot_record_a_reverification(self, client, db_session):
        ncr = _ncr(db_session, disposition=NCRDisposition.REWORK)
        headers = _token_headers(client, db_session, _user(db_session, role=UserRole.QUALITY))
        before = _snapshot(db_session)
        resp = client.post(
            f"{BASE}/ncr/{ncr.id}/reverification", headers=headers, json={"result": "conforming", "notes": NOTES}
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_NCR_REVERIFICATION
        assert _snapshot(db_session) == before
        assert _committed(ncr.id) == []


# ======================================================================= re-verification verb


class TestReverificationVerb:
    @pytest.mark.parametrize("role", [UserRole.QUALITY, UserRole.ADMIN, UserRole.MANAGER])
    @pytest.mark.parametrize("disposition", [NCRDisposition.REWORK, NCRDisposition.REPAIR])
    def test_write_roles_record_it_server_stamped_with_a_committed_update_row(
        self, client, db_session, role, disposition
    ):
        user = _user(db_session, role=role, first_name="Rowan")
        ncr = _ncr(db_session, disposition=disposition, status=NCRStatus.PENDING_DISPOSITION)

        resp = client.post(
            f"{BASE}/ncr/{ncr.id}/reverification",
            headers=_headers(user),
            json={"result": "conforming", "notes": f"  {NOTES}  "},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["reverification_result"] == "conforming"
        assert body["reverification_notes"] == NOTES  # stripped
        assert body["reverified_by"] == user.id and body["reverified_by_name"] == user.full_name
        _assert_recent_utc(body["reverified_at"])
        # The verb writes the re-verification and nothing else.
        assert body["status"] == "pending_disposition" and body["disposition"] == disposition.value

        rows = _committed(ncr.id)
        assert [row["action"] for row in rows] == ["UPDATE"]
        row = rows[0]
        assert row["user_id"] == user.id and row["company_id"] == 1
        assert row["old_values"] == {
            "reverification_result": None,
            "reverification_notes": None,
            "reverified_by": None,
            "reverified_at": None,
        }
        assert row["new_values"]["reverification_result"] == "conforming"
        assert row["new_values"]["reverification_notes"] == NOTES
        assert row["new_values"]["reverified_by"] == user.id and row["new_values"]["reverified_at"]
        assert row["extra_data"]["reverification"] is True

        db_session.expire_all()
        events = (
            db_session.query(OperationalEvent)
            .filter(OperationalEvent.entity_type == "ncr", OperationalEvent.entity_id == ncr.id)
            .all()
        )
        assert [event.event_type for event in events] == ["ncr_updated"]
        assert events[0].event_payload["changed_fields"] == [
            "reverification_notes",
            "reverification_result",
            "reverified_at",
            "reverified_by",
        ]

    @pytest.mark.parametrize("role", [UserRole.SUPERVISOR, UserRole.OPERATOR, UserRole.VIEWER])
    def test_other_roles_are_forbidden_and_nothing_changes(self, client, db_session, role):
        ncr = _ncr(db_session, disposition=NCRDisposition.REWORK)
        before = _snapshot(db_session)
        resp = client.post(
            f"{BASE}/ncr/{ncr.id}/reverification",
            headers=_headers(_user(db_session, role=role)),
            json={"result": "conforming", "notes": NOTES},
        )
        assert resp.status_code == 403, resp.text
        assert _snapshot(db_session) == before

    def test_a_closed_ncr_is_409_and_nothing_changes(self, client, db_session):
        ncr = _ncr(
            db_session, disposition=NCRDisposition.REWORK, status=NCRStatus.CLOSED, closed_date=date(2026, 11, 1)
        )
        before = _snapshot(db_session)
        resp = client.post(
            f"{BASE}/ncr/{ncr.id}/reverification",
            headers=_headers(_user(db_session)),
            json={"result": "conforming", "notes": NOTES},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == f"NCR {ncr.ncr_number} is closed; reopen it to record a re-verification."
        assert _snapshot(db_session) == before

    def test_a_legacy_void_status_ncr_is_409(self, client, db_session):
        ncr = _ncr(db_session, disposition=NCRDisposition.REWORK, status=NCRStatus.VOID)
        before = _snapshot(db_session)
        resp = client.post(
            f"{BASE}/ncr/{ncr.id}/reverification",
            headers=_headers(_user(db_session)),
            json={"result": "conforming", "notes": NOTES},
        )
        assert resp.status_code == 409, resp.text
        assert _snapshot(db_session) == before

    @pytest.mark.parametrize(
        "disposition",
        [NCRDisposition.PENDING, NCRDisposition.USE_AS_IS, NCRDisposition.SCRAP, NCRDisposition.RETURN_TO_VENDOR],
    )
    def test_a_disposition_other_than_rework_or_repair_is_409(self, client, db_session, disposition):
        ncr = _ncr(db_session, disposition=disposition)
        before = _snapshot(db_session)
        resp = client.post(
            f"{BASE}/ncr/{ncr.id}/reverification",
            headers=_headers(_user(db_session)),
            json={"result": "conforming", "notes": NOTES},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == (
            "Re-verification applies to rework and repair dispositions; "
            f"NCR {ncr.ncr_number} is dispositioned {disposition.value}."
        )
        assert _snapshot(db_session) == before

    def test_rerecording_overwrites_and_the_audit_row_keeps_what_it_replaced(self, client, db_session):
        first = _user(db_session, first_name="Rowan")
        second = _user(db_session, first_name="Sage")
        ncr = _ncr(db_session, disposition=NCRDisposition.REWORK)

        one = client.post(
            f"{BASE}/ncr/{ncr.id}/reverification",
            headers=_headers(first),
            json={"result": "nonconforming", "notes": "Two of five still show a burr at the corner."},
        )
        assert one.status_code == 200, one.text
        first_at = _stored(db_session, ncr.id).reverified_at

        two = client.post(
            f"{BASE}/ncr/{ncr.id}/reverification",
            headers=_headers(second),
            json={"result": "conforming", "notes": NOTES},
        )

        assert two.status_code == 200, two.text
        body = two.json()
        assert body["reverification_result"] == "conforming" and body["reverification_notes"] == NOTES
        assert body["reverified_by"] == second.id and body["reverified_by_name"] == second.full_name
        rows = _committed(ncr.id)
        assert [row["action"] for row in rows] == ["UPDATE", "UPDATE"]
        assert rows[1]["user_id"] == second.id
        assert rows[1]["old_values"] == {
            "reverification_result": "nonconforming",
            "reverification_notes": "Two of five still show a burr at the corner.",
            "reverified_by": first.id,
            "reverified_at": first_at.isoformat(),
        }
        assert rows[1]["new_values"]["reverification_result"] == "conforming"
        assert rows[1]["new_values"]["reverified_by"] == second.id
        changes = rows[1]["extra_data"]["changes"]
        assert changes["reverification_result"] == {"old": "nonconforming", "new": "conforming"}
        assert changes["reverified_by"] == {"old": first.id, "new": second.id}

    @pytest.mark.parametrize(
        "body",
        [
            {"result": "conforming"},
            {"result": "conforming", "notes": "   "},
            {"result": "conforming", "notes": ""},
            {"result": "conforming", "notes": "x" * 4001},
            {"result": "pass", "notes": NOTES},
            {"notes": NOTES},
            {"result": "conforming", "notes": NOTES, "reverified_by": 1},
            {"result": "conforming", "notes": NOTES, "reverified_at": "2026-01-01T00:00:00Z"},
        ],
    )
    def test_an_invalid_body_is_422_and_nothing_changes(self, client, db_session, body):
        ncr = _ncr(db_session, disposition=NCRDisposition.REWORK)
        before = _snapshot(db_session)
        resp = client.post(f"{BASE}/ncr/{ncr.id}/reverification", headers=_headers(_user(db_session)), json=body)
        assert resp.status_code == 422, resp.text
        assert _snapshot(db_session) == before

    def test_lost_audit_row_rolls_the_reverification_back(self, client, db_session, monkeypatch):
        ncr = _ncr(db_session, disposition=NCRDisposition.REWORK)
        headers = _headers(_user(db_session))
        before = _snapshot(db_session)
        monkeypatch.setattr(AuditService, "log", lambda *args, **kwargs: None)

        resp = client.post(
            f"{BASE}/ncr/{ncr.id}/reverification", headers=headers, json={"result": "conforming", "notes": NOTES}
        )

        assert resp.status_code == 503, resp.text
        assert resp.json() == {"detail": "Unable to save audit record"}
        assert _snapshot(db_session) == before

    def test_another_companys_ncr_is_404_and_nothing_changes(self, client, db_session):
        foreign = _ncr(db_session, company_id=2, disposition=NCRDisposition.REWORK)
        before = _snapshot(db_session)
        resp = client.post(
            f"{BASE}/ncr/{foreign.id}/reverification",
            headers=_headers(_user(db_session)),
            json={"result": "conforming", "notes": NOTES},
        )
        assert resp.status_code == 404, resp.text
        assert _snapshot(db_session) == before
        assert _stored(db_session, foreign.id).reverification_result is None

    def test_a_voided_ncr_is_404(self, client, db_session):
        ncr = _ncr(db_session, disposition=NCRDisposition.REWORK, is_deleted=True)
        before = _snapshot(db_session)
        resp = client.post(
            f"{BASE}/ncr/{ncr.id}/reverification",
            headers=_headers(_user(db_session)),
            json={"result": "conforming", "notes": NOTES},
        )
        assert resp.status_code == 404, resp.text
        assert _snapshot(db_session) == before


# ======================================================= disposition change clears re-verification


class TestDispositionChangeClearsReverification:
    @pytest.mark.parametrize("new_disposition", ["repair", "scrap", "use_as_is", "pending"])
    def test_a_changed_disposition_clears_it_and_the_audit_row_keeps_it(self, client, db_session, new_disposition):
        verifier = _user(db_session, first_name="Rowan")
        editor = _user(db_session, first_name="Sage")
        ncr = _reverified(db_session, verifier)

        resp = client.put(
            f"{BASE}/ncr/{ncr.id}", headers=_headers(editor), json={"version": 0, "disposition": new_disposition}
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        for field in ("reverification_result", "reverification_notes", "reverified_by", "reverified_at"):
            assert body[field] is None, field
        assert body["reverified_by_name"] is None
        row = _committed(ncr.id)[0]
        assert row["old_values"]["reverification_result"] == "conforming"
        assert row["old_values"]["reverification_notes"] == NOTES
        assert row["old_values"]["reverified_by"] == verifier.id
        assert row["old_values"]["reverified_at"] == STAMPED_AT.isoformat()
        for field in ("reverification_result", "reverification_notes", "reverified_by", "reverified_at"):
            assert row["new_values"][field] is None, field
        assert row["extra_data"]["changes"]["reverification_result"] == {"old": "conforming", "new": None}

    def test_an_unchanged_disposition_keeps_the_reverification(self, client, db_session):
        verifier = _user(db_session)
        ncr = _reverified(db_session, verifier)
        resp = client.put(
            f"{BASE}/ncr/{ncr.id}",
            headers=_headers(_user(db_session)),
            json={"version": 0, "disposition": "rework", "action_taken": ACTION_TAKEN},
        )
        assert resp.status_code == 200, resp.text
        stored = _stored(db_session, ncr.id)
        assert (stored.reverification_result, stored.reverified_by) == ("conforming", verifier.id)

    def test_with_nothing_to_clear_the_audit_row_carries_no_reverification_fields(self, client, db_session):
        ncr = _ncr(db_session, disposition=NCRDisposition.REWORK)
        resp = client.put(
            f"{BASE}/ncr/{ncr.id}", headers=_headers(_user(db_session)), json={"version": 0, "disposition": "repair"}
        )
        assert resp.status_code == 200, resp.text
        assert set(_committed(ncr.id)[0]["new_values"]) == {"disposition", "dispositioned_by", "dispositioned_at"}

    def test_lost_audit_row_keeps_the_reverification(self, client, db_session, monkeypatch):
        ncr = _reverified(db_session, _user(db_session))
        headers = _headers(_user(db_session))
        before = _snapshot(db_session)
        monkeypatch.setattr(AuditService, "log", lambda *args, **kwargs: None)

        resp = client.put(f"{BASE}/ncr/{ncr.id}", headers=headers, json={"version": 0, "disposition": "scrap"})

        assert resp.status_code == 503, resp.text
        assert resp.json() == {"detail": "Unable to save audit record"}
        assert _snapshot(db_session) == before
        assert _stored(db_session, ncr.id).reverification_result == "conforming"


# ============================================================================ response names


class TestResponseNames:
    def test_both_names_are_resolved_on_writes_detail_and_list(self, client, db_session):
        decider = _user(db_session, first_name="Rowan")
        verifier = _user(db_session, first_name="Sage")
        ncr = _ncr(db_session)

        decided = client.put(
            f"{BASE}/ncr/{ncr.id}", headers=_headers(decider), json={"version": 0, "disposition": "rework"}
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["dispositioned_by_name"] == decider.full_name

        verified = client.post(
            f"{BASE}/ncr/{ncr.id}/reverification",
            headers=_headers(verifier),
            json={"result": "conforming", "notes": NOTES},
        )
        assert verified.status_code == 200, verified.text
        assert verified.json()["dispositioned_by_name"] == decider.full_name
        assert verified.json()["reverified_by_name"] == verifier.full_name

        viewer = _headers(_user(db_session, role=UserRole.VIEWER))
        detail = client.get(f"{BASE}/ncr/{ncr.id}", headers=viewer).json()
        assert (detail["dispositioned_by_name"], detail["reverified_by_name"]) == (
            decider.full_name,
            verifier.full_name,
        )
        listed = {row["id"]: row for row in client.get(f"{BASE}/ncr", headers=viewer).json()}
        assert (listed[ncr.id]["dispositioned_by_name"], listed[ncr.id]["reverified_by_name"]) == (
            decider.full_name,
            verifier.full_name,
        )

    def test_unstamped_names_are_null(self, client, db_session):
        ncr = _ncr(db_session)
        body = client.get(f"{BASE}/ncr/{ncr.id}", headers=_headers(_user(db_session))).json()
        assert body["dispositioned_by_name"] is None and body["reverified_by_name"] is None

    def test_a_platform_admin_acting_in_this_company_is_named(self, client, db_session):
        platform = _user(db_session, role=UserRole.PLATFORM_ADMIN, company_id=2, first_name="Pat")
        ncr = _ncr(db_session, disposition=NCRDisposition.REWORK)
        resp = client.post(
            f"{BASE}/ncr/{ncr.id}/reverification",
            headers=_headers(platform, company_id=1),
            json={"result": "conforming", "notes": NOTES},
        )
        assert resp.status_code == 200, resp.text
        body = client.get(f"{BASE}/ncr/{ncr.id}", headers=_headers(_user(db_session, role=UserRole.VIEWER))).json()
        assert body["reverified_by"] == platform.id and body["reverified_by_name"] == platform.full_name

    def test_another_companys_ordinary_user_is_never_named(self, client, db_session):
        foreign = _user(db_session, company_id=2, first_name="Foreignperson")
        ncr = _ncr(
            db_session,
            disposition=NCRDisposition.REWORK,
            dispositioned_by=foreign.id,
            dispositioned_at=STAMPED_AT,
            reverification_result="conforming",
            reverification_notes=NOTES,
            reverified_by=foreign.id,
            reverified_at=STAMPED_AT,
        )
        resp = client.get(f"{BASE}/ncr/{ncr.id}", headers=_headers(_user(db_session, role=UserRole.VIEWER)))
        assert resp.status_code == 200, resp.text
        assert resp.json()["dispositioned_by_name"] is None and resp.json()["reverified_by_name"] is None
        assert "Foreignperson" not in resp.text
