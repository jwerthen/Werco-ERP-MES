"""
Company onboarding service.

Handles creating a new company with all its default seed data:
- Company record
- Initial admin user
- Default quote settings
- Default labor rates
- Default role permissions
"""
import re
from sqlalchemy.orm import Session
from app.models.company import Company
from app.models.user import User, UserRole
from app.models.quote_config import QuoteSettings, LaborRate
from app.models.role_permission import RolePermission, DEFAULT_ROLE_PERMISSIONS
from app.core.security import get_password_hash


def _generate_slug(name: str, db: Session) -> str:
    """Generate a unique URL-safe slug from a company name."""
    base = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    if not base:
        base = "company"
    slug = base
    suffix = 2
    while db.query(Company).filter(Company.slug == slug).first():
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


def onboard_company(
    db: Session,
    name: str,
    admin_email: str,
    admin_first_name: str,
    admin_last_name: str,
    admin_password: str,
    slug: str = None,
    parent_company_id: int = None,
    logo_url: str = None,
    timezone: str = "America/Chicago",
) -> tuple:
    """
    Create a new company with all default configuration.

    Returns: (company, admin_user)
    """
    # Create company
    if not slug:
        slug = _generate_slug(name, db)

    company = Company(
        name=name,
        slug=slug,
        logo_url=logo_url,
        parent_company_id=parent_company_id,
        timezone=timezone,
        is_active=True,
    )
    db.add(company)
    db.flush()  # Get company.id

    # Create admin user
    employee_id = re.sub(r'[^a-zA-Z0-9\-_]', '', admin_email.split('@')[0]) or "admin"
    admin_user = User(
        email=admin_email,
        employee_id=employee_id,
        first_name=admin_first_name,
        last_name=admin_last_name,
        role=UserRole.ADMIN,
        is_active=True,
        is_superuser=False,
        hashed_password=get_password_hash(admin_password),
        company_id=company.id,
    )
    db.add(admin_user)

    # Seed default quote settings
    default_settings = [
        ("default_markup_pct", "35", "number", "Default markup percentage"),
        ("minimum_order_charge", "150", "number", "Minimum order charge"),
        ("rush_multiplier", "1.5", "number", "Rush order multiplier"),
        ("quantity_breaks", '{"10": 0.95, "25": 0.90, "50": 0.85, "100": 0.80}', "json", "Quantity break discounts"),
        ("standard_lead_days", "10", "number", "Standard lead time in days"),
        ("tolerance_surcharges", '{"+/-.005": 1.0, "+/-.001": 1.25, "+/-.0005": 1.5}', "json", "Tolerance surcharges"),
    ]
    for key, value, stype, desc in default_settings:
        db.add(QuoteSettings(
            setting_key=key,
            setting_value=value,
            setting_type=stype,
            description=desc,
            company_id=company.id,
        ))

    # Seed default labor rates
    default_labor = [
        ("Machinist", 45.0),
        ("Welder", 42.0),
        ("Assembler", 35.0),
        ("Inspector", 40.0),
        ("General Labor", 30.0),
    ]
    for labor_name, rate in default_labor:
        db.add(LaborRate(
            name=labor_name,
            rate_per_hour=rate,
            company_id=company.id,
        ))

    # Seed default role permissions
    for role, permissions in DEFAULT_ROLE_PERMISSIONS.items():
        db.add(RolePermission(
            role=role,
            permissions=permissions,
            company_id=company.id,
        ))

    db.commit()
    db.refresh(company)
    db.refresh(admin_user)

    return company, admin_user
