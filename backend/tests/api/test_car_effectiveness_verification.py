"""AS9100D 10.2 corrective-action evidence on a CAR (migration ``127_ncr_car_records``).

Covers ``app/services/quality_record_service.py`` and ``car_pdf_service.py`` through the
``/api/v1/quality`` routes:

* ``POST /quality/car/{id}/effectiveness`` -- only while the CAR is in Verification (409
  otherwise); ``effective`` keeps the status on an UPDATE row, ``not_effective`` returns the
  CAR to corrective action on a STATUS_CHANGE row; server-stamped and re-recordable with old
  -> new on the audit row; Admin / Manager / Quality only (403), never an API token (403);
  required audit (503 and nothing saved); tenant-isolated (404);
* ``effectiveness_check`` is part of a recorded verification: a PUT that CHANGES it is 409,
  an unchanged re-send is fine, and no body sets the result or its stamps;
* a PUT that CHANGES ``root_cause`` or ``corrective_action`` clears the recorded result,
  verifier and instant (old -> new on its audit row), unlocks the evidence for that same
  request, and a close in that request is judged as already cleared; a status-only reopen
  keeps the result;
* the closure gate: a corrective CAR -- ``car_type`` corrective, or NULL -- also needs both
  AS9100D 10.2.1 reviews and a stored ``effective`` result (merged with the request);
  preventive and improvement CARs keep the original four;
* ``effectiveness_verified_by_name`` resolves tenant-scoped;
* the printable CAR carries the two reviews and the effectiveness block, dated in Central,
  with every value escaped (markup prints literally).

Audit rows are read back through a SECOND connection (``export_audit_helpers.fresh_session``)
so a write that logged but never committed fails here.
"""

import itertools
from datetime import date, datetime, timedelta, timezone
from io import BytesIO

import pytest
from fastapi import HTTPException
from pypdf import PdfReader
from sqlalchemy import inspect

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.operational_event import OperationalEvent
from app.models.quality import CARStatus, CARType, CorrectiveActionRequest, NonConformanceReport
from app.models.user import User, UserRole
from app.services import quality_record_service
from app.services.audit_service import AuditService
from app.services.quality_record_service import API_TOKEN_CAR_EFFECTIVENESS
from tests.api.export_audit_helpers import fresh_session

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

BASE = "/api/v1/quality"
API_TOKENS_URL = "/api/v1/api-tokens/"
PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens minted directly; never used for login
EVIDENCE = "Six weeks of final inspection records (lots 41-58) show no edge burrs."
HUMAN_FACTORS = "No fatigue or training factor; the fixture wore out between PM intervals."
SIMILAR = "Checked the two sister deburr fixtures; both within tolerance."
STAMPED_AT = datetime(2026, 10, 28, 15, 0, 0)

BASE_FOUR = {
    "root_cause": "Worn deburr fixture.",
    "corrective_action": "Replace and add fixture to PM.",
    "verification_method": "Inspect 3 consecutive lots.",
    "verification_results": "No burrs in 3 lots.",
}
MISSING_BASE = "root cause, corrective action, verification method, verification results"
MISSING_HUMAN = "human factors review"
MISSING_SIMILAR = "similar nonconformities review"
MISSING_EFFECTIVE = "an effective effectiveness verification"

_seq = itertools.count(1)


# --------------------------------------------------------------------------- builders


def _company(db, company_id=1):
    company = db.query(Company).filter(Company.id == company_id).first()
    if company is None:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"car-eff-co-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def _user(db, role=UserRole.QUALITY, company_id=1, first_name="Quinn"):
    _company(db, company_id)
    n = next(_seq)
    user = User(
        email=f"car-effect-{n}@co{company_id}.test",
        employee_id=f"CARE-{n:05d}",
        first_name=first_name,
        last_name=f"Reviewer{n}",
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
    issued = client.post(API_TOKENS_URL, json={"user_id": user.id, "label": "car probe"}, headers=_headers(admin))
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}


def _car(db, company_id=1, **extra):
    _company(db, company_id)
    n = next(_seq)
    fields = {
        "car_number": f"CAR-E{company_id}-{n:04d}",
        "title": "Corrective action for edge burr",
        "problem_description": "Repeated edge burrs at final inspection.",
        "status": CARStatus.OPEN,
        "priority": 3,
        "company_id": company_id,
    }
    fields.update(extra)
    car = CorrectiveActionRequest(**fields)
    db.add(car)
    db.commit()
    db.refresh(car)
    return car


def _in_verification(db, **extra):
    extra.setdefault("status", CARStatus.VERIFICATION)
    return _car(db, **BASE_FOUR, **extra)


def _verified(db, verifier, result="effective", **extra):
    """A CAR carrying a stored effectiveness verification by ``verifier``."""
    return _car(
        db,
        effectiveness_result=result,
        effectiveness_check=EVIDENCE,
        effectiveness_verified_by=verifier.id,
        effectiveness_verified_at=STAMPED_AT,
        **extra,
    )


def _record(client, car_id, headers, result="effective", evidence=EVIDENCE):
    return client.post(
        f"{BASE}/car/{car_id}/effectiveness", headers=headers, json={"result": result, "evidence": evidence}
    )


# --------------------------------------------------------------------------- readers


