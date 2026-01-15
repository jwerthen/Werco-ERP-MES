# QA/QC Code Review Task List

This document outlines the comprehensive code review tasks for the Werco ERP system. The QA droid should execute these tasks systematically and document all findings in `QA_FINDINGS.md`.

## Instructions for QA Droid

1. Execute each task section in order
2. Use the provided grep/search patterns
3. Document ALL findings in `QA_FINDINGS.md`
4. Categorize by severity: Critical > High > Medium > Low
5. Tag each finding with assignee: `[Backend]` or `[Frontend]`

---

## Phase 1: Security Audit (Critical Priority)

### Task 1.1: Authentication & Authorization Review
**Files to review:**
- `backend/app/api/endpoints/auth.py`
- `backend/app/api/deps.py`
- `backend/app/core/security.py`
- `frontend/src/context/AuthContext.tsx`
- `frontend/src/services/api.ts`

**Check for:**
- [ ] JWT token expiration handling
- [ ] Refresh token rotation
- [ ] Password hashing strength (bcrypt rounds)
- [ ] Session timeout implementation
- [ ] Token storage security (localStorage vs httpOnly cookies)
- [ ] Authorization checks on all protected endpoints
- [ ] Role-based access control implementation
- [ ] MFA implementation status

**Search patterns:**
```bash
grep -r "SECRET_KEY\|JWT\|password\|token" backend/app/core/
grep -r "localStorage\|sessionStorage" frontend/src/
grep -r "get_current_user\|Depends" backend/app/api/endpoints/
```

### Task 1.2: SQL Injection Vulnerability Scan
**Files to review:**
- All files in `backend/app/api/endpoints/`
- `backend/app/services/`

**Check for:**
- [ ] Raw SQL queries with string interpolation
- [ ] Use of `text()` without parameterization
- [ ] Dynamic table/column names
- [ ] Unsafe use of `execute()` or `exec()`

**Search patterns:**
```bash
grep -rn "f\".*SELECT\|f\".*INSERT\|f\".*UPDATE\|f\".*DELETE" backend/app/
grep -rn "\.execute\(.*%\|\.execute\(.*\.format" backend/app/
grep -rn "text\(" backend/app/
```

### Task 1.3: XSS Vulnerability Scan
**Files to review:**
- All `.tsx` files in `frontend/src/`

**Check for:**
- [ ] Use of `dangerouslySetInnerHTML`
- [ ] Unescaped user input in templates
- [ ] URL parameter injection
- [ ] innerHTML assignments

**Search patterns:**
```bash
grep -rn "dangerouslySetInnerHTML" frontend/src/
grep -rn "innerHTML" frontend/src/
grep -rn "document\.write" frontend/src/
```

### Task 1.4: Sensitive Data Exposure
**Files to review:**
- All configuration files
- Logging configurations
- Error handlers

**Check for:**
- [ ] API keys in source code
- [ ] Passwords in logs
- [ ] Stack traces exposed to users
- [ ] Sensitive data in error messages
- [ ] PII in console.log statements

**Search patterns:**
```bash
grep -rn "console\.log\|console\.error\|console\.warn" frontend/src/
grep -rn "logger\.\(debug\|info\|warning\|error\)" backend/app/
grep -rn "password\|secret\|key\|token" --include="*.py" --include="*.ts" --include="*.tsx"
```

### Task 1.5: Input Validation Audit
**Files to review:**
- `backend/app/schemas/` (Pydantic models)
- `frontend/src/validation/schemas.ts`
- `backend/app/core/sanitization.py`

**Check for:**
- [ ] All API endpoints have Pydantic validation
- [ ] String length limits defined
- [ ] Numeric range validation
- [ ] Email/URL format validation
- [ ] File upload validation (type, size)
- [ ] Frontend validation matches backend

**Search patterns:**
```bash
grep -rn "class.*BaseModel" backend/app/schemas/
grep -rn "Field\|validator\|root_validator" backend/app/schemas/
grep -rn "z\.string\|z\.number\|z\.object" frontend/src/
```

