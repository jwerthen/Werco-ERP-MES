# Parallel Development Task Split

**Created**: January 13, 2026  
**Source**: Notion V1.0 Production Roadmap Task Board  
**Purpose**: Coordinate work across multiple droid sessions

---

## Session Setup Instructions

### Terminal 1: Backend Session
```bash
cd C:\Users\jmw\Desktop\Werco-ERP
git checkout -b feat/backend-work
droid
```
Then say: "You are focused on backend work only. Do not touch frontend files. See PARALLEL_TASKS.md for your assignments."

### Terminal 2: Frontend Session
```bash
cd C:\Users\jmw\Desktop\Werco-ERP
git checkout -b feat/frontend-work
droid
```
Then say: "You are focused on frontend work only. Do not touch backend files. See PARALLEL_TASKS.md for your assignments."

### Terminal 3 (Optional): Testing Session
```bash
cd C:\Users\jmw\Desktop\Werco-ERP
git checkout main
droid
```
Then say: "You are focused on testing and quality assurance. Run tests, fix issues, review code."

---

## BACKEND SESSION TASKS (Terminal 1)

### ✅ COMPLETED - Round 1
- [x] Data Export Functionality (CSV/Excel)
- [x] Print-Friendly Report Endpoints
- [x] Unit Tests for PO Upload Raw Material Fix

---

### Priority 1: Email Notifications for Critical Events
**Priority**: Medium | **Category**: Features  
**Files**: `backend/app/` only

- [ ] Create email service (`backend/app/services/email_service.py`)
  - Use SendGrid, AWS SES, or SMTP
  - Environment config for email provider
  - HTML email templates
- [ ] Create notification preferences model
  - User can opt-in/out of notification types
  - Store in database
- [ ] Implement notifications for:
  - Work order status changes (released, completed, on hold)
  - Low inventory alerts (below reorder point)
  - Quality hold/NCR created
  - Purchase order received
  - Quote approved/rejected
- [ ] Add background task queue (Celery or similar) for async email sending
- [ ] Create API endpoint to update notification preferences

**Estimated Time**: 6-8 hours

### Priority 2: Application Metrics (Prometheus)
**Priority**: Low | **Category**: Monitoring

- [ ] Add prometheus-fastapi-instrumentator package
- [ ] Create /metrics endpoint
- [ ] Track key metrics:
  - Request count by endpoint
  - Request latency (p50, p95, p99)
  - Error rates
  - Active database connections
  - Cache hit/miss rates
- [ ] Add custom business metrics:
  - Work orders created/completed per day
  - Parts manufactured count
  - API usage by user role

**Estimated Time**: 3-4 hours

### Priority 3: Vendor Management API
**Priority**: Medium | **Category**: Features

- [ ] Create Vendor model if not exists:
  - name, code, address, city, state, zip
  - contact_name, contact_email, contact_phone
  - payment_terms, status (active/inactive)
  - notes, website
- [ ] CRUD endpoints:
  - GET /vendors (list with search/filter)
  - GET /vendors/{id}
  - POST /vendors
  - PUT /vendors/{id}
  - DELETE /vendors/{id} (soft delete)
