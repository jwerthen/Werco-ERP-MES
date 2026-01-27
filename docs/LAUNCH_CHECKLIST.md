# Werco ERP V1.0 Launch Checklist

## Must-have (Blockers)
- [ ] Production env vars configured (Railway): `ENVIRONMENT=production`, `DEBUG=false`, `SECRET_KEY`, `REFRESH_TOKEN_SECRET_KEY`, `CORS_ORIGINS`, `REACT_APP_API_URL`
- [ ] Database migrations applied (Alembic) or verified schema matches app models
- [ ] Admin login verified and role permissions confirmed
- [ ] Shop floor clock-in/out flow validated (both full and kiosk modes)
- [ ] Analytics dashboards load without 403 for admin/manager/supervisor users
- [ ] Health endpoints return 200: `/health`, `/health/ready`
- [ ] Backup and restore procedure documented and tested (at least once)

## Should-have (Recommended)
- [ ] Sentry DSN configured for error tracking
- [ ] Redis configured for caching/rate limiting
- [ ] Rate limiting confirmed for auth and employee ID login
- [ ] Custom domain configured in Railway + SSL verified
- [ ] Seed data created or production data imported

## Kiosk Mode
- [ ] Confirm kiosk-only simplified view: `/shop-floor/operations?kiosk=1`
- [ ] Verify operator-only access enforced in kiosk mode
- [ ] Kiosk sign-in/out using 4-digit employee ID
- [ ] Work center filtering via `dept`, `work_center_id`, or `work_center_code`

## Smoke Test (15 minutes)
- [ ] Login (admin)
- [ ] Create work order → release → verify operations
- [ ] Clock in/out from shop floor (full view)
- [ ] Kiosk sign-in → start/complete operation (simplified view)
- [ ] Analytics: Production Trends chart loads
- [ ] Export at least one report (CSV)
- [ ] Run launch verification: `railway run --service werco-api python -m scripts.verify_launch`
- [ ] Run smoke test script: `docs/SMOKE_TESTS.md`
