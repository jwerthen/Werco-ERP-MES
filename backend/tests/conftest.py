import os
import random
from datetime import date, timedelta
from typing import Generator

import pytest
from faker import Faker
from fastapi.testclient import TestClient
from passlib.context import CryptContext
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

WORKER_ID = os.getenv("PYTEST_XDIST_WORKER", "master")

# Per-process IN-MEMORY sqlite. SQLite is this suite's intentional backend (production
# is Supabase Postgres); see docs/DEVELOPMENT.md -> "Why the tests run on SQLite".
#
# This used to be a FILE: sqlite:///./test_{worker}.db. ``db_session`` runs
# ``create_all`` + ``drop_all`` over 136 tables for EVERY test, so on a durable
# filesystem the suite spent its time in fsync, not in Python -- measured at 288.8 ms
# per fixture cycle on disk versus ~3 ms here, with kernel time across the run
# collapsing from 391.8s to ~13s. Per-test create/drop semantics are deliberately
# UNCHANGED; only the storage moves.
#
# The shared-cache URI is load-bearing and must not be simplified to "sqlite://".
# A bare in-memory URL gives every new engine its own EMPTY database, and three
# things here open a second engine against this URL: app/db/database.py's own
# QueuePool engine, tests/api/export_audit_helpers.py (which reads through a separate
# engine precisely so it sees only COMMITTED rows -- invariant 2), and the duplicate
# ``tests.conftest`` module copy imported by test_completion_concurrency.py and
# test_completion_perf_batch9.py. Under a bare URL all three would silently address
# different databases. The shared cache keeps them addressing one, exactly as the
# single file did.
#
# Lifetime: a shared-cache in-memory database exists only while at least one
# connection to it is open. The engine below uses StaticPool, which holds its single
# connection open for the life of the process, so the database survives between tests.
# Consequence: NEVER call engine.dispose() on this engine. Under the old file-backed DB
# that was harmless; here it closes the last connection and discards the schema and every
# row in it. (Nothing does today -- every dispose() in the suite belongs to a migration
# test's scratch-file engine or to export_audit_helpers.py's own short-lived engine.)
_MEMORY_DB_URL = f"sqlite:///file:werco_test_{WORKER_ID}?mode=memory&cache=shared&uri=true"

if WORKER_ID == "master":
    TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", _MEMORY_DB_URL)
else:
    TEST_DATABASE_URL = _MEMORY_DB_URL

os.environ["TEST_DATABASE_URL"] = TEST_DATABASE_URL
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["SECRET_KEY"] = "test-secret-key-abcdefghijklmnopqrstuvwxyz123456"
os.environ["REFRESH_TOKEN_SECRET_KEY"] = "test-refresh-secret-key-abcdefghijklmnopqrstuvwxyz123456"
os.environ["ENVIRONMENT"] = "test"
os.environ["SENTRY_DSN"] = ""

import app.core.security as _security
from app.core.security import create_access_token, get_password_hash
from app.db.database import Base, get_db
from app.main import app
from app.models.company import Company
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderOperation

# Create test engine
if "sqlite" in TEST_DATABASE_URL:
    engine = create_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
else:
    engine = create_engine(TEST_DATABASE_URL)

# Create session factory
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Faker instance
fake = Faker()


def _fake_part_name() -> str:
    """Generate a faker-driven part name that is always schema-valid.

    ``PartBase.name`` (inherited by ``PartResponse``/``PartCreate``) requires
    ``min_length=2``. Bare ``fake.word()`` returns a 1-character word (e.g. "a",
    "I") ~0.2% of the time, which is genuinely invalid business data and makes
    any test that round-trips a part through ``PartResponse`` flake with a
    ``ResponseValidationError`` (or a 422 on POST). Appending " part" guarantees
    the result is >= "i part" (6 chars) while preserving faker-driven variety.
    """
    return f"{fake.word()} part"


