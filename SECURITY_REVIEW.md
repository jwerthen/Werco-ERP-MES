# Werco ERP/MES Platform — Security Review

**Date:** 2026-03-30
**Scope:** Full platform (backend, frontend, infrastructure)
**Overall Risk Level:** MEDIUM-HIGH

---

## Executive Summary

This security review covers the entire Werco ERP/MES platform including the FastAPI backend, React frontend, Docker infrastructure, CI/CD pipelines, and deployment configuration. The platform has good foundational security (password hashing, JWT authentication, rate limiting, non-root Docker containers), but several critical and high-severity issues require immediate attention.

| Severity | Count |
|----------|-------|
| Critical | 4 |
| High | 7 |
| Medium | 10 |
| Low | 5 |

---

## CRITICAL Vulnerabilities

### 1. JWT Tokens Stored in localStorage (Frontend)

**Files:**
- `frontend/src/services/api.ts` — token interceptor reads from localStorage
- `frontend/src/context/AuthContext.tsx` — stores tokens via localStorage

**Issue:** Access and refresh tokens are stored in `localStorage`, which is accessible to any JavaScript running on the page. If an XSS vulnerability exists anywhere in the application, an attacker can exfiltrate all tokens.

**Recommendation:**
- Migrate to httpOnly, Secure, SameSite cookies for token storage
- Implement a backend-for-frontend (BFF) pattern if needed
- At minimum, use sessionStorage and reduce token lifetimes

---

### 2. Database Reset Endpoint Available in Production

**File:** `backend/app/api/endpoints/auth.py:540`

**Issue:** The `/reset-database` endpoint truncates all tables. It is gated by `ALLOW_DB_RESET` which **defaults to `true`** and only requires the `SECRET_KEY` as authorization. This endpoint could wipe the entire production database if the environment variable is not explicitly set to `false`.

```python
db.execute(text(f'TRUNCATE TABLE "{table}" CASCADE'))
```

**Recommendation:**
- Change default to `ALLOW_DB_RESET=false`
- Remove this endpoint entirely from production builds
- If retained for development, require multi-factor verification

---

### 3. Hardcoded Default Database Password in Docker Compose

**File:** `docker-compose.yml:10,46,103`

**Issue:** The development docker-compose uses a fallback default password:
```yaml
POSTGRES_PASSWORD: ${DB_PASSWORD:-werco_secure_password}
```
Anyone with repo access can start containers with known credentials.

**Recommendation:**
- Remove default fallback — require explicit `DB_PASSWORD` environment variable
- Fail fast if credentials are not provided

---

### 4. Direct API Token in Frontend Fetch Call

**File:** `frontend/src/pages/JobCosting.tsx:293-299`

**Issue:** A direct `fetch()` call bypasses the centralized API service and manually injects the auth token from localStorage, circumventing any centralized security controls.

**Recommendation:**
- Use the centralized API service (`api.ts`) for all requests
- Audit for other direct `fetch()` or `XMLHttpRequest` calls

---

## HIGH Severity Vulnerabilities

### 5. XSS via innerHTML in Print Component

**File:** `frontend/src/components/PrintButton.tsx`

**Issue:** The component uses plain HTML injection for print functionality, which could allow XSS if user-controlled data flows into the rendered content.

**Recommendation:**
- Sanitize all HTML content before injection using DOMPurify
- Avoid innerHTML; use React's rendering pipeline

---

### 6. Missing CSRF Protection

**File:** `frontend/src/services/api.ts`

**Issue:** No CSRF tokens are included in API requests. While JWT-based auth provides some protection, localStorage-based tokens combined with permissive CORS could allow cross-origin attacks.

**Recommendation:**
- Implement CSRF token validation for state-changing operations
- Use `SameSite=Strict` cookies if migrating to cookie-based auth

---

### 7. Missing File Upload Validation

**File:** `backend/app/api/endpoints/documents.py:106-114`

**Issue:** File uploads lack MIME type validation, content inspection, and handler-level size limits. Only the file extension is checked.

```python
file_ext = os.path.splitext(file.filename)[1]
# No content-type or magic number validation
```

