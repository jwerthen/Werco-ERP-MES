"""Coverage for the refresh-token ``absolute_timeout`` claim and its setting.

``settings.SESSION_ABSOLUTE_TIMEOUT_HOURS`` had NO test coverage at all before
2026-07-29, on either the mint side or the enforcement side, which is how its
default could be changed (24h -> 168h) with nothing failing. These tests pin the
mechanism itself so a future change to the value is a deliberate act:

* ``create_refresh_token`` stamps ``absolute_timeout`` = now + the setting;
* ``verify_refresh_token`` refuses a token whose stamp is in the past;
* the default equals the refresh window (168h = ``REFRESH_TOKEN_EXPIRE_DAYS``),
  so the refresh-token expiry is the binding limit at stock settings;
* **and a refresh RESETS the window** -- see
  ``TestRefreshResetsTheWindow``, which documents the behavior rather than
  asserting a ceiling that does not exist.

That last point is the load-bearing one. The claim "users are forced to
re-authenticate every N hours no matter what" is FALSE and was false long before
the default changed: ``POST /auth/refresh`` mints a brand-new refresh token via
``create_refresh_token``, which has no parameter for carrying an existing stamp
forward and unconditionally recomputes it from ``utcnow()``. The setting therefore
bounds an IDLE window -- a user who touches the app at least once per window is
never forced to re-authenticate. These tests are written to make that concrete, so
nobody re-derives the wrong model from the setting's name.
"""

import inspect
from datetime import datetime, timedelta

import pytest
from jose import jwt

from app.core.config import Settings, settings
from app.core.security import create_refresh_token, verify_refresh_token


def _absolute_timeout_of(token: str) -> datetime:
    """Decode a refresh token and return its ``absolute_timeout`` claim as a datetime."""
    payload = jwt.decode(token, settings.REFRESH_TOKEN_SECRET_KEY, algorithms=[settings.ALGORITHM])
    return datetime.fromisoformat(payload["absolute_timeout"])


@pytest.mark.unit
class TestAbsoluteTimeoutIsStampedAtMint:
    """``create_refresh_token`` bakes the configured window into every token."""

    def test_stamp_is_now_plus_the_configured_hours(self):
        """The claim is ``utcnow() + SESSION_ABSOLUTE_TIMEOUT_HOURS``.

        A 60-second tolerance absorbs clock movement between the mint and the
        assertion without loosening the check to uselessness.
        """
        before = datetime.utcnow()
        token, _session_id, _expires = create_refresh_token(subject="4242")
        after = datetime.utcnow()

        stamp = _absolute_timeout_of(token)
        window = timedelta(hours=settings.SESSION_ABSOLUTE_TIMEOUT_HOURS)
        assert before + window - timedelta(seconds=60) <= stamp <= after + window + timedelta(seconds=60)

    def test_stamp_tracks_the_setting(self, monkeypatch):
        """Lowering the setting genuinely shortens the window on NEW tokens.

        This is the "keep the mechanism, relax the value" property the 2026-07-29
        change relies on: the cap can be re-armed by env var, with no code change.
        """
        monkeypatch.setattr(settings, "SESSION_ABSOLUTE_TIMEOUT_HOURS", 2)
        token, _session_id, _expires = create_refresh_token(subject="4242")

        remaining = _absolute_timeout_of(token) - datetime.utcnow()
        assert timedelta(hours=1, minutes=55) < remaining <= timedelta(hours=2)

    def test_every_refresh_token_carries_the_claim(self):
        """The claim is unconditional -- no code path mints a cap-less refresh token."""
        token, _session_id, _expires = create_refresh_token(subject="4242", company_id=1, read_only=True)
        payload = jwt.decode(token, settings.REFRESH_TOKEN_SECRET_KEY, algorithms=[settings.ALGORITHM])
        assert "absolute_timeout" in payload


