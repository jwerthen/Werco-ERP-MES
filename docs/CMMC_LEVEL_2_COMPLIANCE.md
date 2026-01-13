# CMMC Level 2 Compliance Roadmap

## Overview

**CMMC Level 2** requires implementation of **110 security controls** from **NIST SP 800-171** across **14 control families**. This document tracks Werco ERP's compliance status and remediation roadmap.

**Target Certification Date**: _________________  
**Last Updated**: January 2026  
**Assessment Type**: Third-Party (C3PAO)

---

## Executive Summary

| Category | Status |
|----------|--------|
| Controls Implemented | ~47 of 110 |
| Critical Gaps | 4 |
| High Priority Items | 9 |
| Estimated Remediation | 6-10 weeks |

### Recently Completed
- ✅ **AU-3.3.8** - Immutable Audit Logs (Hash chain integrity)
- ✅ **AC-3.1.1** - Multi-Factor Authentication (TOTP)

---

## Control Family Status

### ✅ ACCESS CONTROL (AC) - 22 Controls

**Current Implementation:**
- [x] Role-based access control (7 roles: admin, manager, supervisor, operator, quality, shipping, viewer)
- [x] Permission-based feature access
- [x] JWT token authentication
- [x] Session management with absolute timeout (24 hours)
- [x] Account lockout after failed attempts

**GAPS:**
- [ ] **AC-3.1.10 - Session Inactivity Timeout** ⚠️ HIGH
  - Need: 15-30 minute inactivity lockout
  - Effort: 3-5 days
- [x] **AC-3.1.1 - Multi-Factor Authentication** ✅ COMPLETE
  - Implemented: TOTP-based MFA using RFC 6238
  - Features:
    - QR code setup for authenticator apps (Google/Microsoft/Authy/1Password)
    - 6-digit TOTP codes with 30-second interval
    - 10 one-time backup codes (SHA-256 hashed)
    - Two-step login flow (password -> MFA code)
    - MFA enable/disable with verification
    - Backup code regeneration
  - API: /auth/mfa/setup, /auth/mfa/verify, /auth/mfa/status, /auth/mfa/disable
  - Frontend: MFA verification during login, 6-digit code input with auto-submit
- [ ] **AC-3.1.12 - Remote Access Control**
  - Need: VPN or additional controls for remote access
  - Effort: 1 week

---

### ✅ AUDIT & ACCOUNTABILITY (AU) - 9 Controls

**Current Implementation:**
- [x] Comprehensive audit logging (AuditService)
- [x] Correlation IDs for request tracing
- [x] IP address and user agent tracking
- [x] User action logging (create, update, delete, login, etc.)
- [x] Old/new value tracking for changes
- [x] Structured JSON logging in production

**GAPS:**
- [x] **AU-3.3.8 - Protect Audit Information** ✅ COMPLETE
  - Implemented: Immutable audit logs with hash chain integrity
  - Features: SHA-256 hashing, sequence numbers, database triggers prevent UPDATE/DELETE
  - API: /audit/integrity/status, /audit/integrity/verify
- [ ] **AU-3.3.9 - Audit Log Backup**
  - Need: Audit logs backed up to separate system
  - Effort: 3-5 days

---

### ⚠️ AWARENESS & TRAINING (AT) - 3 Controls

**Current Implementation:**
- [x] In-app tour system for user onboarding
- [ ] Security training tracking

**GAPS:**
- [ ] **AT-3.2.1 - Security Awareness Training**
  - Need: Track employee security training completion
  - Effort: 1 week (or manual process)
- [ ] **AT-3.2.2 - Role-Based Training**
  - Need: Document role-specific security responsibilities
  - Effort: Process documentation

---

### ✅ CONFIGURATION MANAGEMENT (CM) - 9 Controls

**Current Implementation:**
- [x] Environment-based configuration (.env files)
- [x] Docker containerization
- [x] Infrastructure as code (docker-compose)
- [x] Version control (Git)

