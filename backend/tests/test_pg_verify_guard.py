"""The "local disposable PostgreSQL only" guard shared by the verify scripts.

Those scripts run ``CREATE DATABASE`` / ``DROP DATABASE ... WITH (FORCE)`` behind this
guard, and they run in CI's E2E job. A guard that checks only ``url.host`` is bypassable:
SQLAlchemy's psycopg2 dialect copies URL query parameters over the URL's own host, and libpq
honours ``hostaddr`` / ``service`` (and their environment variables) even when a host is set.
Pure: no database, no network, no ``os.environ`` (every case passes its own environment).
"""

import pytest
import sqlalchemy as sa

from scripts._pg_verify import require_local_test_postgres

pytestmark = pytest.mark.unit

TEST_ENV = {"ENVIRONMENT": "test"}
CI_URL = "postgresql://werco_user:test_password@localhost:5432/werco_erp_e2e"

BYPASS_URLS = {
    "query host": "postgresql://u:p@localhost/db?host=prod.example.com",
    "query hostaddr": "postgresql://u:p@localhost/db?hostaddr=203.0.113.7",
    "query service": "postgresql://u:p@localhost/db?service=prod",
}


@pytest.mark.parametrize("url", sorted(BYPASS_URLS.values()), ids=sorted(BYPASS_URLS))
def test_the_bypass_is_real_so_the_refusal_is_not_vacuous(url):
    """``url.host`` reads local, yet the dialect hands libpq a remote target."""
    parsed = sa.engine.make_url(url)
    assert parsed.host == "localhost"
    _, kwargs = parsed.get_dialect()().create_connect_args(parsed)
    redirected = kwargs["host"] != "localhost" or "hostaddr" in kwargs or "service" in kwargs
    assert redirected, kwargs


@pytest.mark.parametrize(
    "url",
    [
        *BYPASS_URLS.values(),
        "postgresql://u:p@localhost/db?HOST=prod.example.com",
        "postgresql://u:p@localhost/werco_erp_e2e_scratch?dbname=werco_erp_e2e",
        "postgresql://u:p@localhost/db?host=localhost&hostaddr=203.0.113.7",
    ],
)
def test_query_parameters_that_redirect_libpq_are_refused(url):
    with pytest.raises(RuntimeError, match="override the URL's host or database"):
        require_local_test_postgres(url, TEST_ENV)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@db.abcdefghijkl.supabase.co:5432/postgres",
        "postgresql://u:p@aws-0-us-east-1.pooler.supabase.com:6543/postgres",
        "postgresql://u:p@203.0.113.7/db",
        "postgresql://u:p@localhost.example.com/db",
        "postgresql://u:p@localhost,prod.example.com/db",
    ],
)
def test_remote_hosts_are_refused(url):
    with pytest.raises(RuntimeError, match="is not one of"):
        require_local_test_postgres(url, TEST_ENV)


@pytest.mark.parametrize("environ", [TEST_ENV, {**TEST_ENV, "PGHOST": "prod.example.com"}])
def test_a_hostless_url_is_refused_whatever_pghost_says(environ):
    """``postgresql:///db`` would connect wherever PGHOST (or the default socket) points."""
    with pytest.raises(RuntimeError, match="names no host"):
        require_local_test_postgres("postgresql:///db", environ)


@pytest.mark.parametrize("variable", ["PGHOSTADDR", "PGSERVICE"])
def test_libpq_environment_redirects_are_refused_even_with_a_local_host(variable):
    with pytest.raises(RuntimeError, match=variable):
        require_local_test_postgres(CI_URL, {**TEST_ENV, variable: "203.0.113.7"})


@pytest.mark.parametrize(
    ("url", "environ", "reason"),
    [
        (CI_URL, {}, "ENVIRONMENT is not 'test'"),
        (CI_URL, {"ENVIRONMENT": "production"}, "ENVIRONMENT is not 'test'"),
        ("sqlite:///./werco_dev.db", TEST_ENV, "not postgresql"),
    ],
)
def test_wrong_environment_or_backend_is_refused(url, environ, reason):
    with pytest.raises(RuntimeError, match=reason):
        require_local_test_postgres(url, environ)


@pytest.mark.parametrize(
    "url",
    [
        CI_URL,
        "postgresql://werco_user:test_password@127.0.0.1:55432/werco_erp_e2e",
        "postgresql+psycopg2://werco_user:test_password@127.0.0.1:55432/werco_erp_e2e",
        "postgresql://werco_user:test_password@postgres:5432/werco_erp",
        "postgresql://u:p@localhost/db?sslmode=disable&application_name=verify",
    ],
)
def test_local_urls_are_accepted(url):
    accepted = require_local_test_postgres(url, {**TEST_ENV, "PGHOST": "localhost"})
    assert accepted == sa.engine.make_url(url)


def test_accepted_urls_really_connect_locally():
    """What the guard accepts, the dialect does send to a local host with no override."""
    url = require_local_test_postgres(CI_URL, TEST_ENV)
    _, kwargs = url.get_dialect()().create_connect_args(url)
    assert kwargs["host"] == "localhost"
    assert not {"hostaddr", "service"} & set(kwargs)
