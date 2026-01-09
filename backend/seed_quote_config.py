"""
Seed script for quote configuration data.
Run via: python seed_quote_config.py
"""
from app.db.database import SessionLocal
from app.models.quote_config import (
    QuoteMaterial, QuoteMachine, QuoteFinish, QuoteSettings, 
    MaterialCategory, MachineType, LaborRate, OutsideService, ProcessType, CostUnit
)

db = SessionLocal()

def calc_sheet_pricing(price_per_lb, density):
    """Calculate sheet pricing per sq ft based on material cost and thickness"""
    thicknesses = {
        '24ga': 0.0239, '22ga': 0.0299, '20ga': 0.0359, '18ga': 0.0478,
        '16ga': 0.0598, '14ga': 0.0747, '12ga': 0.1046, '11ga': 0.1196,
        '10ga': 0.1345, '7ga': 0.1793,
        '0.125': 0.125, '0.1875': 0.1875, '0.250': 0.250, '0.375': 0.375,
        '0.500': 0.500, '0.625': 0.625, '0.750': 0.750, '1.000': 1.000,
    }
    pricing = {}
    for gauge, thick in thicknesses.items():
        weight_per_sqft = thick * 144 * density
        price = weight_per_sqft * price_per_lb * 1.15  # 15% handling markup
        pricing[gauge] = round(price, 2)
    return pricing


def seed_materials():
    """Seed materials with pricing"""
    print("Seeding materials...")
    
    # Check if already seeded
    existing = db.query(QuoteMaterial).count()
    if existing > 0:
        print(f"  Materials already exist ({existing}). Deleting and re-seeding...")
        db.query(QuoteMaterial).delete()
        db.commit()
    
    materials_data = [
        {
            'name': 'Mild Steel A36',
            'category': MaterialCategory.STEEL,
            'stock_price_per_pound': 0.55,
            'density_lb_per_cubic_inch': 0.284,
            'machinability_factor': 0.6,
            'sheet_pricing': calc_sheet_pricing(0.55, 0.284)
        },
        {
            'name': 'Galvanized Steel G90',
            'category': MaterialCategory.STEEL,
            'stock_price_per_pound': 0.70,
            'density_lb_per_cubic_inch': 0.284,
            'machinability_factor': 0.55,
            'sheet_pricing': calc_sheet_pricing(0.70, 0.284)
        },
        {
            'name': 'Aluminum 5052-H32',
            'category': MaterialCategory.ALUMINUM,
            'stock_price_per_pound': 2.38,
            'density_lb_per_cubic_inch': 0.097,
            'machinability_factor': 1.0,
            'sheet_pricing': calc_sheet_pricing(2.38, 0.097)
        },
        {
            'name': 'Aluminum 6061-T6',
            'category': MaterialCategory.ALUMINUM,
            'stock_price_per_pound': 2.58,
            'density_lb_per_cubic_inch': 0.098,
            'machinability_factor': 1.0,
            'sheet_pricing': calc_sheet_pricing(2.58, 0.098)
        },
        {
            'name': 'Stainless Steel 304',
            'category': MaterialCategory.STAINLESS,
            'stock_price_per_pound': 2.13,
            'density_lb_per_cubic_inch': 0.289,
            'machinability_factor': 0.4,
            'sheet_pricing': calc_sheet_pricing(2.13, 0.289)
        },
        {
            'name': 'Stainless Steel 316',
            'category': MaterialCategory.STAINLESS,
            'stock_price_per_pound': 3.08,
            'density_lb_per_cubic_inch': 0.290,
            'machinability_factor': 0.35,
            'sheet_pricing': calc_sheet_pricing(3.08, 0.290)
        },
    ]

    for mat_data in materials_data:
        mat = QuoteMaterial(**mat_data)
        db.add(mat)
    
    db.commit()
    print(f"  Created {len(materials_data)} materials")


