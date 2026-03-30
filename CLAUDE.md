# Werco ERP/MES - Claude Code Project Context

## Overview

Manufacturing ERP/MES system for job shop operations. Handles work orders, shop floor tracking, BOM management, routing, MRP, quality management (QMS), purchasing, shipping, analytics, and compliance (AS9100D/ISO 9001).

## Tech Stack

- **Backend**: Python 3.11, FastAPI 0.109, SQLAlchemy 2.0, Alembic, Pydantic 2.5
- **Frontend**: React 18, TypeScript 4.9, Tailwind CSS 3.4 + DaisyUI, React Router v6, React Hook Form + Zod
- **Database**: PostgreSQL 15, Redis 7 (cache + job queue via ARQ)
- **Deployment**: Docker Compose, Railway.app, GitHub Actions CI/CD
- **AI Integration**: Anthropic API for quote extraction and DXF parsing

## Architecture

```
API routes (/api/v1/*) → FastAPI endpoints → Service layer → SQLAlchemy models → PostgreSQL
                         ↕                    ↕
                    Pydantic schemas      Redis cache/queue
```

- **Backend**: `backend/app/api/endpoints/` (46 endpoint modules), `backend/app/services/` (~40 services), `backend/app/models/` (37+ models)
- **Frontend**: `frontend/src/pages/` (page components), `frontend/src/components/` (reusable UI), `frontend/src/services/api.ts` (Axios client), `frontend/src/hooks/`, `frontend/src/context/`
- **Migrations**: `backend/alembic/versions/` (25+ migrations)

## Coding Conventions

### Backend (Python)
- **Formatter**: Black (line length 120)
- **Imports**: isort with Black profile
- **Linting**: pylint (max-line-length 120, docstrings not enforced), mypy for type checking, Bandit for security
- **Naming**: snake_case for functions/variables, PascalCase for classes
- **Type hints**: Required on all function signatures
- **Dependencies**: Use FastAPI `Depends()` for DB sessions, auth, and audit service injection

### Frontend (TypeScript/React)
- **TypeScript**: Strict mode enabled
- **Linting**: ESLint (react-app config), Prettier
- **Naming**: camelCase for files/functions, PascalCase for React components
- **Components**: Functional components with hooks only (no class components)
- **Forms**: React Hook Form + Zod validation schemas
- **API calls**: Via `ApiService` singleton in `frontend/src/services/api.ts`
- **Pre-commit**: husky + lint-staged

### Git Commits
- Present tense, lowercase body
- Action prefix: Fix, Add, Remove, Deploy, Polish
- Example: `Add auto-link evidence engine to connect ERP/MES records to QMS clauses`

## Database Patterns

- **Soft deletes**: Use `SoftDeleteMixin` — never hard-delete records (compliance requirement)
- **Audit fields**: All models include `created_at`, `updated_at`, `created_by`
- **Eager loading**: Always use `joinedload()` or `selectinload()` to prevent N+1 queries
- **Enums**: Python enums for constrained values (e.g., `WorkOrderStatus`, `UserRole`)
- **Transactions**: Use `atomic_transaction` context manager for multi-step operations
- **Migrations**: Alembic — verify revision chain, always include downgrade, add indexes on FK and status columns

## Auth & Security

- **Authentication**: JWT access + refresh tokens, session tracking
- **Authorization**: RBAC with 7 roles: admin, manager, supervisor, operator, quality, shipping, viewer
- **Enforcement**: `require_role()` dependency on every endpoint, `PermissionGate` component on frontend
- **Audit logging**: `AuditService` must log all create/update/delete operations (AS9100D requirement)
- **Rate limiting**: slowapi on public endpoints
- **Input validation**: Pydantic on backend, Zod on frontend — validate at both layers

## Compliance Constraints (AS9100D / ISO 9001)

These are non-negotiable requirements:
1. **Audit trails** — Every data mutation must be logged with user, timestamp, and before/after values
2. **No hard deletes** — Use soft delete for all records; data must be recoverable
3. **Traceability** — Lot/serial numbers must chain from receiving through shipping
4. **Document control** — All documents require revision tracking
5. **Change management** — Engineering changes (ECOs) must follow approval workflow

## Key Commands

```bash
# Development
docker compose up -d                              # Start all services
docker compose logs -f backend                    # Backend logs

# Backend
cd backend && python -m pytest                    # Run tests
cd backend && python -m pytest --cov=app          # Tests with coverage
cd backend && black app/ && isort app/            # Format
cd backend && pylint app/                         # Lint
cd backend && mypy app/                           # Type check
cd backend && alembic upgrade head                # Run migrations
cd backend && alembic revision --autogenerate -m "description"  # New migration

# Frontend
cd frontend && npm test                           # Run tests
cd frontend && npm run build                      # Production build
cd frontend && npx playwright test                # E2E tests

# CI checks (what GitHub Actions runs)
cd backend && black --check app/ && isort --check app/ && flake8 app/ && mypy app/ && bandit -r app/
cd frontend && npx eslint src/ && npx tsc --noEmit
```

## Known Critical Issues

- **CRIT-001**: JWT tokens in localStorage (XSS risk) — needs migration to httpOnly cookies. See `JWT_HTTPONLY_COOKIE_MIGRATION.md`
- **HIGH-001**: 100+ instances of `: any` in frontend TypeScript — define proper interfaces
- **HIGH-004**: console.log statements in production code — remove or replace with error logging service

## API Endpoints Overview

46 endpoint modules under `/api/v1/`: auth, work-centers, parts, bom, routing, inventory, mrp, quality, work-orders, shop-floor, purchasing, scheduling, documents, reports, shipping, quotes, users, customers, calibration, scanner, traceability, audit, quote-calc, dxf-parser, rfq-packages, admin/settings, receiving, po-upload, analytics, search, exports, print, oee, downtime, job-costs, tool-management, maintenance, certifications, eco, spc, complaints, supplier-scorecards, qms-standards, errors
