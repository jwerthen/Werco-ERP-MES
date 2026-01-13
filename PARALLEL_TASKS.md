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

### Priority 1: Data Export Functionality (CSV/Excel)
**Priority**: Medium | **Category**: Features  
**Files**: `backend/app/` only

- [ ] Create export endpoints for key entities:
  - GET /work-orders/export?format=csv|xlsx
  - GET /parts/export?format=csv|xlsx
  - GET /inventory/export?format=csv|xlsx
  - GET /purchase-orders/export?format=csv|xlsx
  - GET /quotes/export?format=csv|xlsx
- [ ] Add date range filtering (start_date, end_date params)
- [ ] Add column selection parameter
- [ ] Use openpyxl or xlsxwriter for Excel generation
- [ ] Use csv module for CSV generation
- [ ] Set proper Content-Disposition headers for download

**Estimated Time**: 4-6 hours

### Priority 2: Print-Friendly Report Endpoints
**Priority**: Low | **Category**: Features

- [ ] Create simplified JSON endpoints for print views:
  - GET /work-orders/{id}/print-data
  - GET /quotes/{id}/print-data
  - GET /purchase-orders/{id}/print-data
- [ ] Include all related data in single response (no additional API calls needed)
- [ ] Format dates and numbers for display

**Estimated Time**: 2-3 hours

### Priority 3: Unit Tests for PO Upload Raw Material Fix
**Priority**: Medium | **Category**: Testing

- [ ] Write tests for PO upload endpoint
- [ ] Test raw material parsing logic
- [ ] Test edge cases (missing fields, invalid data)
- [ ] Add to `backend/tests/`

**Estimated Time**: 2-3 hours

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
| Data Export (CSV/Excel) | Medium | ⬜ | N/A | ⬜ |
| PO Upload Unit Tests | Medium | ⬜ | N/A | ⬜ |
| Print Report Endpoints | Low | ⬜ | N/A | ⬜ |
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
