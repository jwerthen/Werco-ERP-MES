"""Maintenance log entries (owner decisions 2026-11-20).

``POST /maintenance/log``:

* ``event_type`` is a fixed vocabulary -- inspection, repair, part_replacement, observation, note.
  ``started`` and ``completed`` are written only by the work order's Start and Complete verbs, so a
  caller sending them (or anything else) gets **422** and nothing is written;
* an entry filed with ``maintenance_wo_id`` takes the work order's target (both target fields
  omitted, or equal to the work order's); a different explicit target is **409**; the inherited target
  is re-resolved inside the tenant, so a legacy work order on another company's machine is a flat
  **404**; a completed or cancelled work order still takes entries (a later note belongs here);
* ``event_date`` defaults to now, may be at most 30 days back and not in the future (5 minutes of
  clock skew allowed); an aware datetime is stored as naive UTC;
* ``description`` is required and trimmed (4000 characters at most); ``cost`` is 0 or more, finite;
* a standing API token may file entries -- its audit row carries ``extra_data.credential`` -- while a
  read-only token meets its own gate (403), and Quality / Shipping / Viewer are refused (403).

``GET /maintenance/work-orders/{wo_id}/logs`` lists a work order's entries (the verbs' and the filed
ones), newest first by ``event_date`` then id, tenant-scoped (404), ``limit`` 1..500; it is not on the
AI-external allowlist. Every log read -- that one, ``/history/{work_center_id}``,
``/assets/{asset_id}/history`` and the POST's own response -- carries ``performed_by_name`` from one
tenant-scoped user lookup (another company's user reads as null), and a legacy row with free-text
``event_type`` still reads.

Every fixture is synthetic; assertions read the STORED rows.
"""

from datetime import datetime as real_datetime
from datetime import timedelta, timezone

import pytest
from fastapi import status
from sqlalchemy.orm import Session

import app.api.endpoints.maintenance as maintenance_endpoints
from app.core.credentials import READ_ONLY_ROUTE_WRITES_DETAIL
from app.core.export_control import AI_EXTERNAL_ROUTE_DENIED_DETAIL
from app.models.audit_log import AuditLog
from app.models.maintenance import MaintenanceLog, MaintenanceLogEntryType, MaintenanceStatus, MaintenanceWorkOrder
from app.models.user import UserRole
from tests.api.export_control_helpers import bearer, issue_token
from tests.api.test_maintenance_assets import make_asset, make_wc
from tests.api.test_maintenance_company_day import FrozenDatetime, _FrozenClock
from tests.api.test_receiving_compliance import _next, headers_for, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

LOG = "/api/v1/maintenance/log"
WORK_ORDERS = "/api/v1/maintenance/work-orders"
ALLOWED = [member.value for member in MaintenanceLogEntryType]

# The instant every log-date check reads (naive UTC, as ``datetime.utcnow()`` returns it).
NOW = real_datetime(2026, 11, 20, 15, 0, 0)


@pytest.fixture
def frozen_now(monkeypatch):
    """Pin the endpoint's ``datetime.utcnow()`` (the log-date window and the verbs' entries) to ``NOW``."""
    monkeypatch.setattr(_FrozenClock, "instant", NOW.replace(tzinfo=timezone.utc))
    monkeypatch.setattr(maintenance_endpoints, "datetime", FrozenDatetime)
    return NOW


@pytest.fixture
def supervisor(db_session):
    return make_user(db_session, role=UserRole.SUPERVISOR, company_id=1)


@pytest.fixture
def headers(supervisor):
    return headers_for(supervisor)


# --------------------------------------------------------------------------- helpers


def wc_wo(client, headers, db: Session, **body):
    wc = make_wc(db)
    created = client.post(WORK_ORDERS, headers=headers, json={"work_center_id": wc.id, "title": "Corrective", **body})
    assert created.status_code == 200, created.text
    return created.json()


def asset_wo(client, headers, db: Session):
    asset = make_asset(db)
    created = client.post(WORK_ORDERS, headers=headers, json={"asset_id": asset.id, "title": "Forklift service"})
    assert created.status_code == 200, created.text
    return created.json()


def file_entry(client, headers, **body):
    payload = {"event_type": "note", "description": "Synthetic note", **body}
    return client.post(LOG, headers=headers, json=payload)


