"""Add work_centers.required_certification_type (Batch 11C / G5-B operator-cert gate)

Revision ID: 043_wc_required_cert_type
Revises: 042_wo_completion_perf_indexes
Create Date: 2026-06-08

Context
-------
The G5-B operator-certification gate lets a work center declare a *required
certification type*: when set, only operators holding an active
``OperatorCertification`` of that type may be assigned to / clock in on the work
center. This migration adds the single nullable column the gate keys off:

    work_centers.required_certification_type  certificationtype NULL

NULL (the common case) means the work center imposes no certification
requirement, so the column is nullable and needs no backfill -- a safe,
metadata-only add against the populated ``work_centers`` table.

Enum type reuse + the migrate-vs-bootstrap divergence (load-bearing)
--------------------------------------------------------------------
The column reuses the SAME enum type the model uses for
``OperatorCertification.certification_type`` -- ``SQLEnum(CertificationType)``,
whose native Postgres type name is ``certificationtype`` (verified) and whose
labels are the UPPERCASE member NAMES (``WELDING``, ``NDT``, ...), because
SQLAlchemy binds/stores native-enum MEMBER NAMES for a ``str``-backed Enum.

There is a real divergence between the two ways a Postgres DB gets built:

  * ``create_all`` bootstrap path (docs/DEVELOPMENT.md): the model's
    ``SQLEnum(CertificationType)`` creates the native ``certificationtype`` type,
    so it ALREADY EXISTS before this migration is stamped over.
  * pure-migration path: migration 024 created
    ``operator_certifications.certification_type`` as ``VARCHAR(50)`` (NOT a
    native enum), so on a DB built only by ``alembic upgrade`` the
    ``certificationtype`` type does NOT exist.

So this migration cannot assume the type exists. Following the precedent in
``007_add_bom_line_type`` (``enum_exists`` guard + create-if-absent), it creates
the ``certificationtype`` type with the exact uppercase labels ``create_all``
would emit IFF it is absent, then adds the column referencing it with
``create_type=False`` so SQLAlchemy never tries to (re-)create the type. Both
paths therefore converge on an identical column definition.

SQLite / create_all path
-------------------------
On SQLite (local dev / pytest ``create_all`` path) native enum types and
``pg_type`` do not exist; the Postgres-specific type handling is dialect-guarded
to no-op and the column is added as the VARCHAR that ``SQLEnum`` renders there,
matching what ``create_all`` emits from the model. The model
(``app/models/work_center.py``) declares the matching
``required_certification_type = Column(SQLEnum(CertificationType), nullable=True)``
so the bootstrap path produces the identical column; keep the two in lock-step.

Idempotent and reversible
-------------------------
- Upgrade guards the ADD COLUMN with an inspector column check (precedent:
  006/036/040), and the enum create with a ``pg_type`` guard (precedent: 007),
  so a re-run is a clean no-op.
- Downgrade drops the column (guarded). It deliberately does NOT drop the
  ``certificationtype`` type: that type is owned by ``operator_certifications``
  (on the bootstrap path) / may be shared, so dropping it here would corrupt
  that table. Leaving the type in place is the conservative, reversible choice.

Locking / operations note
-------------------------
A nullable column add with no default is a metadata-only change on Postgres
(no table rewrite, brief ACCESS EXCLUSIVE only). No backfill pass and no
deploy-ordering constraint: the column simply reads NULL until a work center
opts into a requirement.

Revision id is 25 chars (<= 32) per the create_all -> stamp -> upgrade bootstrap
constraint documented in docs/DEVELOPMENT.md (alembic_version.version_num is
varchar(32) on a freshly bootstrapped DB).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision = "043_wc_required_cert_type"
down_revision = "042_wo_completion_perf_indexes"
branch_labels = None
depends_on = None

TABLE_NAME = "work_centers"
COLUMN_NAME = "required_certification_type"
ENUM_NAME = "certificationtype"

# The UPPERCASE member NAMES SQLAlchemy emits/stores for SQLEnum(CertificationType)
# on Postgres -- kept byte-for-byte in lock-step with
# app/models/operator_certification.py::CertificationType so a defensively-created
# type matches the one create_all builds.
ENUM_LABELS = [
    "WELDING",
    "NDT",
    "CNC_OPERATION",
    "INSPECTION",
    "FORKLIFT",
    "CRANE",
    "SAFETY",
    "PROCESS_SPECIFIC",
    "QUALITY_SYSTEM",
    "OTHER",
]


def _is_postgres(conn) -> bool:
    return conn.dialect.name == "postgresql"


def _table_has_column(conn, table_name: str, column_name: str) -> bool:
    inspector = Inspector.from_engine(conn)
    try:
        columns = [col["name"] for col in inspector.get_columns(table_name)]
    except Exception:
        return False
    return column_name in columns


def _enum_exists(conn, enum_name: str) -> bool:
    row = conn.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = :name"),
        {"name": enum_name},
    ).fetchone()
    return row is not None


def _column_type():
    """The column type to add.

    On Postgres: reference the existing ``certificationtype`` native enum WITHOUT
    re-creating it (``create_type=False``) -- the upgrade ensures the type exists
    first. On SQLite: ``SQLEnum(CertificationType)`` renders as a VARCHAR check, the
    same thing ``create_all`` emits from the model, so the two paths match.
    """
    return postgresql.ENUM(*ENUM_LABELS, name=ENUM_NAME, create_type=False)


def upgrade() -> None:
    conn = op.get_bind()

    if _is_postgres(conn):
        # Reuse the existing certificationtype enum; defensively create it (with the
        # exact labels create_all would build) only if a pure-migration DB lacks it.
        if not _enum_exists(conn, ENUM_NAME):
            labels = ", ".join(f"'{label}'" for label in ENUM_LABELS)
            op.execute(f"CREATE TYPE {ENUM_NAME} AS ENUM ({labels})")
        column_type = _column_type()
    else:
        # SQLite (create_all / pytest path): SQLEnum renders as VARCHAR, matching
        # the model; no native enum type machinery to manage.
        column_type = sa.Enum(*ENUM_LABELS, name=ENUM_NAME)

    if not _table_has_column(conn, TABLE_NAME, COLUMN_NAME):
        op.add_column(
            TABLE_NAME,
            sa.Column(COLUMN_NAME, column_type, nullable=True),
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _table_has_column(conn, TABLE_NAME, COLUMN_NAME):
        op.drop_column(TABLE_NAME, COLUMN_NAME)

    # Intentionally NOT dropping the certificationtype type: it is owned/shared by
    # operator_certifications (bootstrap path) and dropping it would break that
    # table. Leaving it in place is the conservative, reversible choice.