**GAPS:**
- [ ] **CM-3.4.3 - Track Configuration Changes**
  - Need: Automated tracking of infrastructure changes
  - Effort: 1-2 weeks
- [ ] **CM-3.4.5 - Restrict Software Installation**
  - Need: Whitelist approved software
  - Effort: Process documentation

---

### ⚠️ IDENTIFICATION & AUTHENTICATION (IA) - 11 Controls

**Current Implementation:**
- [x] Unique user identification (employee_id, email)
- [x] Password hashing (bcrypt)
- [x] JWT-based authentication
- [x] Token refresh mechanism
- [x] Failed login tracking
- [x] Account lockout

**GAPS:**
- [ ] **IA-3.5.3 - Multi-Factor Authentication** 🔴 CRITICAL
  - Need: MFA for all users accessing CUI
  - Effort: 2-3 weeks
- [ ] **IA-3.5.7 - Password Complexity** 🔴 CRITICAL
  - Need: Minimum 12 chars, uppercase, lowercase, numbers, special chars
  - Effort: 3-5 days
- [ ] **IA-3.5.8 - Password History** ⚠️ HIGH
  - Need: Prevent reuse of last 12 passwords
  - Effort: 3-5 days
- [ ] **IA-3.5.9 - Password Expiration** ⚠️ HIGH
  - Need: 90-day password expiration
  - Effort: 3-5 days
- [ ] **IA-3.5.10 - Temporary Passwords**
  - Need: Force change on first login
  - Effort: 2-3 days

---

### ⚠️ INCIDENT RESPONSE (IR) - 3 Controls

**Current Implementation:**
- [x] Error logging and tracking
- [x] Structured logging with correlation IDs

**GAPS:**
- [ ] **IR-3.6.1 - Incident Response Capability** ⚠️ HIGH
  - Need: Documented incident response procedures
  - Effort: Process documentation
- [ ] **IR-3.6.2 - Incident Tracking** ⚠️ HIGH
  - Need: Automated alerting on security events
  - Effort: 2-3 weeks
- [ ] **IR-3.6.3 - Incident Testing**
  - Need: Regular incident response drills
  - Effort: Process/scheduling

---

### ✅ MAINTENANCE (MA) - 6 Controls

**Current Implementation:**
- [x] Docker-based deployment (easy updates)
- [x] Database migration system (Alembic)
- [x] Deployment runbook documentation

**GAPS:**
- [ ] **MA-3.7.5 - Remote Maintenance**
  - Need: Document and control remote maintenance sessions
  - Effort: Process documentation

---

### ⚠️ MEDIA PROTECTION (MP) - 9 Controls

**Current Implementation:**
- [x] S3 configuration for file storage
- [x] Webhook payload encryption

**GAPS:**
- [ ] **MP-3.8.1 - Media Protection** ⚠️ HIGH
  - Need: Encrypted file uploads for CUI
  - Effort: 1-2 weeks
- [ ] **MP-3.8.3 - Media Sanitization**
  - Need: Procedures for sanitizing media before disposal
  - Effort: Process documentation
- [ ] **MP-3.8.9 - Media Marking**
  - Need: CUI marking on exported files
  - Effort: 1 week

---

### ✅ PHYSICAL PROTECTION (PE) - 6 Controls

**Status**: Using Railway cloud hosting - physical security inherited from provider.

**Documentation Needed:**
- [ ] Document reliance on Railway's SOC 2 compliance
- [ ] Obtain Railway security documentation

---

### ⚠️ PLANNING (PL) - 2 Controls

**GAPS:**
- [ ] **PL-3.12.1 - System Security Plan (SSP)** 🔴 CRITICAL
  - Need: Comprehensive SSP document
  - Effort: 2-4 weeks
- [ ] **PL-3.12.2 - Plan of Action & Milestones (POA&M)**
  - Need: This document serves as starting point
  - Effort: Ongoing

---