def log_rows(db: Session):
    db.expire_all()
    return db.query(MaintenanceLog).order_by(MaintenanceLog.id).all()


def log_audit_rows(db: Session):
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == "maintenance_log", AuditLog.action == "CREATE")
        .order_by(AuditLog.id)
        .all()
    )


def written(db: Session):
    """What a refused request must leave untouched: the log rows and every audit row."""
    db.expire_all()
    return (db.query(MaintenanceLog).count(), db.query(AuditLog).count())


def full_name(user) -> str:
    return f"{user.first_name} {user.last_name}"


def seed_log(db: Session, *, company_id=1, wo=None, work_center_id=None, asset_id=None, **fields):
    """A log row written straight to the table -- a legacy row, or one a test needs exactly."""
    log = MaintenanceLog(
        company_id=company_id,
        work_center_id=work_center_id if work_center_id is not None else (wo or {}).get("work_center_id"),
        asset_id=asset_id if asset_id is not None else (wo or {}).get("asset_id"),
        maintenance_wo_id=(wo or {}).get("id"),
        event_type=fields.pop("event_type", "note"),
        description=fields.pop("description", "Seeded entry"),
        event_date=fields.pop("event_date", NOW - timedelta(days=1)),
        **fields,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def wo_logs(client, headers, wo_id: int, **params):
    return client.get(f"{WORK_ORDERS}/{wo_id}/logs", headers=headers, params=params or None)


# --------------------------------------------------------------------------- event_type


class TestTheEventTypeIsAFixedVocabulary:
    @pytest.mark.parametrize("event_type", ["completed", "started", "calibration", "Inspection", "", " note "])
    def test_anything_outside_the_vocabulary_is_422_and_writes_nothing(self, client, db_session, headers, event_type):
        wo = wc_wo(client, headers, db_session)
        before = written(db_session)
        refused = file_entry(client, headers, maintenance_wo_id=wo["id"], event_type=event_type)
        assert refused.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, refused.text
        assert written(db_session) == before

    @pytest.mark.parametrize("event_type", ALLOWED)
    def test_each_allowed_type_is_filed_and_audited(self, client, db_session, headers, supervisor, event_type):
        wo = wc_wo(client, headers, db_session)
        filed = file_entry(client, headers, maintenance_wo_id=wo["id"], event_type=event_type)
        assert filed.status_code == 200, filed.text
        assert filed.json()["event_type"] == event_type
        assert filed.json()["performed_by"] == supervisor.id
        assert filed.json()["performed_by_name"] == full_name(supervisor)
        [row] = [log for log in log_rows(db_session) if log.id == filed.json()["id"]]
        assert row.event_type == event_type
        [audit] = log_audit_rows(db_session)
        assert audit.resource_id == row.id and audit.new_values["event_type"] == event_type
        assert "credential" not in (audit.extra_data or {})

    def test_the_vocabulary_is_exactly_the_owners_five(self):
        assert ALLOWED == ["inspection", "repair", "part_replacement", "observation", "note"]
        assert not {"started", "completed"} & set(ALLOWED)


# --------------------------------------------------------------------------- the work order's target


class TestAnEntryTakesItsWorkOrdersTarget:
    def test_a_work_center_work_orders_target_is_inherited(self, client, db_session, headers):
        wo = wc_wo(client, headers, db_session)
        filed = file_entry(client, headers, maintenance_wo_id=wo["id"])
        assert filed.status_code == 200, filed.text
        body = filed.json()
        assert (body["work_center_id"], body["asset_id"], body["maintenance_wo_id"]) == (
            wo["work_center_id"],
            None,
            wo["id"],
        )
        [row] = log_rows(db_session)
        assert (row.work_center_id, row.asset_id) == (wo["work_center_id"], None)

    def test_an_asset_work_orders_target_is_inherited(self, client, db_session, headers):
        wo = asset_wo(client, headers, db_session)
        filed = file_entry(client, headers, maintenance_wo_id=wo["id"], event_type="inspection")
        assert filed.status_code == 200, filed.text
        assert (filed.json()["work_center_id"], filed.json()["asset_id"]) == (None, wo["asset_id"])
        [row] = log_rows(db_session)
        assert (row.work_center_id, row.asset_id) == (None, wo["asset_id"])

    def test_the_work_orders_own_target_given_explicitly_is_fine(self, client, db_session, headers):
        on_wc = wc_wo(client, headers, db_session)
        on_asset = asset_wo(client, headers, db_session)
        assert (
            file_entry(
                client, headers, maintenance_wo_id=on_wc["id"], work_center_id=on_wc["work_center_id"]
            ).status_code
            == 200
        )
        assert (
            file_entry(client, headers, maintenance_wo_id=on_asset["id"], asset_id=on_asset["asset_id"]).status_code
            == 200
        )

    @pytest.mark.parametrize("mismatch", ["other-work-center", "other-asset", "work-center-on-asset-wo"])
    def test_a_different_explicit_target_is_409_and_writes_nothing(self, client, db_session, headers, mismatch):
        if mismatch == "other-work-center":
            wo = wc_wo(client, headers, db_session)
            target = {"work_center_id": make_wc(db_session).id}
        elif mismatch == "other-asset":
            wo = asset_wo(client, headers, db_session)
            target = {"asset_id": make_asset(db_session).id}
        else:
            wo = asset_wo(client, headers, db_session)
            target = {"work_center_id": make_wc(db_session).id}
        before = written(db_session)

        refused = file_entry(client, headers, maintenance_wo_id=wo["id"], **target)

        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        assert wo["wo_number"] in refused.json()["detail"]
        assert written(db_session) == before

    def test_both_targets_is_422(self, client, db_session, headers):
        wo = wc_wo(client, headers, db_session)
        asset = make_asset(db_session)
        before = written(db_session)
        for body in (
            {"work_center_id": wo["work_center_id"], "asset_id": asset.id},
            {"work_center_id": wo["work_center_id"], "asset_id": asset.id, "maintenance_wo_id": wo["id"]},
        ):
            refused = file_entry(client, headers, **body)
            assert refused.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, refused.text
        assert written(db_session) == before

    def test_no_work_order_and_no_target_is_422(self, client, db_session, headers):
        before = written(db_session)
        refused = file_entry(client, headers)
        assert refused.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, refused.text
        assert written(db_session) == before

    def test_a_target_without_a_work_order_still_files(self, client, db_session, headers):
        wc = make_wc(db_session)
        filed = file_entry(client, headers, work_center_id=wc.id, event_type="observation")
        assert filed.status_code == 200, filed.text
        assert (filed.json()["work_center_id"], filed.json()["maintenance_wo_id"]) == (wc.id, None)


class TestTheWorkOrderAndItsTargetStayInTenant:
    def test_another_companys_work_order_is_404_and_writes_nothing(self, client, db_session, headers):
        headers_b = headers_for(make_user(db_session, role=UserRole.SUPERVISOR, company_id=2))
        foreign_wc = make_wc(db_session, company_id=2)
        foreign = client.post(WORK_ORDERS, headers=headers_b, json={"work_center_id": foreign_wc.id, "title": "B"})
        assert foreign.status_code == 200, foreign.text
        before = written(db_session)
        for body in ({}, {"work_center_id": foreign_wc.id}, {"work_center_id": make_wc(db_session).id}):
            refused = file_entry(client, headers, maintenance_wo_id=foreign.json()["id"], **body)
            assert refused.status_code == status.HTTP_404_NOT_FOUND, (body, refused.text)
            assert foreign.json()["wo_number"] not in refused.text
        assert written(db_session) == before

    def test_a_legacy_work_order_on_another_companys_machine_is_404(self, client, db_session, headers):
        """``company_id`` is part of no FK, so a pre-fix company-1 work order may point at company 2's
        machine. The inherited target is re-resolved inside the tenant: never file history against it."""
        foreign_wc = make_wc(db_session, company_id=2)
        legacy = MaintenanceWorkOrder(
            company_id=1,
            work_center_id=foreign_wc.id,
            wo_number=f"MWO-LOG-{_next():05d}",
            title="Legacy corrective",
            status=MaintenanceStatus.IN_PROGRESS,
        )
        db_session.add(legacy)
        db_session.commit()
        before = written(db_session)

        refused = file_entry(client, headers, maintenance_wo_id=legacy.id)

        assert refused.status_code == status.HTTP_404_NOT_FOUND, refused.text
        assert foreign_wc.name not in refused.text and foreign_wc.code not in refused.text
        assert written(db_session) == before

    @pytest.mark.parametrize("end", ["completed", "cancelled"])
    def test_a_finished_work_order_still_takes_entries(self, client, db_session, headers, end):
        wo = wc_wo(client, headers, db_session)
        if end == "completed":
            ended = client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=headers, json={})
        else:
            ended = client.put(f"{WORK_ORDERS}/{wo['id']}", headers=headers, json={"status": "cancelled"})
        assert ended.status_code == 200, ended.text
        filed = file_entry(client, headers, maintenance_wo_id=wo["id"], description="Wiper weeping; recheck at next PM")
        assert filed.status_code == 200, filed.text
        assert filed.json()["work_center_id"] == wo["work_center_id"]


