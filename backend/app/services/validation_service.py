from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List
from app.models.part import Part
from app.models.user import User
from app.models.vendor import Vendor
from app.core.validation import ValidationErrorDetail, format_validation_error


class ValidationErrorService:
    """Service for async validation checks (uniqueness, existence)"""

    @staticmethod
    async def validate_part_create(db: AsyncSession, part_number: str) -> List[ValidationErrorDetail]:
        """Validate part number uniqueness for create"""
        errors = []

        existing = await db.execute(
            select(Part).where(Part.part_number == part_number)
        )
        if existing.scalar_one_or_none():
            errors.append(format_validation_error(
                exc=None,
                field="part_number",
                message="Part number already exists",
                error_type="unique"
            ))

        return errors

    @staticmethod
    async def validate_part_update(db: AsyncSession, part_id: int, part_number: str) -> List[ValidationErrorDetail]:
        """Validate part number uniqueness for update (excluding self)"""
        errors = []

        existing = await db.execute(
            select(Part).where(
                Part.part_number == part_number,
                Part.id != part_id
            )
        )
        if existing.scalar_one_or_none():
            errors.append(format_validation_error(
                exc=None,
                field="part_number",
                message="Part number already exists",
                error_type="unique"
            ))

        return errors

    @staticmethod
    async def validate_vendor_create(db: AsyncSession, code: str) -> List[ValidationErrorDetail]:
        """Validate vendor code uniqueness for create"""
        errors = []

        existing = await db.execute(
            select(Vendor).where(Vendor.code == code)
        )
        if existing.scalar_one_or_none():
            errors.append(format_validation_error(
                exc=None,
                field="code",
                message="Vendor code already exists",
                error_type="unique"
            ))

        return errors

    @staticmethod
    async def validate_vendor_update(db: AsyncSession, vendor_id: int, code: str) -> List[ValidationErrorDetail]:
        """Validate vendor code uniqueness for update (excluding self)"""
        errors = []

        existing = await db.execute(
            select(Vendor).where(
                Vendor.code == code,
                Vendor.id != vendor_id
            )
        )
        if existing.scalar_one_or_none():
            errors.append(format_validation_error(
                exc=None,
                field="code",
                message="Vendor code already exists",
                error_type="unique"
            ))

        return errors

    @staticmethod
    async def validate_user_create(db: AsyncSession, email: str, employee_id: str) -> List[ValidationErrorDetail]:
        """Validate user email and employee_id uniqueness for create"""
        errors = []

        # Check email uniqueness
        existing_email = await db.execute(
            select(User).where(User.email == email)
        )
        if existing_email.scalar_one_or_none():
            errors.append(format_validation_error(
                exc=None,
                field="email",
                message="Email already exists",
                error_type="unique"
            ))

        # Check employee_id uniqueness
        existing_id = await db.execute(
            select(User).where(User.employee_id == employee_id)
        )
        if existing_id.scalar_one_or_none():
            errors.append(format_validation_error(
                exc=None,
                field="employee_id",
                message="Employee ID already exists",
                error_type="unique"
            ))

        return errors

    @staticmethod
    async def validate_user_update(db: AsyncSession, user_id: int, email: str, employee_id: str) -> List[ValidationErrorDetail]:
        """Validate user email and employee_id uniqueness for update (excluding self)"""
        errors = []

        # Check email uniqueness
        existing_email = await db.execute(
            select(User).where(
                User.email == email,
                User.id != user_id
            )
        )
        if existing_email.scalar_one_or_none():
            errors.append(format_validation_error(
                exc=None,
                field="email",
                message="Email already exists",
                error_type="unique"
            ))

        # Check employee_id uniqueness
        existing_id = await db.execute(
            select(User).where(
                User.employee_id == employee_id,
                User.id != user_id
            )
        )
        if existing_id.scalar_one_or_none():
            errors.append(format_validation_error(
                exc=None,
                field="employee_id",
                message="Employee ID already exists",
                error_type="unique"
            ))

        return errors

    @staticmethod
    async def validate_part_exists(db: AsyncSession, part_id: int) -> bool:
        """Check if part exists"""
        result = await db.execute(select(Part).where(Part.id == part_id))
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def validate_vendor_exists(db: AsyncSession, vendor_id: int) -> bool:
        """Check if vendor exists and is active"""
        result = await db.execute(select(Vendor).where(Vendor.id == vendor_id, Vendor.is_active == True))
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def validate_user_exists(db: AsyncSession, user_id: int) -> bool:
        """Check if user exists and is active"""
        result = await db.execute(select(User).where(User.id == user_id, User.is_active == True))
        return result.scalar_one_or_none() is not None
