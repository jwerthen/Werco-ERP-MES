# Werco ERP V1.0 Smoke Tests

## Prerequisites
- Production backend and frontend deployed
- Admin credentials available
- At least one work center exists
- At least one part with routing exists

## Smoke Test Checklist (15–20 minutes)

### 1) Auth
- [ ] Open login page
- [ ] Login as admin (email/password)
- [ ] Logout

### 2) Work Orders
- [ ] Create a work order for a part with routing
- [ ] Release work order
- [ ] Verify first operation status = READY

### 3) Shop Floor (Full View)
- [ ] Navigate to `/shop-floor`
- [ ] Select a work center
- [ ] Clock in to a READY operation
- [ ] Clock out with quantity produced
- [ ] Verify operation progress updated

### 4) Kiosk (Simplified View)
- [ ] Open `/shop-floor/operations?kiosk=1`
- [ ] Login with employee ID
- [ ] Start an operation
- [ ] Complete operation (partial or full)
- [ ] Sign out with employee ID

### 5) Analytics
- [ ] Open Analytics dashboard
- [ ] Production Trends chart renders
- [ ] No 403 errors for admin/manager/supervisor roles

### 6) Reports
- [ ] Export any report as CSV

## Notes
- If Production Trends is empty, confirm there are either RUN time entries or completed operations with actual hours.