# --------------------------------------------------------------------------- event_date


class TestTheEventDateWindow:
    @pytest.mark.parametrize(
        "offset",
        [timedelta(days=-30, seconds=-1), timedelta(days=-31), timedelta(minutes=5, seconds=1), timedelta(days=1)],
        ids=["30d-and-1s-back", "31d-back", "5min-1s-ahead", "tomorrow"],
    )
    def test_outside_the_window_is_422_and_writes_nothing(self, client, db_session, headers, frozen_now, offset):
        wo = wc_wo(client, headers, db_session)
        before = written(db_session)
        refused = file_entry(client, headers, maintenance_wo_id=wo["id"], event_date=(NOW + offset).isoformat())
        assert refused.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, refused.text
        assert written(db_session) == before

    @pytest.mark.parametrize(
        "offset",
        [timedelta(days=-30), timedelta(days=-29), timedelta(hours=-1), timedelta(0), timedelta(minutes=5)],
        ids=["exactly-30d-back", "29d-back", "1h-back", "now", "5min-skew"],
    )
    def test_inside_the_window_is_stored_as_given(self, client, db_session, headers, frozen_now, offset):
        wo = wc_wo(client, headers, db_session)
        given = NOW + offset
        filed = file_entry(client, headers, maintenance_wo_id=wo["id"], event_date=given.isoformat())
        assert filed.status_code == 200, filed.text
        [row] = log_rows(db_session)
        assert row.event_date == given
        assert filed.json()["event_date"] == given.isoformat() + "Z"

    def test_an_aware_datetime_is_stored_as_naive_utc(self, client, db_session, headers, frozen_now):
        wo = wc_wo(client, headers, db_session)
        filed = file_entry(client, headers, maintenance_wo_id=wo["id"], event_date="2026-11-20T14:30:00+02:00")
        assert filed.status_code == 200, filed.text
        [row] = log_rows(db_session)
        assert row.event_date == real_datetime(2026, 11, 20, 12, 30) and row.event_date.tzinfo is None
        assert filed.json()["event_date"] == "2026-11-20T12:30:00Z"

    def test_an_aware_datetime_is_judged_in_utc(self, client, db_session, headers, frozen_now):
        """16:00+02:00 is 14:00 UTC -- an hour BEFORE now, so it is accepted; read as a naive 16:00 it
        would be an hour in the future."""
        wo = wc_wo(client, headers, db_session)
        assert (
            file_entry(client, headers, maintenance_wo_id=wo["id"], event_date="2026-11-20T16:00:00+02:00").status_code
            == 200
        )
        # ...and 18:00-05:00 is 23:00 UTC, eight hours ahead: refused.
        refused = file_entry(client, headers, maintenance_wo_id=wo["id"], event_date="2026-11-20T18:00:00-05:00")
        assert refused.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, refused.text

    def test_omitted_is_now(self, client, db_session, headers, frozen_now):
        wo = wc_wo(client, headers, db_session)
        assert file_entry(client, headers, maintenance_wo_id=wo["id"]).status_code == 200
        [row] = log_rows(db_session)
        assert row.event_date == NOW