def seed_machines():
    """Seed machines with cutting speeds"""
    print("Seeding machines...")
    
    existing = db.query(QuoteMachine).count()
    if existing > 0:
        print(f"  Machines already exist ({existing}). Deleting and re-seeding...")
        db.query(QuoteMachine).delete()
        db.commit()
    
    # Laser cutting speeds (inches per minute) by material and thickness
    laser_fiber_speeds = {
        "steel": {
            "24ga": 1200, "22ga": 1000, "20ga": 850, "18ga": 650, "16ga": 500,
            "14ga": 380, "12ga": 280, "10ga": 200, "7ga": 120,
            "0.250": 150, "0.375": 100, "0.500": 70, "0.750": 40, "1.000": 25
        },
        "stainless": {
            "24ga": 900, "22ga": 750, "20ga": 600, "18ga": 450, "16ga": 350,
            "14ga": 260, "12ga": 180, "10ga": 130, "7ga": 80,
            "0.250": 100, "0.375": 65, "0.500": 45, "0.750": 25, "1.000": 15
        },
        "aluminum": {
            "24ga": 1500, "22ga": 1300, "20ga": 1100, "18ga": 900, "16ga": 700,
            "14ga": 550, "12ga": 400, "10ga": 300, "7ga": 200,
            "0.250": 220, "0.375": 150, "0.500": 100, "0.750": 60, "1.000": 35
        }
    }
    
    machines_data = [
        {
            'name': 'Fiber Laser 6kW',
            'machine_type': MachineType.LASER_FIBER,
            'description': '6kW fiber laser for high-speed cutting',
            'rate_per_hour': 150.00,
            'setup_rate_per_hour': 75.00,
            'cutting_speeds': laser_fiber_speeds,
            'typical_setup_hours': 0.25
        },
        {
            'name': 'Press Brake 150T',
            'machine_type': MachineType.PRESS_BRAKE,
            'description': '150 ton press brake for bending',
            'rate_per_hour': 85.00,
            'setup_rate_per_hour': 65.00,
            'bend_time_seconds': 12.0,
            'setup_time_per_bend_type': 300.0,
            'typical_setup_hours': 0.5
        },
        {
            'name': 'CNC Mill 3-Axis',
            'machine_type': MachineType.CNC_MILL_3AXIS,
            'description': 'Haas VF-2 3-axis vertical mill',
            'rate_per_hour': 125.00,
            'setup_rate_per_hour': 85.00,
            'typical_setup_hours': 1.0
        },
        {
            'name': 'CNC Mill 4-Axis',
            'machine_type': MachineType.CNC_MILL_4AXIS,
            'description': 'Haas VF-4 with 4th axis rotary',
            'rate_per_hour': 145.00,
            'setup_rate_per_hour': 95.00,
            'typical_setup_hours': 1.5
        },
        {
            'name': 'CNC Lathe',
            'machine_type': MachineType.CNC_LATHE,
            'description': 'Haas ST-20 CNC turning center',
            'rate_per_hour': 110.00,
            'setup_rate_per_hour': 75.00,
            'typical_setup_hours': 0.75
        },
    ]

    for machine_data in machines_data:
        machine = QuoteMachine(**machine_data)
        db.add(machine)
    
    db.commit()
    print(f"  Created {len(machines_data)} machines")


def seed_finishes():
    """Seed finishing operations"""
    print("Seeding finishes...")
    
    existing = db.query(QuoteFinish).count()
    if existing > 0:
        print(f"  Finishes already exist ({existing}). Deleting and re-seeding...")
        db.query(QuoteFinish).delete()
        db.commit()
    
    finishes_data = [
        {'name': 'Powder Coat - Standard Colors', 'category': 'coating', 'price_per_sqft': 2.50, 'minimum_charge': 35.00, 'additional_days': 3},
        {'name': 'Powder Coat - Custom Color', 'category': 'coating', 'price_per_sqft': 3.50, 'minimum_charge': 75.00, 'additional_days': 5},
        {'name': 'Wet Paint - Standard', 'category': 'coating', 'price_per_sqft': 3.00, 'minimum_charge': 50.00, 'additional_days': 3},
        {'name': 'Zinc Plating - Clear', 'category': 'plating', 'price_per_lb': 1.25, 'minimum_charge': 45.00, 'additional_days': 5},
        {'name': 'Zinc Plating - Yellow', 'category': 'plating', 'price_per_lb': 1.50, 'minimum_charge': 45.00, 'additional_days': 5},
        {'name': 'Anodize Type II - Clear', 'category': 'plating', 'price_per_sqft': 4.00, 'minimum_charge': 50.00, 'additional_days': 5},
        {'name': 'Anodize Type II - Color', 'category': 'plating', 'price_per_sqft': 5.00, 'minimum_charge': 65.00, 'additional_days': 7},
        {'name': 'Anodize Type III - Hard', 'category': 'plating', 'price_per_sqft': 8.00, 'minimum_charge': 100.00, 'additional_days': 7},
        {'name': 'Nickel Plating', 'category': 'plating', 'price_per_sqft': 6.00, 'minimum_charge': 75.00, 'additional_days': 7},
        {'name': 'Passivation', 'category': 'treatment', 'price_per_part': 5.00, 'minimum_charge': 35.00, 'additional_days': 2},
        {'name': 'Deburr - Hand', 'category': 'finishing', 'price_per_part': 2.50, 'minimum_charge': 0.00, 'additional_days': 0},
        {'name': 'Deburr - Tumble', 'category': 'finishing', 'price_per_lb': 0.50, 'minimum_charge': 25.00, 'additional_days': 1},
    ]

    for finish_data in finishes_data:
        finish = QuoteFinish(**finish_data)
        db.add(finish)
    
    db.commit()
    print(f"  Created {len(finishes_data)} finishes")


