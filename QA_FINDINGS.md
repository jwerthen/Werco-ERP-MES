# QA/QC Code Review Findings

**Generated:** 2026-01-15  
**Reviewer:** QA/QC Review  
**Repository:** Werco ERP  
**Branch:** main  

---

## Executive Summary

| Severity | Count | Status |
|----------|-------|--------|
| Critical | 1 | Needs Fix |
| High | 5 | Needs Fix |
| Medium | 8 | Needs Fix |
| Low | 6 | Backlog |
| **Total** | **20** | |

### Top 5 Priority Issues
1. **[CRIT-001]** JWT tokens stored in localStorage (XSS vulnerable)
2. **[HIGH-001]** Extensive use of `any` type in frontend (100+ instances)
3. **[HIGH-002]** Default SECRET_KEY in config.py needs environment override
4. **[HIGH-003]** Missing rate limiting on webhook jobs (TODO in code)
5. **[HIGH-004]** Console.log statements left in production code

---

## Critical Issues (Fix Immediately)

### [CRIT-001] JWT Tokens Stored in localStorage - XSS Vulnerability Risk
- **File**: `frontend/src/services/api.ts:36-39, 221-232, 242-244`
- **Category**: Security
- **Assignee**: Frontend
- **Description**: JWT access tokens, refresh tokens, and expiry times are stored in localStorage. If an XSS attack succeeds, attackers can steal these tokens and impersonate users.
- **Impact**: Complete account takeover if XSS vulnerability is exploited
- **Recommendation**: 
  - Store tokens in httpOnly cookies (set by backend)
  - Use a BFF (Backend for Frontend) pattern
  - At minimum, store only short-lived access tokens in memory and use httpOnly refresh cookies
- **Code Location**:
```typescript
// frontend/src/services/api.ts:36-39
this.token = localStorage.getItem('token');
this.refreshToken = localStorage.getItem('refreshToken');
const expiresAt = localStorage.getItem('tokenExpiresAt');

// Lines 221-232 - setting tokens
localStorage.setItem('token', accessToken);
localStorage.setItem('refreshToken', refreshToken);
localStorage.setItem('tokenExpiresAt', this.tokenExpiresAt.toString());
```

---

## High Priority Issues

### [HIGH-001] Extensive Use of `any` Type in Frontend
- **File**: `frontend/src/services/api.ts` and multiple page files
- **Category**: Code Quality / Type Safety
- **Assignee**: Frontend
- **Description**: Found 100+ instances of `: any` type annotations across the codebase. This bypasses TypeScript's type checking and can lead to runtime errors.
- **Impact**: Reduced type safety, potential runtime errors, harder maintenance
- **Recommendation**: Define proper TypeScript interfaces for all API responses and data structures
- **Key Locations**:
  - `api.ts`: 50+ instances (createWorkCenter, updatePart, createBOM, etc.)
  - `AdminSettings.tsx`: 20+ instances
  - Various page components with `catch (err: any)`
- **Example Fix**:
```typescript
// Current (problematic)
async createWorkCenter(data: any) { ... }

// Recommended
interface WorkCenterCreate {
  name: string;
  code: string;
  hourly_rate: number;
  // ... other fields
}
async createWorkCenter(data: WorkCenterCreate) { ... }
```

### [HIGH-002] Default SECRET_KEY Values in Configuration
- **File**: `backend/app/core/config.py:27-28`
- **Category**: Security
- **Assignee**: Backend
- **Description**: Default secret keys are defined in code. While these should be overridden by environment variables, the defaults are insecure.
- **Impact**: If environment variables are not set, the application runs with known secret keys
- **Recommendation**: 
  - Remove default values or use empty strings that cause startup failure
  - Add validation on startup to ensure secrets are set
