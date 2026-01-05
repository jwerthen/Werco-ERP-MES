# Werco ERP & MES System

A custom Enterprise Resource Planning (ERP) and Manufacturing Execution System (MES) built for Werco Manufacturing. Designed for AS9100D, ISO 9001, and CMMC Level 2 compliance.

## Features

### Shop Floor / MES
- **Work Center Management** - Track Fabrication, CNC, Welding, Paint, Powder Coating, Assembly, Inspection
- **Job Tracking** - Real-time work order status and progress tracking
- **Time Clock** - Operator clock-in/clock-out with production reporting
- **Work Queue** - Visual job queue by work center with priority sorting
- **Dashboard** - Real-time shop floor visibility

### Work Order Management
- Work order creation and routing
- Operation sequencing with work instructions
- Priority-based scheduling
- Customer PO tracking
- Lot/serial number traceability

### Parts & BOM
- Part master with make vs. buy classification
- Multi-level BOM support for assemblies
- Revision control
- Critical characteristic flagging

### Compliance Features (AS9100D / ISO 9001 / CMMC)
- Full audit logging of all actions
- Document control with revision tracking
- Lot traceability
- User authentication with account lockout
- Role-based access control

## Tech Stack

- **Backend**: Python 3.11, FastAPI, SQLAlchemy, PostgreSQL
- **Frontend**: React 18, TypeScript, Tailwind CSS
- **Deployment**: Docker, Docker Compose

## Quick Start

### Prerequisites
- Docker and Docker Compose
- Git

### Installation

1. Clone the repository:
```bash
cd C:\Users\jmw\Desktop\Werco-ERP
```

2. Create environment file:
```bash
copy backend\.env.example backend\.env
# Edit .env with your settings (especially SECRET_KEY)
```

3. Start with Docker:
```bash
docker-compose up -d
```

4. Seed the database (first time only):
```bash
docker-compose exec backend python -m scripts.seed_data
```

5. Access the application:
- Frontend: http://localhost:3000
- API Docs: http://localhost:8000/api/docs

### Default Credentials
- **Admin**: admin@werco.com / admin123
- **Users**: (email) / password123

## Development Setup

### Backend (without Docker)

```bash
cd backend
python -m venv venv
venv\Scripts\activate  # Windows
pip install -r requirements.txt

# Set up PostgreSQL and update DATABASE_URL in .env
python -m scripts.seed_data
uvicorn app.main:app --reload
```

### Frontend (without Docker)

```bash
cd frontend
npm install
npm start
```

## Project Structure

```
Werco-ERP/
├── backend/
│   ├── app/
│   │   ├── api/           # API routes
│   │   ├── core/          # Config, security
│   │   ├── db/            # Database setup
│   │   ├── models/        # SQLAlchemy models
│   │   ├── schemas/       # Pydantic schemas
│   │   └── services/      # Business logic
│   ├── alembic/           # Database migrations
│   ├── scripts/           # Utility scripts
│   └── tests/             # Unit tests
├── frontend/
│   ├── src/
│   │   ├── components/    # React components
│   │   ├── pages/         # Page components
│   │   ├── services/      # API client
│   │   ├── context/       # React context
│   │   └── types/         # TypeScript types
│   └── public/
└── docs/                  # Documentation
```

## API Endpoints

### Authentication
- `POST /api/v1/auth/login` - User login
- `POST /api/v1/auth/register` - Register new user

### Work Centers
- `GET /api/v1/work-centers/` - List work centers
- `POST /api/v1/work-centers/` - Create work center
- `PUT /api/v1/work-centers/{id}` - Update work center

### Parts
- `GET /api/v1/parts/` - List parts
- `POST /api/v1/parts/` - Create part
- `GET /api/v1/parts/{id}` - Get part details

### Work Orders
- `GET /api/v1/work-orders/` - List work orders
- `POST /api/v1/work-orders/` - Create work order
- `POST /api/v1/work-orders/{id}/release` - Release to production
- `POST /api/v1/work-orders/{id}/start` - Start work order

### Shop Floor
- `GET /api/v1/shop-floor/dashboard` - Dashboard data
- `GET /api/v1/shop-floor/my-active-job` - Current user's active job
- `POST /api/v1/shop-floor/clock-in` - Clock in to operation
- `POST /api/v1/shop-floor/clock-out/{id}` - Clock out with production data
- `GET /api/v1/shop-floor/work-center-queue/{id}` - Jobs queued at work center

## User Roles

| Role | Permissions |
|------|-------------|
| Admin | Full system access |
| Manager | Create/edit work orders, parts, users |
| Supervisor | Release work orders, manage operations |
| Operator | Clock in/out, report production |
| Quality | Inspection, NCR management |
| Shipping | Shipping operations |
| Viewer | Read-only access |

## Compliance Notes

### AS9100D / ISO 9001
- Document control with revision tracking
- Lot/serial traceability on all transactions
- Audit trail for all changes
- First Article Inspection (FAI) support
- Non-conformance reporting (future)

### CMMC Level 2
- User authentication with session management
- Account lockout after failed attempts
- Role-based access control
- Comprehensive audit logging
- Encrypted data at rest (PostgreSQL) and in transit (HTTPS)

## Future Modules

- [ ] Estimating & Quoting
- [ ] Quality Management (NCR, CAR, FAI)
- [ ] Inventory Management
- [ ] Purchasing & Receiving
- [ ] Shipping & Invoicing
- [ ] Scheduling & Capacity Planning
- [ ] Document Management
- [ ] Customer Portal

## Support

For questions or issues, contact the Werco IT department.

---
Built for Werco Manufacturing - AS9100D & ISO 9001 Compliant
