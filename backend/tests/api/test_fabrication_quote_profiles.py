"""Private-database checks for reusable, append-only process inputs."""

import importlib.util
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.core.security import create_access_token
from app.models.fabrication_quote_profile import FabricationQuoteProfile
from app.models.role_permission import RolePermission
from app.models.user import UserRole
from app.services.audit_service import AuditService, AuditWriteError
from tests.api.test_receiving_compliance import headers_for, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]
BASE = "/api/v1/fabrication-quote-profiles"


@pytest.fixture
def admin_headers(db_session):
    return headers_for(make_user(db_session, role=UserRole.ADMIN, company_id=1))


def profile(**changes):
    return {
        "name": "200 ton brake / steel / trial basis",
        "process": "brake",
        "machine": "200 ton brake",
        "material": "Steel, grade pending",
        "thickness_mm": "3.175",
        "currency": "USD",
        "template": {
            "id": "source-op",
            "part_id": "source-part",
            "process": "brake",
            "setup_labor_seconds": None,
            "labor_rate_per_hour": None,
            "machine_rate_per_hour": None,
            "recipe": {"kind": "brake", "hits": 2, "feasibility_reviewed": True},
            "evidence": {
                "source": "Synthetic shop trial",
                "reviewed": True,
                "status": "measured",
            },
        },
        "evidence_note": "Synthetic reference; rates and tooling still need review.",
        **changes,
    }


def save(client, headers, body=None):
    response = client.post(BASE, headers=headers, json=profile() if body is None else body)
    assert response.status_code == 200, response.text
    return response.json()


def test_library_starts_empty_and_unknown_rates_remain_unknown(client, db_session, admin_headers):
    assert client.get(BASE, headers=admin_headers).json() == {"items": [], "total": 0}
    row = save(client, admin_headers)
    assert row["revision"] == 1 and row["key"]
    assert row["currency"] == "USD"
    assert row["template"]["labor_rate_per_hour"] is None
    assert row["template"]["machine_rate_per_hour"] is None
    assert row["template"]["setup_labor_seconds"] is None
    assert row["template"]["evidence"]["reviewed"] is False
    assert row["template"]["evidence"]["status"] == "measured"
    assert row["template"]["recipe"]["feasibility_reviewed"] is False
    fetched = client.get(BASE, headers=admin_headers).json()
    assert fetched["total"] == 1
    assert fetched["items"][0]["template"] == row["template"]
    assert len(row["content_sha256"]) == 64


def test_revisions_append_and_stale_write_leaves_history_intact(client, db_session, admin_headers):
    first = save(client, admin_headers)
    body = profile(key=first["key"], expected_revision=1, name="Measured brake cycle")
    body["template"]["recipe"]["seconds_per_hit"] = "8.5"
    second = save(client, admin_headers, body)
    assert second["id"] != first["id"] and second["revision"] == 2
    assert second["key"] == first["key"]
    assert client.post(BASE, headers=admin_headers, json=body).status_code == 409
    fetched = client.get(BASE, headers=admin_headers).json()
    assert fetched["total"] == 1 and fetched["items"][0]["revision"] == 2
    history = client.get(f"{BASE}/{first['key']}/revisions", headers=admin_headers).json()
    assert [item["revision"] for item in history["items"]] == [2, 1]
    assert history["items"][1]["template"]["recipe"]["seconds_per_hit"] is None
    assert db_session.query(FabricationQuoteProfile).count() == 2


def test_profiles_and_history_cannot_cross_tenants(client, db_session, admin_headers):
    row = save(client, admin_headers)
    other = headers_for(make_user(db_session, role=UserRole.ADMIN, company_id=2))
    assert client.get(BASE, headers=other).json()["items"] == []
    assert client.get(f"{BASE}/{row['key']}/revisions", headers=other).status_code == 404
    body = profile(key=row["key"], expected_revision=1)
    assert client.post(BASE, headers=other, json=body).status_code == 404
    own = save(client, other)
    assert own["key"] != row["key"]
    assert len(client.get(BASE, headers=admin_headers).json()["items"]) == 1


def test_read_permissions_and_read_only_company_context(client, db_session, admin_headers):
    save(client, admin_headers)
    operator = make_user(db_session, role=UserRole.OPERATOR, company_id=1)
    headers = headers_for(operator)
    assert client.get(BASE, headers=headers).status_code == 403
    override = RolePermission(company_id=1, role=UserRole.OPERATOR, permissions=["purchasing:view"])
    db_session.add(override)
    db_session.commit()
    assert client.get(BASE, headers=headers).status_code == 200
    assert client.post(BASE, headers=headers, json=profile()).status_code == 403
    platform = make_user(db_session, role=UserRole.PLATFORM_ADMIN, company_id=2)
    token = create_access_token(subject=platform.id, company_id=1, read_only=True)
    readonly = {
        "Authorization": f"Bearer {token}",
        "X-Requested-With": "XMLHttpRequest",
    }
    assert client.get(BASE, headers=readonly).status_code == 200
    assert client.post(BASE, headers=readonly, json=profile()).status_code == 403