```python
# Current
SECRET_KEY: str = "CHANGE-THIS-IN-PRODUCTION"
REFRESH_TOKEN_SECRET_KEY: str = "CHANGE-THIS-REFRESH-SECRET"

# Recommended - fail fast if not configured
SECRET_KEY: str = Field(..., min_length=32)  # Required, no default
```

### [HIGH-003] Missing Rate Limiting Implementation for Webhooks
- **File**: `backend/app/jobs/webhook_jobs.py:151`
- **Category**: Security / Performance
- **Assignee**: Backend
- **Description**: TODO comment indicates Redis-based rate limiting is not implemented
- **Impact**: Webhook endpoints could be abused for DDoS attacks
- **Code**:
```python
# Line 151
# TODO: Implement Redis-based rate limiting
```

### [HIGH-004] Console.log Statements in Production Code
- **File**: Multiple frontend files
- **Category**: Code Quality / Security
- **Assignee**: Frontend
- **Description**: Console.log statements left in production code can leak sensitive information
- **Locations**:
  - `frontend/src/services/errorLogging.ts:119-124`
  - `frontend/src/pages/WorkOrderNew.tsx:133, 137`
  - `frontend/src/components/Tour/TourMenu.tsx:21, 27, 32`
- **Recommendation**: Remove console.log or use a logging service that can be disabled in production

### [HIGH-005] TODO/FIXME Comments Indicate Incomplete Features
- **File**: Multiple backend files
- **Category**: Code Quality
- **Assignee**: Backend
- **Description**: Several TODO comments indicate incomplete critical features
- **Locations**:
  - `backend/app/services/mrp_auto_service.py:249` - "TODO: Implement supplier part mapping"
  - `backend/app/services/mrp_auto_service.py:256` - "TODO: Implement supplier part pricing"
  - `backend/app/jobs/report_jobs.py:19` - "TODO: Implement report generation logic"
  - `backend/app/api/endpoints/errors.py:165` - "TODO: Add integration with alerting service"

---

## Medium Priority Issues

### [MED-001] No SQL Injection Found - Good Practice Confirmed
- **Category**: Security
- **Assignee**: Backend
- **Status**: PASSED
- **Description**: No raw SQL with f-string interpolation found. SQLAlchemy ORM used properly throughout.

### [MED-002] No XSS via dangerouslySetInnerHTML Found
- **Category**: Security
- **Assignee**: Frontend
- **Status**: PASSED
- **Description**: No usage of `dangerouslySetInnerHTML` found in the codebase.

### [MED-003] Extensive nullable=True in Database Models
- **File**: `backend/app/models/*.py`
- **Category**: Database Integrity
- **Assignee**: Backend
- **Description**: Many columns marked as nullable=True. Review each to ensure this is intentional and not leading to data integrity issues.
- **Impact**: Potential for incomplete data records
- **Examples**: 200+ instances across models including foreign keys and audit fields
- **Recommendation**: Review each nullable column and add NOT NULL constraints where data is required

### [MED-004] Many .all() Queries Without Pagination
- **File**: Multiple endpoint files
- **Category**: Performance
- **Assignee**: Backend
- **Description**: Numerous queries using `.all()` without `LIMIT`. Some have limits, but many don't.
- **Impact**: Performance issues with large datasets
- **Recommendation**: Add pagination to all list endpoints using the existing pagination utility

### [MED-005] Good Eager Loading Practices Found
- **Category**: Performance
- **Assignee**: Backend
- **Status**: PASSED
- **Description**: Excellent use of `joinedload` throughout the codebase to prevent N+1 queries.

### [MED-006] Error Handling Uses Broad Exception Catching
- **File**: Multiple frontend page files
- **Category**: Code Quality
- **Assignee**: Frontend
- **Description**: Most error handling uses `catch (err: any)` pattern, losing type information
- **Recommendation**: Create typed error handling utilities

### [MED-007] All Endpoints Have Authorization Checks
- **Category**: Security
- **Assignee**: Backend
- **Status**: PASSED
- **Description**: All 33 endpoint files import and use `get_current_user` or `Depends` for authorization.

