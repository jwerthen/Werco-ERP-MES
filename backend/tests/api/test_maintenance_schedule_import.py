"""The PM schedule import (template ``maintenance-schedules``, one row per task).

Fixtures are SYNTHETIC sheets shaped like a shop's preventive-maintenance workbook once it
is laid out one row per task: monthly way-lube / coolant / hydraulic tasks and a yearly
battery task per machine, one tab's tasks fanned out to several laser work centers, an
"as necessary" task, equipment that is not a work center (a forklift, a compressor), and
the spellings such a sheet uses ("yearly", "Semi-Annual"). No real Werco data is used.
"""

import csv
import io
import json
from datetime import date

import pytest
from fastapi import status
from openpyxl import Workbook, load_workbook
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.maintenance import (
    MaintenanceAsset,
    MaintenanceFrequency,
    MaintenancePriority,
    MaintenanceSchedule,
    MaintenanceType,
)
from app.models.user import UserRole
from app.services import maintenance_schedule_import_service as pm
from app.services.audit_service import AuditService, AuditWriteError
from app.services.import_service import build_import_template_workbook
from tests.api.test_maintenance_assets import make_asset, make_wc
from tests.api.test_receiving_compliance import headers_for, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

PREVIEW = "/api/v1/maintenance/schedules/import/preview"
COMMIT = "/api/v1/maintenance/schedules/import/commit"

COLUMNS = [
    "work_center_code",
    "asset_tag",
    "asset_name",
    "asset_type",
    "schedule_name",
    "task",
    "frequency",
    "frequency_days",
    "next_due_date",
    "estimated_duration_hours",
    "requires_shutdown",
    "priority",
]


def xlsx(rows, *, title_block=False) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "PM"
    if title_block:
        sheet.append(["2026 PREVENTIVE MAINTENANCE SCHEDULE (synthetic)"])
        sheet.append([])
    sheet.append(COLUMNS)
    for row in rows:
        sheet.append([row.get(column, "") for column in COLUMNS])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def as_csv(rows) -> bytes:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in COLUMNS})
    return output.getvalue().encode("utf-8")


def upload(client, url, headers, content, *, name="pm.xlsx", **form):
    return client.post(url, headers=headers, files={"file": (name, io.BytesIO(content))}, data=form or None)


def preview_and_commit(client, headers, content, *, name="pm.xlsx"):
    preview = upload(client, PREVIEW, headers, content, name=name)
    assert preview.status_code == status.HTTP_200_OK, preview.text
    commit = upload(client, COMMIT, headers, content, name=name, expected_source_hash=preview.json()["source_hash"])
    return preview, commit


def audit_rows(db: Session):
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type.in_(["maintenance_schedule", "maintenance_asset", "maintenance_schedule_import"])
        )
        .order_by(AuditLog.id)
        .all()
    )


def schedules(db: Session, company_id: int = 1):
    db.expire_all()
    return (
        db.query(MaintenanceSchedule)
        .filter(MaintenanceSchedule.company_id == company_id)
        .order_by(MaintenanceSchedule.id)
        .all()
    )


@pytest.fixture
def headers(db_session):
    return headers_for(make_user(db_session, role=UserRole.MANAGER, company_id=1))


@pytest.fixture
def lathe(db_session):
    return make_wc(db_session, code="CNC-02")


MONTHLY_AND_YEARLY = [
    {"work_center_code": "CNC-02", "task": "Lubricate ways", "frequency": "Monthly", "requires_shutdown": "yes"},
    {"work_center_code": "cnc-02", "task": "Check coolant concentration", "frequency": "monthly"},
    {"work_center_code": "CNC-02", "task": "Check hydraulic oil level", "frequency": "MONTHLY"},
    {"work_center_code": "CNC-02", "task": "Lubricate  ways", "frequency": "monthly"},  # duplicate (spacing)
    {"work_center_code": "CNC-02", "task": "Replace control batteries", "frequency": "Yearly"},
    {"work_center_code": "CNC-02", "task": "Clean chip conveyor", "frequency": "As necessary"},
]