### Task 1.6: CSRF Protection Review
**Files to review:**
- `backend/app/main.py` (CSRF middleware)
- `frontend/src/services/api.ts`

**Check for:**
- [ ] CSRF tokens on state-changing requests
- [ ] X-Requested-With header enforcement
- [ ] Origin/Referer validation
- [ ] SameSite cookie attribute

---

## Phase 2: Database & Data Integrity

### Task 2.1: Database Model Review
**Files to review:**
- All files in `backend/app/models/`

**Check for:**
- [ ] Primary keys defined on all tables
- [ ] Foreign key constraints with ON DELETE behavior
- [ ] Unique constraints where needed
- [ ] NOT NULL constraints on required fields
- [ ] Index definitions for query performance
- [ ] Audit fields (created_at, updated_at, created_by)
- [ ] Soft delete implementation consistency

**Search patterns:**
```bash
grep -rn "ForeignKey\|relationship\|Index\|UniqueConstraint" backend/app/models/
grep -rn "nullable=True" backend/app/models/
grep -rn "ondelete\|onupdate" backend/app/models/
```

### Task 2.2: Migration Safety Check
**Files to review:**
- `backend/alembic/versions/`

**Check for:**
- [ ] All migrations have downgrade functions
- [ ] No data-destructive operations without backup
- [ ] Proper handling of existing data
- [ ] Index creation is non-blocking where possible

### Task 2.3: Query Performance Review
**Files to review:**
- All files in `backend/app/api/endpoints/`
- All files in `backend/app/services/`

**Check for:**
- [ ] N+1 query patterns (loops with queries)
- [ ] Missing eager loading (joinedload, selectinload)
- [ ] Unbounded SELECT queries (no LIMIT)
- [ ] Complex queries without indexes
- [ ] Transaction handling (commit/rollback)

**Search patterns:**
```bash
grep -rn "db\.query\|session\.query" backend/app/
grep -rn "\.all\(\)" backend/app/
grep -rn "joinedload\|selectinload\|subqueryload" backend/app/
grep -rn "for.*in.*db\.query" backend/app/
```

### Task 2.4: Connection Pool Review
**Files to review:**
- `backend/app/db/database.py`
- `backend/app/core/config.py`

**Check for:**
- [ ] Pool size configuration
- [ ] Pool overflow settings
- [ ] Connection timeout settings
- [ ] Connection recycling
- [ ] Proper session cleanup

---

## Phase 3: API Quality

### Task 3.1: REST API Consistency
**Files to review:**
- All files in `backend/app/api/endpoints/`

**Check for:**
- [ ] Consistent HTTP status codes
- [ ] Consistent error response format
- [ ] Proper HTTP methods (GET for read, POST for create, etc.)
- [ ] Pagination on list endpoints
- [ ] Filtering and sorting options
- [ ] Rate limiting coverage

**Verify each endpoint has:**
- [ ] Input validation via Pydantic
- [ ] Authorization check
- [ ] Audit logging for mutations
- [ ] Proper error handling

### Task 3.2: OpenAPI Documentation
**Files to review:**
- `backend/app/main.py` (tags_metadata)
- All endpoint files

**Check for:**
- [ ] All endpoints have docstrings
- [ ] Response models defined
- [ ] Request body examples
- [ ] Error responses documented
- [ ] Tags properly assigned

**Search patterns:**
```bash
grep -rn "response_model\|status_code\|tags=" backend/app/api/endpoints/
grep -rn '"""' backend/app/api/endpoints/
```

### Task 3.3: Error Handling Consistency
**Files to review:**
- `backend/app/core/exception_handlers.py`
- All endpoint files

**Check for:**
- [ ] All exceptions caught and handled
- [ ] Consistent error response structure
- [ ] No sensitive info in error messages
- [ ] Proper HTTP status codes
- [ ] Logging of errors

**Search patterns:**
```bash
grep -rn "raise HTTPException" backend/app/
grep -rn "except.*:" backend/app/
grep -rn "try:" backend/app/
```

---

## Phase 4: Frontend Quality