**Recommendation:**
- Validate MIME type against an allowlist
- Check file magic numbers (use `python-magic`)
- Enforce file size limits at the handler level
- Store uploads outside the webroot

---

### 8. Subprocess Command Execution for Document Processing

**File:** `backend/app/services/pdf_service.py:278-310`

**Issue:** Legacy `.doc` file extraction uses `subprocess.run(['antiword', abs_path])`. While list format is used (not `shell=True`), insufficient path validation could lead to issues.

**Recommendation:**
- Validate file paths strictly with `pathlib.Path.resolve()`
- Consider pure-Python alternatives
- Add per-user rate limiting on document processing

---

### 9. User Profile Stored in localStorage

**File:** `frontend/src/context/AuthContext.tsx:103-126`

**Issue:** Complete user profile (email, role, department, employee_id) is stored in plain text in localStorage.

**Recommendation:**
- Store minimal data client-side
- Move user profile to server-side session or httpOnly cookies
- Implement integrity verification for any client-stored data

---

### 10. Database Backup Scripts Expose Credentials

**Files:**
- `scripts/backup_database.py:77-78`
- `scripts/db-backup.ps1:168`

**Issue:** `PGPASSWORD` is set as an environment variable, visible in process listings.

**Recommendation:**
- Use `.pgpass` files instead of `PGPASSWORD` environment variable
- Ensure backup scripts run with restricted process visibility

---

### 11. Vulnerable Frontend Dependencies

**File:** `frontend/package.json`

**Issue:** Several dependencies may have known vulnerabilities (axios, handlebars, flatted). The CI pipeline's `safety check || true` allows builds to pass even with known vulnerabilities.

**Recommendation:**
- Run `npm audit` and update vulnerable packages
- Remove `|| true` from safety check in CI (`ci-cd.yml:219`)
- Integrate Dependabot or Snyk for automated vulnerability scanning

---

## MEDIUM Severity Vulnerabilities

### 12. Weak Client IP Detection (IP Spoofing)

**File:** `backend/app/middleware/logging_middleware.py:129-144`

**Issue:** `X-Forwarded-For` header is trusted without validation, allowing attackers to spoof IP addresses and bypass rate limiting.

**Recommendation:**
- Configure a trusted proxy list
- Only trust forwarding headers from known proxies

---

### 13. CORS Origins Not Validated

**File:** `backend/app/core/config.py:104-111`

**Issue:** CORS origins are parsed from a comma-separated string without URL validation. A wildcard `*` could be accidentally set.

**Recommendation:**
- Validate each origin as a proper URL
- Reject wildcard origins in production
- Require HTTPS for non-localhost origins

---

### 14. Redis Unauthenticated in Development

**File:** `docker-compose.yml:36`

**Issue:** Redis started without `--requirepass`, allowing unauthenticated access to cache data on exposed port 6379.

**Recommendation:**
- Add `--requirepass` flag even in development
- Bind Redis to localhost only

---

### 15. Exposed Database and Redis Ports in Development

**File:** `docker-compose.yml:15,28`

**Issue:** PostgreSQL (5432) and Redis (6379) ports are exposed to the host network.

**Recommendation:**
- Bind to `127.0.0.1:5432:5432` instead of `5432:5432`
- Use Docker networks for inter-service communication

---

### 16. Inadequate Session Timeout Implementation

**File:** `frontend/src/context/AuthContext.tsx:44-68`

**Issue:** 15-minute idle timeout is client-side only with no server-side session validation. Tokens remain valid after client-side logout.

**Recommendation:**
- Implement server-side token invalidation
- Add token revocation list (blacklist)
- Implement cross-tab logout via storage events

---

### 17. Kiosk Mode Controlled by URL Parameter

**File:** `frontend/src/App.tsx:118-130`

**Issue:** Kiosk mode is determined by URL parameters without server-side validation, potentially allowing privilege escalation.

**Recommendation:**
- Validate kiosk mode status on the backend
- Store kiosk configuration server-side

---

### 18. Error Logging Exposes Sensitive Information