@pytest.fixture(autouse=True)
def _seed_random_fixture_data():
    """Make all faker/random-driven fixture data deterministic per test.

    There is otherwise no Faker/random seeding anywhere in the suite, so any
    data-shape flake (e.g. a 1-char ``fake.word()``) surfaces as a ~1-in-500
    ghost that vanishes on re-run. Seeding both Faker and the stdlib ``random``
    module (used by ``part_factory`` via ``from random import choice``) before
    each test turns such failures into deterministic, reproducible ones.

    A constant seed is safe for within-test uniqueness: tables are dropped
    between tests (``db_session``) and xdist uses per-worker SQLite DBs, so the
    same generated values never collide across tests. Within a single test the
    generators still advance normally, so multiple fixture calls stay distinct.
    """
    Faker.seed(0)
    random.seed(0)
    yield


# TEST-ONLY bcrypt work factor. Must stay ABOVE the TEST_PASSWORD_HASH line below,
# which is the suite's first hash.
#
# bcrypt is deliberately slow, and at the production cost factor (passlib's default,
# rounds=12) one hash or verify measures ~208 ms on this hardware. That is correct in
# production and pure waste in a test suite that re-hashes the same fixture password
# and re-verifies it on every password-carrying endpoint.
#
# rounds=4 is bcrypt's documented minimum. It changes only HOW LONG the KDF takes --
# not the algorithm, not the salt, not the ``$2b$`` format, not what
# ``verify_password`` accepts -- so every hash/verify assertion in the suite means
# exactly what it meant before.
#
# PRODUCTION IS NOT AFFECTED. ``app/core/security.py`` is untouched and still builds
# its context with no rounds argument; this rebinds the module global only, inside
# the test process. ``get_password_hash``/``verify_password`` resolve ``pwd_context``
# at CALL time, and nothing imports the object itself by value (checked), so the
# rebind reaches every caller. ``tests/test_bcrypt_cost_factor_is_test_only.py``
# fails if anyone ever promotes this into the app package.
_security.pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=4)

# The shared fixture password, and its bcrypt hash for seeding user rows.
#
# This value must PASS ``schemas.user.validate_password_strength``. It is only ever
# hashed here or sent as a login / ``current_password`` value -- none of which runs
# the validator -- so a non-compliant string would work today and detonate later,
# the first time someone reuses the fixture as a ``new_password`` or a create
# payload. It was "TestPassword123!" until 2026-07-29, when the blocklist expansion
# left it two steps from exactly that trap (it contains "password").
#
# Keep it >= 12 characters and free of every entry in
# ``app.schemas.user._COMMON_PASSWORD_PATTERNS``.
TEST_PASSWORD = "Zephyr9!Quill-Test"
TEST_PASSWORD_HASH = get_password_hash(TEST_PASSWORD)