@pytest.mark.parametrize("append", [False, True])
def test_required_audit_failure_rolls_back_profile(client, db_session, admin_headers, monkeypatch, append):
    body = profile()
    if append:
        first = save(client, admin_headers)
        body.update(key=first["key"], expected_revision=1)

    def fail(*args, **kwargs):
        raise AuditWriteError("Synthetic required audit failure")

    monkeypatch.setattr(AuditService, "log_required", fail)
    response = client.post(BASE, headers=admin_headers, json=body)
    assert response.status_code == 503
    assert db_session.query(FabricationQuoteProfile).count() == int(append)


def test_profile_rows_are_immutable_through_direct_sql(client, db_session, admin_headers):
    row = save(client, admin_headers)
    for statement in [
        "UPDATE fabrication_quote_profiles SET name='tampered' WHERE id=:id",
        "DELETE FROM fabrication_quote_profiles WHERE id=:id",
    ]:
        with pytest.raises(DBAPIError):
            db_session.execute(text(statement), {"id": row["id"]})
        db_session.rollback()
    assert db_session.query(FabricationQuoteProfile).one().name == row["name"]


def test_unique_revision_constraint_prevents_two_winners(client, db_session, admin_headers):
    save(client, admin_headers)
    row = db_session.query(FabricationQuoteProfile).one()
    values = {column.name: getattr(row, column.name) for column in row.__table__.columns if column.name != "id"}
    db_session.add(FabricationQuoteProfile(**deepcopy(values)))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
    assert db_session.query(FabricationQuoteProfile).count() == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"name": " "},
        {"evidence_note": " "},
        {"key": str(uuid4())},
        {"expected_revision": 1},
        {"key": str(uuid4()), "expected_revision": True},
        {"thickness_mm": "0"},
        {"thickness_mm": "NaN"},
        {"currency": "XYZ"},
        {"process": "laser"},
    ],
)
def test_invalid_profile_inputs_fail_before_write(client, db_session, admin_headers, changes):
    assert client.post(BASE, headers=admin_headers, json=profile(**changes)).status_code == 422
    assert db_session.query(FabricationQuoteProfile).count() == 0


def test_filter_and_pagination_use_latest_revision_metadata(client, db_session, admin_headers):
    first = save(client, admin_headers)
    body = profile(
        key=first["key"],
        expected_revision=1,
        process="manual",
        machine=None,
        material=None,
        thickness_mm=None,
    )
    body["template"]["process"] = "manual"
    body["template"]["recipe"] = {"kind": "manual"}
    save(client, admin_headers, body)
    assert client.get(BASE, headers=admin_headers, params={"process": "brake"}).json()["items"] == []
    row = client.get(BASE, headers=admin_headers, params={"process": "manual"}).json()["items"][0]
    assert row["machine"] is None and row["material"] is None and row["thickness_mm"] is None
    assert client.get(BASE, headers=admin_headers, params={"page": 2, "per_page": 1}).json()["items"] == []


def load_migration():
    path = Path(__file__).parents[2] / "alembic" / "versions" / "107_fabrication_quote_profiles.py"
    spec = importlib.util.spec_from_file_location("test_profile_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_upgrade_and_downgrade_use_private_database():
    migration = load_migration()
    assert migration.down_revision == "106_fabrication_quoting"
    scratch = create_engine("sqlite:///:memory:")
    try:
        with scratch.begin() as connection:
            connection.execute(text("CREATE TABLE companies (id INTEGER PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            assert "fabrication_quote_profiles" in inspect(connection).get_table_names()
            connection.execute(text("""
                INSERT INTO fabrication_quote_profiles
                    (id, company_id, key, revision, name, process, currency,
                     template_json, evidence_note, content_sha256, created_by, created_at)
                VALUES (1, 1, 'test-key', 1, 'Migration reference', 'manual', 'USD',
                        '{}', 'Test evidence', 'test-sha', 1, '2026-09-15')
            """))
            for statement in [
                "UPDATE fabrication_quote_profiles SET name='tampered' WHERE id=1",
                "DELETE FROM fabrication_quote_profiles WHERE id=1",
            ]:
                with pytest.raises(DBAPIError):
                    connection.execute(text(statement))
            migration.downgrade()
            assert "fabrication_quote_profiles" not in inspect(connection).get_table_names()
    finally:
        scratch.dispose()


def test_postgres_migration_guards_service_storage_and_all_mutation_paths():
    migration = load_migration()
    statements = []

    class CaptureOperations:
        def get_bind(self):
            class Bind:
                class dialect:
                    name = "postgresql"

            return Bind()

        def execute(self, value):
            statements.append(value)

    migration.op = CaptureOperations()
    migration._security()
    assert any("ENABLE ROW LEVEL SECURITY" in statement for statement in statements)
    for role in ("PUBLIC", "anon", "authenticated"):
        assert any(f"FROM {role}" in statement and "REVOKE ALL" in statement for statement in statements)
    for action in ("UPDATE", "DELETE", "TRUNCATE"):
        assert any(f"BEFORE {action} ON fabrication_quote_profiles" in statement for statement in statements)
