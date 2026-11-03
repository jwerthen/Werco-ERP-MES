"""Bring 091's and 093's Data API revocations to their siblings' shape.

Revision ID: 113_data_api_revoke_parity
Revises: 112_consumable_checkouts

Their sibling table migrations (092, and every one from 094 through 112) revoke
the table AND its id sequence from ``PUBLIC`` and, under a ``pg_roles``
existence check, from ``anon`` and ``authenticated``. Two shipped short of that:

  * ``091_user_workspaces`` (``user_workspace_records``) revokes the table from
    ``PUBLIC``/``anon``/``authenticated`` but never touches
    ``user_workspace_records_id_seq``.
  * ``093_mrp_supply_links`` (``mrp_supply_links``) revokes the table and
    sequence from ``anon``/``authenticated`` but never from ``PUBLIC``.

``has_table_privilege`` / ``has_sequence_privilege`` count a ``PUBLIC`` grant
for every role, so a default privilege granting ``PUBLIC`` (or the Data API
roles) would leave those objects reachable. This revision issues the missing
statements. It is a separate revision, not an edit to 091/093, because it adds
an effect those applied revisions never had (CLAUDE.md: never edit an applied
migration; only existence-guard retrofits are sanctioned).

Postgres-only and unconditional apart from the dialect check: never nested
under a table-existence guard (``tests/test_migration_fresh_bootstrap.py``
enforces that), because ``create_all`` can build a table before its migration
runs. Both tables always exist at this point (091/093 create them, guarded, or
``create_all`` already did). On SQLite it emits nothing. Idempotent: revoking
a privilege that is not held is a native no-op, and the role checks keep it
applicable on a Postgres without the Supabase roles (CI, local Docker).

Downgrade is a deliberate no-op. The one repo precedent for a downgrade that
re-grants, ``059_supabase_rls_hardening``, restores a pre-state it audited on
the live database and documented; this revision has no such record. No
migration grants ``PUBLIC`` these privileges, and whether ``anon`` /
``authenticated`` held the 091 sequence depended on each database's default
privileges when 091 ran (059 strips only the ``postgres``-granted ones). The
prior grants are recorded nowhere and cannot be recovered per object.
Re-granting to undo a REVOKE could open Data API access that never existed: an
unrecoverable prior value resolves to the RESTRICTIVE setting (CLAUDE.md).
Downgrading past this revision leaves the objects revoked, which every older
revision already tolerates (092+ revoke the same way).

Operations: catalog-only ACL updates; no rows read or written, no table rewrite,
no backfill, no deploy-ordering constraint (the app connects as the owning
role, which REVOKE FROM PUBLIC does not affect).
"""

from alembic import op

revision = '113_data_api_revoke_parity'
down_revision = '112_consumable_checkouts'
branch_labels = None
depends_on = None

# Fixed migration constants, never request input.
TABLES = ('mrp_supply_links', 'user_workspace_records')
DATA_API_ROLES = ('anon', 'authenticated')


def upgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return
    for table in TABLES:
        op.execute(f'REVOKE ALL ON TABLE {table} FROM PUBLIC')
        op.execute(f'REVOKE ALL ON SEQUENCE {table}_id_seq FROM PUBLIC')
    for role in DATA_API_ROLES:
        revokes = '\n'.join(
            f'    REVOKE ALL ON TABLE {table} FROM {role};\n    REVOKE ALL ON SEQUENCE {table}_id_seq FROM {role};'
            for table in TABLES
        )
        op.execute(f"""DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
{revokes}
  END IF;
END $$""")


def downgrade():
    # Deliberate no-op: see the module docstring. Re-granting privileges whose
    # prior state is unknowable would be a security regression.
    pass
