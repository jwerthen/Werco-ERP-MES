"""
Seed script to populate initial data for Werco ERP.

Run with: python -m scripts.seed_data
"""

import os
import sys
from datetime import date, timedelta

from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings
from app.core.security import get_password_hash
from app.db.database import Base, SessionLocal, engine
from app.models.company import Company
from app.models.inventory import InventoryLocation, LocationType
from app.models.part import Part, PartType
from app.models.purchasing import Vendor
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import (
    OperationStatus,
    WorkOrder,
    WorkOrderOperation,
    WorkOrderStatus,
)

# Import relationship targets that are not re-exported by app.models.__init__.
# SQLAlchemy configures all known mappers on first query, so these need to be
# loaded even though the seed rows below do not create records for each model.
from app.models import customer as _customer_models  # noqa: F401
from app.models import quality as _quality_models  # noqa: F401
from app.models import quote as _quote_models  # noqa: F401

DEFAULT_COMPANY_ID = 1
DEFAULT_COMPANY_NAME = "Werco Manufacturing"
DEFAULT_COMPANY_SLUG = "werco"


def _sync_postgres_sequence(db, table_name: str, column_name: str = "id") -> None:
    """Keep a serial sequence ahead of explicit seed ids."""
    if db.bind.dialect.name != "postgresql":
        return

    db.execute(text(f"""
            SELECT setval(
                pg_get_serial_sequence('{table_name}', '{column_name}'),
                GREATEST((SELECT COALESCE(MAX({column_name}), 1) FROM {table_name}), 1),
                true
            )
            """))


def _get_or_create_default_company(db) -> Company:
    company = (
        db.query(Company).filter(Company.slug == DEFAULT_COMPANY_SLUG).first()
        or db.query(Company).filter(Company.id == DEFAULT_COMPANY_ID).first()
    )
    if company:
        return company

    company = Company(
        id=DEFAULT_COMPANY_ID,
        name=DEFAULT_COMPANY_NAME,
        slug=DEFAULT_COMPANY_SLUG,
        is_active=True,
        timezone="America/Chicago",
    )
    db.add(company)
    db.flush()
    _sync_postgres_sequence(db, "companies")
    print(f"Created default company: {company.name} (id={company.id})")
    return company


def _get_or_create_user(db, company_id: int, data: dict) -> tuple[User, bool]:
    user = (
        db.query(User)
        .filter(User.company_id == company_id, User.email == data["email"])
        .first()
    )
    if user:
        return user, False

    user = User(company_id=company_id, **data)
    db.add(user)
    return user, True


def _get_or_create_by_code(db, model, company_id: int, code: str, data: dict):
    existing = (
        db.query(model)
        .filter(model.company_id == company_id, model.code == code)
        .first()
    )
    if existing:
        return existing, False

    record = model(company_id=company_id, **data)
    db.add(record)
    return record, True


def _get_or_create_part(db, company_id: int, part_number: str, data: dict):
    part = (
        db.query(Part)
        .filter(Part.company_id == company_id, Part.part_number == part_number)
        .first()
    )
    if part:
        return part, False

    part = Part(company_id=company_id, **data)
    db.add(part)
    return part, True


def _get_or_create_work_order(db, company_id: int, work_order_number: str, data: dict):
    work_order = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.work_order_number == work_order_number,
        )
        .first()
    )
    if work_order:
        return work_order, False

    work_order = WorkOrder(company_id=company_id, **data)
    db.add(work_order)
    return work_order, True


