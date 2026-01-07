# Input Validation Implementation Summary

This document summarizes the comprehensive input validation system implemented forWerco ERP.

## Overview

The validation system provides consistent, user-friendly, and secure input validation on both frontend (immediate feedback) and backend (security enforcement). All forms now have field-level validation that matches between frontend Zod schemas and backend Pydantic models.

## Backend Implementation

### 1. Validation Types (`backend/app/core/validation.py`)

Reusable Pydantic annotated types:
- `PartNumber` - 3-50 chars, alphanumeric + dashes, uppercase
- `Revision` - 1-5 chars, uppercase alphanumeric
- `Money` - Decimal with 2 places, 0-999999.99
- `MoneySmall` - Decimal with 4 places, 0-9999.9999
- `Percentage` - 0-100 with 2 decimal places
- `Email`, `Phone`, `Phone` - Validated formats
- `SafeString` - No HTML/script injection characters

### 2. Sanitization (`backend/app/core/sanitization.py`)

HTML sanitization utilities using bleach:
- `sanitize_string()` - Remove HTML/JavaScript
- `sanitize_dict()` - Sanitize all string values in dict
- `validate_file_upload()` - Check type, size, MIME
- `validate_phone_number()` - Phone format validation

### 3. Updated Schemas

All schemas now have comprehensive validation:
- **Parts**: `backend/app/schemas/part.py`
  - Uppercase part numbers, revisions
  - Cost fields as Money/MoneySmall
  - Consistency checks (reorder quantity > 0 when reorder point set)

- **Work Orders**: `backend/app/schemas/work_order.py`
  - Date validation (no past dates, relationships)
  - Sequence validation (10-990, multiples of 10)
  - Time validation (positive decimals)

- **Users**: `backend/app/schemas/user.py`
  - Password strength (12+ chars, upper, lower, number, special)
  - Name capitalization
  - Email and employee_id format validation

- **Purchasing**: `backend/app/schemas/purchasing.py`
  - Vendor code uppercase uniqueness
  - PO date relationships (expected > required)
  - Receipt traceability (lot number, cert numbers)

### 4. Async Validation Service (`backend/app/services/validation_service.py`)

Uniqueness and existence checks:
- Part number uniqueness
- Vendor code uniqueness
- User email/employee_id uniqueness
- Part/Vendor/User existence validation

### 5. Exception Handlers (`backend/app/core/exception_handlers.py`)

Custom error handlers:
- `pydantic_validation_exception_handler` - Format Pydantic errors
- `business_validation_exception_handler` - Handle business logic errors
- Custom exceptions: `BusinessValidationError`, `NotFoundError`, `ConflictError`

## Frontend Implementation

### 1. Zod Schemas (`frontend/src/validation/schemas.ts`)

Mirrors backend exactly with TypeScript typing:
- All enums (PartType, UnitOfMeasure, UserRole, etc.)
- Reusable schemas (partNumberSchema, moneySchema, etc.)
- Entity schemas (partSchema, workOrderSchema, userSchema, etc.)
- TypeScript types inferred from schemas

### 2. Form Components

**FormField** (`frontend/src/components/ui/FormField.tsx`)
- Reusable form field wrapper
- Error display with icon
- Accessibility support

**FormWithValidation** (`frontend/src/components/ui/FormWithValidation.tsx`)
- Generic form wrapper
- Zod resolver integration
- Error mapping from API to fields
- Submit button with loading state

**Example Forms**:
- `PartForm` - Comprehensive part creation/update
- `UserForm` - User creation with password strength
- `UserLoginForm` - Login validation
- `WorkOrderForm` - Work order with date validation

### 3. Utils and Hooks

**useFormErrorHandling** (`frontend/src/hooks/useFormErrorHandling.ts`)
- API error type checking
- Map backend errors to form fields
- Overall form error handling

**useAsyncValidation** (`frontend/src/hooks/useFormErrorHandling.ts`)
- Debounced async validation hook
- Loading state management
- For uniqueness checks (e.g., part number exists)

## Installation Requirements

### Backend (add to requirements.txt):
```txt
bleach==6.1.0  # HTML sanitization
```

### Frontend (add to package.json):
```json
{
  "zod": "^3.22.4",
  "react-hook-form": "^7.49.3",
  "@hookform/resolvers": "^3.3.4"
}
```

Install:
```bash
cd frontend
npm install zod react-hook-form @hookform/resolvers
```

## Usage Examples

### Backend - Create Part with Validation

```python
from app.schemas.part import PartCreate
from app.services.validation_service import ValidationErrorService

# Validation happens automatically via Pydantic
try:
    part_data = PartCreate(
        part_number="WIDGET-001",
        revision="A",
        name="Widget Assembly",
        part_type=PartType.MANUFACTURED,
        unit_of_measure=UnitOfMeasure.EACH,
        # ... other fields
    )

    # Async uniqueness check
    validation_errors = await ValidationErrorService.validate_part_create(db, part_data.part_number)
    if validation_errors:
        raise BusinessValidationError("Part number already exists", validation_errors)
except ValidationError as e:
    # Pydantic field validation errors
    pass
```