### Task 4.1: TypeScript Type Safety
**Files to review:**
- All `.ts` and `.tsx` files in `frontend/src/`

**Check for:**
- [ ] Usage of `any` type
- [ ] Missing type definitions
- [ ] Type assertions (`as`)
- [ ] Non-null assertions (`!`)
- [ ] Proper interface/type definitions

**Search patterns:**
```bash
grep -rn ": any\|: any\[\]" frontend/src/
grep -rn " as " frontend/src/
grep -rn "\!\\." frontend/src/
```

### Task 4.2: React Best Practices
**Files to review:**
- All component files in `frontend/src/`

**Check for:**
- [ ] Missing dependency arrays in useEffect
- [ ] Missing cleanup functions in useEffect
- [ ] Inline function definitions in JSX (performance)
- [ ] Missing key props in lists
- [ ] Direct DOM manipulation
- [ ] Memory leaks (subscriptions, timers)

**Search patterns:**
```bash
grep -rn "useEffect\|useState\|useCallback\|useMemo" frontend/src/
grep -rn "\.map\(" frontend/src/
grep -rn "setInterval\|setTimeout\|addEventListener" frontend/src/
```

### Task 4.3: Error State Handling
**Files to review:**
- All page components in `frontend/src/pages/`

**Check for:**
- [ ] Loading states displayed
- [ ] Error states handled and displayed
- [ ] Empty states handled
- [ ] Network error handling
- [ ] Retry mechanisms

**Search patterns:**
```bash
grep -rn "loading\|isLoading\|setLoading" frontend/src/
grep -rn "error\|isError\|setError" frontend/src/
grep -rn "catch\|\.catch" frontend/src/
```

### Task 4.4: Accessibility (WCAG 2.1 AA)
**Files to review:**
- All component files

**Check for:**
- [ ] Alt text on images
- [ ] ARIA labels on interactive elements
- [ ] Keyboard navigation support
- [ ] Focus management
- [ ] Color contrast (check CSS)
- [ ] Form labels associated with inputs
- [ ] Skip links for navigation
- [ ] Screen reader support

**Search patterns:**
```bash
grep -rn "aria-\|role=" frontend/src/
grep -rn "alt=" frontend/src/
grep -rn "tabIndex\|onKeyDown\|onKeyPress" frontend/src/
grep -rn "<label" frontend/src/
```

### Task 4.5: State Management
**Files to review:**
- `frontend/src/context/`
- `frontend/src/services/api.ts`

**Check for:**
- [ ] Proper context usage (not overused)
- [ ] State updates are immutable
- [ ] No prop drilling (use context instead)
- [ ] API state caching strategy
- [ ] Optimistic updates where appropriate

---

## Phase 5: Testing Coverage

### Task 5.1: Backend Test Coverage
**Files to review:**
- `backend/tests/`

**Check for:**
- [ ] All API endpoints have tests
- [ ] Authentication tests
- [ ] Authorization tests (role-based)
- [ ] Input validation tests
- [ ] Error case tests
- [ ] Database transaction tests

**Run coverage:**
```bash
cd backend && pytest tests/ -v --cov=app --cov-report=html
```

### Task 5.2: Frontend Test Coverage
**Files to review:**
- All `*.test.ts` and `*.test.tsx` files

**Check for:**
- [ ] Critical components have tests
- [ ] Hook tests
- [ ] Utility function tests
- [ ] Form validation tests
- [ ] API integration tests (mocked)

**Run coverage:**
```bash
cd frontend && npm test -- --coverage
```

### Task 5.3: Missing Test Identification
**Identify files without tests:**
- Components in `frontend/src/components/`
- Pages in `frontend/src/pages/`
- Hooks in `frontend/src/hooks/`
- Services in `backend/app/services/`
- Endpoints in `backend/app/api/endpoints/`

---

## Phase 6: Compliance Audit

### Task 6.1: Audit Logging Coverage
**Files to review:**
- `backend/app/services/audit_service.py`
- All endpoint files

**Check for:**
- [ ] All CREATE operations logged
- [ ] All UPDATE operations logged
- [ ] All DELETE operations logged
- [ ] Login/logout logged
- [ ] Permission changes logged
- [ ] Failed access attempts logged
- [ ] Audit log integrity (hash chain)