# --------------------------------------------------------------------------- description and cost


class TestDescriptionAndCost:
    @pytest.mark.parametrize("description", ["", "   ", "\n\t "])
    def test_a_blank_description_is_422(self, client, db_session, headers, description):
        wo = wc_wo(client, headers, db_session)
        before = written(db_session)
        refused = file_entry(client, headers, maintenance_wo_id=wo["id"], description=description)
        assert refused.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, refused.text
        assert written(db_session) == before

    def test_the_description_is_trimmed_on_store(self, client, db_session, headers):
        wo = wc_wo(client, headers, db_session)
        filed = file_entry(client, headers, maintenance_wo_id=wo["id"], description="  Belt tension checked \n")
        assert filed.status_code == 200, filed.text
        assert filed.json()["description"] == "Belt tension checked"
        assert log_rows(db_session)[0].description == "Belt tension checked"

    def test_4000_characters_is_the_limit(self, client, db_session, headers):
        wo = wc_wo(client, headers, db_session)
        assert file_entry(client, headers, maintenance_wo_id=wo["id"], description="x" * 4000).status_code == 200
        before = written(db_session)
        refused = file_entry(client, headers, maintenance_wo_id=wo["id"], description="x" * 4001)
        assert refused.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, refused.text
        assert written(db_session) == before

    @pytest.mark.parametrize("cost", ["-0.01", "-5", "NaN", "Infinity", "-Infinity", '"nan"', '"inf"', "10000000.01"])
    def test_a_negative_non_finite_or_huge_cost_is_422(self, client, db_session, headers, cost):
        wo = wc_wo(client, headers, db_session)
        before = written(db_session)
        raw = (
            f'{{"maintenance_wo_id": {wo["id"]}, "event_type": "repair", "description": "Replaced fuse", '
            f'"cost": {cost}}}'
        )
        refused = client.post(LOG, headers={**headers, "Content-Type": "application/json"}, content=raw)
        assert refused.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, refused.text
        assert written(db_session) == before

    def test_cost_defaults_to_zero_and_a_positive_one_is_kept(self, client, db_session, headers):
        wo = wc_wo(client, headers, db_session)
        assert file_entry(client, headers, maintenance_wo_id=wo["id"]).json()["cost"] == 0
        assert (
            file_entry(client, headers, maintenance_wo_id=wo["id"], event_type="repair", cost=42.5).json()["cost"]
            == 42.5
        )


