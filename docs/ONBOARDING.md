# Developer Onboarding Guide

Welcome to the Werco ERP project! This guide will help you get set up and running quickly.

## Prerequisites

Before you begin, ensure you have the following installed:

- **Node.js** v18+ (check with `node --version`)
- **Python** 3.11+ (check with `python --version`)
- **PostgreSQL** 15+ (or use Docker)
- **Redis** 7+ (optional but recommended, or use Docker)
- **Docker & Docker Compose** (recommended for local development)
- **Git** (check with `git --version`)

### Recommended Tools

- **VS Code** with extensions:
  - Python
  - ESLint
  - Prettier
  - Tailwind CSS IntelliSense
  - GitLens
- **pgAdmin** or **DBeaver** for database management
- **Postman** or **Insomnia** for API testing

## Quick Start (Docker)

The fastest way to get started is using Docker Compose:

```bash
# 1. Clone the repository
git clone https://github.com/jwerthen/Werco-ERP-MES.git
cd Werco-ERP-MES

# 2. Copy environment files
cp .env.example .env
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env

# 3. Start all services
docker-compose up -d

# 4. Check service status
docker-compose ps

# 5. View logs
docker-compose logs -f backend
```

The app will be available at:
- **Frontend**: http://localhost:3000
- **Backend API**: http://localhost:8000
- **API Docs**: http://localhost:8000/api/docs

### Default Admin Credentials
- Email: `admin@werco.com`
- Password: (seeded on first run, check backend logs)

## Manual Setup (Without Docker)

### 1. Backend Setup

```bash
# Navigate to backend
cd backend

# Create Python virtual environment
python -m venv venv

# Activate virtual environment
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env with your database credentials

# Run database migrations
alembic upgrade head

# Seed initial data (creates admin user)
python -m app.seed

# Start the development server
uvicorn app.main:app --reload --port 8000
```

### 2. Frontend Setup

```bash
# Navigate to frontend
cd frontend

# Install dependencies
npm install

# Copy and configure environment
cp .env.example .env
# Edit .env if needed

# Start the development server
npm start
```

### 3. Database Setup (Manual)

```bash
# Create PostgreSQL database
createdb werco_erp

# Or using psql
psql -c "CREATE DATABASE werco_erp;"
```

Update `backend/.env` with your database URL:
```
DATABASE_URL=postgresql://your_user:your_password@localhost:5432/werco_erp
```

## Project Structure

```
Werco-ERP/
├── backend/                 # FastAPI backend
│   ├── app/
│   │   ├── api/            # API endpoints
│   │   │   └── endpoints/  # Route handlers
│   │   ├── core/           # Core config, security
│   │   ├── db/             # Database session, mixins
│   │   ├── models/         # SQLAlchemy models
│   │   ├── schemas/        # Pydantic schemas
│   │   └── services/       # Business logic
│   ├── alembic/            # Database migrations
│   ├── tests/              # Backend tests
│   └── requirements.txt
│
├── frontend/               # React frontend
│   ├── src/
│   │   ├── components/     # React components
│   │   ├── pages/          # Page components
│   │   ├── services/       # API client
│   │   ├── context/        # React context
│   │   ├── hooks/          # Custom hooks
│   │   ├── utils/          # Utility functions
│   │   └── types/          # TypeScript types
│   ├── e2e/                # Playwright E2E tests
│   └── package.json
│
├── docs/                   # Documentation
├── load-tests/             # k6 load tests
├── nginx/                  # Nginx config (production)
├── scripts/                # Utility scripts
├── docker-compose.yml      # Development Docker setup
└── docker-compose.prod.yml # Production Docker setup
```

## Development Workflow

### Git Branch Strategy

- `main` - Production-ready code
- `develop` - Integration branch (if used)
- `feature/*` - New features
- `bugfix/*` - Bug fixes
- `hotfix/*` - Urgent production fixes

### Creating a Feature

```bash
# Create feature branch
git checkout -b feature/your-feature-name

# Make changes and commit
git add .
git commit -m "feat: Add your feature"

# Push and create PR
git push origin feature/your-feature-name
```

### Commit Message Format

Follow conventional commits:
- `feat:` - New features
- `fix:` - Bug fixes
- `docs:` - Documentation
- `style:` - Code style (formatting)
- `refactor:` - Code refactoring
- `test:` - Tests
- `chore:` - Build/config changes

### Running Tests

```bash
# Backend tests
cd backend
pytest

# Frontend unit tests
cd frontend
npm test

# E2E tests (requires running app)
npm run test:e2e

# Load tests
k6 run load-tests/smoke.js
```

### Linting and Formatting

```bash
# Backend
cd backend
ruff check .
ruff format .

# Frontend
cd frontend
npm run lint
npm run format
```

## API Documentation

Interactive API documentation is available at:
- **Swagger UI**: http://localhost:8000/api/docs
- **ReDoc**: http://localhost:8000/api/redoc
- **OpenAPI JSON**: http://localhost:8000/api/openapi.json

## Common Tasks

### Creating a New API Endpoint

1. Add route in `backend/app/api/endpoints/`
2. Create/update schema in `backend/app/schemas/`
3. Add business logic in `backend/app/services/`
4. Register route in `backend/app/api/router.py`

### Adding a New Page

1. Create page component in `frontend/src/pages/`
2. Add route in `frontend/src/App.tsx`
3. Add navigation link in `frontend/src/components/Layout.tsx`

### Database Migrations

```bash
cd backend

# Create new migration
alembic revision --autogenerate -m "Description"

# Run migrations
alembic upgrade head

# Rollback one step
alembic downgrade -1
```

## Troubleshooting

### Backend won't start
- Check DATABASE_URL in `.env`
- Ensure PostgreSQL is running
- Run `alembic upgrade head` if tables missing

### Frontend shows CORS errors
- Ensure backend is running on port 8000
- Check CORS_ORIGINS in backend `.env`
- Frontend must run on http://localhost:3000

### "Module not found" errors (Python)
- Activate virtual environment: `source venv/bin/activate`
- Reinstall: `pip install -r requirements.txt`

### "Module not found" errors (JavaScript)
- Delete node_modules: `rm -rf node_modules`
- Reinstall: `npm install`

### Database connection refused
- Start PostgreSQL: `docker-compose up -d db`
- Check port 5432 is available

## Getting Help

- Check `docs/` folder for detailed documentation
- Review `ENVIRONMENT_VARIABLES.md` for config options
- Check GitHub issues for known problems
- Ask in team Slack/Discord channel

## Security Reminders

- Never commit `.env` files
- Use strong, unique passwords
- Rotate secrets after team changes
- Keep dependencies updated
- Run `npm audit` and `pip audit` regularly

## Next Steps

1. Explore the codebase structure
2. Read through existing tests
3. Try making a small change
4. Review open issues for tasks
5. Ask questions early!

Welcome aboard! 🚀