def _committed(resource_id, action=None):
    with fresh_session() as verify:
        query = verify.query(AuditLog).filter(AuditLog.resource_type == "car", AuditLog.resource_id == resource_id)
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
    """Every CAR / NCR / audit / event row, for "nothing changed" assertions."""
    db.expire_all()
    return {
        model.__tablename__: [
            {column.key: getattr(row, column.key) for column in inspect(model).columns}
            for row in db.query(model).order_by(model.id).all()
        ]
        for model in (CorrectiveActionRequest, NonConformanceReport, AuditLog, OperationalEvent)
    }


def _stored(db, car_id):
    db.expire_all()
    return db.get(CorrectiveActionRequest, car_id)


def _car_events(db, car_id):
    db.expire_all()
    return (
        db.query(OperationalEvent)
        .filter(OperationalEvent.entity_type == "car", OperationalEvent.entity_id == car_id)
        .order_by(OperationalEvent.id)
        .all()
    )


def _assert_recent_utc(value: str):
    assert value.endswith("Z"), value
    stamped = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert abs(datetime.now(timezone.utc) - stamped) < timedelta(minutes=5)


def _pdf_text(content: bytes) -> str:
    reader = PdfReader(BytesIO(content))
    return " ".join(" ".join((page.extract_text() or "").split()) for page in reader.pages)


# ======================================================================= effectiveness verb


class TestEffectivenessVerb:
    @pytest.mark.parametrize("role", [UserRole.QUALITY, UserRole.ADMIN, UserRole.MANAGER])
    def test_effective_keeps_the_status_and_writes_one_update_row(self, client, db_session, role):
        user = _user(db_session, role=role, first_name="Rowan")
        car = _in_verification(db_session, effectiveness_check="Draft evidence from the first lot.")

        resp = _record(client, car.id, _headers(user), evidence=f"  {EVIDENCE}  ")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "verification"
        assert body["effectiveness_result"] == "effective"
        assert body["effectiveness_check"] == EVIDENCE  # the evidence, stripped
        assert body["effectiveness_verified_by"] == user.id
        assert body["effectiveness_verified_by_name"] == user.full_name
        _assert_recent_utc(body["effectiveness_verified_at"])

        rows = _committed(car.id)
        assert [row["action"] for row in rows] == ["UPDATE"]
        row = rows[0]
        assert row["user_id"] == user.id and row["company_id"] == 1
        assert row["old_values"] == {
            "effectiveness_check": "Draft evidence from the first lot.",
            "effectiveness_result": None,
            "effectiveness_verified_by": None,
            "effectiveness_verified_at": None,
        }
        assert row["new_values"]["effectiveness_check"] == EVIDENCE
        assert row["new_values"]["effectiveness_result"] == "effective"
        assert row["new_values"]["effectiveness_verified_by"] == user.id
        assert row["new_values"]["effectiveness_verified_at"]
        assert "status" not in row["new_values"]
        assert row["extra_data"]["effectiveness_verification"] is True

        events = _car_events(db_session, car.id)
        assert [event.event_type for event in events] == ["car_updated"]
        assert events[0].event_payload["status"] == events[0].event_payload["previous_status"] == "verification"

    def test_not_effective_returns_the_car_to_corrective_action_on_a_status_change_row(self, client, db_session):
        user = _user(db_session, first_name="Rowan")
        car = _in_verification(db_session)

        resp = _record(client, car.id, _headers(user), result="not_effective")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "corrective_action"
        assert body["effectiveness_result"] == "not_effective"
        assert body["effectiveness_verified_by"] == user.id
        rows = _committed(car.id)
        assert [row["action"] for row in rows] == ["STATUS_CHANGE"]
        row = rows[0]
        assert (row["old_values"]["status"], row["new_values"]["status"]) == ("verification", "corrective_action")
        assert row["new_values"]["effectiveness_result"] == "not_effective"
        assert row["extra_data"]["effectiveness_verification"] is True
        assert "not effective" in row["description"] and "corrective_action" in row["description"]

        events = _car_events(db_session, car.id)
        assert [event.event_type for event in events] == ["car_updated"]
        assert events[0].event_payload["previous_status"] == "verification"
        assert events[0].event_payload["status"] == "corrective_action"

    def test_rerecording_overwrites_and_the_audit_row_keeps_what_it_replaced(self, client, db_session):
        first = _user(db_session, first_name="Rowan")
        second = _user(db_session, first_name="Sage")
        car = _in_verification(db_session)

        one = _record(client, car.id, _headers(first), result="not_effective", evidence="Burrs recurred on lot 44.")
        assert one.status_code == 200, one.text
        first_at = _stored(db_session, car.id).effectiveness_verified_at
        # Back through corrective action and into verification again, by the ordinary PUT.
        back = client.put(
            f"{BASE}/car/{car.id}", headers=_headers(first), json={"version": 0, "status": "verification"}
        )
        assert back.status_code == 200, back.text

        two = _record(client, car.id, _headers(second))

        assert two.status_code == 200, two.text
        body = two.json()
        assert body["effectiveness_result"] == "effective" and body["effectiveness_check"] == EVIDENCE
        assert body["effectiveness_verified_by"] == second.id
        assert body["effectiveness_verified_by_name"] == second.full_name
        rows = _committed(car.id)
        assert [row["action"] for row in rows] == ["STATUS_CHANGE", "STATUS_CHANGE", "UPDATE"]
        last = rows[-1]
        assert last["user_id"] == second.id
        assert last["old_values"] == {
            "effectiveness_check": "Burrs recurred on lot 44.",
            "effectiveness_result": "not_effective",
            "effectiveness_verified_by": first.id,
            "effectiveness_verified_at": first_at.isoformat(),
        }
        assert last["extra_data"]["changes"]["effectiveness_result"] == {"old": "not_effective", "new": "effective"}
        assert last["extra_data"]["changes"]["effectiveness_verified_by"] == {"old": first.id, "new": second.id}

    @pytest.mark.parametrize(
        "status",
        [
            CARStatus.OPEN,
            CARStatus.ROOT_CAUSE_ANALYSIS,
            CARStatus.CORRECTIVE_ACTION,
            CARStatus.CLOSED,
            CARStatus.VOID,
        ],
    )
    def test_outside_verification_is_409_and_nothing_changes(self, client, db_session, status):
        car = _car(db_session, status=status)
        before = _snapshot(db_session)
        resp = _record(client, car.id, _headers(_user(db_session)))
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == (
            f"Record effectiveness once CAR {car.car_number} is in Verification; it is {status.value}."
        )
        assert _snapshot(db_session) == before

    def test_an_api_token_is_refused_and_nothing_changes(self, client, db_session):
        car = _in_verification(db_session)
        headers = _token_headers(client, db_session, _user(db_session, role=UserRole.QUALITY))
        before = _snapshot(db_session)
        resp = _record(client, car.id, headers)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_CAR_EFFECTIVENESS
        assert _snapshot(db_session) == before
        assert _committed(car.id) == []

    @pytest.mark.parametrize("role", [UserRole.SUPERVISOR, UserRole.OPERATOR, UserRole.VIEWER])
    def test_other_roles_are_forbidden_and_nothing_changes(self, client, db_session, role):
        car = _in_verification(db_session)
        before = _snapshot(db_session)
        resp = _record(client, car.id, _headers(_user(db_session, role=role)))
        assert resp.status_code == 403, resp.text
        assert _snapshot(db_session) == before

    @pytest.mark.parametrize("result", ["effective", "not_effective"])
    def test_lost_audit_row_rolls_the_verification_back(self, client, db_session, monkeypatch, result):
        car = _in_verification(db_session)
        headers = _headers(_user(db_session))
        before = _snapshot(db_session)
        monkeypatch.setattr(AuditService, "log", lambda *args, **kwargs: None)

        resp = _record(client, car.id, headers, result=result)

        assert resp.status_code == 503, resp.text
        assert resp.json() == {"detail": "Unable to save audit record"}
        assert _snapshot(db_session) == before
        assert _stored(db_session, car.id).status == CARStatus.VERIFICATION

    @pytest.mark.parametrize(
        "body",
        [
            {"result": "effective"},
            {"result": "effective", "evidence": "   "},
            {"result": "effective", "evidence": ""},
            {"result": "effective", "evidence": "x" * 4001},
            {"result": "worked", "evidence": EVIDENCE},
            {"evidence": EVIDENCE},
            {"result": "effective", "evidence": EVIDENCE, "effectiveness_verified_by": 1},
            {"result": "effective", "evidence": EVIDENCE, "status": "closed"},
        ],
    )
    def test_an_invalid_body_is_422_and_nothing_changes(self, client, db_session, body):
        car = _in_verification(db_session)
        before = _snapshot(db_session)
        resp = client.post(f"{BASE}/car/{car.id}/effectiveness", headers=_headers(_user(db_session)), json=body)
        assert resp.status_code == 422, resp.text
        assert _snapshot(db_session) == before

    def test_another_companys_car_is_404_and_nothing_changes(self, client, db_session):
        foreign = _in_verification(db_session, company_id=2)
        before = _snapshot(db_session)
        resp = _record(client, foreign.id, _headers(_user(db_session)))
        assert resp.status_code == 404, resp.text
        assert _snapshot(db_session) == before
        assert _stored(db_session, foreign.id).effectiveness_result is None