# ---------------------------------------------------------------------------
# Pure parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("monthly", MaintenanceFrequency.MONTHLY),
        ("Yearly", MaintenanceFrequency.ANNUAL),
        ("annually", MaintenanceFrequency.ANNUAL),
        ("Semi-Annual", MaintenanceFrequency.SEMI_ANNUAL),
        ("semi_annual", MaintenanceFrequency.SEMI_ANNUAL),
        ("Bi-Weekly", MaintenanceFrequency.BIWEEKLY),
        ("QUARTERLY.", MaintenanceFrequency.QUARTERLY),
        ("custom", MaintenanceFrequency.CUSTOM),
        ("  daily ", MaintenanceFrequency.DAILY),
    ],
)
def test_parse_frequency_reads_sheet_spellings(text, expected):
    assert pm.parse_frequency(text) is expected


@pytest.mark.parametrize("text", ["As necessary", "as needed", "AS REQUIRED"])
def test_as_necessary_has_no_cadence(text):
    with pytest.raises(pm.AsNeededFrequency):
        pm.parse_frequency(text)


@pytest.mark.parametrize("text", ["bi-annual", "usage_based", "every so often", ""])
def test_unknown_or_ambiguous_frequencies_are_refused(text):
    with pytest.raises(ValueError) as caught:
        pm.parse_frequency(text)
    assert not isinstance(caught.value, pm.AsNeededFrequency)


@pytest.mark.parametrize(
    "stored,items",
    [
        ('["Oil ways", "Check coolant"]', ["Oil ways", "Check coolant"]),
        ('[{"item": "Grease"}, {"task": "Drain"}]', ["Grease", "Drain"]),
        ("- Oil ways\n\n* Check coolant\n", ["Oil ways", "Check coolant"]),
        ("", []),
        (None, []),
    ],
)
def test_parse_checklist_is_tolerant(stored, items):
    assert pm.parse_checklist(stored) == items


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