@pytest.fixture(autouse=True)
def _allow_ai_egress_by_default(monkeypatch):
    """Neutralize the per-company AI-egress kill switch for the whole suite.

    ``run_llm_task`` now consults ``llm_client._ai_egress_allowed(company_id)``
    before any Anthropic call. That helper opens its own short-lived session via
    ``_usage_session_factory`` and fails CLOSED (returns False) whenever it can't
    affirmatively read ``Company.allow_ai_egress`` -- which is exactly what
    happens in tests that stub ``get_anthropic_client`` / ``_usage_session_factory``
    but exercise a real ``run_llm_task`` path: there is no readable company row,
    so the call would raise ``LLMEgressDisabledError`` and break.

    This mirrors how telemetry's ``_usage_session_factory`` is already neutralized
    per-test: we patch the single egress seam to allow by default. Tests that need
    the OFF path simply ``monkeypatch.setattr(llm_client, "_ai_egress_allowed",
    lambda company_id=None: False)`` (or pass a callable) -- because they share
    this fixture's ``monkeypatch`` instance, that later setattr wins and is undone
    at teardown. Tests that stub ``run_llm_task`` itself never reach the gate and
    are unaffected.
    """
    import app.services.llm_client as llm_client

    monkeypatch.setattr(llm_client, "_ai_egress_allowed", lambda company_id=None: True)
    yield


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Clear slowapi rate-limit counters before every test.

    Rate limiting is enabled in the test environment (settings.RATE_LIMIT_ENABLED
    defaults True), and the limiter's in-memory storage persists for the life of a
    worker process. Without a reset, the stricter per-path auth limits (e.g.
    /api/v1/auth/login at 5/min, keyed by the fixed TestClient address) would
    accumulate across unrelated tests and fire spurious 429s. Resetting per test
    isolates each test's request budget so only tests that intentionally exceed a
    limit see a 429.
    """
    try:
        from app.main import app

        limiter = getattr(app.state, "limiter", None)
        if limiter is not None:
            try:
                limiter.reset()
            except Exception:
                storage = getattr(limiter, "_storage", None)
                if storage is not None:
                    storage.reset()
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _reset_login_throttles():
    """Clear every per-IP FAILED-login counter before and after each test.

    ``app/core/login_throttle.py`` holds one ``FailedLoginThrottle`` per unauthenticated
    login route (``/auth/employee-login``, ``/auth/login``), each a MODULE-LEVEL singleton
    whose memory-mode store lives for the life of the worker process — so a test that
    spends a route's budget leaves a 429 armed for whatever unrelated test the xdist worker
    picks up next, keyed on the fixed TestClient address they all share. Same hazard the
    slowapi reset above exists for, one layer down.

    EVERY instance is reset, discovered by walking the module rather than by name. Naming
    them is how this leaked the first time: a suite-local fixture reset the kiosk counter
    only, so when ``/auth/login`` got its own instance the new counter accumulated across a
    whole file and produced failures that reproduced in file order and vanished when the
    test was run alone. Walking the module isolates the NEXT route's throttle on the day it
    is added rather than the day it corrupts somebody's test.

    Redis keys are deliberately untouched (``reset()`` clears the in-memory store and drops
    the client); the suite runs in memory mode and production never calls this.
    """
    try:
        from app.core import login_throttle

        throttles = [
            value for value in vars(login_throttle).values() if isinstance(value, login_throttle.FailedLoginThrottle)
        ]
    except Exception:  # pragma: no cover - import shape changed
        throttles = []
    for throttle in throttles:
        throttle.reset()
    yield
    for throttle in throttles:
        throttle.reset()


@pytest.fixture(scope="function")
def db_session() -> Generator[Session, None, None]:
    """Create a fresh database session for each test."""
    # Create all tables
    Base.metadata.create_all(bind=engine)

    session = TestingSessionLocal()
    # Seed the default test company (required for all tenant-scoped models)
    company = session.query(Company).filter(Company.id == 1).first()
    if not company:
        company = Company(id=1, name="Werco Manufacturing", slug="werco", is_active=True)
        session.add(company)
        session.commit()
    try:
        yield session
    finally:
        session.close()
        # Drop all tables after test
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def test_company(db_session: Session) -> Company:
    """Return the default test company."""
    return db_session.query(Company).filter(Company.id == 1).first()


@pytest.fixture(scope="function")
def client(db_session: Session) -> TestClient:
    """Create a test client with database override."""

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def fake_data() -> Faker:
    """Return a Faker instance for generating test data."""
    return fake


@pytest.fixture
def test_user(db_session: Session) -> User:
    """Create a test user."""
    user = User(
        email="testuser@werco.com",
        employee_id="EMP-TEST-001",
        first_name="Test",
        last_name="User",
        hashed_password=TEST_PASSWORD_HASH,
        role=UserRole.MANAGER,
        is_active=True,
        company_id=1,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def test_user_credentials() -> dict:
    """Return test user credentials for login."""
    return {"email": "testuser@werco.com", "password": TEST_PASSWORD}


@pytest.fixture
def admin_user(db_session: Session) -> User:
    """Create an admin user."""
    user = User(
        email="admin@werco.com",
        employee_id="EMP-ADMIN-001",
        first_name="Admin",
        last_name="User",
        hashed_password=TEST_PASSWORD_HASH,
        role=UserRole.ADMIN,
        is_active=True,
        company_id=1,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def operator_user(db_session: Session) -> User:
    """Create an operator user."""
    user = User(
        email="operator@werco.com",
        employee_id="EMP-OP-001",
        first_name="Operator",
        last_name="User",
        hashed_password=TEST_PASSWORD_HASH,
        role=UserRole.OPERATOR,
        is_active=True,
        company_id=1,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def supervisor_user(db_session: Session) -> User:
    """Create a supervisor user.

    Distinct from the manager ``test_user`` so RBAC tests can pin the
    SUPERVISOR role explicitly (supervisors have no ``users:*`` access on the
    backend). Mirrors ``operator_user``/``admin_user``.
    """
    user = User(
        email="supervisor@werco.com",
        employee_id="EMP-SUP-001",
        first_name="Supervisor",
        last_name="User",
        hashed_password=TEST_PASSWORD_HASH,
        role=UserRole.SUPERVISOR,
        is_active=True,
        company_id=1,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def inactive_user(db_session: Session) -> User:
    """Create an inactive user."""
    user = User(
        email="inactive@werco.com",
        employee_id="EMP-INACTIVE-001",
        first_name="Inactive",
        last_name="User",
        hashed_password=TEST_PASSWORD_HASH,
        role=UserRole.OPERATOR,
        is_active=False,
        company_id=1,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def inactive_user_credentials() -> dict:
    """Return inactive user credentials."""
    return {"email": "inactive@werco.com", "password": TEST_PASSWORD}


@pytest.fixture
def auth_headers(test_user: User) -> dict:
    """Return authentication headers with test user token."""
    access_token = create_access_token(subject=test_user.id, company_id=test_user.company_id)
    return {"Authorization": f"Bearer {access_token}", "X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def admin_headers(admin_user: User) -> dict:
    """Return authentication headers with admin user token."""
    access_token = create_access_token(subject=admin_user.id, company_id=admin_user.company_id)
    return {"Authorization": f"Bearer {access_token}", "X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def manager_headers(test_user: User) -> dict:
    """Return authentication headers with manager user token."""
    access_token = create_access_token(subject=test_user.id, company_id=test_user.company_id)
    return {"Authorization": f"Bearer {access_token}", "X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def operator_headers(operator_user: User) -> dict:
    """Return authentication headers with operator user token."""
    access_token = create_access_token(subject=operator_user.id, company_id=operator_user.company_id)
    return {"Authorization": f"Bearer {access_token}", "X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def supervisor_headers(supervisor_user: User) -> dict:
    """Return authentication headers with supervisor user token."""
    access_token = create_access_token(subject=supervisor_user.id, company_id=supervisor_user.company_id)
    return {"Authorization": f"Bearer {access_token}", "X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def created_user(db_session: Session) -> dict:
    """Create a user and return its data."""
    user = User(
        email="created@werco.com",
        employee_id="EMP-CREATED-001",
        first_name="Created",
        last_name="User",
        hashed_password=TEST_PASSWORD_HASH,
        role=UserRole.OPERATOR,
        is_active=True,
        company_id=1,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return {"id": user.id, "email": user.email, "version": getattr(user, 'version', 0)}


@pytest.fixture
def test_work_center(db_session: Session) -> WorkCenter:
    """Create a test work center."""
    work_center = WorkCenter(
        name=f"Sawing {fake.pyint(min_value=1, max_value=100)}",
        code=f"SAW-{fake.pyint(min_value=1, max_value=100)}",
        work_center_type="welding",
        description=fake.sentence(),
        hourly_rate=fake.pyfloat(min_value=50, max_value=150),
        is_active=True,
        company_id=1,
    )
    db_session.add(work_center)
    db_session.commit()
    db_session.refresh(work_center)
    return work_center


@pytest.fixture
def test_part(db_session: Session) -> Part:
    """Create a test part."""
    part = Part(
        part_number=f"P-{fake.pyint(min_value=10000, max_value=99999)}",
        name=_fake_part_name(),
        description=fake.sentence(),
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
    )
    db_session.add(part)
    db_session.commit()
    db_session.refresh(part)
    return part


@pytest.fixture
def test_work_order(db_session: Session, test_part: Part, test_work_center: WorkCenter) -> WorkOrder:
    """Create a test work order."""
    work_order = WorkOrder(
        work_order_number=f"WO-{fake.pyint(min_value=10000, max_value=99999)}",
        customer_name=fake.company(),
        part_id=test_part.id,
        quantity_ordered=fake.pyint(min_value=10, max_value=1000),
        status="draft",
        priority=2,
        due_date=date.today() + timedelta(days=30),
        company_id=1,
    )
    db_session.add(work_order)

    db_session.flush()
    operation = WorkOrderOperation(
        work_order_id=work_order.id,
        work_center_id=test_work_center.id,
        sequence=10,
        name="Test Operation",
        company_id=1,
    )
    db_session.add(operation)
    db_session.commit()
    db_session.refresh(work_order)
    return work_order


@pytest.fixture
def sample_work_order_data(test_part: Part):
    """Return sample work order data for API requests."""
    return {
        "part_id": test_part.id,
        "quantity_ordered": fake.pyint(min_value=10, max_value=1000),
        "customer_name": fake.company(),
        "priority": 2,
        "due_date": (date.today() + timedelta(days=30)).isoformat(),
    }


@pytest.fixture
def sample_part_data():
    """Return sample part data for API requests."""
    return {
        "part_number": f"P-{fake.pyint(min_value=10000, max_value=99999)}",
        "name": _fake_part_name(),
        "description": fake.sentence(),
        "part_type": "manufactured",
        "unit_of_measure": "each",
    }


@pytest.fixture
def sample_work_center_data():
    """Return sample work center data for API requests."""
    return {
        "name": fake.word(),
        "code": f"WC-{fake.pyint(min_value=1, max_value=100)}",
        "work_center_type": "welding",
        "description": fake.sentence(),
        "hourly_rate": fake.pyfloat(min_value=50, max_value=150),
    }


@pytest.fixture
def test_vendor(db_session: Session):
    """Create a test vendor."""
    from app.models.purchasing import Vendor

    vendor = Vendor(
        name=fake.company(),
        code=f"V-{fake.pyint(min_value=100, max_value=999)}",
        contact_name=fake.name(),
        email=fake.email(),
        phone=fake.phone_number(),
        is_active=True,
        company_id=1,
    )
    db_session.add(vendor)
    db_session.commit()
    db_session.refresh(vendor)
    return vendor


@pytest.fixture
def vendor_factory(db_session: Session):
    """Factory for creating vendors."""
    from app.models.purchasing import Vendor

    def create_vendor(name: str, code: str = None) -> Vendor:
        vendor = Vendor(
            name=name,
            code=code or f"V-{fake.pyint(min_value=100, max_value=999)}",
            contact_name=fake.name(),
            email=fake.email(),
            phone=fake.phone_number(),
            is_active=True,
            company_id=1,
        )
        db_session.add(vendor)
        db_session.commit()
        db_session.refresh(vendor)
        return vendor

    return create_vendor


@pytest.fixture
def part_factory(db_session: Session):
    """Factory for creating parts."""
    from random import choice

    def create_part(part_number: str, name: str = None) -> Part:
        part = Part(
            part_number=part_number,
            name=name or _fake_part_name(),
            description=fake.sentence(),
            part_type=choice(["manufactured", "purchased"]),
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        db_session.add(part)
        db_session.commit()
        db_session.refresh(part)
        return part

    return create_part