# --------------------------------------------------------------------------- who may file


class TestWhoMayFile:
    def test_a_full_api_token_may_file_and_is_attributed(self, client, db_session, headers, admin_headers, supervisor):
        wo = wc_wo(client, headers, db_session)
        token_id, token = issue_token(client, admin_headers, supervisor)
        filed = file_entry(client, bearer(token), maintenance_wo_id=wo["id"], event_type="observation")
        assert filed.status_code == 200, filed.text
        assert filed.json()["performed_by"] == supervisor.id
        [audit] = log_audit_rows(db_session)
        assert audit.extra_data and audit.extra_data.get("credential")
        assert str(token_id) in str(audit.extra_data["credential"])

    def test_a_read_only_token_meets_its_own_gate(self, client, db_session, headers, admin_headers, supervisor):
        wo = wc_wo(client, headers, db_session)
        _id, token = issue_token(client, admin_headers, supervisor, scope="read_only", ai_external=False)
        before = written(db_session)
        refused = file_entry(client, bearer(token), maintenance_wo_id=wo["id"])
        assert refused.status_code == status.HTTP_403_FORBIDDEN, refused.text
        assert refused.json()["detail"] == READ_ONLY_ROUTE_WRITES_DETAIL
        assert written(db_session) == before

    @pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.SHIPPING, UserRole.QUALITY])
    def test_roles_that_do_not_perform_maintenance_are_403(self, client, db_session, headers, role):
        wo = wc_wo(client, headers, db_session)
        before = written(db_session)
        refused = file_entry(
            client, headers_for(make_user(db_session, role=role, company_id=1)), maintenance_wo_id=wo["id"]
        )
        assert refused.status_code == status.HTTP_403_FORBIDDEN, refused.text
        assert written(db_session) == before

    def test_an_operator_may_file(self, client, db_session, headers):
        wo = wc_wo(client, headers, db_session)
        operator = make_user(db_session, role=UserRole.OPERATOR, company_id=1)
        filed = file_entry(client, headers_for(operator), maintenance_wo_id=wo["id"], event_type="part_replacement")
        assert filed.status_code == 200, filed.text
        assert filed.json()["performed_by_name"] == full_name(operator)