### ✅ PERSONNEL SECURITY (PS) - 2 Controls

**Current Implementation:**
- [x] User account management
- [x] Role-based access

**GAPS:**
- [ ] **PS-3.9.2 - Personnel Termination**
  - Need: Documented termination procedures (disable accounts, revoke access)
  - Effort: Process documentation

---

### ⚠️ RISK ASSESSMENT (RA) - 3 Controls

**GAPS:**
- [ ] **RA-3.11.1 - Risk Assessment** ⚠️ HIGH
  - Need: Periodic vulnerability scanning
  - Effort: Tooling + process
- [ ] **RA-3.11.2 - Vulnerability Scanning**
  - Need: Automated security scanning
  - Effort: 1-2 weeks
- [ ] **RA-3.11.3 - Vulnerability Remediation**
  - Need: Track and remediate vulnerabilities
  - Effort: Ongoing process

---

### ⚠️ SECURITY ASSESSMENT (CA) - 4 Controls

**GAPS:**
- [ ] **CA-3.12.1 - Security Control Assessment**
  - Need: Periodic self-assessment
  - Effort: Process
- [ ] **CA-3.12.3 - Continuous Monitoring**
  - Need: Security monitoring dashboards
  - Effort: 2-3 weeks

---

### ⚠️ SYSTEM & COMMUNICATIONS PROTECTION (SC) - 16 Controls

**Current Implementation:**
- [x] HTTPS/TLS encryption in transit (Railway/nginx)
- [x] CORS controls
- [x] Input validation
- [x] API rate limiting

**GAPS:**
- [ ] **SC-3.13.8 - Data at Rest Encryption** 🔴 CRITICAL
  - Need: Encrypt CUI fields in database
  - Effort: 2-4 weeks
- [ ] **SC-3.13.11 - CUI Encryption**
  - Need: FIPS 140-2 validated encryption
  - Effort: Validation + implementation
- [ ] **SC-3.13.16 - Data at Rest Protection**
  - Need: Database-level or field-level encryption
  - Effort: 2-4 weeks

---

### ✅ SYSTEM & INFORMATION INTEGRITY (SI) - 7 Controls

**Current Implementation:**
- [x] Input validation (Pydantic schemas)
- [x] Error boundaries (React)
- [x] Database constraints

**GAPS:**
- [ ] **SI-3.14.1 - Flaw Remediation**
  - Need: Patch management process
  - Effort: Process documentation
- [ ] **SI-3.14.6 - Security Alerting**
  - Need: Automated security event alerts
  - Effort: 1-2 weeks
- [ ] **SI-3.14.7 - Software/Firmware Integrity**
  - Need: Verify integrity of updates
  - Effort: 1 week

---

## Priority Remediation Roadmap

### Phase 1: Critical (Weeks 1-4)
| Item | Effort | Owner | Status |
|------|--------|-------|--------|
| Multi-Factor Authentication (TOTP) | 2-3 weeks | | ⬜ Not Started |
| Password Policy Enforcement | 1 week | | ⬜ Not Started |
| Encryption at Rest | 2-4 weeks | | ⬜ Not Started |
| System Security Plan (SSP) | 2-4 weeks | | ⬜ Not Started |

### Phase 2: High Priority (Weeks 5-8)
| Item | Effort | Owner | Status |
|------|--------|-------|--------|
| Session Inactivity Timeout | 3-5 days | | ⬜ Not Started |
| Audit Log Protection (AU-3.3.8) | 1-2 weeks | | ✅ Complete |
| Incident Response Procedures | 1-2 weeks | | ⬜ Not Started |
| Automated Security Alerting | 2-3 weeks | | ⬜ Not Started |
| Vulnerability Scanning Setup | 1-2 weeks | | ⬜ Not Started |