**File:** `frontend/src/services/errorLogging.ts:88-104`

**Issue:** Full stack traces, URLs with parameters, and user agent strings are logged, potentially exposing code structure and sensitive data.

**Recommendation:**
- Sanitize error messages before logging
- Strip sensitive URL parameters
- Limit stack trace depth in production

---

### 19. No Secrets Manager Integration (Production)

**File:** `docker-compose.prod.yml`

**Issue:** All secrets passed via environment variables from `.env.prod` file. No integration with Docker Secrets or external secret managers.

**Recommendation:**
- Integrate with Docker Secrets, AWS Secrets Manager, or HashiCorp Vault
- Implement secret rotation mechanisms

---

### 20. Bandit Security Scanner Configured with Limited Scope

**File:** `.github/workflows/ci-cd.yml:56`

**Issue:** `bandit -r app -s B101 -ll` skips B101 and only shows high/very-high severity issues, missing medium-severity vulnerabilities.

**Recommendation:**
- Run Bandit at `-l` (low) severity in CI
- Review and address suppressed rules periodically

---

### 21. CSP Allows `unsafe-inline`

**File:** `nginx/nginx.prod.conf:95-96`

**Issue:** Content Security Policy includes `'unsafe-inline'` for scripts and styles, reducing XSS protection.

**Recommendation:**
- Use nonce-based CSP for inline scripts/styles
- Migrate inline scripts to external files

---

## LOW Severity Vulnerabilities

### 22. Missing HSTS Preload Directive

**File:** `backend/app/main.py:373`

**Issue:** HSTS header missing `preload` directive.

**Fix:** `"max-age=63072000; includeSubDomains; preload"`

---

### 23. Weak Random Session ID Generation

**File:** `frontend/src/services/errorLogging.ts:170`

**Issue:** Uses `Math.random()` (not cryptographically secure) for session IDs.

**Fix:** Use `crypto.getRandomValues()`.

---

### 24. Hardcoded Test Credentials in Load Tests

**File:** `load-tests/config.js:9-12`

**Issue:** Default test email `admin@werco.com` is hardcoded.

**Fix:** Read from environment variables only, with no defaults.

---

### 25. SQL Echo in Debug Mode

**File:** `backend/app/db/database.py:20`

**Issue:** `echo=settings.DEBUG` logs all SQL queries including potentially sensitive data.

**Fix:** Disable SQL echo by default; enable only on explicit demand.

---

### 26. Sensitive Data in Query Parameter Logs

**File:** `backend/app/core/logging.py:58-96`

**Issue:** Request query parameters logged without filtering sensitive values.

**Fix:** Redact common sensitive parameter names (password, token, secret, key, api_key).

---

## Positive Security Findings

The platform does implement several important security controls:

- **Password hashing** with bcrypt
- **JWT authentication** with access/refresh token pattern
- **Account lockout** after failed login attempts (5 attempts/min)
- **Rate limiting** on authentication endpoints
- **Non-root Docker containers** in all Dockerfiles
- **Multi-stage Docker builds** to minimize attack surface
- **Security headers** (X-Frame-Options, X-Content-Type-Options, HSTS, Referrer-Policy)
- **Internal Docker networks** in production compose
- **Pre-commit hooks** with security linting
- **Proper .gitignore** excluding .env files, secrets, and certificates

---

## Remediation Priority

### Immediate (This Sprint)
1. Change `ALLOW_DB_RESET` default to `false`
2. Remove hardcoded default passwords from docker-compose.yml
3. Migrate JWT tokens from localStorage to httpOnly cookies
4. Add file upload content validation

### Short-Term (Next 2 Sprints)
5. Implement CSRF protection
6. Sanitize HTML in PrintButton component
7. Fix IP spoofing in rate limiter
8. Update vulnerable npm dependencies
9. Remove `safety check || true` from CI pipeline

### Medium-Term (Next Quarter)
10. Integrate secrets manager for production
11. Implement server-side session management
12. Add nonce-based CSP (remove unsafe-inline)
13. Implement token revocation/blacklist
14. Add comprehensive input validation with zod