@pytest.mark.unit
class TestAbsoluteTimeoutIsEnforcedAtVerify:
    """``verify_refresh_token`` is where the stamp actually bites."""

    def test_rejects_a_token_whose_stamp_is_in_the_past(self, monkeypatch):
        """A past ``absolute_timeout`` yields ``None`` (the 401 path in /auth/refresh).

        Minted through the real helper with a negative window rather than by
        hand-rolling a JWT, so the test exercises the same claim format the
        production mint produces.
        """
        monkeypatch.setattr(settings, "SESSION_ABSOLUTE_TIMEOUT_HOURS", -1)
        token, _session_id, _expires = create_refresh_token(subject="4242")
        assert _absolute_timeout_of(token) < datetime.utcnow()

        assert verify_refresh_token(token) is None

    def test_accepts_a_token_whose_stamp_is_in_the_future(self):
        """Positive control: an in-window token verifies and returns its claims."""
        token, session_id, _expires = create_refresh_token(subject="4242", company_id=7)

        payload = verify_refresh_token(token)
        assert payload is not None
        assert payload["user_id"] == "4242"
        assert payload["session_id"] == session_id
        assert payload["company_id"] == 7

    def test_rejection_is_the_stamp_and_not_jwt_expiry(self, monkeypatch):
        """The refusal comes from the stamp check, not from ``exp``.

        With the window negative but ``REFRESH_TOKEN_EXPIRE_DAYS`` untouched, the
        JWT itself is still perfectly valid -- it decodes fine -- and yet
        ``verify_refresh_token`` returns None. That isolates the absolute-timeout
        branch as the sole cause.
        """
        monkeypatch.setattr(settings, "SESSION_ABSOLUTE_TIMEOUT_HOURS", -1)
        token, _session_id, _expires = create_refresh_token(subject="4242")

        # Decodes cleanly: signature good, not past `exp`.
        payload = jwt.decode(token, settings.REFRESH_TOKEN_SECRET_KEY, algorithms=[settings.ALGORITHM])
        assert payload["type"] == "refresh"
        assert datetime.fromtimestamp(payload["exp"]) > datetime.now()

        assert verify_refresh_token(token) is None


@pytest.mark.unit
class TestRefreshResetsTheWindow:
    """NAMED FOR WHAT IT IS: refreshing RESETS the cap; it is not a session ceiling.

    Do not read these as a defect report -- they pin CURRENT, long-standing
    behavior that the 2026-07-29 default change did not touch. They exist so that
    any doc or claim of the form "sessions terminate after N hours regardless of
    activity" can be checked against something executable.
    """

    def test_create_refresh_token_cannot_carry_a_stamp_forward(self):
        """There is no parameter through which a caller could preserve the original
        window -- so no call site can, however carefully it is written."""
        params = set(inspect.signature(create_refresh_token).parameters)
        assert "absolute_timeout" not in params
        assert params == {"subject", "session_id", "company_id", "read_only"}

    def test_reminting_with_the_same_session_id_starts_a_new_window(self, monkeypatch):
        """What ``POST /auth/refresh`` does: mint a new refresh token reusing the
        session_id. The session is continuous, yet the new token's window is
        recomputed from ``utcnow()`` -- strictly later than, and unrelated to, the
        original stamp.

        The two-value setting change makes the reset unambiguous: had the original
        cap survived the refresh, the second token would still expire ~1 hour out.
        """
        monkeypatch.setattr(settings, "SESSION_ABSOLUTE_TIMEOUT_HOURS", 1)
        first_token, session_id, _expires = create_refresh_token(subject="4242")
        first_stamp = _absolute_timeout_of(first_token)

        monkeypatch.setattr(settings, "SESSION_ABSOLUTE_TIMEOUT_HOURS", 24)
        second_token, second_session_id, _expires = create_refresh_token(subject="4242", session_id=session_id)
        second_stamp = _absolute_timeout_of(second_token)

        assert second_session_id == session_id, "same session, so this is a refresh and not a fresh login"
        assert second_stamp > first_stamp, "the window was reset, not inherited"
        assert second_stamp - datetime.utcnow() > timedelta(hours=23)

    def test_a_token_refreshed_before_expiry_never_hits_the_cap(self, monkeypatch):
        """The practical consequence: an ACTIVE user is never forced to re-login.

        Refresh ten times inside a 1-hour window and every resulting token still
        verifies -- total elapsed session life is irrelevant because each mint
        restarts the clock. This is an idle timeout, not a session lifetime.
        """
        monkeypatch.setattr(settings, "SESSION_ABSOLUTE_TIMEOUT_HOURS", 1)
        token, session_id, _expires = create_refresh_token(subject="4242")

        for _ in range(10):
            payload = verify_refresh_token(token)
            assert payload is not None, "an in-window token must keep verifying"
            token, session_id, _expires = create_refresh_token(subject="4242", session_id=session_id)

        assert verify_refresh_token(token) is not None
        assert _absolute_timeout_of(token) > datetime.utcnow()


@pytest.mark.unit
class TestAbsoluteTimeoutDefault:
    """The shipped default, pinned against the model-field default rather than the
    live ``settings`` object so an env var in the runner cannot mask a code change."""

    def test_default_is_168_hours(self):
        assert Settings.model_fields["SESSION_ABSOLUTE_TIMEOUT_HOURS"].default == 168

    def test_default_equals_the_refresh_window(self):
        """168h == ``REFRESH_TOKEN_EXPIRE_DAYS`` (7 days).

        At stock settings the refresh token's own expiry is the binding limit and
        the absolute cap can never fire first. If someone lowers
        REFRESH_TOKEN_EXPIRE_DAYS without revisiting this, that intent is broken --
        hence the assertion rather than a comment.
        """
        refresh_days = Settings.model_fields["REFRESH_TOKEN_EXPIRE_DAYS"].default
        assert Settings.model_fields["SESSION_ABSOLUTE_TIMEOUT_HOURS"].default == refresh_days * 24