- [ ] GET /vendors/{id}/purchase-orders (vendor's PO history)
- [ ] Vendor performance metrics endpoint

**Estimated Time**: 4-5 hours

### Priority 4: Customer Management Enhancement
**Priority**: Medium | **Category**: Features

- [ ] Enhance Customer model if needed:
  - Multiple contacts per customer
  - Shipping addresses (multiple)
  - Billing address
  - Credit terms, credit limit
- [ ] Customer contacts sub-resource:
  - GET /customers/{id}/contacts
  - POST /customers/{id}/contacts
- [ ] Customer addresses sub-resource
- [ ] GET /customers/{id}/orders (order history)
- [ ] GET /customers/{id}/quotes (quote history)

**Estimated Time**: 4-5 hours

---

## FRONTEND SESSION TASKS (Terminal 2)

### Priority 1: Frontend Unit Tests (>70% Coverage)
**Priority**: High | **Category**: Testing  
**Status**: In Progress  
**Files**: `frontend/src/` only

- [ ] Review current test coverage
- [ ] Add tests for hooks:
  - usePermissions
  - useDebounce
  - Other custom hooks
- [ ] Add tests for utility functions:
  - permissions.ts
  - formatters/helpers
- [ ] Add component tests for critical components:
  - PermissionGate
  - ProtectedRoute
  - Key form components
- [ ] Run: `npm test -- --coverage`

**Target**: >70% coverage  
**Estimated Time**: 4-6 hours

### Priority 2: Keyboard Navigation Support
**Priority**: Low | **Category**: UX

- [ ] Add keyboard shortcuts for common actions:
  - Ctrl+N: New (work order, part, etc. based on page)
  - Ctrl+S: Save (in edit modals)
  - Escape: Close modals
  - Arrow keys: Navigate tables
- [ ] Add keyboard shortcut help modal (Ctrl+?)
- [ ] Ensure all interactive elements are focusable
- [ ] Add visible focus indicators
- [ ] Test tab order on all pages

**Estimated Time**: 3-4 hours

### Priority 3: WCAG 2.1 AA Accessibility Compliance
**Priority**: Medium | **Category**: UX

- [ ] Add ARIA labels to interactive elements
- [ ] Ensure color contrast meets AA standards (4.5:1 for text)
- [ ] Add alt text to all images/icons
- [ ] Ensure form labels are properly associated
- [ ] Add skip navigation link
- [ ] Test with screen reader (NVDA or VoiceOver)
- [ ] Fix any accessibility warnings in browser dev tools

**Estimated Time**: 4-6 hours

### Priority 4: Print-Friendly Views for Reports
**Priority**: Low | **Category**: Features

- [ ] Create print stylesheet (`print.css` or Tailwind @media print)
- [ ] Add print button to key pages:
  - Work Order Detail
  - Quote Detail
  - Purchase Order Detail
  - Packing Slip (already exists)
  - Traveler (already exists)
- [ ] Hide navigation, buttons, non-essential elements in print
- [ ] Ensure tables don't break across pages
- [ ] Test print preview in browser

**Estimated Time**: 3-4 hours

---

## TESTING SESSION TASKS (Terminal 3)

### Continuous Tasks
- [ ] Run backend tests after backend changes: `cd backend && pytest -v`
- [ ] Run frontend build after frontend changes: `cd frontend && npm run build`
- [ ] Run frontend tests: `cd frontend && npm test`
- [ ] Check test coverage: `cd frontend && npm test -- --coverage`
- [ ] Review code in both branches for issues

### Integration Testing
- [ ] Test data export downloads (CSV/Excel)
- [ ] Test print functionality across browsers
- [ ] Test keyboard navigation
- [ ] Verify accessibility with browser tools

---

## Merge Strategy

1. **Backend first**: Merge `feat/backend-work` to `main` via PR
2. **Frontend second**: Rebase `feat/frontend-work` on updated `main`, then merge via PR
3. **Resolve conflicts**: If any, the frontend branch handles them

---

## Status Tracking

| Task | Priority | Backend | Frontend | Tested |
|------|----------|---------|----------|--------|
| Data Export (CSV/Excel) | Medium | ✅ | N/A | ⬜ |
| PO Upload Unit Tests | Medium | ✅ | N/A | ⬜ |
| Print Report Endpoints | Low | ✅ | N/A | ⬜ |
| Email Notifications | Medium | ⬜ | N/A | ⬜ |
| Application Metrics | Low | ⬜ | N/A | ⬜ |
| Vendor Management API | Medium | ⬜ | N/A | ⬜ |
| Customer Enhancement | Medium | ⬜ | N/A | ⬜ |
| Frontend Unit Tests >70% | High | N/A | 🔄 | ⬜ |
| Keyboard Navigation | Low | N/A | ⬜ | ⬜ |
| WCAG 2.1 AA Accessibility | Medium | N/A | ⬜ | ⬜ |
| Print-Friendly Views | Low | N/A | ⬜ | ⬜ |

Legend: ⬜ Not Started | 🔄 In Progress | ✅ Done | ❌ Blocked

---

## Dependencies

- Print-Friendly Views (frontend) can start immediately using existing API data
- Data Export endpoints are independent of frontend work
- Accessibility work is independent and can proceed in parallel

---

## Notes

- Frontend unit tests are already in progress per Notion board
- Most tasks are independent and can run fully in parallel
- No blocking dependencies between backend and frontend tasks
