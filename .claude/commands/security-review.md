Perform a security audit of the Werco ERP/MES codebase. This is a manufacturing system subject to AS9100D/ISO 9001 compliance — security issues have real regulatory consequences.

## Audit Scope

Review the following areas systematically. For each finding, report: file path, severity (Critical/High/Medium/Low), description, and recommended fix.

### 1. Authentication & Session Management
- Check `backend/app/core/security.py` and `backend/app/api/endpoints/auth.py`
- Check `frontend/src/services/api.ts` for token storage (CRIT-001: JWT in localStorage is a known issue)
- Verify token expiry, refresh token rotation, session invalidation
- Check for hardcoded secrets or default credentials in config

### 2. Authorization & Access Control
- Verify every endpoint in `backend/app/api/endpoints/` uses `require_role()` dependency
- Check for privilege escalation paths (can operator access admin endpoints?)
- Verify `PermissionGate` is used on sensitive frontend routes/actions
- Check that audit-critical operations (delete, approve, release) require appropriate roles

### 3. Input Validation & Injection
- Check all Pydantic schemas in `backend/app/schemas/` for proper field constraints
- Look for raw SQL queries (should use SQLAlchemy ORM exclusively)
- Check for command injection in file upload handlers (`po_upload.py`, `dxf_parser.py`)
- Verify HTML sanitization (bleach) on user-generated content
- Check Zod schemas in `frontend/src/validation/` match backend constraints

### 4. OWASP Top 10 Checks
- **A01 Broken Access Control**: Missing auth checks, IDOR vulnerabilities
- **A02 Cryptographic Failures**: Weak hashing, exposed secrets, insecure transport
- **A03 Injection**: SQL, command, LDAP, XSS
- **A04 Insecure Design**: Missing rate limits, no account lockout
- **A05 Security Misconfiguration**: Debug mode, default credentials, CORS too permissive
- **A06 Vulnerable Components**: Check `requirements.txt` and `package.json` for known CVEs
- **A07 Authentication Failures**: Weak password policy, no MFA
- **A08 Data Integrity Failures**: Unsigned tokens, missing CSRF protection
- **A09 Logging Failures**: Verify `AuditService` covers all mutations
- **A10 SSRF**: Check external API calls (Anthropic, email, webhooks)

### 5. Manufacturing/Compliance-Specific Concerns
- Can audit logs be tampered with or deleted?
- Are soft deletes enforced (no `DELETE FROM` or `session.delete()` without SoftDeleteMixin)?
- Is traceability chain intact (lot/serial from receiving → WIP → shipping)?
- Are document revisions immutable once approved?
- Can ECO approval workflow be bypassed?

## Known Issues (from QA_FINDINGS.md)
Reference these existing findings and check if they've been fixed:
- CRIT-001: JWT tokens in localStorage
- HIGH-002: Default SECRET_KEY
- HIGH-003: Missing rate limiting on webhook jobs
- HIGH-005: TODO comments indicating incomplete security features

## Output Format
Summarize findings in a table grouped by severity, then provide detailed remediation steps for Critical and High issues.
