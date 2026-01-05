"""
Seed script to populate initial data for Werco ERP
Run with: python -m scripts.seed_data
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.database import SessionLocal, engine, Base
from app.models import *
from app.core.security import get_password_hash
from datetime import datetime, date, timedelta

def seed_database():
    # Create tables
    Base.metadata.create_all(bind=engine)
    
    db = SessionLocal()
    
    try:
        # Check if already seeded
        if db.query(User).first():
            print("Database already seeded. Skipping...")
            return
        
        print("Seeding database...")
        
        # Create admin user
        admin = User(
            employee_id="EMP001",
            email="admin@werco.com",
            hashed_password=get_password_hash("admin123"),
            first_name="System",
            last_name="Administrator",
            role=UserRole.ADMIN,
            department="IT",
            is_superuser=True
        )
        db.add(admin)
        
        # Create sample users
        users_data = [
            ("EMP002", "jsmith@werco.com", "John", "Smith", UserRole.MANAGER, "Production"),
            ("EMP003", "mjohnson@werco.com", "Mary", "Johnson", UserRole.SUPERVISOR, "Fabrication"),
            ("EMP004", "bwilliams@werco.com", "Bob", "Williams", UserRole.OPERATOR, "CNC"),
            ("EMP005", "sjones@werco.com", "Sarah", "Jones", UserRole.QUALITY, "Quality"),
            ("EMP006", "dwilson@werco.com", "David", "Wilson", UserRole.OPERATOR, "Assembly"),
        ]
        
        for emp_id, email, first, last, role, dept in users_data:
            user = User(
                employee_id=emp_id,
                email=email,
                hashed_password=get_password_hash("password123"),
                first_name=first,
                last_name=last,
                role=role,
                department=dept
            )
            db.add(user)
        
        db.flush()
        print("Created users")
        
        # Create work centers
        work_centers_data = [
            ("FAB-01", "Fabrication Bay 1", WorkCenterType.FABRICATION, 75.00, "Main", "Bay 1"),
            ("FAB-02", "Fabrication Bay 2", WorkCenterType.FABRICATION, 75.00, "Main", "Bay 2"),
            ("CNC-01", "Haas VF-2", WorkCenterType.CNC_MACHINING, 125.00, "Main", "CNC Area"),
            ("CNC-02", "Haas VF-4", WorkCenterType.CNC_MACHINING, 150.00, "Main", "CNC Area"),
            ("CNC-03", "Haas ST-20", WorkCenterType.CNC_MACHINING, 110.00, "Main", "CNC Area"),
            ("WLD-01", "Welding Station 1", WorkCenterType.WELDING, 85.00, "Main", "Weld Shop"),
            ("WLD-02", "Welding Station 2", WorkCenterType.WELDING, 85.00, "Main", "Weld Shop"),
            ("PNT-01", "Paint Booth 1", WorkCenterType.PAINT, 65.00, "Finishing", "Paint"),
            ("PWD-01", "Powder Coating Line", WorkCenterType.POWDER_COATING, 70.00, "Finishing", "Powder"),
            ("ASM-01", "Assembly Station 1", WorkCenterType.ASSEMBLY, 55.00, "Main", "Assembly"),
            ("ASM-02", "Assembly Station 2", WorkCenterType.ASSEMBLY, 55.00, "Main", "Assembly"),
            ("INS-01", "Inspection Station", WorkCenterType.INSPECTION, 60.00, "Quality", "QC"),
            ("SHP-01", "Shipping", WorkCenterType.SHIPPING, 45.00, "Warehouse", "Shipping"),
        ]
        
        work_centers = {}
        for code, name, wc_type, rate, building, area in work_centers_data:
            wc = WorkCenter(
                code=code,
                name=name,
                work_center_type=wc_type,
                hourly_rate=rate,
                building=building,
                area=area
            )
            db.add(wc)
            work_centers[code] = wc
        
        db.flush()
        print("Created work centers")
        
        # Create sample parts
        parts_data = [
            ("WERCO-001", "Mounting Bracket Assembly", PartType.ASSEMBLY, "A", True),
            ("WERCO-001-01", "Bracket - Main", PartType.MANUFACTURED, "B", True),
            ("WERCO-001-02", "Bracket - Support", PartType.MANUFACTURED, "A", False),
            ("WERCO-001-03", "Gusset Plate", PartType.MANUFACTURED, "A", False),
            ("HW-001", "Bolt M10x30", PartType.PURCHASED, "A", False),
            ("HW-002", "Nut M10", PartType.PURCHASED, "A", False),
            ("RAW-001", "Steel Plate 0.25 x 48 x 96", PartType.RAW_MATERIAL, "A", False),
            ("WERCO-002", "Control Panel Enclosure", PartType.ASSEMBLY, "A", True),
            ("WERCO-002-01", "Enclosure Body", PartType.MANUFACTURED, "C", True),
            ("WERCO-002-02", "Door Panel", PartType.MANUFACTURED, "B", False),
        ]
        
        parts = {}
        for pn, name, ptype, rev, critical in parts_data:
            part = Part(
                part_number=pn,
                name=name,
                part_type=ptype,
                revision=rev,
                is_critical=critical,
                requires_inspection=True,
                standard_cost=100.00 if ptype == PartType.ASSEMBLY else 25.00
            )
            db.add(part)
            parts[pn] = part
        
        db.flush()
        print("Created parts")
        
        # Create sample work orders
        work_orders_data = [
            ("WO-20260102-001", "WERCO-001", 10, "in_progress", 2, "ACME Corp", "PO-12345", date.today() + timedelta(days=5)),
            ("WO-20260102-002", "WERCO-001-01", 25, "released", 3, "ACME Corp", "PO-12345", date.today() + timedelta(days=3)),
            ("WO-20260102-003", "WERCO-002", 5, "draft", 5, "TechCo", "PO-67890", date.today() + timedelta(days=14)),
            ("WO-20260102-004", "WERCO-002-01", 15, "in_progress", 1, "TechCo", "PO-67890", date.today() + timedelta(days=7)),
        ]
        
        for wo_num, part_num, qty, status, priority, customer, po, due in work_orders_data:
            wo = WorkOrder(
                work_order_number=wo_num,
                part_id=parts[part_num].id,
                quantity_ordered=qty,
                status=WorkOrderStatus(status),
                priority=priority,
                customer_name=customer,
                customer_po=po,
                due_date=due,
                lot_number=f"LOT-{wo_num[-3:]}",
                created_by=admin.id
            )
            db.add(wo)
            db.flush()
            
            # Add operations for this work order
            if part_num == "WERCO-001":
                ops = [
                    (10, "FAB-01", "Cut Materials", OperationStatus.COMPLETE, 0.5, 2.0),
                    (20, "WLD-01", "Weld Assembly", OperationStatus.IN_PROGRESS, 0.25, 3.0),
                    (30, "PNT-01", "Paint", OperationStatus.PENDING, 0.25, 1.5),
                    (40, "INS-01", "Final Inspection", OperationStatus.PENDING, 0.1, 0.5),
                ]
            elif part_num == "WERCO-001-01":
                ops = [
                    (10, "FAB-01", "Cut Blank", OperationStatus.READY, 0.25, 0.5),
                    (20, "CNC-01", "Machine Features", OperationStatus.PENDING, 0.5, 1.5),
                    (30, "INS-01", "Inspection", OperationStatus.PENDING, 0.1, 0.25),
                ]
            elif part_num == "WERCO-002-01":
                ops = [
                    (10, "FAB-02", "Shear & Form", OperationStatus.COMPLETE, 0.5, 1.0),
                    (20, "WLD-02", "Weld Seams", OperationStatus.IN_PROGRESS, 0.25, 2.0),
                    (30, "PWD-01", "Powder Coat", OperationStatus.PENDING, 0.25, 1.0),
                    (40, "INS-01", "Final QC", OperationStatus.PENDING, 0.1, 0.5),
                ]
            else:
                ops = [
                    (10, "ASM-01", "Assembly", OperationStatus.PENDING, 0.5, 2.0),
                    (20, "INS-01", "Inspection", OperationStatus.PENDING, 0.1, 0.5),
                ]
            
            for seq, wc_code, name, op_status, setup, run in ops:
                op = WorkOrderOperation(
                    work_order_id=wo.id,
                    work_center_id=work_centers[wc_code].id,
                    sequence=seq,
                    operation_number=f"OP{seq}",
                    name=name,
                    status=op_status,
                    setup_time_hours=setup,
                    run_time_hours=run
                )
                db.add(op)
        
        db.commit()
        print("Created work orders with operations")
        
        # Create inventory locations
        from app.models.inventory import InventoryLocation
        locations_data = [
            ("RECV-01", "Receiving Dock 1", "MAIN", "receiving"),
            ("WH1-A-01", "Warehouse 1 Aisle A Bin 1", "MAIN", "bin"),
            ("WH1-A-02", "Warehouse 1 Aisle A Bin 2", "MAIN", "bin"),
            ("WH1-B-01", "Warehouse 1 Aisle B Bin 1", "MAIN", "bin"),
            ("WH1-B-02", "Warehouse 1 Aisle B Bin 2", "MAIN", "bin"),
            ("QC-HOLD", "Quality Hold Area", "MAIN", "quarantine"),
            ("SHIP-01", "Shipping Dock 1", "MAIN", "shipping"),
            ("FLOOR-FAB", "Fabrication Floor Stock", "MAIN", "floor"),
            ("FLOOR-CNC", "CNC Floor Stock", "MAIN", "floor"),
            ("FLOOR-ASM", "Assembly Floor Stock", "MAIN", "floor"),
        ]
        for code, name, warehouse, loc_type in locations_data:
            loc = InventoryLocation(code=code, name=name, warehouse=warehouse, location_type=loc_type)
            db.add(loc)
        db.commit()
        print("Created inventory locations")
        
        # Create sample vendors
        from app.models.purchasing import Vendor
        vendors_data = [
            ("VND-001", "Acme Steel Supply", "John Smith", "john@acmesteel.com", True),
            ("VND-002", "FastBolt Hardware", "Jane Doe", "jane@fastbolt.com", True),
            ("VND-003", "Premier Coatings", "Bob Wilson", "bob@premiercoat.com", True),
        ]
        for code, name, contact, email, approved in vendors_data:
            vendor = Vendor(code=code, name=name, contact_name=contact, email=email, is_approved=approved, is_active=True)
            db.add(vendor)
        db.commit()
        print("Created vendors")
        
        print("\n" + "="*50)
        print("Database seeded successfully!")
        print("="*50)
        print("\nDefault login credentials:")
        print("  Admin: admin@werco.com / admin123")
        print("  Users: <email> / password123")
        print("\n")
        
    except Exception as e:
        db.rollback()
        print(f"Error seeding database: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_database()
