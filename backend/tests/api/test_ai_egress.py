"""API + migration coverage for the per-company ``allow_ai_egress`` kill switch.

Three groups:

1. ``PUT /companies/me/ai-egress`` — the ADMIN-only operator toggle. Proves the
   role tightening (ADMIN/superuser 200; MANAGER and OPERATOR 403), that the flag
   flips and persists on ``GET /companies/me``, that a real change writes BOTH an
   UPDATE and a STATUS_CHANGE audit row while a no-op write skips the
   STATUS_CHANGE, and that only the caller's OWN company is ever mutated.

2. ``POST /qms-standards/{id}/upload-pdf`` — the endpoint-layer degrade: when AI
   egress is OFF for the company the extraction is refused with 403 (a policy
   decision, not a server error). ``run_llm_task`` is stubbed to raise
   ``LLMEgressDisabledError`` so no Anthropic call happens.

3. The 054 migration backfill — driven directly against an isolated SQLite engine
   via an Alembic ``Operations`` context (the importlib precedent from
   tests/api/test_completion_perf_batch9.py): existing tenants end ON after the
   first-time add-column, the add+backfill are SKIPPED when the column already
   exists (the create_all bootstrap path -> seeded companies stay OFF), and the
   column round-trips on downgrade.

The default seeded company is id=1 (tests/conftest.py); company 2 is created on
demand. Audit rows carry a tamper-evident hash chain -- we only READ them back,
never insert directly.
"""

import importlib.util
import io
from pathlib import Path

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

import app.api.endpoints.qms_standards as qms_endpoint
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.qms_standard import QMSStandard
from app.models.user import User, UserRole
from app.services.llm_client import LLMEgressDisabledError

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

AI_EGRESS_URL = "/api/v1/companies/me/ai-egress"
COMPANY_ME_URL = "/api/v1/companies/me"

COMPANY_A = 1
COMPANY_B = 2

# Module-level counter -> globally unique natural keys across xdist worker DBs.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(
            id=company_id,
            name=f"Company {company_id}",
            slug=f"company-{company_id}",
            is_active=True,
        )
        db.add(company)
        db.commit()
    return company


def _make_user(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    role: UserRole = UserRole.ADMIN,
    is_superuser: bool = False,
) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"egress-{n}@co{company_id}.test",
        employee_id=f"EGR-{n:05d}",
        first_name="Egress",
        last_name=f"C{company_id}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",  # tokens minted directly; never used for login
        role=role,
        is_active=True,
        is_superuser=is_superuser,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers_for(user: User, *, active_company_id: int = None) -> dict:
    cid = active_company_id if active_company_id is not None else user.company_id
    token = create_access_token(subject=user.id, company_id=cid)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


# ===========================================================================
# 1. PUT /companies/me/ai-egress — ADMIN-only operator toggle
# ===========================================================================
class TestAIEgressToggleRBAC:
    def test_admin_can_enable_and_response_reflects_it(self, client: TestClient, db_session: Session):
        admin = _make_user(db_session, role=UserRole.ADMIN)
        resp = client.put(AI_EGRESS_URL, headers=_headers_for(admin), json={"allow_ai_egress": True})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["allow_ai_egress"] is True

        # Persisted on the company row.
        db_session.expire_all()
        company = db_session.query(Company).filter(Company.id == COMPANY_A).first()
        assert company.allow_ai_egress is True

    def test_superuser_can_toggle(self, client: TestClient, db_session: Session):
        """A superuser (role aside) is authorized by require_role."""
        su = _make_user(db_session, role=UserRole.OPERATOR, is_superuser=True)
        resp = client.put(AI_EGRESS_URL, headers=_headers_for(su), json={"allow_ai_egress": True})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["allow_ai_egress"] is True

    def test_manager_is_forbidden(self, client: TestClient, db_session: Session):
        """Role tightening: MANAGER is NO LONGER authorized (was previously)."""
        manager = _make_user(db_session, role=UserRole.MANAGER)
        resp = client.put(AI_EGRESS_URL, headers=_headers_for(manager), json={"allow_ai_egress": True})
        assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text

        # The company flag is untouched (default OFF).
        db_session.expire_all()
        company = db_session.query(Company).filter(Company.id == COMPANY_A).first()
        assert company.allow_ai_egress is False

    def test_operator_is_forbidden(self, client: TestClient, db_session: Session):
        operator = _make_user(db_session, role=UserRole.OPERATOR)
        resp = client.put(AI_EGRESS_URL, headers=_headers_for(operator), json={"allow_ai_egress": True})
        assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text


