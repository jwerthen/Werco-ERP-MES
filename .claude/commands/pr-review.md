Perform a thorough code review of the current changes (staged or recent commits). This review enforces Werco ERP/MES coding standards, compliance requirements, and quality gates.

## Review Checklist

### 1. Type Safety
- **Backend**: All functions must have type hints on parameters and return values
- **Frontend**: No new `: any` type annotations (HIGH-001 — already 100+ instances to clean up). Define proper TypeScript interfaces for all data structures
- Check that Pydantic schemas match the SQLAlchemy model fields they represent
- Verify Zod frontend validation schemas align with backend Pydantic schemas

### 2. Database & Query Patterns
- **Eager loading**: All relationship access must use `joinedload()` or `selectinload()`. Flag any lazy-loaded relationship access in loops (N+1 queries)
- **Soft deletes**: No `session.delete()` on models with `SoftDeleteMixin`. Use the soft delete pattern instead
- **Audit fields**: New models must include `created_at`, `updated_at`, `created_by` fields
- **Indexes**: Foreign key columns and status/enum columns must have indexes
- **Transactions**: Multi-step mutations must use `atomic_transaction` context manager

### 3. Security & Auth
- **RBAC**: Every new endpoint must have `require_role()` dependency with appropriate role list
- **Audit logging**: Every create/update/delete operation must call `AuditService` to log the mutation with before/after values (AS9100D requirement)
- **Input validation**: Validate on both layers — Pydantic backend, Zod frontend
- **No secrets**: No hardcoded credentials, API keys, or connection strings
- **Sanitization**: User-provided HTML/text must pass through bleach

### 4. Frontend Patterns
- Functional components with hooks only (no class components)
- Use `PermissionGate` for role-restricted UI elements
- No `console.log` in production code (HIGH-004). Use the error logging service instead
- Forms must use React Hook Form + Zod validation
- API calls through `ApiService` singleton only

### 5. Code Quality
- **Black formatting** (120 line length) on all Python files
- **isort** import ordering (Black profile)
- No TODO/FIXME comments without a linked issue or QA finding number
- No commented-out code blocks
- Error handling: catch specific exceptions, not bare `except:`

### 6. Compliance (AS9100D / ISO 9001)
- Audit trail completeness: Can every data change be traced to a user and timestamp?
- Document revisions: Are document changes creating new revisions (not overwriting)?
- Traceability: Do lot/serial operations maintain the chain?
- Soft delete: Is data recoverable? No hard deletes?

### 7. Migration Review (if applicable)
- Does the migration have a valid `revision` and `down_revision` chain?
- Is there a working `downgrade()` function?
- Are new foreign key columns indexed?
- Will the migration work on a database with existing production data?

## Output Format
For each finding, report:
- **File:Line** — location
- **Severity** — Critical / High / Medium / Low / Nit
- **Issue** — what's wrong
- **Fix** — specific recommendation

End with a summary: APPROVE, REQUEST CHANGES, or BLOCK (for Critical/compliance issues).