# ================================================================ effectiveness_check lock


class TestEffectivenessEvidenceLock:
    @pytest.mark.parametrize("result", ["effective", "not_effective"])
    @pytest.mark.parametrize("new_evidence", ["Rewritten after the fact.", "", None])
    def test_changing_the_evidence_of_a_recorded_verification_is_409(self, client, db_session, result, new_evidence):
        car = _verified(db_session, _user(db_session), result=result, status=CARStatus.VERIFICATION)
        before = _snapshot(db_session)
        resp = client.put(
            f"{BASE}/car/{car.id}",
            headers=_headers(_user(db_session)),
            json={"version": 0, "effectiveness_check": new_evidence, "rma_number": "RMA-9"},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == (
            f"CAR {car.car_number} has a recorded effectiveness verification; record a new one with the "
            "effectiveness action to change its evidence."
        )
        assert _snapshot(db_session) == before

    def test_an_unchanged_resend_with_another_edit_is_fine(self, client, db_session):
        verifier = _user(db_session)
        car = _verified(db_session, verifier, status=CARStatus.VERIFICATION)
        resp = client.put(
            f"{BASE}/car/{car.id}",
            headers=_headers(_user(db_session)),
            json={"version": 0, "effectiveness_check": EVIDENCE, "rma_number": "RMA-9"},
        )
        assert resp.status_code == 200, resp.text
        stored = _stored(db_session, car.id)
        assert stored.rma_number == "RMA-9" and stored.effectiveness_check == EVIDENCE
        assert stored.effectiveness_verified_by == verifier.id
        assert _committed(car.id)[0]["new_values"] == {"rma_number": "RMA-9"}

    def test_without_a_recorded_result_the_evidence_stays_editable(self, client, db_session):
        car = _in_verification(db_session, effectiveness_check="Draft evidence.")
        resp = client.put(
            f"{BASE}/car/{car.id}",
            headers=_headers(_user(db_session)),
            json={"version": 0, "effectiveness_check": "Fuller draft evidence."},
        )
        assert resp.status_code == 200, resp.text
        assert _stored(db_session, car.id).effectiveness_check == "Fuller draft evidence."

    def test_no_put_body_sets_the_result_or_its_stamps(self, client, db_session):
        car = _in_verification(db_session)
        resp = client.put(
            f"{BASE}/car/{car.id}",
            headers=_headers(_user(db_session)),
            json={
                "version": 0,
                "rma_number": "RMA-9",
                "effectiveness_result": "effective",
                "effectiveness_verified_by": 1,
                "effectiveness_verified_at": "2026-01-01T00:00:00Z",
            },
        )
        assert resp.status_code == 200, resp.text
        stored = _stored(db_session, car.id)
        assert stored.rma_number == "RMA-9"
        assert stored.effectiveness_result is None
        assert stored.effectiveness_verified_by is None and stored.effectiveness_verified_at is None

    def test_create_stores_the_reviews_and_due_date_but_never_a_result(self, client, db_session):
        user = _user(db_session)
        resp = client.post(
            f"{BASE}/car",
            headers=_headers(user),
            json={
                "title": "Burrs on flange edge at receiving",
                "problem_description": "Customer found burrs on 5 of 20 brackets at receiving inspection.",
                "human_factors_review": HUMAN_FACTORS,
                "similar_nonconformities_review": SIMILAR,
                "effectiveness_due": "2027-01-15",
                "effectiveness_result": "effective",
                "effectiveness_verified_by": user.id,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["human_factors_review"] == HUMAN_FACTORS
        assert body["similar_nonconformities_review"] == SIMILAR
        assert body["effectiveness_due"] == "2027-01-15"
        assert body["effectiveness_result"] is None and body["effectiveness_verified_by"] is None
        new = _committed(body["id"], "CREATE")[0]["new_values"]
        assert new["human_factors_review"] == HUMAN_FACTORS and new["effectiveness_due"] == "2027-01-15"
        assert new["effectiveness_result"] is None

    def test_reviews_and_due_date_are_edited_with_an_audited_put(self, client, db_session):
        car = _car(db_session, status=CARStatus.CORRECTIVE_ACTION)
        resp = client.put(
            f"{BASE}/car/{car.id}",
            headers=_headers(_user(db_session)),
            json={
                "version": 0,
                "human_factors_review": HUMAN_FACTORS,
                "similar_nonconformities_review": SIMILAR,
                "effectiveness_due": "2027-01-15",
            },
        )
        assert resp.status_code == 200, resp.text
        row = _committed(car.id)[0]
        assert row["old_values"] == {
            "human_factors_review": None,
            "similar_nonconformities_review": None,
            "effectiveness_due": None,
        }
        assert row["new_values"] == {
            "human_factors_review": HUMAN_FACTORS,
            "similar_nonconformities_review": SIMILAR,
            "effectiveness_due": "2027-01-15",
        }


# ========================================================================= closure gate


def _close(client, car_id, headers, **body):
    return client.put(f"{BASE}/car/{car_id}", headers=headers, json={"version": 0, "status": "closed", **body})


class TestClosureGate:
    def test_a_corrective_car_with_only_the_original_four_is_refused(self, client, db_session):
        car = _in_verification(db_session, car_type=CARType.CORRECTIVE)
        before = _snapshot(db_session)
        resp = _close(client, car.id, _headers(_user(db_session)))
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == f"Closure requires {MISSING_HUMAN}, {MISSING_SIMILAR}, {MISSING_EFFECTIVE}"
        assert _snapshot(db_session) == before

    def test_every_missing_item_is_named_in_order(self, client, db_session):
        car = _car(db_session, status=CARStatus.VERIFICATION)
        resp = _close(client, car.id, _headers(_user(db_session)))
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == (
            f"Closure requires {MISSING_BASE}, {MISSING_HUMAN}, {MISSING_SIMILAR}, {MISSING_EFFECTIVE}"
        )

    def test_stored_reviews_and_an_effective_result_close_it(self, client, db_session):
        user = _user(db_session)
        car = _verified(
            db_session,
            user,
            status=CARStatus.VERIFICATION,
            human_factors_review=HUMAN_FACTORS,
            similar_nonconformities_review=SIMILAR,
            **BASE_FOUR,
        )
        resp = _close(client, car.id, _headers(user))
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "closed"
        assert _committed(car.id)[0]["extra_data"]["closure"] is True

    def test_reviews_supplied_in_the_closing_request_count(self, client, db_session):
        user = _user(db_session)
        car = _verified(db_session, user, status=CARStatus.VERIFICATION, **BASE_FOUR)
        resp = _close(
            client,
            car.id,
            _headers(user),
            human_factors_review=HUMAN_FACTORS,
            similar_nonconformities_review=SIMILAR,
        )
        assert resp.status_code == 200, resp.text
        row = _committed(car.id)[0]
        assert row["action"] == "STATUS_CHANGE"
        assert row["new_values"]["human_factors_review"] == HUMAN_FACTORS
        assert row["new_values"]["similar_nonconformities_review"] == SIMILAR

    @pytest.mark.parametrize(
        "field,missing", [("human_factors_review", MISSING_HUMAN), ("similar_nonconformities_review", MISSING_SIMILAR)]
    )
    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blanking_a_stored_review_in_the_closing_request_is_refused(
        self, client, db_session, field, missing, blank
    ):
        user = _user(db_session)
        car = _verified(
            db_session,
            user,
            status=CARStatus.VERIFICATION,
            human_factors_review=HUMAN_FACTORS,
            similar_nonconformities_review=SIMILAR,
            **BASE_FOUR,
        )
        before = _snapshot(db_session)
        resp = _close(client, car.id, _headers(user), **{field: blank})
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == f"Closure requires {missing}"
        assert _snapshot(db_session) == before

    def test_a_not_effective_result_does_not_close(self, client, db_session):
        user = _user(db_session)
        car = _verified(
            db_session,
            user,
            result="not_effective",
            status=CARStatus.VERIFICATION,
            human_factors_review=HUMAN_FACTORS,
            similar_nonconformities_review=SIMILAR,
            **BASE_FOUR,
        )
        before = _snapshot(db_session)
        resp = _close(client, car.id, _headers(user))
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == f"Closure requires {MISSING_EFFECTIVE}"
        assert _snapshot(db_session) == before

    def test_the_result_cannot_be_supplied_in_the_closing_request(self, client, db_session):
        user = _user(db_session)
        car = _in_verification(db_session, human_factors_review=HUMAN_FACTORS, similar_nonconformities_review=SIMILAR)
        resp = _close(client, car.id, _headers(user), effectiveness_result="effective")
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == f"Closure requires {MISSING_EFFECTIVE}"

    @pytest.mark.parametrize("car_type", [CARType.PREVENTIVE, CARType.IMPROVEMENT])
    def test_preventive_and_improvement_cars_keep_the_original_four(self, client, db_session, car_type):
        car = _in_verification(db_session, car_type=car_type)
        resp = _close(client, car.id, _headers(_user(db_session)))
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "closed"

    @pytest.mark.parametrize("car_type", [CARType.PREVENTIVE, CARType.IMPROVEMENT])
    def test_preventive_and_improvement_cars_still_need_the_original_four(self, client, db_session, car_type):
        car = _car(db_session, car_type=car_type, status=CARStatus.VERIFICATION)
        resp = _close(client, car.id, _headers(_user(db_session)))
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == f"Closure requires {MISSING_BASE}"

    def test_a_null_car_type_is_held_to_the_corrective_gate(self, client, db_session):
        car = _in_verification(db_session)
        car.car_type = None
        db_session.commit()
        assert _stored(db_session, car.id).car_type is None
        before = _snapshot(db_session)

        resp = _close(client, car.id, _headers(_user(db_session)))

        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == f"Closure requires {MISSING_HUMAN}, {MISSING_SIMILAR}, {MISSING_EFFECTIVE}"
        assert _snapshot(db_session) == before

    def test_the_service_gate_passes_a_null_type_car_with_all_its_evidence(self):
        # The NULL-type positive path, judged directly on the service gate (the route test below
        # pins what the route does with such a row afterwards).
        car = CorrectiveActionRequest(
            car_number="CAR-UNIT-1",
            car_type=None,
            human_factors_review=HUMAN_FACTORS,
            similar_nonconformities_review=SIMILAR,
            effectiveness_result="effective",
            **BASE_FOUR,
        )
        quality_record_service._check_car_closure(car, {"status": CARStatus.CLOSED})
        car.effectiveness_result = None
        with pytest.raises(HTTPException) as refused:
            quality_record_service._check_car_closure(car, {"status": CARStatus.CLOSED})
        assert refused.value.detail == f"Closure requires {MISSING_EFFECTIVE}"

    def test_a_null_type_car_with_all_its_evidence_closes_and_reads_back(self, client, db_session):
        user = _user(db_session)
        car = _in_verification(
            db_session,
            human_factors_review=HUMAN_FACTORS,
            similar_nonconformities_review=SIMILAR,
            effectiveness_result="effective",
        )
        car.car_type = None
        db_session.commit()

        assert client.get(f"{BASE}/car/{car.id}", headers=_headers(user)).status_code == 200
        resp = _close(client, car.id, _headers(user))
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "closed"


# ======================================================= rewrite clears the effectiveness result

REVIEWED_FIELDS = ("root_cause", "corrective_action")
RESULT_FIELDS = ("effectiveness_result", "effectiveness_verified_by", "effectiveness_verified_at")
REWRITTEN = {
    "root_cause": "Fixture clamp torque was never specified on the setup sheet.",
    "corrective_action": "Add a torque spec to the setup sheet and a torque check to first-piece.",
}


def _closeable_verified(db, verifier, result="effective", **extra):
    """A corrective CAR in Verification with every closure item, including a recorded result."""
    extra.setdefault("status", CARStatus.VERIFICATION)
    return _verified(
        db,
        verifier,
        result=result,
        human_factors_review=HUMAN_FACTORS,
        similar_nonconformities_review=SIMILAR,
        **BASE_FOUR,
        **extra,
    )


class TestRewriteClearsEffectiveness:
    """A verification vouches for the cause and action it reviewed, and no more.

    A PUT that CHANGES ``root_cause`` or ``corrective_action`` clears the recorded result, its
    verifier and its instant (old -> new on that request's audit row); the evidence text stays
    and is unlocked; a close in the same request is judged as already cleared.
    """

    @pytest.mark.parametrize("field", REVIEWED_FIELDS)
    def test_a_rewrite_in_the_closing_request_is_judged_as_cleared(self, client, db_session, field):
        verifier = _user(db_session)
        car = _closeable_verified(db_session, verifier)
        before = _snapshot(db_session)

        resp = _close(client, car.id, _headers(_user(db_session)), **{field: REWRITTEN[field]})

        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == f"Closure requires {MISSING_EFFECTIVE}"
        # Refused before any write: the recorded result is still there.
        assert _snapshot(db_session) == before
        assert _stored(db_session, car.id).effectiveness_result == "effective"

    @pytest.mark.parametrize("field", REVIEWED_FIELDS)
    def test_a_rewrite_then_a_close_is_refused(self, client, db_session, field):
        verifier = _user(db_session)
        editor = _user(db_session, first_name="Sage")
        car = _closeable_verified(db_session, verifier)

        rewrite = client.put(
            f"{BASE}/car/{car.id}", headers=_headers(editor), json={"version": 0, field: REWRITTEN[field]}
        )
        assert rewrite.status_code == 200, rewrite.text
        before = _snapshot(db_session)

        closing = _close(client, car.id, _headers(editor))

        assert closing.status_code == 422, closing.text
        assert closing.json()["detail"] == f"Closure requires {MISSING_EFFECTIVE}"
        assert _snapshot(db_session) == before

    @pytest.mark.parametrize("result", ["effective", "not_effective"])
    @pytest.mark.parametrize("field", REVIEWED_FIELDS)
    def test_the_audit_row_records_the_cleared_result_old_to_new(self, client, db_session, field, result):
        verifier = _user(db_session, first_name="Rowan")
        editor = _user(db_session, first_name="Sage")
        car = _closeable_verified(db_session, verifier, result=result)

        resp = client.put(
            f"{BASE}/car/{car.id}", headers=_headers(editor), json={"version": 0, field: REWRITTEN[field]}
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        for cleared in RESULT_FIELDS:
            assert body[cleared] is None, cleared
        assert body["effectiveness_verified_by_name"] is None
        # The evidence text stays for the next recording.
        assert body["effectiveness_check"] == EVIDENCE
        stored = _stored(db_session, car.id)
        assert stored.effectiveness_check == EVIDENCE and stored.effectiveness_result is None

        rows = _committed(car.id)
        assert [row["action"] for row in rows] == ["UPDATE"]
        row = rows[0]
        assert row["user_id"] == editor.id
        assert row["old_values"] == {
            field: BASE_FOUR[field],
            "effectiveness_result": result,
            "effectiveness_verified_by": verifier.id,
            "effectiveness_verified_at": STAMPED_AT.isoformat(),
        }
        assert row["new_values"] == {
            field: REWRITTEN[field],
            "effectiveness_result": None,
            "effectiveness_verified_by": None,
            "effectiveness_verified_at": None,
        }
        changes = row["extra_data"]["changes"]
        assert changes["effectiveness_result"] == {"old": result, "new": None}
        assert changes["effectiveness_verified_by"] == {"old": verifier.id, "new": None}

    @pytest.mark.parametrize(
        "field",
        [
            "preventive_action",
            "root_cause_analysis",
            "containment_action",
            "verification_method",
            "verification_results",
            "human_factors_review",
            "similar_nonconformities_review",
        ],
    )
    def test_a_rewrite_outside_the_two_keeps_the_result(self, client, db_session, field):
        verifier = _user(db_session)
        car = _closeable_verified(db_session, verifier)
        value = "Rewritten text that the effectiveness review did not depend on."

        resp = client.put(
            f"{BASE}/car/{car.id}", headers=_headers(_user(db_session)), json={"version": 0, field: value}
        )

        assert resp.status_code == 200, resp.text
        stored = _stored(db_session, car.id)
        assert (stored.effectiveness_result, stored.effectiveness_verified_by, stored.effectiveness_verified_at) == (
            "effective",
            verifier.id,
            STAMPED_AT,
        )
        assert _committed(car.id)[0]["new_values"] == {field: value}

    @pytest.mark.parametrize("field", REVIEWED_FIELDS)
    def test_the_evidence_may_change_in_the_same_request_as_the_rewrite(self, client, db_session, field):
        car = _closeable_verified(db_session, _user(db_session))
        headers = _headers(_user(db_session))
        new_evidence = "Re-review pending against the rewritten corrective action."

        resp = client.put(
            f"{BASE}/car/{car.id}",
            headers=headers,
            json={"version": 0, field: REWRITTEN[field], "effectiveness_check": new_evidence},
        )

        assert resp.status_code == 200, resp.text
        stored = _stored(db_session, car.id)
        assert stored.effectiveness_check == new_evidence and stored.effectiveness_result is None
        row = _committed(car.id)[0]
        assert row["old_values"]["effectiveness_check"] == EVIDENCE
        assert row["new_values"]["effectiveness_check"] == new_evidence
        # ... and with no result left to protect, the evidence stays editable on its own.
        later = client.put(
            f"{BASE}/car/{car.id}", headers=headers, json={"version": 0, "effectiveness_check": "Second draft."}
        )
        assert later.status_code == 200, later.text

    def test_an_evidence_change_without_a_rewrite_is_still_409(self, client, db_session):
        car = _closeable_verified(db_session, _user(db_session))
        before = _snapshot(db_session)
        resp = client.put(
            f"{BASE}/car/{car.id}",
            headers=_headers(_user(db_session)),
            json={"version": 0, "preventive_action": "Add to PM.", "effectiveness_check": "Rewritten evidence."},
        )
        assert resp.status_code == 409, resp.text
        assert _snapshot(db_session) == before

    def test_an_unchanged_resend_of_the_cause_and_action_keeps_the_result(self, client, db_session):
        verifier = _user(db_session)
        car = _closeable_verified(db_session, verifier)
        resp = client.put(
            f"{BASE}/car/{car.id}",
            headers=_headers(_user(db_session)),
            json={
                "version": 0,
                "root_cause": BASE_FOUR["root_cause"],
                "corrective_action": BASE_FOUR["corrective_action"],
                "effectiveness_check": EVIDENCE,
                "rma_number": "RMA-9",
            },
        )
        assert resp.status_code == 200, resp.text
        assert _stored(db_session, car.id).effectiveness_result == "effective"
        assert _committed(car.id)[0]["new_values"] == {"rma_number": "RMA-9"}

    def test_with_no_recorded_result_a_rewrite_records_no_effectiveness_fields(self, client, db_session):
        car = _in_verification(db_session, effectiveness_check="Draft evidence.")
        resp = client.put(
            f"{BASE}/car/{car.id}",
            headers=_headers(_user(db_session)),
            json={"version": 0, "corrective_action": REWRITTEN["corrective_action"]},
        )
        assert resp.status_code == 200, resp.text
        assert _committed(car.id)[0]["new_values"] == {"corrective_action": REWRITTEN["corrective_action"]}

    @pytest.mark.parametrize("reopen_to", ["verification", "corrective_action", "open"])
    def test_a_status_only_reopen_keeps_the_result(self, client, db_session, reopen_to):
        verifier = _user(db_session)
        closer = _user(db_session)
        car = _closeable_verified(
            db_session, verifier, status=CARStatus.CLOSED, closed_date=date(2026, 11, 1), closed_by=closer.id
        )

        resp = client.put(
            f"{BASE}/car/{car.id}", headers=_headers(_user(db_session)), json={"version": 0, "status": reopen_to}
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == reopen_to
        stored = _stored(db_session, car.id)
        assert (stored.effectiveness_result, stored.effectiveness_verified_by, stored.effectiveness_verified_at) == (
            "effective",
            verifier.id,
            STAMPED_AT,
        )
        row = _committed(car.id)[0]
        assert row["extra_data"]["reopen"] is True
        assert not any(key.startswith("effectiveness") for key in row["new_values"])

    def test_a_closed_car_still_refuses_the_rewrite_itself(self, client, db_session):
        car = _closeable_verified(db_session, _user(db_session), status=CARStatus.CLOSED, closed_date=date(2026, 11, 1))
        before = _snapshot(db_session)
        resp = client.put(
            f"{BASE}/car/{car.id}",
            headers=_headers(_user(db_session)),
            json={"version": 0, "corrective_action": REWRITTEN["corrective_action"]},
        )
        assert resp.status_code == 409, resp.text
        assert _snapshot(db_session) == before

    def test_lost_audit_row_keeps_the_recorded_result(self, client, db_session, monkeypatch):
        car = _closeable_verified(db_session, _user(db_session))
        headers = _headers(_user(db_session))
        before = _snapshot(db_session)
        monkeypatch.setattr(AuditService, "log", lambda *args, **kwargs: None)

        resp = client.put(
            f"{BASE}/car/{car.id}",
            headers=headers,
            json={"version": 0, "corrective_action": REWRITTEN["corrective_action"]},
        )

        assert resp.status_code == 503, resp.text
        assert resp.json() == {"detail": "Unable to save audit record"}
        assert _snapshot(db_session) == before
        assert _stored(db_session, car.id).effectiveness_result == "effective"


# ============================================================================ response names


class TestResponseNames:
    def test_the_verifier_is_named_on_the_verb_detail_and_list(self, client, db_session):
        verifier = _user(db_session, first_name="Rowan")
        car = _in_verification(db_session)
        resp = _record(client, car.id, _headers(verifier))
        assert resp.status_code == 200, resp.text
        assert resp.json()["effectiveness_verified_by_name"] == verifier.full_name

        viewer = _headers(_user(db_session, role=UserRole.VIEWER))
        detail = client.get(f"{BASE}/car/{car.id}", headers=viewer).json()
        assert detail["effectiveness_verified_by_name"] == verifier.full_name
        listed = {row["id"]: row for row in client.get(f"{BASE}/car", headers=viewer).json()}
        assert listed[car.id]["effectiveness_verified_by_name"] == verifier.full_name

    def test_a_platform_admin_acting_in_this_company_is_named(self, client, db_session):
        platform = _user(db_session, role=UserRole.PLATFORM_ADMIN, company_id=2, first_name="Pat")
        car = _in_verification(db_session)
        resp = _record(client, car.id, _headers(platform, company_id=1))
        assert resp.status_code == 200, resp.text
        body = client.get(f"{BASE}/car/{car.id}", headers=_headers(_user(db_session, role=UserRole.VIEWER))).json()
        assert body["effectiveness_verified_by_name"] == platform.full_name

    def test_another_companys_ordinary_user_is_never_named_on_the_car_or_its_print(self, client, db_session):
        foreign = _user(db_session, company_id=2, first_name="Foreignperson")
        car = _verified(db_session, foreign, status=CARStatus.VERIFICATION)
        viewer = _headers(_user(db_session, role=UserRole.VIEWER))

        body = client.get(f"{BASE}/car/{car.id}", headers=viewer)
        pdf = client.get(f"{BASE}/car/{car.id}/pdf", headers=viewer)

        assert body.status_code == 200 and pdf.status_code == 200
        assert body.json()["effectiveness_verified_by_name"] is None
        assert "Foreignperson" not in body.text
        assert "Foreignperson" not in _pdf_text(pdf.content)


# ============================================================================== printable CAR


class TestPrintableCar:
    def test_the_reviews_and_effectiveness_block_print_with_a_central_date(self, client, db_session):
        verifier = _user(db_session, first_name="Rowan")
        # 03:30 UTC on Nov 14 is still Nov 13 in Central (CST, UTC-6).
        car = _car(
            db_session,
            status=CARStatus.VERIFICATION,
            human_factors_review=HUMAN_FACTORS,
            similar_nonconformities_review=SIMILAR,
            effectiveness_result="effective",
            effectiveness_check=EVIDENCE,
            effectiveness_verified_by=verifier.id,
            effectiveness_verified_at=datetime(2026, 11, 14, 3, 30, 0),
        )

        resp = client.get(f"{BASE}/car/{car.id}/pdf", headers=_headers(_user(db_session, role=UserRole.VIEWER)))

        assert resp.status_code == 200, resp.text
        text = _pdf_text(resp.content)
        for label in (
            "Human Factors Review",
            "Similar Nonconformities Review",
            "Effectiveness Verification - Result",
            "Verified by",
            "Evidence Reviewed for Effectiveness",
        ):
            assert label in text, label
        assert HUMAN_FACTORS in text and SIMILAR in text and EVIDENCE in text
        assert "Effective" in text and verifier.full_name in text
        assert "11/13/2026" in text and "11/14/2026" not in text

    def test_a_not_effective_result_prints_its_label(self, client, db_session):
        car = _verified(db_session, _user(db_session), result="not_effective", status=CARStatus.CORRECTIVE_ACTION)
        text = _pdf_text(client.get(f"{BASE}/car/{car.id}/pdf", headers=_headers(_user(db_session))).content)
        assert "Not effective" in text

    def test_an_unrecorded_car_still_prints_the_empty_boxes(self, client, db_session):
        car = _car(db_session)
        text = _pdf_text(client.get(f"{BASE}/car/{car.id}/pdf", headers=_headers(_user(db_session))).content)
        for label in (
            "Human Factors Review",
            "Similar Nonconformities Review",
            "Effectiveness Verification - Result",
            "Evidence Reviewed for Effectiveness",
        ):
            assert label in text, label
        assert "Not effective" not in text

    @pytest.mark.parametrize(
        "value", ["Acme <b>Corp</b>", "Check OD<ID before press", "R&D Group", "Dim is 2.500 <REF> per print"]
    )
    def test_hostile_values_print_literally(self, client, db_session, value):
        car = _car(
            db_session,
            status=CARStatus.VERIFICATION,
            human_factors_review=f"Human {value}",
            similar_nonconformities_review=f"Similar {value}",
            effectiveness_result="effective",
            effectiveness_check=f"Evidence {value}",
            effectiveness_verified_by=_user(db_session).id,
            effectiveness_verified_at=STAMPED_AT,
        )
        resp = client.get(f"{BASE}/car/{car.id}/pdf", headers=_headers(_user(db_session)))
        assert resp.status_code == 200, resp.text
        text = _pdf_text(resp.content)
        assert f"Human {value}" in text
        assert f"Similar {value}" in text
        assert f"Evidence {value}" in text

    def test_the_print_context_resolves_the_effectiveness_values(self, db_session):
        verifier = _user(db_session, first_name="Rowan")
        car = _verified(db_session, verifier, result="not_effective", status=CARStatus.CORRECTIVE_ACTION)
        context = quality_record_service.car_print_context(db_session, 1, car)
        assert context["effectiveness_result"] == "Not effective"
        assert context["effectiveness_evidence"] == EVIDENCE
        assert context["effectiveness_verified_by"] == verifier.full_name
        assert context["effectiveness_verified_date"] == "10/28/2026"

    def test_another_companys_car_pdf_is_404(self, client, db_session):
        car = _verified(db_session, _user(db_session, company_id=2), company_id=2)
        resp = client.get(f"{BASE}/car/{car.id}/pdf", headers=_headers(_user(db_session)))
        assert resp.status_code == 404, resp.text


def test_a_record_with_a_date_only_field_reads_back_unchanged(client, db_session):
    # effectiveness_due is a date-only field: served as YYYY-MM-DD, never shifted to UTC.
    car = _car(db_session, effectiveness_due=date(2026, 12, 31))
    body = client.get(f"{BASE}/car/{car.id}", headers=_headers(_user(db_session))).json()
    assert body["effectiveness_due"] == "2026-12-31"