class TestAIEgressTogglePersistenceAndAudit:
    def test_flip_persists_on_get_company_me(self, client: TestClient, db_session: Session):
        admin = _make_user(db_session, role=UserRole.ADMIN)
        headers = _headers_for(admin)

        # Default OFF.
        get0 = client.get(COMPANY_ME_URL, headers=headers)
        assert get0.status_code == status.HTTP_200_OK, get0.text
        assert get0.json()["allow_ai_egress"] is False

        # Enable, then read it back via GET.
        put = client.put(AI_EGRESS_URL, headers=headers, json={"allow_ai_egress": True})
        assert put.status_code == status.HTTP_200_OK, put.text
        get1 = client.get(COMPANY_ME_URL, headers=headers)
        assert get1.json()["allow_ai_egress"] is True

    def test_real_change_writes_update_and_status_change_rows(self, client: TestClient, db_session: Session):
        """Enabling (OFF -> ON) writes BOTH an UPDATE row and a STATUS_CHANGE row
        (ai_egress_disabled -> ai_egress_enabled) on the tamper-evident trail."""
        admin = _make_user(db_session, role=UserRole.ADMIN)

        resp = client.put(AI_EGRESS_URL, headers=_headers_for(admin), json={"allow_ai_egress": True})
        assert resp.status_code == status.HTTP_200_OK, resp.text

        rows = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "company", AuditLog.resource_id == COMPANY_A)
            .all()
        )
        actions = [r.action for r in rows]
        assert "UPDATE" in actions
        assert "STATUS_CHANGE" in actions

        status_row = next(r for r in rows if r.action == "STATUS_CHANGE")
        assert status_row.old_values == {"status": "ai_egress_disabled"}
        assert status_row.new_values == {"status": "ai_egress_enabled"}
        assert status_row.company_id == COMPANY_A

    def test_no_op_write_skips_status_change_row(self, client: TestClient, db_session: Session):
        """Setting the SAME value (OFF -> OFF) writes NO STATUS_CHANGE row -- the
        security event only fires on an actual change.

        ``log_update`` additionally suppresses its own row when old == new (no
        diff to record), so a true no-op writes nothing at all on the company.
        The headline invariant here is the absence of the STATUS_CHANGE event."""
        admin = _make_user(db_session, role=UserRole.ADMIN)

        # Company defaults OFF; write False again (no change).
        resp = client.put(AI_EGRESS_URL, headers=_headers_for(admin), json={"allow_ai_egress": False})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["allow_ai_egress"] is False

        rows = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "company", AuditLog.resource_id == COMPANY_A)
            .all()
        )
        actions = [r.action for r in rows]
        # The security-relevant status change must NOT be recorded for a no-op.
        assert "STATUS_CHANGE" not in actions
        # And since nothing actually changed, log_update's diff suppression means
        # no UPDATE row either -- a no-op leaves a clean trail.
        assert "UPDATE" not in actions

    def test_only_callers_company_changes(self, client: TestClient, db_session: Session):
        """Tenant safety: toggling company A leaves company B's flag untouched.
        The company is taken from the token, never the request body."""
        _ensure_company(db_session, COMPANY_B)
        admin_a = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)

        resp = client.put(AI_EGRESS_URL, headers=_headers_for(admin_a), json={"allow_ai_egress": True})
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.expire_all()
        company_a = db_session.query(Company).filter(Company.id == COMPANY_A).first()
        company_b = db_session.query(Company).filter(Company.id == COMPANY_B).first()
        assert company_a.allow_ai_egress is True
        assert company_b.allow_ai_egress is False