# --------------------------------------------------------------------------- GET /work-orders/{id}/logs


class TestAWorkOrdersLog:
    def test_the_verbs_entries_and_filed_ones_newest_first_ties_by_id(
        self, client, db_session, headers, supervisor, frozen_now
    ):
        wo = wc_wo(client, headers, db_session)
        assert client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=headers).status_code == 200
        two_days = file_entry(
            client, headers, maintenance_wo_id=wo["id"], event_date=(NOW - timedelta(days=2)).isoformat()
        )
        one_day = file_entry(
            client,
            headers,
            maintenance_wo_id=wo["id"],
            event_type="repair",
            event_date=(NOW - timedelta(days=1)).isoformat(),
        )
        at_now = file_entry(client, headers, maintenance_wo_id=wo["id"], event_type="observation")
        assert client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=headers, json={}).status_code == 200
        other = wc_wo(client, headers, db_session)
        file_entry(client, headers, maintenance_wo_id=other["id"])  # another work order's entry

        listed = wo_logs(client, headers, wo["id"])

        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert (body["maintenance_wo_id"], body["wo_number"]) == (wo["id"], wo["wo_number"])
        logs = body["logs"]
        # Three entries share "now" (started, the filed observation, completed): newest id first.
        assert [entry["event_type"] for entry in logs] == ["completed", "observation", "started", "repair", "note"]
        assert logs[0]["id"] > logs[1]["id"] > logs[2]["id"]
        assert logs[1]["id"] == at_now.json()["id"]
        assert {logs[0]["event_date"], logs[1]["event_date"], logs[2]["event_date"]} == {NOW.isoformat() + "Z"}
        assert [logs[3]["id"], logs[4]["id"]] == [one_day.json()["id"], two_days.json()["id"]]
        assert all(entry["maintenance_wo_id"] == wo["id"] for entry in logs)
        assert {entry["performed_by_name"] for entry in logs} == {full_name(supervisor)}

        limited = wo_logs(client, headers, wo["id"], limit=2).json()["logs"]
        assert [entry["id"] for entry in limited] == [entry["id"] for entry in logs[:2]]

    @pytest.mark.parametrize("limit", [0, 501, -1])
    def test_a_limit_outside_1_to_500_is_422(self, client, db_session, headers, limit):
        wo = wc_wo(client, headers, db_session)
        assert wo_logs(client, headers, wo["id"], limit=limit).status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert wo_logs(client, headers, wo["id"], limit=500).status_code == 200

    def test_another_companys_work_order_is_404_and_never_lists_its_entries(self, client, db_session, headers):
        headers_b = headers_for(make_user(db_session, role=UserRole.SUPERVISOR, company_id=2))
        foreign_wc = make_wc(db_session, company_id=2)
        foreign = client.post(
            WORK_ORDERS, headers=headers_b, json={"work_center_id": foreign_wc.id, "title": "B"}
        ).json()
        secret = file_entry(client, headers_b, maintenance_wo_id=foreign["id"], description="ACME-SECRET finding")
        assert secret.status_code == 200, secret.text

        refused = wo_logs(client, headers, foreign["id"])
        missing = wo_logs(client, headers, foreign["id"] + 10_000)

        assert refused.status_code == missing.status_code == status.HTTP_404_NOT_FOUND
        assert refused.json() == missing.json()
        assert "ACME-SECRET" not in refused.text and foreign["wo_number"] not in refused.text

    def test_a_row_naming_another_companys_work_order_is_not_listed(self, client, db_session, headers):
        """The read is scoped on the LOG's company too: a company-2 row pointing at company 1's work
        order id (a legacy shape) never appears in company 1's list."""
        wo = wc_wo(client, headers, db_session)
        make_wc(db_session, company_id=2)
        seed_log(db_session, company_id=2, wo=wo, description="ACME-SECRET foreign row")
        listed = wo_logs(client, headers, wo["id"])
        assert listed.status_code == 200 and listed.json()["logs"] == []
        assert "ACME-SECRET" not in listed.text

    def test_a_performer_from_another_company_reads_as_no_name(self, client, db_session, headers, supervisor):
        wo = wc_wo(client, headers, db_session)
        outsider = make_user(db_session, role=UserRole.SUPERVISOR, company_id=2)
        outsider.first_name, outsider.last_name = "Acme", "Outsider"
        db_session.commit()
        foreign_performer = seed_log(db_session, wo=wo, performed_by=outsider.id)
        nobody = seed_log(db_session, wo=wo, performed_by=None, event_date=NOW - timedelta(days=2))
        own = seed_log(db_session, wo=wo, performed_by=supervisor.id, event_date=NOW - timedelta(days=3))

        logs = {entry["id"]: entry for entry in wo_logs(client, headers, wo["id"]).json()["logs"]}

        assert logs[foreign_performer.id]["performed_by"] == outsider.id
        assert logs[foreign_performer.id]["performed_by_name"] is None
        assert logs[nobody.id]["performed_by_name"] is None
        assert logs[own.id]["performed_by_name"] == full_name(supervisor)
        assert "Outsider" not in wo_logs(client, headers, wo["id"]).text

    def test_an_ai_external_token_is_refused_but_a_read_only_one_reads(
        self, client, db_session, headers, admin_headers, supervisor
    ):
        wo = wc_wo(client, headers, db_session)
        file_entry(client, headers, maintenance_wo_id=wo["id"])
        _id, ai_token = issue_token(client, admin_headers, supervisor, scope="read_only", ai_external=True)
        refused = wo_logs(client, bearer(ai_token), wo["id"])
        assert refused.status_code == status.HTTP_403_FORBIDDEN, refused.text
        assert refused.json()["detail"] == AI_EXTERNAL_ROUTE_DENIED_DETAIL

        _id, read_only = issue_token(client, admin_headers, supervisor, scope="read_only", ai_external=False)
        allowed = wo_logs(client, bearer(read_only), wo["id"])
        assert allowed.status_code == 200, allowed.text
        assert len(allowed.json()["logs"]) == 1