### Phase 3: Medium Priority (Weeks 9-12)
| Item | Effort | Owner | Status |
|------|--------|-------|--------|
| Media Protection (Encrypted Uploads) | 1-2 weeks | | ⬜ Not Started |
| Security Training Tracking | 1 week | | ⬜ Not Started |
| Continuous Monitoring Dashboard | 2-3 weeks | | ⬜ Not Started |
| Configuration Change Tracking | 1-2 weeks | | ⬜ Not Started |

### Phase 4: Documentation & Process (Ongoing)
| Item | Owner | Status |
|------|-------|--------|
| System Security Plan (SSP) | | ⬜ Not Started |
| Incident Response Plan | | ⬜ Not Started |
| Personnel Termination Procedures | | ⬜ Not Started |
| Media Sanitization Procedures | | ⬜ Not Started |
| Risk Assessment Process | | ⬜ Not Started |
| Railway SOC 2 Documentation | | ⬜ Not Started |

---

## Technical Implementation Notes

### MFA Implementation (TOTP)
```
Backend:
- Add pyotp library
- Add mfa_secret, mfa_enabled fields to User model
- Create /auth/mfa/setup and /auth/mfa/verify endpoints
- Modify login flow to require MFA if enabled

Frontend:
- QR code display for setup
- 6-digit code input during login
- MFA management in user settings
```

### Password Policy Implementation
```
Backend (app/core/security.py):
- Minimum length: 12 characters
- Require: uppercase, lowercase, number, special char
- Password history: store last 12 hashes
- Expiration: 90 days
- Minimum age: 1 day

User model additions:
- password_history (JSON array of hashes)
- password_expires_at (DateTime)
- must_change_password (Boolean)
```

### Data at Rest Encryption
```
Options:
1. PostgreSQL TDE (Transparent Data Encryption)
   - Requires PostgreSQL Enterprise or AWS RDS
   
2. Application-level encryption
   - Encrypt CUI fields before storage
   - Use Fernet (symmetric) or RSA (asymmetric)
   - Store encryption keys in secrets manager
   
3. Column-level encryption
   - SQLAlchemy-utils encrypted types
   - Encrypt specific CUI columns
```

### Session Inactivity Timeout
```
Frontend:
- Track last activity timestamp
- Show warning modal at 25 minutes
- Auto-logout at 30 minutes

Backend:
- Add last_activity_at to session/token
- Validate inactivity on each request
- Return 401 if inactive too long
```

---

## Assessment Preparation Checklist

### Pre-Assessment (3 months before)
- [ ] Complete all Phase 1 & 2 remediation
- [ ] Document all controls in SSP
- [ ] Complete POA&M for any remaining gaps
- [ ] Train staff on security procedures
- [ ] Conduct internal assessment

### Assessment Readiness (1 month before)
- [ ] Review SSP for accuracy
- [ ] Verify all controls are operational
- [ ] Prepare evidence documentation
- [ ] Brief all staff on assessment process
- [ ] Schedule C3PAO assessment

### During Assessment
- [ ] Designate assessment coordinator
- [ ] Provide assessor workspace
- [ ] Have technical staff available
- [ ] Document any findings immediately

---

## Resources

### Official Documentation
- [CMMC Model Overview](https://dodcio.defense.gov/cmmc/)
- [NIST SP 800-171 Rev 2](https://csrc.nist.gov/publications/detail/sp/800-171/rev-2/final)
- [CMMC Level 2 Assessment Guide](https://dodcio.defense.gov/Portals/0/Documents/CMMC/AssessmentGuideL2v2.pdf)

### Tools & Services
- C3PAO Directory: [Cyber AB Marketplace](https://cyberab.org/Catalog)
- Self-Assessment: NIST 800-171 DoD Assessment Methodology

---

## Change Log

| Date | Change | Author |
|------|--------|--------|
| 2026-01-13 | Initial compliance roadmap created | System |
| 2026-01-13 | AU-3.3.8 Audit Log Protection implemented | Droid |
| | | |

---

*This document should be reviewed and updated monthly during remediation and quarterly after certification.*