# ===========================================================================
# 2. POST /qms-standards/{id}/upload-pdf — endpoint degrade: 403 when egress OFF
# ===========================================================================
def _text_pdf_bytes(body: str) -> bytes:
    """A real, pypdf-parseable PDF whose extracted text exceeds the 50-char floor
    the qms upload endpoint requires before it calls the model."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    y = 720
    for line in body.split("\n"):
        c.drawString(72, y, line)
        y -= 16
    c.showPage()
    c.save()
    return buf.getvalue()


class TestQMSUploadEgressOff:
    def test_upload_pdf_returns_403_when_egress_disabled(self, client: TestClient, db_session: Session, monkeypatch):
        admin = _make_user(db_session, role=UserRole.ADMIN)
        standard = QMSStandard(
            company_id=COMPANY_A,
            name="AS9100D",
            version="2016",
            standard_body="SAE",
            is_active=True,
        )
        db_session.add(standard)
        db_session.commit()
        db_session.refresh(standard)

        # Stub the model call to raise the egress kill-switch error -- no Anthropic
        # call happens, and the endpoint must translate it to a 403.
        def _raise(*args, **kwargs):
            raise LLMEgressDisabledError(company_id=COMPANY_A)

        monkeypatch.setattr(qms_endpoint, "run_llm_task", _raise)

        pdf = _text_pdf_bytes(
            "Quality Manual clause 4.1 Understanding the organization and its context. "
            "Clause 8.5.2 Identification and traceability requirements apply here."
        )
        resp = client.post(
            f"/api/v1/qms-standards/{standard.id}/upload-pdf",
            headers=_headers_for(admin),
            files={"file": ("manual.pdf", io.BytesIO(pdf), "application/pdf")},
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
        assert "disabled" in resp.json()["detail"].lower()
        assert "allow_ai_egress" in resp.json()["detail"]


# ===========================================================================
# 3. Migration 054 backfill — grandfather ON, new-tenants OFF, guard-skip
# ===========================================================================
MIGRATION_054_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "054_company_allow_ai_egress.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig054", str(MIGRATION_054_PATH))
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


def _bind_op(mig, conn):
    """Bind the migration module's ``op`` proxy to an Alembic Operations context
    over ``conn`` so ``upgrade()``/``downgrade()`` run against our test engine."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ctx = MigrationContext.configure(conn)
    mig.op = Operations(ctx)


class TestMigration054Backfill:
    def test_existing_tenants_grandfathered_on_after_first_add(self):
        """First-time migration on an established DB (column does NOT yet exist):
        the add-column runs and the backfill flips every existing tenant ON."""
        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT)"))
            conn.execute(text("INSERT INTO companies (id, name) VALUES (1, 'Acme'), (2, 'Beta')"))

            mig = _load_migration()
            _bind_op(mig, conn)
            mig.upgrade()

            rows = dict(conn.execute(text("SELECT id, allow_ai_egress FROM companies ORDER BY id")).fetchall())
            # Both pre-existing tenants are grandfathered ON.
            assert rows[1] in (1, True)
            assert rows[2] in (1, True)

    def test_guard_skip_when_column_already_exists_leaves_seeded_off(self):
        """create_all bootstrap path: the column ALREADY exists (default false). The
        guarded add AND its inner backfill are BOTH skipped, so freshly-seeded
        companies keep server_default false (new-tenants-OFF)."""
        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            conn.execute(
                text(
                    "CREATE TABLE companies "
                    "(id INTEGER PRIMARY KEY, name TEXT, allow_ai_egress BOOLEAN NOT NULL DEFAULT (0))"
                )
            )
            conn.execute(
                text("INSERT INTO companies (id, name, allow_ai_egress) VALUES (1, 'Acme', 0), (2, 'Beta', 0)")
            )

            mig = _load_migration()
            _bind_op(mig, conn)
            mig.upgrade()  # column present -> add + backfill both skipped (no-op)

            rows = dict(conn.execute(text("SELECT id, allow_ai_egress FROM companies ORDER BY id")).fetchall())
            # Backfill did NOT run -> seeded companies stay OFF.
            assert rows[1] in (0, False)
            assert rows[2] in (0, False)

    def test_new_tenant_after_migration_defaults_off(self):
        """A company INSERTed after the add-column (omitting the flag) takes the
        server_default OFF -- the grandfather UPDATE only touched rows that existed
        at migration time."""
        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT)"))
            conn.execute(text("INSERT INTO companies (id, name) VALUES (1, 'Acme')"))

            mig = _load_migration()
            _bind_op(mig, conn)
            mig.upgrade()

            # New tenant created post-migration, flag omitted.
            conn.execute(text("INSERT INTO companies (id, name) VALUES (3, 'Gamma')"))
            new_flag = conn.execute(text("SELECT allow_ai_egress FROM companies WHERE id = 3")).scalar()
            # server_default 'false' governs the new row (SQLite stores the literal).
            assert new_flag in (0, False, "false")
            # While the grandfathered tenant is ON.
            old_flag = conn.execute(text("SELECT allow_ai_egress FROM companies WHERE id = 1")).scalar()
            assert old_flag in (1, True)

    def test_downgrade_drops_the_column(self):
        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT)"))
            conn.execute(text("INSERT INTO companies (id, name) VALUES (1, 'Acme')"))

            mig = _load_migration()
            _bind_op(mig, conn)
            mig.upgrade()
            assert "allow_ai_egress" in [c[1] for c in conn.execute(text("PRAGMA table_info(companies)")).fetchall()]

            mig.downgrade()
            cols = [c[1] for c in conn.execute(text("PRAGMA table_info(companies)")).fetchall()]
            assert "allow_ai_egress" not in cols
