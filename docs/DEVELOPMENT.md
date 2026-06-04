# Development Guide

This guide covers development practices, testing, and contribution guidelines for Werco ERP.

## Environment Setup

### Prerequisites
- Python 3.11+
- Node.js 18+
- PostgreSQL 15+ (only for the Postgres/Supabase path; local dev defaults to SQLite)
- Docker & Docker Compose (optional but recommended)
- Git

### Local Development Setup

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd Werco-ERP
   ```

2. **Backend Setup**
   ```bash
   cd backend
   python -m venv venv
   venv\Scripts\activate  # Windows
   pip install -r requirements.txt
   pip install -r requirements-dev.txt
   ```

3. **Frontend Setup**
   ```bash
   cd frontend
   npm install
   ```

4. **Environment Variables**
   ```bash
   copy backend\.env.example backend\.env
   # Edit backend\.env with your configuration
   ```

5. **Database Setup**

   You can develop against either local SQLite (default, zero-setup) or Postgres.

   - **Local SQLite (default):** `backend/.env.example` ships with
     `DATABASE_URL=sqlite:///./werco_dev.db` — no database server required.
     Tables are created by the seed script (next step); `alembic upgrade head`
     is **not** used on SQLite (the migrations are Postgres-targeted and query
     `information_schema` / `table_schema='public'`, so they fail on SQLite).
   - **Postgres / Supabase:** point `DATABASE_URL` at a Postgres instance and
     use Alembic for schema management (see the next step and "Database Migrations" below).
     ```bash
     # Optional local Postgres via Docker
     docker-compose up -d db
     ```

6. **Create Schema**
   - **Local SQLite:** run the seed script — it calls `Base.metadata.create_all`
     and then seeds demo data (the test suite bootstraps the same way via
     `tests/conftest.py`):
     ```bash
     cd backend
     python -m scripts.seed_data
     ```
     Seeds the demo company with login users — `admin@werco.com / admin123`
     (admin) and the remaining seeded users at `<email> / password123`
     (e.g. `jsmith@werco.com` manager, `bwilliams@werco.com` operator).
   - **Postgres / Supabase:** apply Alembic migrations instead of `create_all`:
     ```bash
     cd backend
     python -m alembic upgrade head
     # then optionally seed
     python -m scripts.seed_data
     # (or, inside the container) docker-compose exec backend python -m scripts.seed_data
     ```

## Development Workflow

### Running the Application

**Backend (Development)**
```bash
cd backend
uvicorn app.main:app --reload --port 8000
```

**Frontend (Development)**
```bash
cd frontend
npm start
```
The Vite dev server runs on **http://localhost:5173**. It reads
`REACT_APP_*`-prefixed env vars (injected via `vite.config.ts` `define`);
set `REACT_APP_API_URL=http://localhost:8000/api/v1` in `frontend/.env`
(the app also defaults to that when unset — see `src/services/api.ts`).
The backend's default `CORS_ORIGINS` already includes `http://localhost:5173`,
so the SPA can call the API in local dev without extra config.

**Using Docker Compose**
```bash
docker-compose up
```

### Code Quality

**Backend**
```bash
cd backend

# Format code
black app/
isort app/

# Lint code
flake8 app/

# Type checking
mypy app

# Run security checks
bandit -r app

# Run tests
pytest tests/ -v
pytest tests/ --cov=app --cov-report=html
```

**Frontend**
```bash
cd frontend

# Format code
npm run format

# Lint code
npm run lint
npm run lint:fix

# Type checking
npm run type-check

# Run tests
npm test
npm run test:coverage
```

### Pre-commit Hooks

Pre-commit hooks are configured to run automatically before commits:
```bash
# Install hooks (first time only)
cd frontend
npm run prepare
cd ..
pre-commit install
```

## Testing

### Backend Testing

**Run all tests**
```bash
cd backend
pytest tests/ -v
```

**Run with coverage**
```bash
pytest tests/ --cov=app --cov-report=html --cov-report=term
```

**Run specific test file**
```bash
pytest tests/api/test_work_orders.py -v
```

**Run specific test**
```bash
pytest tests/api/test_work_orders.py::TestWorkOrdersAPI::test_create_work_order -v
```

**Run by marker**
```bash
pytest tests/ -m unit  # Unit tests only
pytest tests/ -m api   # API tests only
pytest tests/ -m integration  # Integration tests only
```

### Frontend Testing

**Run all tests**
```bash
cd frontend
npm test
```

**Run tests in watch mode**
```bash
npm run test:watch
```

**Run tests with coverage**
```bash
npm run test:coverage
```

