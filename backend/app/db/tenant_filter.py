"""
Tenant filtering helpers for multi-company data isolation.

Usage:
    from app.db.tenant_filter import tenant_query, tenant_filter

    # Start a new tenant-scoped query
    work_orders = tenant_query(db, WorkOrder, company_id).filter(...).all()

    # Apply tenant filter to an existing query (e.g., with joins)
    query = db.query(WorkOrder).join(Part)
    query = tenant_filter(query, WorkOrder, company_id)
"""
from sqlalchemy.orm import Session, Query


def tenant_query(db: Session, model, company_id: int) -> Query:
    """Start a query scoped to a specific company."""
    return db.query(model).filter(model.company_id == company_id)


def tenant_filter(query: Query, model, company_id: int) -> Query:
    """Apply company_id filter to an existing query."""
    return query.filter(model.company_id == company_id)
