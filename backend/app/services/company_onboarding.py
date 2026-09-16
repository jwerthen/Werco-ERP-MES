"""
Company onboarding service.

Handles creating a new company with all its default seed data:
- Company record
- Initial admin user
- Default role permissions
"""

import re

from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.role_permission import DEFAULT_ROLE_PERMISSIONS, RolePermission
from app.models.user import UserRole
from app.services.audit_service import AuditService
from app.services.user_provisioning import atomic_security_write, audit_user_created, create_tenant_user


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
    actor=None,
    request=None,
) -> tuple:
    """
    Create a new company with all default configuration.

    Returns: (company, admin_user)
    """
    with atomic_security_write(db):
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
        admin_user = create_tenant_user(
            db,
            email=admin_email,
            employee_id=employee_id,
            first_name=admin_first_name,
            last_name=admin_last_name,
            role=UserRole.ADMIN,
            password=admin_password,
            company_id=company.id,
            created_by=actor.id if actor else None,
        )

        # Seed default role permissions
        for role, permissions in DEFAULT_ROLE_PERMISSIONS.items():
            db.add(
                RolePermission(
                    role=role,
                    permissions=permissions,
                    company_id=company.id,
                )
            )

        audit = AuditService(db, actor or admin_user, request)
        audit.log_required(
            action="CREATE",
            resource_type="company",
            resource_id=company.id,
            resource_identifier=company.name,
            company_id=company.id,
            description=f"Created company {company.name}",
            new_values={"name": company.name, "slug": company.slug, "is_active": company.is_active},
        )
        audit_user_created(audit, admin_user, source="company_onboarding")
    db.refresh(company)
    db.refresh(admin_user)

    return company, admin_user
