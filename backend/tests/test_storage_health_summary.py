"""The document-storage entry on /health/detailed and /api/v1/setup/health.

``/health/detailed`` is unauthenticated, so the summary must say WHICH backend new
uploads go to and where it points without ever carrying a credential, a full endpoint
URL or a filesystem path.
"""

import json
import os

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.core.config import settings
from app.schemas.storage import StorageStatus
from app.services import storage_service
from app.services.storage_service import (
    STORAGE_WARNING_EPHEMERAL,
    STORAGE_WARNING_UNKNOWN,
    _endpoint_hostname,
    _path_on_mounted_volume,
    describe_storage_configuration,
)

SECRET_KEY_ID = "AKIAFICTIONALKEYID00"
SECRET_ACCESS_KEY = "fictional/secret/access/key/value/0000000000"
ENDPOINT_WITH_SECRETS = "https://user:hunter2@objects.example.test:8443/some/path?X-Amz-Signature=deadbeef"


@pytest.fixture
def s3_settings(monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    monkeypatch.setattr(settings, "S3_BUCKET_NAME", "fictional-docs-bucket")
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", ENDPOINT_WITH_SECRETS)
    monkeypatch.setattr(settings, "AWS_REGION", "us-east-2")
    monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", SECRET_KEY_ID)
    monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", SECRET_ACCESS_KEY)


def _assert_no_secret(payload) -> None:
    text = json.dumps(payload)
    for secret in (SECRET_KEY_ID, SECRET_ACCESS_KEY, "hunter2", "user:", "deadbeef", "X-Amz", "/some/path", "8443"):
        assert secret not in text, secret


@pytest.mark.unit
class TestDescribeStorageConfiguration:
    def test_s3_reports_bucket_and_hostname_only(self, s3_settings):
        summary = describe_storage_configuration()

        assert summary["backend"] == "s3"
        assert summary["durability"] == "durable"
        assert summary["bucket"] == "fictional-docs-bucket"
        assert summary["endpoint_host"] == "objects.example.test"
        assert summary["region"] == "us-east-2"
        assert summary["warning"] is None
        assert summary["upload_dir_on_mounted_volume"] is None
        _assert_no_secret(summary)
        StorageStatus(**summary)

    def test_s3_never_builds_a_client(self, s3_settings, monkeypatch):
        def _boom(**_kwargs):
            raise AssertionError("health output must not instantiate the S3 backend")

        monkeypatch.setattr(storage_service, "get_s3_storage", _boom)
        assert describe_storage_configuration()["backend"] == "s3"

    def test_s3_default_aws_endpoint_is_null_host(self, s3_settings, monkeypatch):
        monkeypatch.setattr(settings, "S3_ENDPOINT_URL", None)
        assert describe_storage_configuration()["endpoint_host"] is None

    def test_local_without_mount_is_ephemeral_and_warns(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setattr(storage_service, "_path_on_mounted_volume", lambda path: False)

        summary = describe_storage_configuration()

        assert summary["backend"] == "local"
        assert summary["durability"] == "ephemeral"
        assert summary["warning"] == STORAGE_WARNING_EPHEMERAL
        assert summary["upload_dir_configured"] is True
        assert summary["upload_dir_exists"] is False
        assert summary["bucket"] is None and summary["endpoint_host"] is None
        assert str(tmp_path) not in json.dumps(summary)
        StorageStatus(**summary)

    def test_local_on_volume_has_no_warning(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
        monkeypatch.setattr(storage_service, "_path_on_mounted_volume", lambda path: True)

        summary = describe_storage_configuration()

        assert summary["durability"] == "volume"
        assert summary["upload_dir_exists"] is True
        assert summary["upload_dir_on_mounted_volume"] is True
        assert summary["warning"] is None

    def test_local_unreadable_mount_state_is_unknown(self, monkeypatch):
        monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
        monkeypatch.delenv("UPLOAD_DIR", raising=False)
        monkeypatch.setattr(storage_service, "_path_on_mounted_volume", lambda path: None)

        summary = describe_storage_configuration()

        assert summary["durability"] == "unknown"
        assert summary["warning"] == STORAGE_WARNING_UNKNOWN
        assert summary["upload_dir_configured"] is False

    def test_does_not_create_the_upload_dir(self, monkeypatch, tmp_path):
        target = tmp_path / "never-created"
        monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
        monkeypatch.setenv("UPLOAD_DIR", str(target))

        describe_storage_configuration()

        assert not target.exists()


@pytest.mark.unit
class TestMountHeuristic:
    def test_mount_point_ancestor_counts(self, monkeypatch, tmp_path):
        mount = str(tmp_path)
        real_ismount = os.path.ismount
        monkeypatch.setattr(os.path, "ismount", lambda p: p == os.path.realpath(mount) or real_ismount(p))
        assert _path_on_mounted_volume(str(tmp_path / "missing" / "deeper")) is True

    def test_root_alone_does_not_count(self, monkeypatch):
        root = os.path.abspath(os.sep)
        monkeypatch.setattr(os.path, "ismount", lambda p: p == root)
        assert _path_on_mounted_volume(os.path.join(root, "app", "uploads")) is False

    def test_os_error_is_unknown(self, monkeypatch):
        def _raise(_p):
            raise OSError("unreadable")

        monkeypatch.setattr(os.path, "ismount", _raise)
        monkeypatch.setattr(os.path, "exists", lambda p: True)
        assert _path_on_mounted_volume("/app/uploads") is None

    @pytest.mark.parametrize(
        "url,expected",
        [
            (None, None),
            ("", None),
            ("https://objects.example.test", "objects.example.test"),
            (ENDPOINT_WITH_SECRETS, "objects.example.test"),
            ("not a url", "(unparseable endpoint)"),
            ("objects.example.test", "(unparseable endpoint)"),
        ],
    )
    def test_endpoint_hostname(self, url, expected):
        assert _endpoint_hostname(url) == expected


@pytest.mark.api
class TestStorageOnHealthDetailed:
    def test_detailed_health_carries_storage_without_secrets(self, client: TestClient, s3_settings):
        response = client.get("/health/detailed")

        assert response.status_code == status.HTTP_200_OK
        storage = response.json()["checks"]["storage"]
        assert storage["backend"] == "s3"
        assert storage["durability"] == "durable"
        # Unauthenticated: the document store's address is never published here.
        assert storage["bucket"] is None
        assert storage["endpoint_host"] is None
        assert storage["region"] is None
        assert "fictional-docs-bucket" not in response.text
        assert "objects.example.test" not in response.text
        _assert_no_secret(response.json())

    def test_detailed_health_survives_a_storage_read_failure(self, client: TestClient, monkeypatch):
        def _boom(**_kwargs):
            raise RuntimeError("broken")

        monkeypatch.setattr(storage_service, "describe_storage_configuration", _boom)
        response = client.get("/health/detailed")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["checks"]["storage"] == {"status": "unknown", "error": "RuntimeError"}


@pytest.mark.api
@pytest.mark.requires_db
class TestStorageOnSetupHealth:
    def test_setup_health_carries_storage(self, client: TestClient, auth_headers: dict, s3_settings):
        response = client.get("/api/v1/setup/health", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        storage = response.json()["storage"]
        assert storage["backend"] == "s3"
        assert storage["bucket"] == "fictional-docs-bucket"
        assert storage["durability"] == "durable"
        _assert_no_secret(response.json())

    def test_setup_health_storage_null_on_read_failure(self, client: TestClient, auth_headers: dict, monkeypatch):
        from app.api.endpoints import setup as setup_endpoint

        def _boom(**_kwargs):
            raise RuntimeError("broken")

        monkeypatch.setattr(setup_endpoint, "describe_storage_configuration", _boom)
        response = client.get("/api/v1/setup/health", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["storage"] is None


# ---------------------------------------------------------------------------
# Test-engineer additions: edge cases and endpoint contracts not covered above.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDescribeStorageEdges:
    def test_backend_name_is_case_insensitive(self, s3_settings, monkeypatch):
        # config.py's validator accepts "S3"; the summary must not misreport it as local.
        monkeypatch.setattr(settings, "STORAGE_BACKEND", "S3")
        assert describe_storage_configuration()["backend"] == "s3"

    def test_unset_backend_reads_as_local(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "STORAGE_BACKEND", None)
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
        assert describe_storage_configuration()["backend"] == "local"

    def test_empty_bucket_and_region_are_null_not_empty_strings(self, s3_settings, monkeypatch):
        monkeypatch.setattr(settings, "S3_BUCKET_NAME", "")
        monkeypatch.setattr(settings, "AWS_REGION", "")
        summary = describe_storage_configuration()
        assert summary["bucket"] is None
        assert summary["region"] is None

    def test_local_never_reports_the_s3_bucket_default(self, monkeypatch, tmp_path):
        # S3_BUCKET_NAME has a non-empty default even on local installs; the local
        # branch must not surface it as if documents went there.
        monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
        monkeypatch.setattr(settings, "S3_BUCKET_NAME", "fictional-docs-bucket")
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
        summary = describe_storage_configuration()
        assert summary["bucket"] is None
        assert summary["region"] is None
        assert "fictional-docs-bucket" not in json.dumps(summary)

    def test_local_summary_carries_no_credentials(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
        monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", SECRET_KEY_ID)
        monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", SECRET_ACCESS_KEY)
        monkeypatch.setattr(settings, "S3_ENDPOINT_URL", ENDPOINT_WITH_SECRETS)
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
        _assert_no_secret(describe_storage_configuration())

    def test_every_branch_validates_against_the_schema(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
        for mounted in (True, False, None):
            monkeypatch.setattr(storage_service, "_path_on_mounted_volume", lambda path, m=mounted: m)
            StorageStatus(**describe_storage_configuration())


@pytest.mark.unit
class TestMountHeuristicEdges:
    def test_the_path_itself_being_a_mount_counts(self, monkeypatch, tmp_path):
        target = os.path.realpath(str(tmp_path))
        monkeypatch.setattr(os.path, "ismount", lambda p: p == target)
        assert _path_on_mounted_volume(str(tmp_path)) is True

    def test_no_mount_anywhere_is_false(self, monkeypatch, tmp_path):
        monkeypatch.setattr(os.path, "ismount", lambda p: False)
        assert _path_on_mounted_volume(str(tmp_path / "a" / "b")) is False

    def test_malformed_endpoint_url_yields_the_unparseable_sentinel(self):
        # urlparse raises ValueError on an unbalanced IPv6 bracket; a configured endpoint
        # must never read as None, which the panel renders as the AWS default.
        assert _endpoint_hostname("https://[::1") == "(unparseable endpoint)"

    def test_set_but_empty_upload_dir_describes_the_fallback(self, monkeypatch, tmp_path):
        fallback = tmp_path / "fallback-dir"
        fallback.mkdir()
        monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
        monkeypatch.setenv("UPLOAD_DIR", "")
        monkeypatch.setenv("UPLOAD_DIR_FALLBACK", str(fallback))
        seen = []
        real_isdir = os.path.isdir
        monkeypatch.setattr(os.path, "isdir", lambda p: seen.append(p) or real_isdir(p))

        summary = describe_storage_configuration()

        assert summary["upload_dir_exists"] is True
        assert seen == [str(fallback)]


@pytest.mark.api
class TestHealthDetailedContract:
    def test_storage_never_changes_the_overall_status(self, client: TestClient, monkeypatch):
        # SQLite has no version(), so the real database check always reads "unhealthy"
        # in tests and the status would be "degraded" whatever storage said. Stub the
        # database probe healthy so this assertion can actually fail.
        from app.db import database as database_module

        class _Row:
            def fetchone(self):
                return ("PostgreSQL 16 (fictional test stub)",)

        class _HealthySession:
            def execute(self, *_args, **_kwargs):
                return _Row()

            def close(self):
                pass

        monkeypatch.setattr(database_module, "SessionLocal", lambda: _HealthySession())
        baseline = client.get("/health/detailed").json()["status"]
        assert baseline == "healthy"

        def _boom(**_kwargs):
            raise RuntimeError("broken")

        monkeypatch.setattr(storage_service, "describe_storage_configuration", _boom)
        failed = client.get("/health/detailed").json()["status"]

        monkeypatch.setattr(
            storage_service,
            "describe_storage_configuration",
            lambda: {"backend": "local", "durability": "ephemeral", "warning": STORAGE_WARNING_EPHEMERAL},
        )
        ephemeral = client.get("/health/detailed").json()["status"]

        assert failed == baseline
        assert ephemeral == baseline

    def test_storage_is_served_without_authentication(self, client: TestClient, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "distinct-upload-dir-name"))

        response = client.get("/health/detailed")

        assert response.status_code == status.HTTP_200_OK
        storage = response.json()["checks"]["storage"]
        assert storage["backend"] == "local"
        StorageStatus(**storage)
        # No filesystem path on the unauthenticated probe.
        assert "distinct-upload-dir-name" not in response.text
        assert str(tmp_path) not in response.text

    def test_failure_error_names_the_class_only(self, client: TestClient, monkeypatch):
        def _boom(**_kwargs):
            raise RuntimeError(f"cannot read {SECRET_ACCESS_KEY}")

        monkeypatch.setattr(storage_service, "describe_storage_configuration", _boom)
        response = client.get("/health/detailed")
        assert SECRET_ACCESS_KEY not in response.text


@pytest.mark.api
@pytest.mark.requires_db
class TestSetupHealthStorageContract:
    def test_requires_authentication(self, client: TestClient, s3_settings):
        response = client.get("/api/v1/setup/health")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert "fictional-docs-bucket" not in response.text

    def test_matches_the_health_detailed_summary_except_the_location(
        self, client: TestClient, auth_headers: dict, s3_settings
    ):
        detailed = client.get("/health/detailed").json()["checks"]["storage"]
        setup = client.get("/api/v1/setup/health", headers=auth_headers).json()["storage"]
        assert setup["bucket"] == "fictional-docs-bucket"
        assert setup["endpoint_host"] == "objects.example.test"
        assert setup["region"] == "us-east-2"
        location = ("bucket", "endpoint_host", "region")
        assert {k: v for k, v in setup.items() if k not in location} == {
            k: v for k, v in detailed.items() if k not in location
        }

    def test_local_path_not_exposed_on_setup_health(
        self, client: TestClient, auth_headers: dict, monkeypatch, tmp_path
    ):
        target = tmp_path / "distinct-upload-dir-name"
        monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
        monkeypatch.setenv("UPLOAD_DIR", str(target))

        response = client.get("/api/v1/setup/health", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        storage = response.json()["storage"]
        assert storage["backend"] == "local"
        assert storage["upload_dir_configured"] is True
        assert storage["upload_dir_exists"] is False
        assert "distinct-upload-dir-name" not in response.text
        # A pure read: reporting the folder never creates it.
        assert not target.exists()

    def test_an_invalid_summary_shape_reads_as_null_not_500(self, client: TestClient, auth_headers: dict, monkeypatch):
        from app.api.endpoints import setup as setup_endpoint

        monkeypatch.setattr(
            setup_endpoint,
            "describe_storage_configuration",
            lambda: {"backend": "gcs", "durability": "durable"},
        )
        response = client.get("/api/v1/setup/health", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["storage"] is None

    def test_setup_health_writes_no_audit_row(self, client: TestClient, auth_headers: dict, db_session, s3_settings):
        from app.models.audit_log import AuditLog

        before = db_session.query(AuditLog).count()
        response = client.get("/api/v1/setup/health", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        db_session.expire_all()
        assert db_session.query(AuditLog).count() == before