**Search patterns:**
```bash
grep -rn "AuditService\|AuditLog\|log_action" backend/app/
grep -rn "@router\.\(post\|put\|patch\|delete\)" backend/app/api/endpoints/
```

### Task 6.2: Data Traceability
**Files to review:**
- `backend/app/api/endpoints/traceability.py`
- Models with lot/serial tracking

**Check for:**
- [ ] Lot number tracking
- [ ] Serial number tracking
- [ ] Parent-child relationships preserved
- [ ] Complete audit trail for parts

### Task 6.3: Access Control Matrix
**Files to review:**
- `backend/app/models/role_permission.py`
- `frontend/src/utils/permissions.ts`

**Document:**
- [ ] All roles defined
- [ ] All permissions defined
- [ ] Role-permission mapping
- [ ] Endpoint-permission mapping

---

## Phase 7: Code Quality

### Task 7.1: Dead Code Detection
**Search for:**
- [ ] Unused imports
- [ ] Unused functions/methods
- [ ] Commented-out code blocks
- [ ] Unreachable code
- [ ] TODO/FIXME/HACK comments

**Search patterns:**
```bash
grep -rn "TODO\|FIXME\|HACK\|XXX\|BUG" --include="*.py" --include="*.ts" --include="*.tsx"
grep -rn "# pylint: disable\|# noqa\|# type: ignore" backend/app/
grep -rn "// @ts-ignore\|// eslint-disable" frontend/src/
```

### Task 7.2: Code Duplication
**Check for:**
- [ ] Similar functions that could be consolidated
- [ ] Copy-pasted code blocks
- [ ] Similar API patterns that could be generalized

### Task 7.3: Complexity Analysis
**Check for:**
- [ ] Functions over 50 lines
- [ ] Files over 500 lines
- [ ] Deeply nested conditionals (> 3 levels)
- [ ] Functions with > 5 parameters

**Search patterns:**
```bash
wc -l backend/app/**/*.py | sort -n
wc -l frontend/src/**/*.tsx | sort -n
```

---

## Phase 8: Performance

### Task 8.1: Frontend Bundle Analysis
**Check for:**
- [ ] Large dependencies
- [ ] Unused dependencies
- [ ] Code splitting opportunities
- [ ] Lazy loading implementation

**Run analysis:**
```bash
cd frontend && npm run build -- --stats
npx webpack-bundle-analyzer build/bundle-stats.json
```

### Task 8.2: API Response Times
**Check for:**
- [ ] Slow queries (> 100ms)
- [ ] Large response payloads
- [ ] Missing pagination
- [ ] Caching opportunities

### Task 8.3: Database Query Analysis
**Check for:**
- [ ] Missing indexes on frequently queried columns
- [ ] Full table scans
- [ ] Unused indexes
- [ ] Query optimization opportunities

---

## Output Requirements

After completing all tasks, produce `QA_FINDINGS.md` with:

1. **Executive Summary**
   - Total issues by severity
   - Top 5 critical issues
   - Recommended priority order

2. **Detailed Findings**
   - Each issue with unique ID (e.g., SEC-001, PERF-002)
   - File path and line number
   - Category and severity
   - Assignee (Backend/Frontend)
   - Description and impact
   - Recommended fix

3. **Metrics**
   - Test coverage percentages
   - Files reviewed count
   - Issues by category breakdown

4. **Action Items**
   - Immediate actions (Critical)
   - Short-term actions (High)
   - Long-term improvements (Medium/Low)

---

## Execution Checklist

- [ ] Phase 1: Security Audit complete
- [ ] Phase 2: Database Review complete
- [ ] Phase 3: API Quality complete
- [ ] Phase 4: Frontend Quality complete
- [ ] Phase 5: Testing Coverage complete
- [ ] Phase 6: Compliance Audit complete
- [ ] Phase 7: Code Quality complete
- [ ] Phase 8: Performance complete
- [ ] QA_FINDINGS.md created and complete
