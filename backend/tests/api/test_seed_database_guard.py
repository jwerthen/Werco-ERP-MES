"""
The ``python -m scripts.seed_data`` seeder creates demo accounts with well-known
throwaway passwords (admin123 / password123). It must be hard-disabled in production
so weak credentials can never be planted in a CUI (CMMC/AS9100D) environment —
production tenants are bootstrapped through the company-onboarding flow, which
enforces the password-strength policy on the admin password.

(The related ``POST /admin/settings/seed-database`` endpoint was hardened
separately in PR #119 — it generates strong one-time credentials at runtime —
so the CLI seeder is the remaining well-known-password path; it is guarded here.)
"""

import pytest

from app.core.config import settings


@pytest.mark.unit
class TestSeedScriptProductionGuard:
    """`python -m scripts.seed_data` must refuse to run in production."""

    def test_seed_script_refuses_production_without_override(self, monkeypatch):
        """ENVIRONMENT=production with no override exits(1) before any DB work."""
        # The guard reads settings.ENVIRONMENT (matching the app), not os.getenv.
        monkeypatch.setattr(settings, "ENVIRONMENT", "production")
        monkeypatch.delenv("SEED_ALLOW_PRODUCTION", raising=False)

        from scripts.seed_data import seed_database

        with pytest.raises(SystemExit) as exc_info:
            seed_database()
        assert exc_info.value.code == 1

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "banana"])
    def test_seed_script_override_fails_closed_on_non_affirmative_values(self, monkeypatch, value):
        """Only affirmative override values (1/true/yes) open the guard.

        Regression fence for the override parsing: a refactor to a mere
        presence check (``os.getenv(...) is not None``) would let
        ``SEED_ALLOW_PRODUCTION=0`` seed prod while the delenv-based test
        above stayed green. Every non-affirmative value must still exit(1).
        """
        monkeypatch.setattr(settings, "ENVIRONMENT", "production")
        monkeypatch.setenv("SEED_ALLOW_PRODUCTION", value)

        from scripts.seed_data import seed_database

        with pytest.raises(SystemExit) as exc_info:
            seed_database()
        assert exc_info.value.code == 1

    def test_seed_script_refuses_supabase_database_target(self, monkeypatch):
        """A Supabase DATABASE_URL refuses even when ENVIRONMENT is not production.

        The 'local shell pointed at the prod DB' path: ENVIRONMENT defaults to
        "development" on a workstation, so the guard must also key on the
        database target itself, not just the environment name.
        """
        monkeypatch.setattr(settings, "ENVIRONMENT", "development")
        monkeypatch.delenv("SEED_ALLOW_PRODUCTION", raising=False)
        # is_supabase_database is a derived property (parses the resolved URL), so
        # patch it at the class; the guard's contract is "the app says the DB is
        # Supabase" — URL-parsing correctness is config's own concern.
        monkeypatch.setattr(type(settings), "is_supabase_database", property(lambda self: True))

        from scripts.seed_data import seed_database

        with pytest.raises(SystemExit) as exc_info:
            seed_database()
        assert exc_info.value.code == 1

    def test_seed_script_proceeds_outside_production(self, monkeypatch):
        """ENVIRONMENT=development with a non-Supabase DB passes the guard.

        Fails-safe direction: an over-eager guard would break local/CI/E2E
        seeding (the e2e workflow seeds under ENVIRONMENT=test), and the only
        automated coverage of that is deliberately non-blocking. Same
        create_all-sentinel technique as the override test — zero DB work.
        """
        monkeypatch.setattr(settings, "ENVIRONMENT", "development")
        monkeypatch.delenv("SEED_ALLOW_PRODUCTION", raising=False)
        assert not settings.is_supabase_database  # precondition: test DB is local

        import scripts.seed_data as seed_data

        class _GuardPassed(Exception):
            """Sentinel raised from the first post-guard statement."""

        def _raise_guard_passed(*args, **kwargs):
            raise _GuardPassed()

        monkeypatch.setattr(seed_data.Base.metadata, "create_all", _raise_guard_passed)

        with pytest.raises(_GuardPassed):
            seed_data.seed_database()

    def test_seed_script_override_bypasses_production_guard(self, monkeypatch):
        """SEED_ALLOW_PRODUCTION=1 lets seeding proceed PAST the production guard.

        Complements the no-override test above: proves the override env var is
        actually parsed and honored (i.e. the guard's ``and not allow_prod`` branch
        works — without it, the override would be dead and prod would still exit).
        We do NOT run the real seeder: the first statement after the guard
        (``Base.metadata.create_all``) is stubbed to raise a sentinel, so reaching it
        proves control passed the guard rather than hitting ``sys.exit(1)`` — with
        zero DB side effects.
        """
        monkeypatch.setattr(settings, "ENVIRONMENT", "production")
        monkeypatch.setenv("SEED_ALLOW_PRODUCTION", "1")

        import scripts.seed_data as seed_data

        class _GuardPassed(Exception):
            """Sentinel raised from the first post-guard statement."""

        def _raise_guard_passed(*args, **kwargs):
            raise _GuardPassed()

        # Stub the very first thing seed_database() does after the guard. If the guard
        # had exited we'd get SystemExit; getting _GuardPassed proves we got past it.
        monkeypatch.setattr(seed_data.Base.metadata, "create_all", _raise_guard_passed)

        with pytest.raises(_GuardPassed):
            seed_data.seed_database()
