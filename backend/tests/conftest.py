import pytest
import asyncio
from typing import AsyncGenerator, Generator
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker
import faker
from faker import Faker

from app.main import app
from app.db.database import Base, get_db
from app.core.security import create_access_token
from app.models.user import User
from app.models.work_order import WorkOrder
from app.models.work_center import WorkCenter
from app.models.part import Part

# Test database URL
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

# Create test engine
engine = create_async_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

# Create async session factory
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# Faker instance
fake = Faker()


@pytest.fixture(scope="session")
def event_loop() -> Generator:
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="function")
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Create a fresh database session for each test."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session_factory() as session:
        yield session
        await session.rollback()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(scope="function")
def client(db_session: AsyncSession) -> TestClient:
    """Create a test client with database override."""

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def fake_data() -> Faker:
    """Return a Faker instance for generating test data."""
    return fake


@pytest.fixture
async def test_user(db_session: AsyncSession) -> User:
    """Create a test user."""
    user = User(
        email=fake.email(),
        hashed_password="$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW",  # "password"
        full_name=fake.name(),
        role="manager",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    """Create an admin user."""
    user = User(
        email="admin@werco.com",
        hashed_password="$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW",  # "password"
        full_name="Admin User",
        role="admin",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
def auth_headers(test_user: User) -> dict:
    """Return authentication headers with test user token."""
    access_token = create_access_token(data={"sub": test_user.email})
    return {"Authorization": f"Bearer {access_token}"}


@pytest.fixture
def admin_headers(admin_user: User) -> dict:
    """Return authentication headers with admin user token."""
    access_token = create_access_token(data={"sub": admin_user.email})
    return {"Authorization": f"Bearer {access_token}"}


@pytest.fixture
async def test_work_center(db_session: AsyncSession) -> WorkCenter:
    """Create a test work center."""
    work_center = WorkCenter(
        name=f"Sawing {fake.pyint(min_value=1, max_value=100)}",
        code=f"SAW-{fake.pyint(min_value=1, max_value=100)}",
        type="sawing",
        description=fake.sentence(),
        hourly_rate=fake.pyfloat(min_value=50, max_value=150),
        is_active=True,
    )
    db_session.add(work_center)
    await db_session.commit()
    await db_session.refresh(work_center)
    return work_center


@pytest.fixture
async def test_part(db_session: AsyncSession) -> Part:
    """Create a test part."""
    part = Part(
        number=f"P-{fake.pyint(min_value=10000, max_value=99999)}",
        name=fake.word(),
        description=fake.sentence(),
        type="manufactured",
        unit_of_measure="EA",
        material_type="ST-304",
        is_active=True,
    )
    db_session.add(part)
    await db_session.commit()
    await db_session.refresh(part)
    return part


@pytest.fixture
async def test_work_order(db_session: AsyncSession, test_part: Part) -> WorkOrder:
    """Create a test work order."""
    work_order = WorkOrder(
        number=f"WO-{fake.pyint(min_value=10000, max_value=99999)}",
        customer_name=fake.company(),
        part_id=test_part.id,
        quantity=fake.pyint(min_value=10, max_value=1000),
        status="planned",
        priority=2,
        due_date=fake.date_this_year(after_today=True),
    )
    db_session.add(work_order)
    await db_session.commit()
    await db_session.refresh(work_order)
    return work_order


@pytest.fixture
def sample_work_order_data(test_part: Part):
    """Return sample work order data for API requests."""
    return {
        "number": f"WO-{fake.pyint(min_value=10000, max_value=99999)}",
        "customer_name": fake.company(),
        "part_id": test_part.id,
        "quantity": fake.pyint(min_value=10, max_value=1000),
        "status": "planned",
        "priority": 2,
        "due_date": fake.date_this_year(after_today=True).isoformat(),
    }


@pytest.fixture
def sample_part_data():
    """Return sample part data for API requests."""
    return {
        "number": f"P-{fake.pyint(min_value=10000, max_value=99999)}",
        "name": fake.word(),
        "description": fake.sentence(),
        "type": "manufactured",
        "unit_of_measure": "EA",
        "material_type": "ST-304",
    }


@pytest.fixture
def sample_work_center_data():
    """Return sample work center data for API requests."""
    return {
        "name": fake.word(),
        "code": f"WC-{fake.pyint(min_value=1, max_value=100)}",
        "type": "welding",
        "description": fake.sentence(),
        "hourly_rate": fake.pyfloat(min_value=50, max_value=150),
    }


@pytest.fixture
async def test_vendor(db_session: AsyncSession):
    """Create a test vendor."""
    from app.models.purchasing import Vendor
    
    vendor = Vendor(
        name=fake.company(),
        code=f"V-{fake.pyint(min_value=100, max_value=999)}",
        contact_person=fake.name(),
        email=fake.email(),
        phone=fake.phone_number(),
        address=fake.address(),
        is_active=True
    )
    db_session.add(vendor)
    await db_session.commit()
    await db_session.refresh(vendor)
    return vendor


@pytest.fixture
def vendor_factory(db_session: AsyncSession):
    """Factory for creating vendors."""
    from app.models.purchasing import Vendor
    
    async def create_vendor(name: str, code: str = None) -> Vendor:
        vendor = Vendor(
            name=name,
            code=code or f"V-{fake.pyint(min_value=100, max_value=999)}",
            contact_person=fake.name(),
            email=fake.email(),
            phone=fake.phone_number(),
            is_active=True
        )
        db_session.add(vendor)
        await db_session.commit()
        await db_session.refresh(vendor)
        return vendor
    
    return create_vendor


@pytest.fixture
def part_factory(db_session: AsyncSession):
    """Factory for creating parts."""
    from app.models.part import Part
    from random import choice
    
    async def create_part(part_number: str, name: str = None) -> Part:
        part = Part(
            part_number=part_number,
            name=name or fake.word(),
            description=fake.sentence(),
            type=choice(["manufactured", "purchased"]),
            unit_of_measure="EA",
            is_active=True
        )
        db_session.add(part)
        await db_session.commit()
        await db_session.refresh(part)
        return part
    
    return create_part