### [MED-008] Rate Limiting Implemented at Application Level
- **Category**: Security
- **Assignee**: Backend
- **Status**: PASSED
- **Description**: Rate limiting implemented via slowapi in `main.py` with configurable paths.

---

## Low Priority Issues

### [LOW-001] Accessibility Partially Implemented
- **File**: Multiple component files
- **Category**: Accessibility (WCAG)
- **Assignee**: Frontend
- **Description**: ARIA attributes found in 12 component files. Good start but needs full audit.
- **Locations with ARIA**: Layout.tsx, FormField.tsx, ErrorFallback.tsx, etc.
- **Recommendation**: Complete WCAG 2.1 AA audit for all interactive components

### [LOW-002] Test Coverage Gaps
- **Category**: Testing
- **Assignee**: Both
- **Description**: Limited test files exist but coverage needs expansion
- **Backend Tests** (8 files):
  - test_auth.py, test_health.py, test_parts.py, test_po_upload.py
  - test_users.py, test_work_orders.py, test_services.py
- **Frontend Tests** (10 files):
  - Hooks: usePermissions, useOptimisticForm, useFormErrorHandling
  - Utils: permissions, optimisticLock
  - Components: PermissionGate, ProtectedRoute, Skeleton, LoadingButton, FormField
- **Missing**: Many pages and services lack tests

### [LOW-003] User Data Also Stored in localStorage
- **File**: `frontend/src/context/AuthContext.tsx:101-124`
- **Category**: Security
- **Assignee**: Frontend
- **Description**: User object stored in localStorage alongside tokens
- **Recommendation**: Keep user data in memory/context only

### [LOW-004] Session Storage Used for Error Tracking
- **File**: `frontend/src/services/errorLogging.ts:186-189`
- **Category**: Code Quality
- **Assignee**: Frontend
- **Description**: Session ID stored in sessionStorage - acceptable but document the rationale

### [LOW-005] Form Backup Using localStorage
- **File**: `frontend/src/components/ErrorBoundary/FormErrorBoundary.tsx:65, 196, 203`
- **Category**: Code Quality
- **Assignee**: Frontend
- **Description**: Form data backed up to localStorage for recovery - acceptable feature

### [LOW-006] Tour State in localStorage
- **File**: `frontend/src/context/TourContext.tsx:42, 86, 97`
- **Category**: Code Quality
- **Assignee**: Frontend
- **Description**: Tour completion state stored in localStorage - acceptable for UX preference

---

## Issues by Category

### Security Issues
| ID | Severity | Title | Assignee | Status |
|----|----------|-------|----------|--------|
| CRIT-001 | Critical | JWT tokens in localStorage | Frontend | Open |
| HIGH-002 | High | Default SECRET_KEY values | Backend | Open |
| HIGH-003 | High | Missing webhook rate limiting | Backend | Open |
| MED-001 | Medium | SQL Injection scan | Backend | PASSED |
| MED-002 | Medium | XSS scan | Frontend | PASSED |
| MED-007 | Medium | Authorization checks | Backend | PASSED |
| MED-008 | Medium | Rate limiting | Backend | PASSED |

### Code Quality Issues
| ID | Severity | Title | Assignee | Status |
|----|----------|-------|----------|--------|
| HIGH-001 | High | Extensive `any` type usage | Frontend | Open |
| HIGH-004 | High | Console.log in production | Frontend | Open |
| HIGH-005 | High | TODO/FIXME incomplete features | Backend | Open |
| MED-006 | Medium | Broad exception catching | Frontend | Open |

### Performance Issues
| ID | Severity | Title | Assignee | Status |
|----|----------|-------|----------|--------|
| MED-004 | Medium | Queries without pagination | Backend | Open |
| MED-005 | Medium | Eager loading (joinedload) | Backend | PASSED |

