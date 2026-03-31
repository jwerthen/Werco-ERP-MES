"""
Pagination utilities for API endpoints.

Provides reusable pagination logic with consistent response format.
"""
from math import ceil
from typing import List, Any
from pydantic import BaseModel
from sqlalchemy.orm import Query


class PaginationMeta(BaseModel):
    """Pagination metadata returned in responses."""
    page: int
    page_size: int
    total_count: int
    total_pages: int
    has_next: bool
    has_previous: bool


T = TypeVar('T')


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