def seed_settings():
    """Seed global quote settings"""
    print("Seeding settings...")
    
    existing = db.query(QuoteSettings).count()
    if existing > 0:
        print(f"  Settings already exist ({existing}). Deleting and re-seeding...")
        db.query(QuoteSettings).delete()
        db.commit()
    
    settings_data = [
        {'setting_key': 'default_markup_pct', 'setting_value': '35', 'setting_type': 'number', 'description': 'Default markup percentage'},
        {'setting_key': 'minimum_order_charge', 'setting_value': '150', 'setting_type': 'number', 'description': 'Minimum charge per order'},
        {'setting_key': 'rush_multiplier', 'setting_value': '1.5', 'setting_type': 'number', 'description': 'Price multiplier for rush orders'},
        {'setting_key': 'standard_lead_days', 'setting_value': '10', 'setting_type': 'number', 'description': 'Standard lead time in business days'},
        {'setting_key': 'quantity_breaks', 'setting_value': '{"10": 0.95, "25": 0.90, "50": 0.85, "100": 0.80}', 'setting_type': 'json', 'description': 'Quantity discount tiers'},
        {'setting_key': 'tolerance_surcharges', 'setting_value': '{"+/-.005": 1.0, "+/-.001": 1.25, "+/-.0005": 1.5}', 'setting_type': 'json', 'description': 'Tolerance tier surcharges'},
        {'setting_key': 'setup_charge_per_operation', 'setting_value': '45', 'setting_type': 'number', 'description': 'Base setup charge per operation'},
        {'setting_key': 'programming_rate_per_hour', 'setting_value': '95', 'setting_type': 'number', 'description': 'CNC programming rate'},
    ]

    for setting_data in settings_data:
        setting = QuoteSettings(**setting_data)
        db.add(setting)
    
    db.commit()
    print(f"  Created {len(settings_data)} settings")


def seed_labor_rates():
    """Seed labor rates"""
    print("Seeding labor rates...")
    
    existing = db.query(LaborRate).count()
    if existing > 0:
        print(f"  Labor rates already exist ({existing}). Deleting and re-seeding...")
        db.query(LaborRate).delete()
        db.commit()
    
    labor_data = [
        {'name': 'Welder', 'rate_per_hour': 85.00, 'description': 'MIG/TIG welding operations'},
        {'name': 'Machinist', 'rate_per_hour': 75.00, 'description': 'Manual machining operations'},
        {'name': 'Assembler', 'rate_per_hour': 55.00, 'description': 'Assembly operations'},
        {'name': 'Fabricator', 'rate_per_hour': 65.00, 'description': 'General fabrication'},
        {'name': 'Inspector', 'rate_per_hour': 60.00, 'description': 'Quality inspection'},
        {'name': 'Painter', 'rate_per_hour': 55.00, 'description': 'Paint/coating operations'},
    ]

    for labor_item in labor_data:
        labor = LaborRate(**labor_item)
        db.add(labor)
    
    db.commit()
    print(f"  Created {len(labor_data)} labor rates")


def seed_outside_services():
    """Seed outside services"""
    print("Seeding outside services...")
    
    existing = db.query(OutsideService).count()
    if existing > 0:
        print(f"  Outside services already exist ({existing}). Deleting and re-seeding...")
        db.query(OutsideService).delete()
        db.commit()
    
    services_data = [
        {'name': 'Heat Treat - Stress Relief', 'vendor_name': 'ABC Heat Treat', 'process_type': ProcessType.HEAT_TREAT, 'default_cost': 3.50, 'cost_unit': CostUnit.PER_LB, 'minimum_charge': 75.00, 'typical_lead_days': 5},
        {'name': 'Heat Treat - Harden & Temper', 'vendor_name': 'ABC Heat Treat', 'process_type': ProcessType.HEAT_TREAT, 'default_cost': 5.00, 'cost_unit': CostUnit.PER_LB, 'minimum_charge': 100.00, 'typical_lead_days': 7},
        {'name': 'Zinc Plating', 'vendor_name': 'Quality Plating', 'process_type': ProcessType.PLATING, 'default_cost': 1.25, 'cost_unit': CostUnit.PER_LB, 'minimum_charge': 45.00, 'typical_lead_days': 5},
        {'name': 'Anodize Type II', 'vendor_name': 'Precision Anodize', 'process_type': ProcessType.PLATING, 'default_cost': 4.00, 'cost_unit': CostUnit.PER_SQFT, 'minimum_charge': 50.00, 'typical_lead_days': 5},
        {'name': 'Powder Coating', 'vendor_name': 'Midwest Coatings', 'process_type': ProcessType.COATING, 'default_cost': 2.50, 'cost_unit': CostUnit.PER_SQFT, 'minimum_charge': 35.00, 'typical_lead_days': 3},
    ]

    for service_data in services_data:
        service = OutsideService(**service_data)
        db.add(service)
    
    db.commit()
    print(f"  Created {len(services_data)} outside services")


if __name__ == "__main__":
    print("=" * 50)
    print("Seeding Quote Configuration Data")
    print("=" * 50)
    
    try:
        seed_materials()
        seed_machines()
        seed_finishes()
        seed_settings()
        seed_labor_rates()
        seed_outside_services()
        
        print("\n" + "=" * 50)
        print("Quote configuration seeding complete!")
        print("=" * 50)
    except Exception as e:
        print(f"\nError: {e}")
        db.rollback()
        raise
    finally:
        db.close()
