"""The work-center list cache is keyed PER COMPANY (invariant #1).

``GET /work-centers/`` caches its default-parameter result for 15 minutes. The QUERY was
always company-scoped; the CACHE KEY was the bare, install-wide ``work_centers:list``. So
the first tenant to request the list populated it, and for the next 15 minutes every other
tenant asking the same endpoint was served that company's machine roster verbatim -- a
cross-tenant disclosure with no query defect anywhere near it. Live wherever Redis is
configured.

Why these tests install a fake Redis
------------------------------------
``CacheService`` no-ops entirely when Redis is unavailable (``_enabled`` is False), which
is the state of the test environment -- so a test that just called the endpoint twice would
pass against the BROKEN key too, having never cached anything. These tests therefore stand
a minimal in-memory fake in for ``cache._redis`` and flip ``_enabled``, so the caching path
actually executes. Without that, this whole file is a no-op that proves nothing.

The fake implements only what ``CacheService`` uses: ``get`` / ``set`` / ``setex`` /
``delete`` / ``scan_iter``. ``scan_iter`` matters -- ``invalidate_entity`` deletes by the
glob ``work_centers:list*``, and part of the contract here is that the pattern still
matches the newly per-company keys.
"""

import fnmatch

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core import cache as cache_module
from app.core.cache import (
    CacheKeys,
    _work_centers_list_key,
    invalidate_work_centers_cache,
)
from app.core.security import create_access_token
from app.models.company import Company
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


class FakeRedis:
    """Just enough Redis for ``CacheService``. Values are stored as the JSON strings
    ``CacheService`` hands over, so the round-trip through ``json.dumps``/``loads`` is
    exercised rather than bypassed."""

    def __init__(self):
        self.store: dict = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value

    def setex(self, key, ttl, value):
        self.store[key] = value

    def delete(self, *keys):
        n = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                n += 1
        return n

    def scan_iter(self, match=None):
        return [k for k in list(self.store) if match is None or fnmatch.fnmatch(k, match)]

    def exists(self, key):
        return 1 if key in self.store else 0


@pytest.fixture
def live_cache(monkeypatch):
    """Turn the cache ON for the duration of one test, backed by the fake."""
    fake = FakeRedis()
    monkeypatch.setattr(cache_module.cache, "_redis", fake, raising=False)
    monkeypatch.setattr(cache_module.cache, "_enabled", True, raising=False)
    yield fake
    monkeypatch.setattr(cache_module.cache, "_enabled", False, raising=False)


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def make_user(db: Session, *, company_id: int) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"wccache-{n}@co{company_id}.test",
        employee_id=f"WCC-{n:05d}",
        first_name="Cache",
        last_name=f"C{company_id}",
        hashed_password=TEST_PASSWORD_HASH,
        role=UserRole.ADMIN,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_work_center(db: Session, *, company_id: int, code: str) -> WorkCenter:
    _ensure_company(db, company_id)
    wc = WorkCenter(
        code=code,
        name=f"Machine {code}",
        work_center_type="fabrication",
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def _codes(response) -> set:
    return {row["code"] for row in response.json()}


def test_two_tenants_do_not_share_a_cached_work_center_roster(
    client: TestClient, db_session: Session, live_cache: FakeRedis
):
    """THE regression. Company A warms the cache; company B must not be served A's machines.

    Against the old install-wide key this fails loudly: B's response is A's roster.
    """
    user_a = make_user(db_session, company_id=COMPANY_A)
    user_b = make_user(db_session, company_id=COMPANY_B)
    wc_a = make_work_center(db_session, company_id=COMPANY_A, code=f"CACHE-A-{_next()}")
    wc_b = make_work_center(db_session, company_id=COMPANY_B, code=f"CACHE-B-{_next()}")

    # A warms the cache (default params -> the cached path).
    first_a = client.get("/api/v1/work-centers/", headers=headers_for(user_a))
    assert first_a.status_code == status.HTTP_200_OK, first_a.text
    assert wc_a.code in _codes(first_a)

    # B asks the same question and must get ITS OWN roster.
    resp_b = client.get("/api/v1/work-centers/", headers=headers_for(user_b))
    assert resp_b.status_code == status.HTTP_200_OK, resp_b.text
    codes_b = _codes(resp_b)
    assert wc_b.code in codes_b, "company B must see its own machine"
    assert wc_a.code not in codes_b, "company B must NOT be served company A's roster from the cache"

    # And A is still served A's, now genuinely from the cache.
    second_a = client.get("/api/v1/work-centers/", headers=headers_for(user_a))
    assert wc_a.code in _codes(second_a)
    assert wc_b.code not in _codes(second_a)


def test_the_cache_is_actually_being_exercised(client: TestClient, db_session: Session, live_cache: FakeRedis):
    """Guard on the guard: if the caching path silently stopped running, the isolation test
    above would pass vacuously. This pins that a per-company key really is written."""
    user_a = make_user(db_session, company_id=COMPANY_A)
    make_work_center(db_session, company_id=COMPANY_A, code=f"CACHE-W-{_next()}")

    assert client.get("/api/v1/work-centers/", headers=headers_for(user_a)).status_code == status.HTTP_200_OK

    assert _work_centers_list_key(COMPANY_A) in live_cache.store, "the default-param read must populate the cache"
    assert CacheKeys.WORK_CENTERS_LIST not in live_cache.store, "the bare install-wide key must never be written"


def test_the_key_carries_the_company_id():
    """The key shape is the whole fix -- pin it so a refactor cannot quietly drop the id."""
    assert _work_centers_list_key(7) != _work_centers_list_key(8)
    assert str(7) in _work_centers_list_key(7)


def test_invalidation_still_matches_the_per_company_keys(live_cache: FakeRedis):
    """``invalidate_entity`` deletes by the glob ``work_centers:list*``. Scoping the key
    must not have escaped that pattern -- if it had, a status flip would leave every
    tenant's roster stale for 15 minutes."""
    live_cache.store[_work_centers_list_key(COMPANY_A)] = "[]"
    live_cache.store[_work_centers_list_key(COMPANY_B)] = "[]"

    invalidate_work_centers_cache()

    assert _work_centers_list_key(COMPANY_A) not in live_cache.store
    assert _work_centers_list_key(COMPANY_B) not in live_cache.store
