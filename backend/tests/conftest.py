import os
from typing import Generator

import pytest
from faker import Faker
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

WORKER_ID = os.getenv("PYTEST_XDIST_WORKER", "master")
if WORKER_ID == "master":
    TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "sqlite:///./test.db")
else:
    TEST_DATABASE_URL = f"sqlite:///./test_{WORKER_ID}.db"

os.environ["TEST_DATABASE_URL"] = TEST_DATABASE_URL
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["SECRET_KEY"] = "test-secret-key-abcdefghijklmnopqrstuvwxyz123456"
os.environ["REFRESH_TOKEN_SECRET_KEY"] = "test-refresh-secret-key-abcdefghijklmnopqrstuvwxyz123456"
os.environ["ENVIRONMENT"] = "test"
os.environ["SENTRY_DSN"] = ""

from app.main import app
from app.core.security import create_access_token, get_password_hash
from app.db.database import Base, get_db
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

# Test password (hashed version of "TestPassword123!")
TEST_PASSWORD = "TestPassword123!"
TEST_PASSWORD_HASH = get_password_hash(TEST_PASSWORD)


@pytest.fixture(scope="function")
def db_session() -> Generator[Session, None, None]:
    """Create a fresh database session for each test."""
    # Create all tables
    Base.metadata.create_all(bind=engine)
    
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        # Drop all tables after test
        Base.metadata.drop_all(bind=engine)


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
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def test_user_credentials() -> dict:
    """Return test user credentials for login."""
    return {
        "email": "testuser@werco.com",
        "password": TEST_PASSWORD
    }


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
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def inactive_user_credentials() -> dict:
    """Return inactive user credentials."""
    return {
        "email": "inactive@werco.com",
        "password": TEST_PASSWORD
    }


@pytest.fixture
def auth_headers(test_user: User) -> dict:
    """Return authentication headers with test user token."""
    access_token = create_access_token(subject=test_user.id)
    return {"Authorization": f"Bearer {access_token}", "X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def admin_headers(admin_user: User) -> dict:
    """Return authentication headers with admin user token."""
    access_token = create_access_token(subject=admin_user.id)
    return {"Authorization": f"Bearer {access_token}", "X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def manager_headers(test_user: User) -> dict:
    """Return authentication headers with manager user token."""
    access_token = create_access_token(subject=test_user.id)
    return {"Authorization": f"Bearer {access_token}", "X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def operator_headers(operator_user: User) -> dict:
    """Return authentication headers with operator user token."""
    access_token = create_access_token(subject=operator_user.id)
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
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return {
        "id": user.id,
        "email": user.email,
        "version": getattr(user, 'version', 0)
    }


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
        name=fake.word(),
        description=fake.sentence(),
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
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
        due_date=fake.date_this_year(after_today=True),
    )
    db_session.add(work_order)

    db_session.flush()
    operation = WorkOrderOperation(
        work_order_id=work_order.id,
        work_center_id=test_work_center.id,
        sequence=10,
        name="Test Operation",
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
        "due_date": fake.date_this_year(after_today=True).isoformat(),
    }


@pytest.fixture
def sample_part_data():
    """Return sample part data for API requests."""
    return {
        "part_number": f"P-{fake.pyint(min_value=10000, max_value=99999)}",
        "name": fake.word(),
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
        is_active=True
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
            is_active=True
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
            name=name or fake.word(),
            description=fake.sentence(),
            part_type=choice(["manufactured", "purchased"]),
            unit_of_measure="each",
            is_active=True
        )
        db_session.add(part)
        db_session.commit()
        db_session.refresh(part)
        return part
    
    return create_part