### Test Coverage Targets

- Backend: 70% minimum coverage
- Frontend: 70% minimum coverage

## Project Structure

```
Werco-ERP/
├── backend/
│   ├── app/
│   │   ├── api/              # FastAPI endpoints
│   │   ├── core/             # Configuration, security, cache
│   │   ├── db/               # Database setup and connection
│   │   ├── models/           # SQLAlchemy ORM models
│   │   ├── schemas/          # Pydantic schemas for validation
│   │   ├── services/         # Business logic
│   │   └── main.py           # FastAPI application entry point
│   ├── tests/                # Backend tests
│   │   ├── api/              # API endpoint tests
│   │   ├── conftest.py       # Pytest fixtures
│   │   └── ...
│   ├── alembic/              # Database migrations
│   ├── scripts/              # Utility scripts
│   ├── requirements.txt      # Production dependencies
│   └── requirements-dev.txt  # Development dependencies
├── frontend/
│   ├── src/
│   │   ├── components/       # Reusable React components
│   │   ├── pages/            # Page-level components
│   │   ├── services/         # API client and data fetching
│   │   ├── context/          # React context providers
│   │   └── types/            # TypeScript type definitions
│   ├── public/               # Static assets
│   ├── jest.config.js        # Jest configuration
│   ├── tsconfig.json         # TypeScript configuration
│   └── package.json          # Node dependencies
└── docs/                     # Documentation
```

## Database Migrations

### Create a new migration
```bash
cd backend
alembic revision --autogenerate -m "Description of changes"
```

### Apply migrations
```bash
alembic upgrade head
```

### Rollback migration
```bash
alembic downgrade -1
```

### View migration history
```bash
alembic history
```

## API Documentation

Once the backend is running, access the interactive API documentation:
- Swagger UI: http://localhost:8000/api/docs
- ReDoc: http://localhost:8000/api/redoc
- OpenAPI JSON: http://localhost:8000/api/openapi.json

## Adding New Features

### Backend Feature

1. **Create/update model** in `app/models/`
2. **Create schema** in `app/schemas/`
3. **Implement service** in `app/services/` (business logic)
4. **Create API endpoint** in `app/api/endpoints/`
5. **Add tests** in `tests/`
6. **Create migration** if database changes needed

### Frontend Feature

1. **Update types** in `src/types/`
2. **Create/update service** in `src/services/`
3. **Create component** in `src/components/`
4. **Add route** in `src/App.tsx`
5. **Write tests** alongside component files

## Debugging

### Backend Debugging

You can run the backend with detailed logging:
```bash
export LOG_LEVEL=DEBUG
uvicorn app.main:app --reload --port 8000
```

### Frontend Debugging

- Use React DevTools extension
- Check browser console for errors
- Use VS Code debugger with configuration

## Performance Optimization

### Backend
- Use Redis caching for frequently accessed data
- Optimize database queries with proper indexes
- Use pagination for large datasets
- Implement async operations where possible

### Frontend
- Use React.memo for expensive components
- Implement code splitting and lazy loading
- Optimize bundle size
- Use virtual scrolling for long lists

## Common Issues

### Backend won't start
- Verify DATABASE_URL in .env (local default is SQLite; if using Postgres, confirm the server is running)
- For local SQLite, make sure the schema exists — run `python -m scripts.seed_data` (do not run `alembic upgrade head` against SQLite)
- Check port 8000 is not in use

### Frontend build errors
- Clear node_modules: `rm -rf node_modules package-lock.json && npm install`
- Check TypeScript errors with `npm run type-check`

### Docker issues
- Stop all containers: `docker-compose down`
- Remove volumes: `docker-compose down -v`
- Rebuild: `docker-compose build --no-cache`

## CI/CD Pipeline

The project uses GitHub Actions for CI/CD:
- Runs on every push and pull request
- Executes tests, linting, and type checking
- Builds Docker images
- Runs security scans
- Deploys after successful runs

## Security Considerations

- Never commit `.env` files
- Use strong, random SECRET_KEY in production
- Enable rate limiting
- Keep dependencies updated
- Run security audits regularly
- Use HTTPS in production

## Contributing

1. Create a feature branch from `main` or `develop`
2. Make changes with proper tests
3. Ensure all tests pass
4. Run code quality checks
5. Submit a pull request with clear description
6. Address review feedback

## Additional Resources

- FastAPI Documentation: https://fastapi.tiangolo.com/
- React Documentation: https://react.dev/
- SQLAlchemy Documentation: https://docs.sqlalchemy.org/
- Tailwind CSS: https://tailwindcss.com/docs