def seed_database():
    # Demo seed data uses well-known throwaway passwords (admin123 / password123),
    # so refuse to run against a production database — planting weak credentials in a
    # CUI environment is a CMMC/AS9100D exposure. Prod tenants are bootstrapped via the
    # company-onboarding flow (which enforces the password-strength policy). Set
    # SEED_ALLOW_PRODUCTION=1 only to override deliberately (e.g. a throwaway sandbox).
    # Both signals come from `settings` (not bare os.getenv) so they match the app's own
    # resolution, .env file included. Two independent triggers, either one refuses:
    #   - ENVIRONMENT=production — the deployed-prod invocation (e.g. `railway run`);
    #   - a Supabase database target — a local shell pointed at the prod Postgres via
    #     DATABASE_URL would otherwise slip past an ENVIRONMENT-only check, because a
    #     local environment defaults to "development".
    allow_prod = os.getenv("SEED_ALLOW_PRODUCTION", "").strip().lower() in ("1", "true", "yes")
    if (settings.ENVIRONMENT == "production" or settings.is_supabase_database) and not allow_prod:
        reason = (
            "ENVIRONMENT=production"
            if settings.ENVIRONMENT == "production"
            else f"database host '{settings.safe_database_host}' is Supabase (the production Postgres)"
        )
        print(
            f"Refusing to seed demo data: {reason}. Demo accounts use "
            "well-known passwords and must not be created in production. Bootstrap prod "
            "through the company-onboarding flow, or set SEED_ALLOW_PRODUCTION=1 to override."
        )
        sys.exit(1)

    Base.metadata.create_all(bind=engine)

    db = SessionLocal()

    try:
        print("Seeding database...")
        company = _get_or_create_default_company(db)
        company_id = company.id

        created_counts = {
            "users": 0,
            "work_centers": 0,
            "parts": 0,
            "work_orders": 0,
            "work_order_operations": 0,
            "inventory_locations": 0,
            "vendors": 0,
        }

        admin, created = _get_or_create_user(
            db,
            company_id,
            {
                "employee_id": "EMP001",
                "email": "admin@werco.com",
                "hashed_password": get_password_hash("admin123"),
                "first_name": "System",
                "last_name": "Administrator",
                "role": UserRole.ADMIN,
                "department": "IT",
                "is_superuser": True,
            },
        )
        created_counts["users"] += int(created)

        users_data = [
            (
                "EMP002",
                "jsmith@werco.com",
                "John",
                "Smith",
                UserRole.MANAGER,
                "Production",
            ),
            (
                "EMP003",
                "mjohnson@werco.com",
                "Mary",
                "Johnson",
                UserRole.SUPERVISOR,
                "Fabrication",
            ),
            (
                "EMP004",
                "bwilliams@werco.com",
                "Bob",
                "Williams",
                UserRole.OPERATOR,
                "CNC",
            ),
            (
                "EMP005",
                "sjones@werco.com",
                "Sarah",
                "Jones",
                UserRole.QUALITY,
                "Quality",
            ),
            (
                "EMP006",
                "dwilson@werco.com",
                "David",
                "Wilson",
                UserRole.OPERATOR,
                "Assembly",
            ),
        ]

        for emp_id, email, first, last, role, dept in users_data:
            _, created = _get_or_create_user(
                db,
                company_id,
                {
                    "employee_id": emp_id,
                    "email": email,
                    "hashed_password": get_password_hash("password123"),
                    "first_name": first,
                    "last_name": last,
                    "role": role,
                    "department": dept,
                },
            )
            created_counts["users"] += int(created)

        db.flush()
        print(f"Users ready ({created_counts['users']} created)")

        work_centers_data = [
            ("FAB-01", "Fabrication Bay 1", "fabrication", 75.00, "Main", "Bay 1"),
            ("FAB-02", "Fabrication Bay 2", "fabrication", 75.00, "Main", "Bay 2"),
            ("CNC-01", "Haas VF-2", "cnc_machining", 125.00, "Main", "CNC Area"),
            ("CNC-02", "Haas VF-4", "cnc_machining", 150.00, "Main", "CNC Area"),
            ("CNC-03", "Haas ST-20", "cnc_machining", 110.00, "Main", "CNC Area"),
            ("WLD-01", "Welding Station 1", "welding", 85.00, "Main", "Weld Shop"),
            ("WLD-02", "Welding Station 2", "welding", 85.00, "Main", "Weld Shop"),
            ("PNT-01", "Paint Booth 1", "paint", 65.00, "Finishing", "Paint"),
            (
                "PWD-01",
                "Powder Coating Line",
                "powder_coating",
                70.00,
                "Finishing",
                "Powder",
            ),
            ("ASM-01", "Assembly Station 1", "assembly", 55.00, "Main", "Assembly"),
            ("ASM-02", "Assembly Station 2", "assembly", 55.00, "Main", "Assembly"),
            ("INS-01", "Inspection Station", "inspection", 60.00, "Quality", "QC"),
            ("SHP-01", "Shipping", "shipping", 45.00, "Warehouse", "Shipping"),
        ]

        work_centers = {}
        for code, name, wc_type, rate, building, area in work_centers_data:
            wc, created = _get_or_create_by_code(
                db,
                WorkCenter,
                company_id,
                code,
                {
                    "code": code,
                    "name": name,
                    "work_center_type": wc_type,
                    "hourly_rate": rate,
                    "building": building,
                    "area": area,
                },
            )
            work_centers[code] = wc
            created_counts["work_centers"] += int(created)

        db.flush()
        print(f"Work centers ready ({created_counts['work_centers']} created)")

        parts_data = [
            ("WERCO-001", "Mounting Bracket Assembly", PartType.ASSEMBLY, "A", True),
            ("WERCO-001-01", "Bracket - Main", PartType.MANUFACTURED, "B", True),
            ("WERCO-001-02", "Bracket - Support", PartType.MANUFACTURED, "A", False),
            ("WERCO-001-03", "Gusset Plate", PartType.MANUFACTURED, "A", False),
            ("HW-001", "Bolt M10x30", PartType.PURCHASED, "A", False),
            ("HW-002", "Nut M10", PartType.PURCHASED, "A", False),
            (
                "RAW-001",
                "Steel Plate 0.25 x 48 x 96",
                PartType.RAW_MATERIAL,
                "A",
                False,
            ),
            ("WERCO-002", "Control Panel Enclosure", PartType.ASSEMBLY, "A", True),
            ("WERCO-002-01", "Enclosure Body", PartType.MANUFACTURED, "C", True),
            ("WERCO-002-02", "Door Panel", PartType.MANUFACTURED, "B", False),
        ]

        parts = {}
        for pn, name, ptype, rev, critical in parts_data:
            part, created = _get_or_create_part(
                db,
                company_id,
                pn,
                {
                    "part_number": pn,
                    "name": name,
                    "part_type": ptype,
                    "revision": rev,
                    "is_critical": critical,
                    "requires_inspection": True,
                    "standard_cost": 100.00 if ptype == PartType.ASSEMBLY else 25.00,
                    "created_by": admin.id,
                },
            )
            parts[pn] = part
            created_counts["parts"] += int(created)

        db.flush()
        print(f"Parts ready ({created_counts['parts']} created)")

        work_orders_data = [
            (
                "WO-20260102-001",
                "WERCO-001",
                10,
                WorkOrderStatus.IN_PROGRESS,
                2,
                "ACME Corp",
                "PO-12345",
                date.today() + timedelta(days=5),
            ),
            (
                "WO-20260102-002",
                "WERCO-001-01",
                25,
                WorkOrderStatus.RELEASED,
                3,
                "ACME Corp",
                "PO-12345",
                date.today() + timedelta(days=3),
            ),
            (
                "WO-20260102-003",
                "WERCO-002",
                5,
                WorkOrderStatus.DRAFT,
                5,
                "TechCo",
                "PO-67890",
                date.today() + timedelta(days=14),
            ),
            (
                "WO-20260102-004",
                "WERCO-002-01",
                15,
                WorkOrderStatus.IN_PROGRESS,
                1,
                "TechCo",
                "PO-67890",
                date.today() + timedelta(days=7),
            ),
        ]

        for (
            wo_num,
            part_num,
            qty,
            status,
            priority,
            customer,
            po,
            due,
        ) in work_orders_data:
            wo, created = _get_or_create_work_order(
                db,
                company_id,
                wo_num,
                {
                    "work_order_number": wo_num,
                    "part_id": parts[part_num].id,
                    "quantity_ordered": qty,
                    "status": status,
                    "priority": priority,
                    "customer_name": customer,
                    "customer_po": po,
                    "due_date": due,
                    "lot_number": f"LOT-{wo_num[-3:]}",
                    "created_by": admin.id,
                },
            )
            created_counts["work_orders"] += int(created)
            db.flush()

            if part_num == "WERCO-001":
                ops = [
                    (10, "FAB-01", "Cut Materials", OperationStatus.COMPLETE, 0.5, 2.0),
                    (
                        20,
                        "WLD-01",
                        "Weld Assembly",
                        OperationStatus.IN_PROGRESS,
                        0.25,
                        3.0,
                    ),
                    (30, "PNT-01", "Paint", OperationStatus.PENDING, 0.25, 1.5),
                    (
                        40,
                        "INS-01",
                        "Final Inspection",
                        OperationStatus.PENDING,
                        0.1,
                        0.5,
                    ),
                ]
            elif part_num == "WERCO-001-01":
                ops = [
                    (10, "FAB-01", "Cut Blank", OperationStatus.READY, 0.25, 0.5),
                    (
                        20,
                        "CNC-01",
                        "Machine Features",
                        OperationStatus.PENDING,
                        0.5,
                        1.5,
                    ),
                    (30, "INS-01", "Inspection", OperationStatus.PENDING, 0.1, 0.25),
                ]
            elif part_num == "WERCO-002-01":
                ops = [
                    (10, "FAB-02", "Shear & Form", OperationStatus.COMPLETE, 0.5, 1.0),
                    (
                        20,
                        "WLD-02",
                        "Weld Seams",
                        OperationStatus.IN_PROGRESS,
                        0.25,
                        2.0,
                    ),
                    (30, "PWD-01", "Powder Coat", OperationStatus.PENDING, 0.25, 1.0),
                    (40, "INS-01", "Final QC", OperationStatus.PENDING, 0.1, 0.5),
                ]
            else:
                ops = [
                    (10, "ASM-01", "Assembly", OperationStatus.PENDING, 0.5, 2.0),
                    (20, "INS-01", "Inspection", OperationStatus.PENDING, 0.1, 0.5),
                ]

            for seq, wc_code, name, op_status, setup, run in ops:
                existing_op = (
                    db.query(WorkOrderOperation)
                    .filter(
                        WorkOrderOperation.company_id == company_id,
                        WorkOrderOperation.work_order_id == wo.id,
                        WorkOrderOperation.sequence == seq,
                    )
                    .first()
                )
                if existing_op:
                    continue

                db.add(
                    WorkOrderOperation(
                        company_id=company_id,
                        work_order_id=wo.id,
                        work_center_id=work_centers[wc_code].id,
                        sequence=seq,
                        operation_number=f"OP{seq}",
                        name=name,
                        status=op_status,
                        setup_time_hours=setup,
                        run_time_hours=run,
                    )
                )
                created_counts["work_order_operations"] += 1

        db.flush()
        print(
            "Work orders ready "
            f"({created_counts['work_orders']} orders, "
            f"{created_counts['work_order_operations']} operations created)"
        )

        locations_data = [
            ("RECV-01", "Receiving Dock 1", "MAIN", LocationType.RECEIVING),
            ("WH1-A-01", "Warehouse 1 Aisle A Bin 1", "MAIN", LocationType.BIN),
            ("WH1-A-02", "Warehouse 1 Aisle A Bin 2", "MAIN", LocationType.BIN),
            ("WH1-B-01", "Warehouse 1 Aisle B Bin 1", "MAIN", LocationType.BIN),
            ("WH1-B-02", "Warehouse 1 Aisle B Bin 2", "MAIN", LocationType.BIN),
            ("QC-HOLD", "Quality Hold Area", "MAIN", LocationType.QUARANTINE),
            ("SHIP-01", "Shipping Dock 1", "MAIN", LocationType.SHIPPING),
            ("FLOOR-FAB", "Fabrication Floor Stock", "MAIN", LocationType.FLOOR),
            ("FLOOR-CNC", "CNC Floor Stock", "MAIN", LocationType.FLOOR),
            ("FLOOR-ASM", "Assembly Floor Stock", "MAIN", LocationType.FLOOR),
        ]
        for code, name, warehouse, loc_type in locations_data:
            _, created = _get_or_create_by_code(
                db,
                InventoryLocation,
                company_id,
                code,
                {
                    "code": code,
                    "name": name,
                    "warehouse": warehouse,
                    "location_type": loc_type,
                },
            )
            created_counts["inventory_locations"] += int(created)

        vendors_data = [
            ("VND-001", "Acme Steel Supply", "John Smith", "john@acmesteel.com", True),
            ("VND-002", "FastBolt Hardware", "Jane Doe", "jane@fastbolt.com", True),
            ("VND-003", "Premier Coatings", "Bob Wilson", "bob@premiercoat.com", True),
        ]
        for code, name, contact, email, approved in vendors_data:
            _, created = _get_or_create_by_code(
                db,
                Vendor,
                company_id,
                code,
                {
                    "code": code,
                    "name": name,
                    "contact_name": contact,
                    "email": email,
                    "is_approved": approved,
                    "is_active": True,
                },
            )
            created_counts["vendors"] += int(created)

        db.commit()
        print(
            f"Inventory locations ready ({created_counts['inventory_locations']} created)"
        )
        print(f"Vendors ready ({created_counts['vendors']} created)")

        print("\n" + "=" * 50)
        print("Database seeded successfully!")
        print("=" * 50)
        print(f"\nCompany: {company.name} (id={company.id})")
        print("Default login credentials:")
        print("  Admin: admin@werco.com / admin123")
        print("  Users: <email> / password123")
        print()

    except Exception as e:
        db.rollback()
        print(f"Error seeding database: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_database()
