"""
Pagination utilities for API endpoints.

Provides reusable pagination logic with consistent response format.
"""
from math import ceil
from typing import TypeVar, Generic, List, Any, Optional
from pydantic import BaseModel, Field
from sqlalchemy.orm import Query


class PaginationParams(BaseModel):
    """Query parameters for pagination."""
    page: int = Field(default=1, ge=1, description="Page number (1-indexed)")
    page_size: int = Field(default=50, ge=1, le=200, description="Items per page (max 200)")


class PaginationMeta(BaseModel):
    """Pagination metadata returned in responses."""
    page: int
    page_size: int
    total_count: int
    total_pages: int
    has_next: bool
    has_previous: bool


T = TypeVar('T')


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated response wrapper."""
    data: List[Any]
    pagination: PaginationMeta
    
    class Config:
        arbitrary_types_allowed = True


def paginate_query(
    query: Query,
    page: int = 1,
    page_size: int = 50,
    max_page_size: int = 200
) -> tuple[Query, PaginationMeta]:
    """
    Apply pagination to a SQLAlchemy query.
    
    Args:
        query: SQLAlchemy query object
        page: Page number (1-indexed)
        page_size: Number of items per page
        max_page_size: Maximum allowed page size
        
    Returns:
        Tuple of (paginated query, pagination metadata)
        
    Example:
        query = db.query(WorkOrder).filter(...)
        paginated_query, meta = paginate_query(query, page=2, page_size=25)
        items = paginated_query.all()
        return {"data": items, "pagination": meta.dict()}
    """
    # Enforce limits
    page = max(1, page)
    page_size = max(1, min(page_size, max_page_size))
    
    # Get total count (before pagination)
    total_count = query.count()
    
    # Calculate pagination metadata
    total_pages = ceil(total_count / page_size) if total_count > 0 else 1
    
    # Adjust page if it exceeds total pages
    if page > total_pages and total_pages > 0:
        page = total_pages
    
    # Apply offset and limit
    offset = (page - 1) * page_size
    paginated_query = query.offset(offset).limit(page_size)
    
    meta = PaginationMeta(
        page=page,
        page_size=page_size,
        total_count=total_count,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_previous=page > 1
    )
    
    return paginated_query, meta


def create_paginated_response(
    items: List[Any],
    page: int,
    page_size: int,
    total_count: int
) -> dict:
    """
    Create a paginated response dictionary.
    
    Useful when you already have the items and total count
    (e.g., after manual processing).
    
    Args:
        items: List of items for the current page
        page: Current page number
        page_size: Items per page
        total_count: Total number of items across all pages
        
    Returns:
        Dictionary with 'data' and 'pagination' keys
    """
    total_pages = ceil(total_count / page_size) if total_count > 0 else 1
    
    return {
        "data": items,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_count": total_count,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_previous": page > 1
        }
    }