# --------------------------------------------------------------------------- the history reads


class TestTheHistoryReads:
    def test_both_histories_carry_the_performers_name(self, client, db_session, headers, supervisor):
        on_wc = wc_wo(client, headers, db_session)
        on_asset = asset_wo(client, headers, db_session)
        operator = make_user(db_session, role=UserRole.OPERATOR, company_id=1)
        assert file_entry(client, headers, maintenance_wo_id=on_wc["id"]).status_code == 200
        assert file_entry(client, headers_for(operator), maintenance_wo_id=on_asset["id"]).status_code == 200

        wc_history = client.get(f"/api/v1/maintenance/history/{on_wc['work_center_id']}", headers=headers)
        asset_history = client.get(f"/api/v1/maintenance/assets/{on_asset['asset_id']}/history", headers=headers)

        assert wc_history.status_code == asset_history.status_code == 200
        [wc_entry] = wc_history.json()["logs"]
        [asset_entry] = asset_history.json()["logs"]
        assert wc_entry["performed_by_name"] == full_name(supervisor)
        assert asset_entry["performed_by_name"] == full_name(operator)

    def test_a_legacy_free_text_event_type_still_reads_everywhere(self, client, db_session, headers, supervisor):
        """The column stays String(50): rows written before the vocabulary keep reading."""
        on_wc = wc_wo(client, headers, db_session)
        on_asset = asset_wo(client, headers, db_session)
        legacy_wc = seed_log(db_session, wo=on_wc, event_type="calibration check", performed_by=supervisor.id)
        legacy_asset = seed_log(db_session, wo=on_asset, event_type="Tire rotation", performed_by=supervisor.id)

        reads = {
            "work order": wo_logs(client, headers, on_wc["id"]),
            "work center": client.get(f"/api/v1/maintenance/history/{on_wc['work_center_id']}", headers=headers),
            "asset": client.get(f"/api/v1/maintenance/assets/{on_asset['asset_id']}/history", headers=headers),
        }
        for name, response in reads.items():
            assert response.status_code == 200, (name, response.text)
        assert [entry["event_type"] for entry in reads["work order"].json()["logs"]] == ["calibration check"]
        assert [entry["id"] for entry in reads["work center"].json()["logs"]] == [legacy_wc.id]
        [asset_entry] = reads["asset"].json()["logs"]
        assert (asset_entry["id"], asset_entry["event_type"]) == (legacy_asset.id, "Tire rotation")
        assert asset_entry["performed_by_name"] == full_name(supervisor)
