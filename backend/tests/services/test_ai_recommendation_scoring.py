"""Unit tests for the deterministic Action Inbox recommendation score (B0.3).

score = priority_weight x confidence x age_decay x impact_magnitude, computed at list
time and never persisted. These tests cover the priority/age/impact permutations and the
null handling promised by the design note — no DB required (duck-typed recommendations).
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.ai_learning_service import (
    DEFAULT_PRIORITY_WEIGHT,
    MAX_IMPACT_MAGNITUDE,
    MIN_AGE_DECAY,
    MIN_IMPACT_MAGNITUDE,
    NO_EXPIRY_DECAY_FLOOR,
    PRIORITY_SCORE_WEIGHTS,
    _age_decay,
    _impact_magnitude,
    _priority_weight,
    score_recommendation,
)

NOW = datetime(2026, 6, 10, 12, 0, 0)


def make_recommendation(**overrides) -> SimpleNamespace:
    defaults = dict(
        priority="medium",
        confidence_score=0.5,
        created_at=NOW,
        expires_at=None,
        impact={},
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.unit
class TestPriorityWeight:
    def test_known_priorities_are_strictly_ordered(self):
        assert (
            _priority_weight("high") > _priority_weight("medium") > _priority_weight("low") > _priority_weight("info")
        )

    @pytest.mark.parametrize("priority,expected", sorted(PRIORITY_SCORE_WEIGHTS.items()))
    def test_exact_weights(self, priority, expected):
        assert _priority_weight(priority) == expected

    def test_unknown_and_null_priorities_use_default_weight(self):
        assert _priority_weight("urgent") == DEFAULT_PRIORITY_WEIGHT
        assert _priority_weight(None) == DEFAULT_PRIORITY_WEIGHT
        assert _priority_weight("") == DEFAULT_PRIORITY_WEIGHT

    def test_priority_is_normalized(self):
        assert _priority_weight("  HIGH ") == PRIORITY_SCORE_WEIGHTS["high"]


@pytest.mark.unit
class TestAgeDecay:
    def test_fresh_with_expiry_is_full_strength(self):
        assert _age_decay(NOW, NOW + timedelta(days=10), NOW) == pytest.approx(1.0)

    def test_halfway_to_expiry_decays_linearly(self):
        decay = _age_decay(NOW - timedelta(days=5), NOW + timedelta(days=5), NOW)
        assert decay == pytest.approx(1.0 - (1.0 - MIN_AGE_DECAY) * 0.5)

    def test_at_and_past_expiry_hits_floor(self):
        assert _age_decay(NOW - timedelta(days=10), NOW, NOW) == MIN_AGE_DECAY
        assert _age_decay(NOW - timedelta(days=10), NOW - timedelta(days=1), NOW) == MIN_AGE_DECAY

    def test_degenerate_expiry_before_creation_is_full_strength_until_expired(self):
        created = NOW + timedelta(days=2)
        assert _age_decay(created, created - timedelta(days=1), NOW) == pytest.approx(1.0)

    def test_no_expiry_decays_mildly_from_created_at(self):
        assert _age_decay(NOW, None, NOW) == pytest.approx(1.0)
        fifteen_days = _age_decay(NOW - timedelta(days=15), None, NOW)
        assert fifteen_days == pytest.approx(1.0 - 0.5 * (1.0 - NO_EXPIRY_DECAY_FLOOR))
        assert _age_decay(NOW - timedelta(days=90), None, NOW) == NO_EXPIRY_DECAY_FLOOR

    def test_null_created_at_is_treated_as_fresh(self):
        assert _age_decay(None, None, NOW) == pytest.approx(1.0)

    def test_timezone_aware_and_naive_datetimes_score_identically(self):
        aware_created = (NOW - timedelta(days=5)).replace(tzinfo=timezone.utc)
        aware_expires = (NOW + timedelta(days=5)).replace(tzinfo=timezone.utc)
        naive = _age_decay(NOW - timedelta(days=5), NOW + timedelta(days=5), NOW)
        assert _age_decay(aware_created, aware_expires, NOW) == pytest.approx(naive)


@pytest.mark.unit
class TestImpactMagnitude:
    def test_defaults_to_one_without_numeric_signal(self):
        assert _impact_magnitude(None) == 1.0
        assert _impact_magnitude({}) == 1.0
        assert _impact_magnitude("not-a-dict") == 1.0
        assert _impact_magnitude({"expected": "fewer edits"}) == 1.0
        assert _impact_magnitude({"magnitude": "huge"}) == 1.0
        assert _impact_magnitude({"magnitude": True}) == 1.0

    def test_non_positive_and_non_finite_values_are_ignored(self):
        assert _impact_magnitude({"magnitude": 0}) == 1.0
        assert _impact_magnitude({"magnitude": -5}) == 1.0
        assert _impact_magnitude({"magnitude": float("nan")}) == 1.0
        assert _impact_magnitude({"magnitude": float("inf")}) == 1.0

    def test_fractional_values_pass_through_with_floor(self):
        assert _impact_magnitude({"magnitude": 0.5}) == 0.5
        assert _impact_magnitude({"magnitude": 1.0}) == 1.0
        assert _impact_magnitude({"magnitude": 0.01}) == MIN_IMPACT_MAGNITUDE

    def test_large_values_are_log_scaled_and_capped(self):
        assert _impact_magnitude({"magnitude": 10}) == pytest.approx(1.0 + 1.0 / 3.0)
        assert _impact_magnitude({"magnitude": 1000}) == MAX_IMPACT_MAGNITUDE
        assert _impact_magnitude({"magnitude": 10**9}) == MAX_IMPACT_MAGNITUDE

    def test_alternate_numeric_keys_are_recognized(self):
        assert _impact_magnitude({"estimated_value": 10}) == pytest.approx(1.0 + 1.0 / 3.0)
        assert _impact_magnitude({"impact_score": 0.75}) == 0.75


@pytest.mark.unit
class TestScoreRecommendation:
    def test_full_formula(self):
        recommendation = make_recommendation(
            priority="high",
            confidence_score=0.8,
            created_at=NOW - timedelta(days=5),
            expires_at=NOW + timedelta(days=5),
            impact={"magnitude": 0.5},
        )
        # 1.0 (high) x 0.8 (confidence) x 0.6 (halfway decay) x 0.5 (impact) = 0.24
        assert score_recommendation(recommendation, now=NOW) == pytest.approx(0.24)

    def test_null_confidence_defaults_to_half(self):
        scored_null = score_recommendation(make_recommendation(confidence_score=None), now=NOW)
        scored_half = score_recommendation(make_recommendation(confidence_score=0.5), now=NOW)
        assert scored_null == scored_half

    def test_zero_confidence_is_respected_not_defaulted(self):
        assert score_recommendation(make_recommendation(confidence_score=0.0), now=NOW) == 0.0

    def test_out_of_range_confidence_is_clamped(self):
        scored = score_recommendation(make_recommendation(confidence_score=5.0), now=NOW)
        assert scored == score_recommendation(make_recommendation(confidence_score=1.0), now=NOW)

    def test_priority_permutations_order_scores(self):
        scores = [
            score_recommendation(make_recommendation(priority=priority), now=NOW)
            for priority in ("high", "medium", "low", "info")
        ]
        assert scores == sorted(scores, reverse=True)
        assert len(set(scores)) == len(scores)

    def test_high_impact_can_outrank_higher_priority(self):
        boosted_info = make_recommendation(priority="info", confidence_score=1.0, impact={"magnitude": 1000})
        plain_low = make_recommendation(priority="low", confidence_score=0.9)
        assert score_recommendation(boosted_info, now=NOW) > score_recommendation(plain_low, now=NOW)

    def test_null_impact_and_null_expiry_are_safe(self):
        recommendation = make_recommendation(impact=None, expires_at=None, confidence_score=None)
        assert score_recommendation(recommendation, now=NOW) == pytest.approx(0.6 * 0.5)

    def test_score_is_rounded_to_four_decimals(self):
        recommendation = make_recommendation(priority="low", confidence_score=0.333333)
        scored = score_recommendation(recommendation, now=NOW)
        assert scored == round(scored, 4)