### Database Issues
| ID | Severity | Title | Assignee | Status |
|----|----------|-------|----------|--------|
| MED-003 | Medium | Extensive nullable columns | Backend | Review |

### Testing Issues
| ID | Severity | Title | Assignee | Status |
|----|----------|-------|----------|--------|
| LOW-002 | Low | Test coverage gaps | Both | Open |

### Accessibility Issues
| ID | Severity | Title | Assignee | Status |
|----|----------|-------|----------|--------|
| LOW-001 | Low | Partial ARIA implementation | Frontend | Open |

---

## Test Coverage Summary

### Backend Tests (8 files)
- `backend/tests/api/test_auth.py`
- `backend/tests/api/test_health.py`
- `backend/tests/api/test_parts.py`
- `backend/tests/api/test_po_upload.py`
- `backend/tests/api/test_users.py`
- `backend/tests/api/test_work_orders.py`
- `backend/tests/test_services.py`
- `backend/tests/conftest.py`

### Frontend Tests (10 files)
- Hooks: usePermissions, useOptimisticForm, useFormErrorHandling
- Utils: permissions, optimisticLock
- Components: PermissionGate, ProtectedRoute, Skeleton, LoadingButton, FormField

---

## Metrics

| Metric | Value |
|--------|-------|
| Total Files Reviewed | 150+ |
| Backend Endpoint Files | 33 |
| Frontend Page Files | 25+ |
| Backend Model Files | 20+ |
| Backend Test Files | 8 |
| Frontend Test Files | 10 |
| Security Patterns Checked | 6 |
| Issues Found | 20 |
| Issues Passed | 5 |

---

## Action Items

### Immediate Actions (Critical - Fix within 24 hours)
- [ ] **CRIT-001**: Migrate JWT token storage from localStorage to httpOnly cookies

### Short-term Actions (High - Fix within 1 week)
- [ ] **HIGH-001**: Create TypeScript interfaces for all API data types
- [ ] **HIGH-002**: Add startup validation for required secret keys
- [ ] **HIGH-003**: Implement Redis-based rate limiting for webhooks
- [ ] **HIGH-004**: Remove or conditionally disable console.log statements
- [ ] **HIGH-005**: Address TODO items for supplier mapping and report generation

### Medium-term Actions (Medium - Fix within 1 month)
- [ ] **MED-003**: Audit nullable columns and add constraints where needed
- [ ] **MED-004**: Add pagination to remaining list endpoints
- [ ] **MED-006**: Create typed error handling utilities

### Long-term Improvements (Low - Backlog)
- [ ] **LOW-001**: Complete WCAG 2.1 AA accessibility audit
- [ ] **LOW-002**: Expand test coverage to all pages and services
- [ ] **LOW-003**: Review all localStorage usage for security implications

---

## Notes for Development Teams

### For Backend Team
1. Priority: Ensure SECRET_KEY and REFRESH_TOKEN_SECRET_KEY are always set via environment variables
2. Review TODO comments and create tickets for incomplete features
3. Add pagination limits to all `.all()` queries that could return large datasets
4. Excellent job on authorization checks and eager loading!

### For Frontend Team
1. **Critical Priority**: Work with backend to implement httpOnly cookie-based auth
2. Create a `types/` directory with interfaces for all API responses
3. Replace all `catch (err: any)` with typed error handling
4. Remove or disable console.log statements in production builds
5. Good job on partial accessibility implementation - complete the audit

---

## Review Progress

- [x] Phase 1: Security Audit
- [x] Phase 2: Database & Data Integrity
- [x] Phase 3: API Quality
- [x] Phase 4: Frontend Quality
- [x] Phase 5: Testing Coverage
- [x] Phase 6: Compliance Audit
- [x] Phase 7: Code Quality
- [x] Phase 8: Performance

---

*Generated by QA/QC Code Review - 2026-01-15*