class TestPreview:
    def test_groups_tasks_into_one_schedule_per_frequency_and_writes_nothing(self, client, db_session, headers, lathe):
        resp = upload(client, PREVIEW, headers, xlsx(MONTHLY_AND_YEARLY))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        by_name = {row["name"]: row for row in body["schedules"]}
        assert set(by_name) == {"CNC-02 Monthly PM", "CNC-02 Annual PM"}
        monthly = by_name["CNC-02 Monthly PM"]
        assert monthly["tasks"] == ["Lubricate ways", "Check coolant concentration", "Check hydraulic oil level"]
        assert monthly["outcome"] == "create" and monthly["target_type"] == "work_center"
        assert monthly["target_id"] == lathe.id and monthly["requires_shutdown"] is True
        assert by_name["CNC-02 Annual PM"]["frequency"] == "annual"

        rows = {row["row"]: row for row in body["rows"]}
        assert rows[5]["warnings"] and "listed once" in rows[5]["warnings"][0]
        assert rows[7]["outcome"] == "skipped" and "no cadence" in rows[7]["warnings"][0]
        summary = body["summary"]
        assert summary["schedules_to_create"] == 2 and summary["skipped_rows"] == 1 and summary["error_count"] == 0
        assert len(body["source_hash"]) == 64 and body["committed"] is False

        assert schedules(db_session) == [] and audit_rows(db_session) == []

    def test_one_tab_fans_out_to_every_laser(self, client, db_session, headers):
        lasers = [make_wc(db_session, code=f"LAS-0{n}") for n in range(1, 5)]
        rows = [
            {"work_center_code": wc.code, "task": task, "frequency": "monthly"}
            for wc in lasers
            for task in ("Clean lens", "Check chiller")
        ]
        body = upload(client, PREVIEW, headers, as_csv(rows), name="laser.csv").json()
        assert sorted(row["name"] for row in body["schedules"]) == [f"LAS-0{n} Monthly PM" for n in range(1, 5)]
        assert all(row["tasks"] == ["Clean lens", "Check chiller"] for row in body["schedules"])

    def test_title_block_above_the_header_is_skipped(self, client, db_session, headers, lathe):
        body = upload(client, PREVIEW, headers, xlsx(MONTHLY_AND_YEARLY[:1], title_block=True)).json()
        assert [row["name"] for row in body["schedules"]] == ["CNC-02 Monthly PM"]

    def test_custom_frequency_and_explicit_schedule_name(self, client, db_session, headers, lathe):
        rows = [
            {"work_center_code": "CNC-02", "task": "Check belts", "frequency": "custom", "frequency_days": "45"},
            {"work_center_code": "CNC-02", "task": "Grease", "frequency": "weekly", "schedule_name": "Lathe weekly"},
        ]
        body = upload(client, PREVIEW, headers, xlsx(rows)).json()
        names = {row["name"]: row for row in body["schedules"]}
        assert names["CNC-02 Every 45 Days PM"]["frequency_days"] == 45
        assert "Lathe weekly" in names

    @pytest.mark.parametrize(
        "row,fragment",
        [
            ({"work_center_code": "NOPE-1", "task": "t", "frequency": "monthly"}, "no work center"),
            ({"work_center_code": "CNC-02", "asset_tag": "FL-1", "task": "t", "frequency": "monthly"}, "not both"),
            ({"task": "t", "frequency": "monthly"}, "work_center_code or asset_tag"),
            ({"asset_tag": "GHOST-1", "task": "t", "frequency": "monthly"}, "fill asset_name"),
            ({"work_center_code": "CNC-02", "task": "t", "frequency": "bi-annual"}, "not recognized"),
            ({"work_center_code": "CNC-02", "task": "t", "frequency": "usage_based"}, "not recognized"),
            ({"work_center_code": "CNC-02", "task": "t", "frequency": "custom"}, "frequency_days is required"),
            ({"work_center_code": "CNC-02", "task": "", "frequency": "monthly"}, "task is required"),
            ({"work_center_code": "CNC-02", "task": "t", "frequency": "monthly", "priority": "asap"}, "priority"),
            ({"work_center_code": "CNC-02", "task": "t", "frequency": "monthly", "requires_shutdown": "maybe"}, "true"),
            ({"work_center_code": "CNC-02", "task": "t", "frequency": "monthly", "next_due_date": "soon"}, "date"),
        ],
    )
    def test_row_errors(self, client, db_session, headers, lathe, row, fragment):
        body = upload(client, PREVIEW, headers, xlsx([row])).json()
        [result] = body["rows"]
        assert result["outcome"] == "error" and fragment in result["error"], result
        assert body["summary"]["error_count"] == 1

    def test_conflicting_schedule_values_and_two_cadences_under_one_name(self, client, db_session, headers, lathe):
        rows = [
            {"work_center_code": "CNC-02", "task": "a", "frequency": "monthly", "estimated_duration_hours": "1"},
            {"work_center_code": "CNC-02", "task": "b", "frequency": "monthly", "estimated_duration_hours": "2"},
            {"work_center_code": "CNC-02", "task": "c", "frequency": "monthly", "schedule_name": "Lathe PM"},
            {"work_center_code": "CNC-02", "task": "d", "frequency": "annual", "schedule_name": "lathe pm"},
        ]
        rows_out = {row["row"]: row for row in upload(client, PREVIEW, headers, xlsx(rows)).json()["rows"]}
        assert "conflicts with row 2" in rows_out[3]["error"]
        assert "not loaded" in rows_out[2]["error"]  # its schedule is blocked by row 3
        assert "more than one frequency" in rows_out[4]["error"] and "more than one frequency" in rows_out[5]["error"]

    def test_assets_to_create_and_their_name_rules(self, client, db_session, headers):
        make_asset(db_session, tag="AC-01", name="Air compressor 1")
        rows = [
            {
                "asset_tag": "FL-01",
                "asset_name": "Forklift 1",
                "asset_type": "forklift",
                "task": "Check tires",
                "frequency": "monthly",
            },
            {"asset_tag": "fl-01", "task": "Check forks", "frequency": "monthly"},
            {"asset_tag": "AC-01", "asset_name": "Renamed?", "task": "Drain tank", "frequency": "weekly"},
            {"asset_tag": "SAW-9", "asset_name": "Cold saw", "task": "Blade", "frequency": "monthly"},
            {"asset_tag": "SAW-9", "asset_name": "Band saw", "task": "Coolant", "frequency": "monthly"},
        ]
        body = upload(client, PREVIEW, headers, xlsx(rows)).json()
        # SAW-9 is listed from its clean row 5; row 6's conflict still makes the commit refuse.
        assert [(a["asset_tag"], a["name"], a["asset_type"], a["rows"]) for a in body["assets_to_create"]] == [
            ("FL-01", "Forklift 1", "forklift", [2, 3]),
            ("SAW-9", "Cold saw", None, [5]),
        ]
        rows_out = {row["row"]: row for row in body["rows"]}
        assert "never renames" in rows_out[4]["warnings"][0] and rows_out[4]["outcome"] == "create"
        assert "differs from row 5" in rows_out[6]["error"]
        fl = next(row for row in body["schedules"] if row["target_code"] == "FL-01")
        assert fl["name"] == "FL-01 Monthly PM" and fl["tasks"] == ["Check tires", "Check forks"]

    def test_a_code_or_tag_matching_two_targets_by_case_is_refused_not_guessed(self, client, db_session, headers):
        """Codes resolve case-insensitively, but the database keys are case-SENSITIVE, so two
        rows can differ only in case. The import must refuse rather than pick one -- a PM
        loaded onto the wrong machine is a quality record about the wrong equipment."""
        make_wc(db_session, code="LAS-09")
        make_wc(db_session, code="las-09")
        make_asset(db_session, tag="FL-09")
        make_asset(db_session, tag="fl-09")
        rows = [
            {"work_center_code": "Las-09", "task": "Clean lens", "frequency": "monthly"},
            {"asset_tag": "Fl-09", "task": "Check tires", "frequency": "monthly"},
        ]
        out = {row["row"]: row for row in upload(client, PREVIEW, headers, xlsx(rows)).json()["rows"]}
        assert out[2]["outcome"] == "error" and "matches 2 work centers" in out[2]["error"]
        assert out[3]["outcome"] == "error" and "matches 2 assets" in out[3]["error"]

    def test_another_companys_work_centers_are_invisible(self, client, db_session, headers):
        make_wc(db_session, company_id=2, code="ACME-SECRET-WC")
        body = upload(
            client,
            PREVIEW,
            headers,
            xlsx([{"work_center_code": "ACME-SECRET-WC", "task": "t", "frequency": "monthly"}]),
        ).json()
        assert body["rows"][0]["outcome"] == "error" and "no work center" in body["rows"][0]["error"]

    def test_bad_files_are_400_and_roles_are_planning_only(self, client, db_session, headers, lathe):
        assert upload(client, PREVIEW, headers, b"not a workbook", name="pm.pdf").status_code == 400
        assert upload(client, PREVIEW, headers, b"task,priority\nx,low\n", name="pm.csv").status_code == 400
        for role in (UserRole.OPERATOR, UserRole.QUALITY, UserRole.VIEWER):
            other = headers_for(make_user(db_session, role=role, company_id=1))
            assert upload(client, PREVIEW, other, xlsx(MONTHLY_AND_YEARLY)).status_code == 403


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


