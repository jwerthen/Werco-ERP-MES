from fastapi import Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from app.core.validation import ValidationErrorResponse, ValidationErrorDetail


async def pydantic_validation_exception_handler(request: Request, exc: PydanticValidationError) -> JSONResponse:
    """Handle Pydantic validation errors"""
    errors = []
    for error in exc.errors():
        field_name = ".".join(str(loc) for loc in error["loc"])
        errors.append(
            ValidationErrorDetail(
                field=field_name,
                message=error["msg"],
                type=error["type"]
            )
        )

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ValidationErrorResponse(error=errors).model_dump()
    )


async def business_validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle custom business validation errors (e.g., uniqueness checks, existence checks)"""
    # The exception should have a details attribute with ValidationErrorDetail objects
    if hasattr(exc, 'details') and isinstance(exc.details, list):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=ValidationErrorResponse(
                message="Business validation failed",
                details=exc.details
            ).model_dump()
        )

    # Fallback for regular exceptions with a message
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ValidationErrorResponse(
            message=str(exc) or "Business validation failed",
            details=[]
        ).model_dump()
    )


async def not_found_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle 404 not found errors"""
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={
            "error": "NOT_FOUND",
            "message": str(exc) or "Resource not found",
            "details": []
        }
    )


async def conflict_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle 409 conflict errors (e.g., concurrent modification)"""
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "error": "CONFLICT",
            "message": str(exc) or "Resource conflict",
            "details": []
        }
    )


class BusinessValidationError(Exception):
    """Custom exception for business validation failures"""

    def __init__(self, message: str, details: list[ValidationErrorDetail] = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


class NotFoundError(Exception):
    """Custom exception for not found errors"""

    def __init__(self, message: str = "Resource not found"):
        super().__init__(message)
        self.message = message


class ConflictError(Exception):
    """Custom exception for conflict errors (e.g., optimistic locking)"""

    def __init__(self, message: str = "Resource conflict"):
        super().__init__(message)
        self.message = message
