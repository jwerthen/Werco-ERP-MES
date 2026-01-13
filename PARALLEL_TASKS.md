# Parallel Development Task Split

**Created**: January 13, 2026  
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

### Priority 1: CMMC Password Policy (IA-3.5.7/8/9)
**Branch**: `feat/backend-work`  
**Files**: `backend/app/` only

- [ ] Add password validation to User model/auth
  - Minimum 12 characters
  - Uppercase, lowercase, numbers, special chars
  - Check against common passwords list
- [ ] Add password history tracking
  - New table: `password_history` (user_id, hashed_password, created_at)
  - Prevent reuse of last 12 passwords
- [ ] Add password expiration
  - Add `password_expires_at` to User model
  - 90-day expiration policy
  - Force password change on login if expired
- [ ] Migration for new fields/tables

**Estimated Time**: 4-6 hours

### Priority 2: Session Inactivity Enhancement (AC-3.1.10)
- [ ] Add `last_activity_at` timestamp to session tracking
- [ ] Create middleware to check inactivity on each request
- [ ] Return 401 with `session_expired` reason if inactive > 15 min
- [ ] Add API endpoint to refresh activity timestamp

**Estimated Time**: 2-3 hours

### Priority 3: Vendor Management API
- [ ] Create Vendor model (name, code, address, contact, status)
- [ ] CRUD endpoints: GET/POST/PUT/DELETE /vendors
- [ ] Vendor search endpoint
- [ ] Link to existing PurchaseOrder model

**Estimated Time**: 3-4 hours

---

## FRONTEND SESSION TASKS (Terminal 2)

### Priority 1: Password Change UI
**Branch**: `feat/frontend-work`  
**Files**: `frontend/src/` only

- [ ] Create PasswordChangeModal component
  - Current password field
  - New password field with strength indicator
  - Confirm password field
  - Show requirements (12 chars, uppercase, etc.)
- [ ] Add "Change Password" option to user menu/settings
- [ ] Handle password expiration redirect
  - If API returns 401 with `password_expired`, show change modal
- [ ] Add API methods for password change

**Estimated Time**: 3-4 hours

### Priority 2: Session Timeout Warning UI
- [ ] Update AuthContext to track activity
- [ ] Show warning modal at 14 minutes of inactivity
- [ ] "Stay Logged In" button to refresh session
- [ ] Auto-logout at 15 minutes with message

**Note**: Frontend already has 15-min idle timeout, verify it matches backend.

**Estimated Time**: 1-2 hours

### Priority 3: Vendors Page
- [ ] Create Vendors.tsx page
  - List view with search/filter
  - Add/Edit modal
  - Status badges (active/inactive)
- [ ] Add to navigation (under Purchasing or Admin)
- [ ] Connect to backend API (once available)

**Estimated Time**: 3-4 hours

### Priority 4: MFA Setup UI (Prep for later)
- [ ] Create MFASetupModal component
  - QR code display area
  - 6-digit code input
  - Backup codes display
- [ ] Add to user settings/security section
- [ ] Stub API methods (will implement backend later)

**Estimated Time**: 2-3 hours

---

## TESTING SESSION TASKS (Terminal 3)

### Continuous Tasks
- [ ] Run backend tests after backend changes: `cd backend && pytest -v`
- [ ] Run frontend build after frontend changes: `cd frontend && npm run build`
- [ ] Run frontend tests: `cd frontend && npm test`
- [ ] Review code in both branches for issues

### Integration Testing
- [ ] Once both branches are ready, test password flow end-to-end
- [ ] Test session timeout behavior
- [ ] Verify no regressions in existing features

---

## Merge Strategy

1. **Backend first**: Merge `feat/backend-work` to `main` via PR
2. **Frontend second**: Rebase `feat/frontend-work` on updated `main`, then merge via PR
3. **Resolve conflicts**: If any, the frontend branch handles them

---

## Communication Points

When backend completes an API:
- Update this file with "✅ DONE" 
- Note any API changes from the original plan

When frontend needs an API not yet ready:
- Use mock data temporarily
- Note the dependency here

---

## Status Tracking

| Task | Backend | Frontend | Tested |
|------|---------|----------|--------|
| Password Policy | ⬜ | ⬜ | ⬜ |
| Session Inactivity | ⬜ | ⬜ | ⬜ |
| Vendors | ⬜ | ⬜ | ⬜ |
| MFA Prep | N/A | ⬜ | ⬜ |

Legend: ⬜ Not Started | 🔄 In Progress | ✅ Done | ❌ Blocked
