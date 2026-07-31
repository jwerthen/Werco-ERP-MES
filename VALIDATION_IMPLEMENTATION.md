# Input Validation Implementation Summary

This document summarizes the comprehensive input validation system implemented for Werco ERP.

> **Partially stale — verify against code before relying on it.** The sanitization sections
> were corrected on 2026-07-30 when ingest-time HTML sanitization was removed (see §2). The
> following claims elsewhere in this file were **checked and found false** at the same time and
> are **not** yet rewritten — they predate several refactors and are out of that change's scope:
> `sanitize_string` / `sanitize_dict` / `validate_file_upload` / `validate_phone_number` no
> longer exist (the last two never did); `SafeString` is still declared in
> `backend/app/core/validation.py` but is **referenced by nothing**; `backend/tests/test_validation.py`
> does not exist; `frontend/src/components/ui/FormWithValidation.tsx` and the whole
> `frontend/src/components/forms/` directory (`PartForm`, `UserForm`, `UserLoginForm`,
> `WorkOrderForm`) do not exist; and the password rule under
> [Validation Rules Summary → Users](#users) contradicts the correct one in
> [§3 Updated Schemas](#3-updated-schemas). `frontend/src/components/ui/FormField.tsx` **is**
> current — see CLAUDE.md → Frontend architecture.

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
- `SafeString` - Declared, but currently applied to no field (see the staleness note above)

### 2. Escaping at the sink (`backend/app/services/pdf_text.py`)

**There is no ingest-time HTML sanitization.** `backend/app/core/sanitization.py` and the
`bleach` dependency were **removed on 2026-07-30**. The middleware that called them rewrote
every JSON request body with a markup-stripped copy *before persistence*, which silently
corrupted quality records — ASME Y14.5 drawing notation is angle-bracketed, so an inspection
note reading `Dim is 2.500 <REF> per print` was stored as `Dim is 2.500  per print`. It also
covered less than it claimed (`sanitize_dict` did not recurse into dicts nested in lists, and
top-level JSON arrays skipped the middleware entirely, so BOM/PO/routing line items were never
sanitized) and failed open on any sanitizer exception.

The rule now is **store the operator's bytes verbatim; escape where they are interpreted**:

- `pdf_escape()` (`backend/app/services/pdf_text.py`) — escapes `&`, `<`, `>` for the one
  backend sink that interprets markup, reportlab `Paragraph`, which parses a mini-HTML dialect.
  Applied at every interpolation in `quote_pdf_service.py` and `coc_pdf_service.py`.
- HTML email is already safe — Jinja2 `Environment(autoescape=select_autoescape(['html','xml']))`.
- Thermal labels use `canvas.drawString`, and reportlab `Table` cells take plain strings;
  neither parses markup.
- The SPA renders no raw HTML (zero `dangerouslySetInnerHTML`, zero `innerHTML` writes), so
  React's output escaping covers the browser.

Two guard tests keep that argument true: `backend/tests/test_frontend_no_raw_html_render_guard.py`
and `backend/tests/test_pdf_text_escaping.py`. Full rationale:
[docs/SECURITY_ADVISORY_SUPPRESSIONS.md → bleach removed](docs/SECURITY_ADVISORY_SUPPRESSIONS.md#bleach-removed--escape-at-the-sink-2026-07-30).

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
  - Password strength (12+ chars + common-weak-substring blocklist; no character-class rules
    as of 2026-07-29)
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

Nothing. This system is pure Pydantic — no extra runtime dependency. (`bleach` was listed
here until 2026-07-30; it is removed and must not be added back — see §2.)

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

1. **XSS Prevention**: escaping at the sink, **not** input sanitization — React escapes on
   output and the SPA renders no raw HTML; reportlab `Paragraph` is escaped via `pdf_escape`;
   HTML email autoescapes through Jinja2. Stored strings are byte-exact (see §2)
2. **SQL Injection**: Handled by SQLAlchemy, UUID format validated
3. **Injection Attacks**: the `SafeString` type would block `< > { }`, but it is applied to no
   field today — and blocking `<` would reject legitimate ASME Y14.5 notation, so do not apply
   it to free-text quality fields
4. **File Upload**: Type, size, MIME validation
5. **Password Strength**: Minimum length + weak-password blocklist enforced
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
pytest tests/test_validation.py -v

# Frontend
npm install
npm test -- --testPathPattern=validation
```

## Files Created/Modified

### Backend:
- `backend/app/core/validation.py` - Annotated types and validators
- ~~`backend/app/core/sanitization.py`~~ - **deleted 2026-07-30**; replaced by
  `backend/app/services/pdf_text.py` (escape at the sink)
- `backend/app/core/exception_handlers.py` - Error handlers
- `backend/app/services/validation_service.py` - Async validation service
- `backend/app/schemas/part.py` - Updated with validation
- `backend/app/schemas/work_order.py` - Updated with validation
- `backend/app/schemas/user.py` - Updated with validation
- `backend/app/schemas/purchasing.py` - Updated with validation
- `backend/requirements.txt` - ~~Added bleach~~ (removed 2026-07-30)

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