### Frontend - Part Form

```tsx
import { PartForm } from '@/components/forms/PartForm';

function CreatePartPage() {
  const handleSubmit = async (data: PartFormData) => {
    await api.post('/api/parts', data);
  };

  return (
    <PartForm
      onSubmit={handleSubmit}
      submitButtonText="Create Part"
      isSubmitting={isCreating}
    />
  );
}
```

## Validation Rules Summary

### Parts
- **part_number**: 3-50 chars, uppercase alphanumeric + dashes
- **revision**: 1-5 chars, uppercase alphanumeric
- **name**: 2-255 chars
- **description**: Max 2000 chars
- **costs**: 0-999999.99 (2 decimals)
- **lead_time_days**: 0-365
- **inventory**: 0-9999.9999 (4 decimals)
- **consistency**: Reorder qty > 0 when reorder point set

### Work Orders
- **part_id**: Positive integer, must exist
- **quantity**: Positive, 0-999999.9999
- **priority**: 1-10 (1=highest)
- **dates**: Today or future, expected > due date
- **operations**: Sequence 10-990, multiples of 10

### Users
- **email**: Valid email, unique
- **employee_id**: Alphanumeric + hyphens/underscores, unique
- **names**: Letters only, auto-capitalized
- **password**: 12+ chars, upper + lower + number + special
- **role**: Valid enum value

### Vendors
- **code**: 2-20 chars, uppercase alphanumeric + dashes
- **name**: 2-200 chars
- **state/country**: 2-letter ISO codes
- **lead_time_days**: 0-365

### PO Lines
- **quantity**: Positive
- **unit_price**: Positive, 0-999999.99
- **notes**: Max 500 chars

### Receipts
- **quantity**: Positive
- **lot_number**: Required for AS9100D traceability
- **cert_number/heat_number**: Optional
- **coc_attached**: Boolean

## Error Response Format

Backend validation errors return:

```json
{
  "error": "VALIDATION_ERROR",
  "message": "Input validation failed",
  "details": [
    { "field": "part_number", "message": "Part number already exists", "type": "unique" },
    { "field": "unit_cost", "message": "Required for BUY parts", "type": "conditional_required" }
  ]
}
```

## Security Features

1. **XSS Prevention**: All string inputs sanitized with bleach
2. **SQL Injection**: Handled by SQLAlchemy, UUID format validated
3. **Injection Attacks**: SafeString validation blocks `< > { }`
4. **File Upload**: Type, size, MIME validation
5. **Password Strength**: Complex requirements enforced
6. **Audit Trail**: Validation errors logged

## AS9100D Compliance Features

1. **Traceability**: Lot numbers required on receipts
2. **Inspection**: Required inspection flags
3. **Quality**: Vendor certification tracking
4. **Data Integrity**: Comprehensive validation prevents corrupt data

## Next Steps for Remaining Forms

To add validation to new forms:

1. Define Zod schema in `frontend/src/validation/schemas.ts`
2. Create Pydantic schema in `backend/app/schemas/`
3. Use `FormWithValidation` wrapper
4. Add FormField for each input
5. Handle async validation for uniqueness checks in `validation_service.py`

## Testing

```bash
# Backend
pip install bleach
pytest tests/test_validation.py -v

# Frontend
npm install
npm test -- --testPathPattern=validation
```

## Files Created/Modified

### Backend:
- `backend/app/core/validation.py` - Annotated types and validators
- `backend/app/core/sanitization.py` - Sanitization utilities
- `backend/app/core/exception_handlers.py` - Error handlers
- `backend/app/services/validation_service.py` - Async validation service
- `backend/app/schemas/part.py` - Updated with validation
- `backend/app/schemas/work_order.py` - Updated with validation
- `backend/app/schemas/user.py` - Updated with validation
- `backend/app/schemas/purchasing.py` - Updated with validation
- `backend/requirements.txt` - Added bleach

### Frontend:
- `frontend/src/validation/schemas.ts` - Zod schemas
- `frontend/src/components/ui/FormField.tsx` - Form field component
- `frontend/src/components/ui/FormWithValidation.tsx` - Form wrapper
- `frontend/src/hooks/useFormErrorHandling.ts` - Error handling hooks
- `frontend/src/components/forms/PartForm.tsx` - Part form
- `frontend/src/components/forms/UserForm.tsx` - User form
- `frontend/src/components/forms/UserLoginForm.tsx` - Login form
- `frontend/src/components/forms/WorkOrderForm.tsx` - Work order form
- `frontend/package.json` - Added zod, react-hook-form, @hookform/resolvers
