"""App-free helpers shared by the disposable-PostgreSQL verify scripts.

Imports nothing from ``app``: ``verify_fabrication_quoting_postgres``'s migration-only pass
must keep running without ERP settings.

The guard is the reason this module exists. These scripts ``CREATE DATABASE`` and ``DROP
DATABASE ... WITH (FORCE)``, so "local only" has to mean the host libpq will actually connect
to, not the host the URL appears to name.
"""

import os
from contextlib import contextmanager
from uuid import uuid4

import sqlalchemy as sa

DATA_API_ROLES = ("anon", "authenticated")

# ``postgres`` is the docker-compose service name; the rest are loopback.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres"})

# SQLAlchemy's psycopg2 dialect copies URL query parameters into the connect kwargs AFTER the
# URL's own host and database (``opts.update(url.query)``), and libpq honours them. So
# ``postgresql://u:p@localhost/db?host=<remote>`` connects to <remote> while ``url.host`` still
# reads ``localhost``. ``hostaddr`` and ``service`` redirect the same way. ``dbname`` does not
# leave the machine, but it would override the database a caller set, so a "scratch" database
# could silently resolve to the seeded one.
REDIRECTING_QUERY_KEYS = frozenset({"host", "hostaddr", "service", "dbname"})

# libpq fills every keyword the connection did not set from the environment (and from the
# service file a service name selects). PGHOSTADDR, or a PGSERVICE entry's hostaddr, picks the
# physical address EVEN WHEN host is set (host is then only the name used for auth and TLS), so
# both are refused outright. PGHOST is consulted only when the URL names no host, and a hostless
# URL is refused below, so PGHOST cannot redirect a URL this guard accepts.
REDIRECTING_ENV_VARS = ("PGHOSTADDR", "PGSERVICE")


def require_local_test_postgres(url, environ=None):
    """Return ``url`` as a URL object if these scripts may run against it; raise otherwise.

    Accepts only: ``ENVIRONMENT=test``; a ``postgresql`` backend; an explicit host in
    ``LOCAL_HOSTS``; no ``host``/``hostaddr``/``service``/``dbname`` query parameter; and no
    ``PGHOSTADDR``/``PGSERVICE`` in the environment.
    """
    environ = os.environ if environ is None else environ
    url = sa.engine.make_url(url)
    problems = []
    if environ.get("ENVIRONMENT") != "test":
        problems.append("ENVIRONMENT is not 'test'")
    if url.get_backend_name() != "postgresql":
        problems.append(f"backend is {url.get_backend_name()!r}, not postgresql")
    if not url.host:
        problems.append("the URL names no host (libpq would fall back to PGHOST or a socket)")
    elif url.host not in LOCAL_HOSTS:
        problems.append(f"host {url.host!r} is not one of {sorted(LOCAL_HOSTS)}")
    redirecting = sorted(key for key in url.query if key.lower() in REDIRECTING_QUERY_KEYS)
    if redirecting:
        problems.append(f"query parameter(s) {redirecting} override the URL's host or database")
    environment = [name for name in REDIRECTING_ENV_VARS if environ.get(name)]
    if environment:
        problems.append(f"{environment} set in the environment would redirect libpq")
    if problems:
        raise RuntimeError("This check requires a local, disposable PostgreSQL test database: " + "; ".join(problems))
    return url


@contextmanager
def scratch_database(url, prefix):
    """Yield the URL of a new, uniquely named database beside ``url``'s; drop it on exit.

    ``url``'s own database is only connected to, to issue CREATE/DROP DATABASE; nothing is
    written to it. The drop is ``WITH (FORCE)`` so a lingering child connection cannot leave
    the scratch database behind.
    """
    url = require_local_test_postgres(url)
    # Identifier contains only our fixed prefix and a generated hexadecimal UUID.
    name = prefix + uuid4().hex
    admin = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
        try:
            yield url.set(database=name)
        finally:
            with admin.connect() as connection:
                connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


def existing_data_api_roles(connection):
    """The Data API roles that already exist in this cluster."""
    return [
        role
        for role in DATA_API_ROLES
        if connection.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"), {"role": role}
        ).scalar_one()
    ]


def create_missing_data_api_roles(connection):
    """Create each absent Data API role ``NOLOGIN`` inside the caller's transaction.

    CI's test owner is a superuser, so this exercises the migrations' conditional REVOKE
    branches on stock PostgreSQL. The roles roll back with the caller's transaction; an
    existing role is never altered. Returns the roles that were already present.
    """
    present = existing_data_api_roles(connection)
    for role in DATA_API_ROLES:
        if role not in present:
            connection.execute(sa.text(f'CREATE ROLE "{role}" NOLOGIN'))
    return present


def grant_inherited_data_api_privileges(connection, schema):
    """Simulate inherited Data API grants, so denial checks exercise the REVOKEs, not pristine defaults."""
    for objects in ("TABLES", "SEQUENCES"):
        connection.execute(
            sa.text(
                f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" GRANT ALL ON {objects} TO PUBLIC, anon, authenticated'
            )
        )


def trigger_names(connection, schema, tables):
    """``{(table, trigger)}`` for the non-internal triggers on ``tables`` in ``schema``."""
    return set(
        connection.execute(
            sa.text(
                "SELECT c.relname, t.tgname FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema AND c.relname = ANY(:tables) AND NOT t.tgisinternal"
            ),
            {"schema": schema, "tables": list(tables)},
        ).all()
    )