class TestCommit:
    def test_creates_schedules_assets_and_required_audit_then_is_idempotent(self, client, db_session, headers, lathe):
        rows = MONTHLY_AND_YEARLY + [
            {
                "asset_tag": "FL-01",
                "asset_name": "Forklift 1",
                "task": "Check tires",
                "frequency": "monthly",
                "priority": "high",
            },
        ]
        content = xlsx(rows)
        _, commit = preview_and_commit(client, headers, content)
        assert commit.status_code == 200, commit.text
        body = commit.json()
        assert body["committed"] is True and body["summary"]["schedules_to_create"] == 3
        assert all(row["schedule_id"] for row in body["schedules"])
        assert body["assets_to_create"][0]["asset_id"]

        stored = {schedule.name: schedule for schedule in schedules(db_session)}
        monthly = stored["CNC-02 Monthly PM"]
        assert monthly.work_center_id == lathe.id and monthly.asset_id is None
        assert (
            monthly.frequency == MaintenanceFrequency.MONTHLY and monthly.maintenance_type == MaintenanceType.PREVENTIVE
        )
        assert json.loads(monthly.checklist) == [
            "Lubricate ways",
            "Check coolant concentration",
            "Check hydraulic oil level",
        ]
        assert monthly.requires_shutdown is True and monthly.estimated_duration_hours == 1.0
        assert monthly.next_due_date == pm.central_today() and monthly.is_active is True
        assert stored["CNC-02 Annual PM"].frequency == MaintenanceFrequency.ANNUAL
        forklift = db_session.query(MaintenanceAsset).filter(MaintenanceAsset.asset_tag == "FL-01").one()
        assert stored["FL-01 Monthly PM"].asset_id == forklift.id and stored["FL-01 Monthly PM"].work_center_id is None
        assert stored["FL-01 Monthly PM"].priority == MaintenancePriority.HIGH

        actions = [(row.resource_type, row.action) for row in audit_rows(db_session)]
        assert actions.count(("maintenance_schedule", "CREATE")) == 3
        assert actions.count(("maintenance_asset", "CREATE")) == 1
        assert actions[-1] == ("maintenance_schedule_import", "IMPORT")
        create_row = next(
            row
            for row in audit_rows(db_session)
            if row.resource_type == "maintenance_schedule" and row.resource_id == monthly.id
        )
        assert create_row.extra_data["source"] == "pm_import" and create_row.extra_data["rows"] == [2, 3, 4, 5]

        before = len(audit_rows(db_session))
        again_preview, again = preview_and_commit(client, headers, content)
        assert {row["outcome"] for row in again_preview.json()["schedules"]} == {"unchanged"}
        assert again.status_code == 200 and again.json()["summary"]["schedules_to_create"] == 0
        assert len(schedules(db_session)) == 3 and len(audit_rows(db_session)) == before
        assert db_session.query(MaintenanceAsset).count() == 1

    def test_updates_an_existing_active_schedule_by_name_ignoring_case(self, client, db_session, headers, lathe):
        existing = MaintenanceSchedule(
            company_id=1,
            work_center_id=lathe.id,
            name="cnc-02 monthly pm",
            frequency=MaintenanceFrequency.QUARTERLY,
            checklist="Old task",
            next_due_date=date(2026, 12, 1),
            estimated_duration_hours=3,
            is_active=True,
        )
        retired = MaintenanceSchedule(
            company_id=1,
            work_center_id=lathe.id,
            name="CNC-02 Annual PM",
            frequency=MaintenanceFrequency.ANNUAL,
            is_active=False,
        )
        db_session.add_all([existing, retired])
        db_session.commit()

        preview, commit = preview_and_commit(client, headers, xlsx(MONTHLY_AND_YEARLY))
        plan = {row["name"]: row for row in preview.json()["schedules"]}
        assert plan["CNC-02 Monthly PM"]["outcome"] == "update"
        assert set(plan["CNC-02 Monthly PM"]["changes"]) == {"frequency", "checklist", "requires_shutdown"}
        assert plan["CNC-02 Annual PM"]["outcome"] == "create"
        assert any("inactive schedule" in w for row in preview.json()["rows"] for w in row["warnings"])
        assert commit.status_code == 200, commit.text

        db_session.refresh(existing)
        assert existing.frequency == MaintenanceFrequency.MONTHLY and existing.name == "cnc-02 monthly pm"
        assert json.loads(existing.checklist)[0] == "Lubricate ways"
        assert existing.next_due_date == date(2026, 12, 1) and existing.estimated_duration_hours == 3  # blanks keep
        update = next(row for row in audit_rows(db_session) if row.action == "UPDATE")
        assert update.resource_id == existing.id and update.old_values["frequency"] == "quarterly"
        assert len(schedules(db_session)) == 3

    def test_any_row_error_refuses_409_and_writes_nothing(self, client, db_session, headers, lathe):
        rows = MONTHLY_AND_YEARLY + [{"work_center_code": "NOPE", "task": "t", "frequency": "monthly"}]
        _, commit = preview_and_commit(client, headers, xlsx(rows))
        assert commit.status_code == status.HTTP_409_CONFLICT
        assert commit.json()["detail"]["errors"][0]["row"] == 8
        assert schedules(db_session) == [] and audit_rows(db_session) == []

    def test_a_different_file_than_previewed_is_409(self, client, db_session, headers, lathe):
        preview = upload(client, PREVIEW, headers, xlsx(MONTHLY_AND_YEARLY))
        other = xlsx(MONTHLY_AND_YEARLY[:2])
        commit = upload(client, COMMIT, headers, other, expected_source_hash=preview.json()["source_hash"])
        assert commit.status_code == 409 and schedules(db_session) == []

    def test_audit_failure_rolls_everything_back_503(self, client, db_session, headers, lathe, monkeypatch):
        def refuse(self, *args, **kwargs):
            raise AuditWriteError("audit store down")

        monkeypatch.setattr(AuditService, "log_required", refuse)
        rows = MONTHLY_AND_YEARLY + [
            {"asset_tag": "FL-01", "asset_name": "Forklift", "task": "t", "frequency": "monthly"}
        ]
        _, commit = preview_and_commit(client, headers, xlsx(rows))
        assert commit.status_code == 503
        assert schedules(db_session) == [] and db_session.query(MaintenanceAsset).count() == 0

    def test_never_touches_another_companys_schedule(self, client, db_session, headers, lathe):
        foreign_wc = make_wc(db_session, company_id=2, code="CNC-02")
        foreign = MaintenanceSchedule(
            company_id=2, work_center_id=foreign_wc.id, name="CNC-02 Monthly PM", checklist="theirs", is_active=True
        )
        db_session.add(foreign)
        db_session.commit()
        _, commit = preview_and_commit(client, headers, xlsx(MONTHLY_AND_YEARLY))
        assert commit.status_code == 200
        db_session.refresh(foreign)
        assert foreign.checklist == "theirs"
        assert {s.work_center_id for s in schedules(db_session, company_id=1)} == {lathe.id}

    def test_a_mis_tenanted_legacy_schedule_on_our_machine_is_never_upserted(self, client, db_session, headers, lathe):
        """The test above cannot tell whether the schedule read is tenant-scoped: the foreign
        schedule sits on the foreign company's OWN work center, which the upsert key (target
        id, name) never matches anyway. This one is the shape where the scope is load-bearing:
        a legacy row stamped company 2 that points at company 1's machine under the very
        name the import derives. Unscoped, the import would rewrite another tenant's row."""
        make_wc(db_session, company_id=2, code="OTHER-1")  # company 2 exists with a machine of its own
        legacy = MaintenanceSchedule(
            company_id=2, work_center_id=lathe.id, name="CNC-02 Monthly PM", checklist="theirs", is_active=True
        )
        db_session.add(legacy)
        db_session.commit()

        preview, commit = preview_and_commit(client, headers, xlsx(MONTHLY_AND_YEARLY))
        plan = {row["name"]: row for row in preview.json()["schedules"]}
        assert plan["CNC-02 Monthly PM"]["outcome"] == "create"
        assert commit.status_code == 200, commit.text
        db_session.expire_all()
        assert db_session.get(MaintenanceSchedule, legacy.id).checklist == "theirs"
        assert db_session.get(MaintenanceSchedule, legacy.id).company_id == 2
        mine = [s for s in schedules(db_session, company_id=1) if s.name == "CNC-02 Monthly PM"]
        assert len(mine) == 1 and mine[0].id != legacy.id

    def test_another_companys_asset_tag_never_binds(self, client, db_session, headers):
        """Asset tags are unique per company only. A tag that exists only in company 2 is
        unknown to company 1: without asset_name it is a row error, with asset_name company 1
        gets its OWN asset -- the foreign asset is never targeted."""
        foreign = make_asset(db_session, company_id=2, tag="FL-77", name="ACME-SECRET forklift")
        bare = upload(
            client, PREVIEW, headers, xlsx([{"asset_tag": "FL-77", "task": "Check tires", "frequency": "monthly"}])
        ).json()
        assert bare["rows"][0]["outcome"] == "error" and "no asset tagged 'FL-77'" in bare["rows"][0]["error"]
        assert "ACME-SECRET" not in str(bare)

        named = [{"asset_tag": "FL-77", "asset_name": "Forklift", "task": "Check tires", "frequency": "monthly"}]
        preview, commit = preview_and_commit(client, headers, xlsx(named))
        assert [a["asset_tag"] for a in preview.json()["assets_to_create"]] == ["FL-77"]
        assert commit.status_code == 200, commit.text
        db_session.expire_all()
        own = db_session.query(MaintenanceAsset).filter_by(company_id=1, asset_tag="FL-77").one()
        [schedule] = schedules(db_session, company_id=1)
        assert schedule.asset_id == own.id and own.id != foreign.id
        assert db_session.get(MaintenanceAsset, foreign.id).name == "ACME-SECRET forklift"

    def test_two_active_schedules_with_the_upsert_name_are_a_row_error(self, client, db_session, headers, lathe):
        """The upsert key is (target, name). When the database already holds two ACTIVE
        schedules under that key, the import cannot know which one the sheet means, so it
        refuses rather than picking one."""
        twins = [
            MaintenanceSchedule(company_id=1, work_center_id=lathe.id, name=name, checklist="old", is_active=True)
            for name in ("CNC-02 Monthly PM", "cnc-02 monthly pm")
        ]
        db_session.add_all(twins)
        db_session.commit()
        preview, commit = preview_and_commit(client, headers, xlsx(MONTHLY_AND_YEARLY[:1]))
        [row] = preview.json()["rows"]
        assert row["outcome"] == "error" and "2 active schedules on CNC-02" in row["error"]
        assert all(str(twin.id) in row["error"] for twin in twins)
        assert commit.status_code == status.HTTP_409_CONFLICT
        db_session.expire_all()
        assert {db_session.get(MaintenanceSchedule, twin.id).checklist for twin in twins} == {"old"}


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------


def test_template_is_listed_and_a_filled_copy_imports(client, db_session, headers, lathe):
    listed = client.get("/api/v1/import/templates", headers=headers).json()["templates"]
    entry = next(item for item in listed if item["entity"] == "maintenance-schedules")
    assert entry["columns"][:2] == ["work_center_code", "asset_tag"] and "task" in entry["columns"]

    workbook = load_workbook(io.BytesIO(build_import_template_workbook("maintenance-schedules")))
    sheet = workbook["Import"]
    header = [cell.value for cell in sheet[1]]
    filled = [""] * len(header)
    filled[header.index("work_center_code")] = "CNC-02"
    filled[header.index("task")] = "Lubricate ways"
    filled[header.index("frequency")] = "monthly"
    sheet.append(filled)
    output = io.BytesIO()
    workbook.save(output)
    body = upload(client, PREVIEW, headers, output.getvalue()).json()
    assert [row["name"] for row in body["schedules"]] == ["CNC-02 Monthly PM"]
    assert body["summary"]["total_rows"] == 1  # the "# " guidance row is not data
