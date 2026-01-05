from app.db.database import SessionLocal
from app.models.quote_config import QuoteMaterial, MaterialCategory

db = SessionLocal()

# Delete existing materials
db.query(QuoteMaterial).delete()
db.commit()

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
        # Weight per sq ft = thickness * 144 sq in * density
        weight_per_sqft = thick * 144 * density
        price = weight_per_sqft * price_per_lb * 1.15  # 15% handling markup
        pricing[gauge] = round(price, 2)
    return pricing

# Create materials with standardized pricing (midpoint of ranges)
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
print('Updated 6 materials with standardized pricing:')
print()

for mat in db.query(QuoteMaterial).all():
    print(f'{mat.name}: ${mat.stock_price_per_pound}/lb')
    print(f'  16ga=${mat.sheet_pricing.get("16ga")}/sqft, 1/4"=${mat.sheet_pricing.get("0.250")}/sqft, 1/2"=${mat.sheet_pricing.get("0.500")}/sqft')
    print()

db.close()
