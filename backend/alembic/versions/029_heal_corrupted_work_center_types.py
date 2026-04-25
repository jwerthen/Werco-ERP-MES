"""Heal corrupted work center type slugs

Revision ID: 029_heal_corrupted_work_center_types
Revises: 028_add_po_line_item_and_po_date
Create Date: 2026-04-25

A bug in normalize_work_center_type used double-escaped \\s in a raw
string, which caused [\\s-]+ to match every literal 's' character and
replace it with '_'. Any work center type saved during the affected
window got mangled (e.g. laser → la_er, assembly → a_embly,
press_brake → pre__brake, inspection → in_pection, shipping → hipping).

The regex was fixed in app/services/work_center_type_service.py, but the
already-corrupted values are still in work_centers.work_center_type and
in the quote_settings JSON for key 'work_center_types'. The corrupted
in-use values lock the admin UI's delete button, so users can't clean
up their type list.

This migration remaps the five known default-derived corrupted slugs
back to their originals on work_centers, and rewrites the saved JSON
to drop the corrupted entries (the real ones get re-merged on next
read because the list endpoint includes in-use types).

Only the five default-derived corruptions are remapped. Custom types
that happened to contain an 's' followed by another 's', dash, or
backslash also got mangled but we cannot reliably reverse them — those
will continue to surface in the admin UI as in-use slugs and the
operator can rename them manually.
"""
import json

from alembic import op


revision = '029_heal_corrupted_work_center_types'
down_revision = '028_add_po_line_item_and_po_date'
branch_labels = None
depends_on = None


# corrupted_slug -> original_slug for the default types only
SLUG_REMAP = {
    'la_er': 'laser',
    'a_embly': 'assembly',
    'pre__brake': 'press_brake',
    'in_pection': 'inspection',
    'hipping': 'shipping',
}


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Heal work_centers.work_center_type values.
    for corrupted, original in SLUG_REMAP.items():
        conn.exec_driver_sql(
            "UPDATE work_centers SET work_center_type = %s "
            "WHERE work_center_type = %s",
            (original, corrupted),
        )

    # 2. Heal the saved JSON list in quote_settings. We rewrite the row
    # in place: drop any corrupted entries, leave everything else alone.
    # The admin list endpoint merges in-use types in, so the real slugs
    # come back automatically without us needing to add them here.
    row = conn.exec_driver_sql(
        "SELECT setting_value FROM quote_settings "
        "WHERE setting_key = 'work_center_types'"
    ).fetchone()
    if row and row[0]:
        try:
            saved = json.loads(row[0])
        except (TypeError, ValueError):
            saved = None
        if isinstance(saved, list):
            cleaned = [t for t in saved if t not in SLUG_REMAP]
            if cleaned != saved:
                conn.exec_driver_sql(
                    "UPDATE quote_settings SET setting_value = %s "
                    "WHERE setting_key = 'work_center_types'",
                    (json.dumps(cleaned),),
                )


def downgrade() -> None:
    # Intentionally a no-op. The corrupted values were a bug; we don't
    # want to recreate them on downgrade.
    pass
