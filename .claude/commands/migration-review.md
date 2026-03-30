Review the Alembic database migration(s) in `backend/alembic/versions/`. If a specific migration file is provided as an argument, review that file. Otherwise, review the most recent migration.

## Migration Review Checklist

### 1. Revision Chain Integrity
- Read the migration's `revision` and `down_revision` values
- Verify `down_revision` matches the `revision` of the previous migration in the chain
- Run: `cd backend && alembic heads` to check for multiple heads (branching)
- If there are multiple heads, this is a blocker — the chain must be linear

### 2. Upgrade Function
- Does it create tables, add columns, or alter existing schema?
- Are new foreign key columns indexed? (Required pattern: `op.create_index()` after `op.add_column()` for FK fields)
- Are new status/enum columns indexed?
- Are column types appropriate? (e.g., `sa.Numeric` for money, not `sa.Float`)
- Are `nullable` constraints correct? Will existing rows satisfy NOT NULL constraints?
- If adding an enum type, is it created with `sa.Enum(..., create_type=True)`?

### 3. Downgrade Function
- **Must exist and be functional** — empty `pass` downgrades are not acceptable
- Does it correctly reverse every operation in `upgrade()`?
- If upgrade creates a table, downgrade must drop it
- If upgrade adds a column, downgrade must drop it
- If upgrade creates an index, downgrade must drop it
- Test mentally: can this migration be rolled back without data loss?

### 4. Data Safety
- Will this migration work on a database with existing production data?
- If adding NOT NULL columns to existing tables, is there a `server_default` or a data migration step?
- If renaming columns, are dependent views/functions updated?
- If dropping columns, is the data backed up or migrated first?
- Are there any operations that could lock large tables for extended periods? (e.g., adding columns with defaults on tables with millions of rows)

### 5. Compliance Considerations
- Does this migration affect audit trail tables? (If so, extra scrutiny — audit data must be preserved)
- Does it maintain soft delete infrastructure? (SoftDeleteMixin columns: `is_deleted`, `deleted_at`, `deleted_by`)
- Does it affect traceability fields (lot_number, serial_number, batch_id)?
- If modifying document tables, are revision tracking fields preserved?

### 6. Naming Conventions
- Table names: lowercase, snake_case, plural (e.g., `work_orders`, `audit_logs`)
- Column names: lowercase, snake_case
- Index names: `ix_<table>_<column>` pattern
- Foreign key names: `fk_<table>_<column>_<ref_table>` pattern
- Migration filename: descriptive of the change

## Output Format
Report findings as:
- **PASS**: Check passed
- **WARN**: Non-blocking concern
- **FAIL**: Must fix before applying

End with: SAFE TO APPLY or DO NOT APPLY (with reasons).
