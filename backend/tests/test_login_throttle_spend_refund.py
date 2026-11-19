"""``FailedLoginThrottle.try_spend`` / ``refund`` -- the floor-PIN per-IP budget's primitives.

The public badge door charges each PIN attempt at RESERVATION, before bcrypt, so the charge must be
atomic with the check (``try_spend``: the increment IS the check, so a burst cannot overshoot), and
a correct PIN gives its unit back (``refund``), so honest sign-ins cost the address nothing. Both
are pinned in memory mode and against a Redis-shaped client.
"""

from __future__ import annotations

from typing import Dict, Optional

import pytest

from app.core.login_throttle import FailedLoginThrottle

pytestmark = pytest.mark.unit

IP = "203.0.113.9"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _throttle(max_failures: int = 3, window: int = 900, cooldown: int = 600) -> FailedLoginThrottle:
    throttle = FailedLoginThrottle(
        key_prefix="test:spend", max_failures=max_failures, window_seconds=window, cooldown_seconds=cooldown
    )
    throttle._redis_client = lambda: None  # type: ignore[method-assign]  # memory mode
    return throttle


class TestMemoryMode:
    def test_spends_up_to_the_budget_then_refuses_without_spending(self):
        throttle = _throttle()
        assert [throttle.try_spend(IP) for _ in range(5)] == [True, True, True, False, False]
        assert throttle._memory[IP][0] == 3, "a refused attempt spends nothing"
        assert throttle.blocked_retry_after(IP) is not None

    def test_reaching_the_budget_starts_the_cooldown_and_a_refusal_never_extends_it(self):
        clock = _Clock()
        throttle = _throttle(cooldown=600)
        throttle._now = clock  # type: ignore[method-assign]
        for _ in range(3):
            throttle.try_spend(IP)
        expires = throttle._memory[IP][1]
        assert expires == clock.now + 600
        clock.now += 100
        assert throttle.try_spend(IP) is False
        assert throttle._memory[IP][1] == expires

    def test_a_refund_gives_a_unit_back_and_lifts_the_block(self):
        throttle = _throttle()
        for _ in range(3):
            throttle.try_spend(IP)
        throttle.refund(IP)
        assert throttle.blocked_retry_after(IP) is None
        assert throttle.try_spend(IP) is True

    def test_a_refund_never_goes_below_zero(self):
        throttle = _throttle()
        throttle.refund(IP)
        assert IP not in throttle._memory
        throttle.try_spend(IP)
        throttle.refund(IP)
        throttle.refund(IP)
        assert IP not in throttle._memory
        assert throttle.try_spend(IP) is True and throttle._memory[IP][0] == 1

    def test_an_expired_window_starts_fresh(self):
        clock = _Clock()
        throttle = _throttle(window=900, cooldown=600)
        throttle._now = clock  # type: ignore[method-assign]
        for _ in range(3):
            throttle.try_spend(IP)
        clock.now += 601
        assert throttle.try_spend(IP) is True and throttle._memory[IP][0] == 1


class _FakeRedis:
    """The handful of Redis commands the throttle uses, with TTLs recorded (not enforced)."""

    def __init__(self) -> None:
        self.values: Dict[str, int] = {}
        self.ttls: Dict[str, int] = {}

    def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    def decr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) - 1
        return self.values[key]

    def expire(self, key: str, seconds: int) -> None:
        self.ttls[key] = seconds

    def delete(self, key: str) -> None:
        self.values.pop(key, None)
        self.ttls.pop(key, None)

    def get(self, key: str) -> Optional[str]:
        value = self.values.get(key)
        return None if value is None else str(value)

    def ttl(self, key: str) -> int:
        return self.ttls.get(key, -1)


class TestRedisMode:
    def _throttle(self) -> tuple[FailedLoginThrottle, _FakeRedis]:
        redis = _FakeRedis()
        throttle = FailedLoginThrottle(
            key_prefix="test:spend", max_failures=3, window_seconds=900, cooldown_seconds=600
        )
        throttle._redis_client = lambda: redis  # type: ignore[method-assign]
        return throttle, redis

    def test_the_increment_is_the_check_and_an_over_budget_unit_is_given_back(self):
        throttle, redis = self._throttle()
        assert [throttle.try_spend(IP) for _ in range(4)] == [True, True, True, False]
        key = "test:spend:" + IP
        assert redis.values[key] == 3
        assert redis.ttls[key] == 600, "reaching the budget re-arms the TTL to the cooldown"
        assert throttle.blocked_retry_after(IP) == 600

    def test_a_refund_decrements_and_never_leaves_a_negative_key(self):
        throttle, redis = self._throttle()
        key = "test:spend:" + IP
        throttle.try_spend(IP)
        throttle.refund(IP)
        assert key not in redis.values
        throttle.refund(IP)
        assert key not in redis.values

    def test_a_storage_error_fails_open(self):
        throttle = FailedLoginThrottle(
            key_prefix="test:spend", max_failures=1, window_seconds=900, cooldown_seconds=600
        )

        class _Broken:
            def incr(self, key):
                raise ConnectionError("redis down")

            def decr(self, key):
                raise ConnectionError("redis down")

        throttle._redis_client = lambda: _Broken()  # type: ignore[method-assign]
        assert throttle.try_spend(IP) is True
        throttle.refund(IP)  # must not raise
