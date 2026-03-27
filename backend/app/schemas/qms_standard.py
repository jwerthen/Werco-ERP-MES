from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


# ============ QMS Standard Schemas ============

class QMSStandardCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=255, description="Standard name (e.g. AS9100D)")
    version: Optional[str] = Field(None, max_length=50)
    description: Optional[str] = Field(None, max_length=5000)
    standard_body: Optional[str] = Field(None, max_length=255)
    document_id: Optional[int] = Field(None, gt=0)


class QMSStandardUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=255)
    version: Optional[str] = Field(None, max_length=50)
    description: Optional[str] = Field(None, max_length=5000)
    standard_body: Optional[str] = Field(None, max_length=255)
    document_id: Optional[int] = Field(None, gt=0)
    is_active: Optional[bool] = None


# ============ QMS Clause Schemas ============

class QMSClauseCreate(BaseModel):
    clause_number: str = Field(..., min_length=1, max_length=50, description="Clause number (e.g. 8.5.2)")
    title: str = Field(..., min_length=2, max_length=500, description="Clause title")
    description: Optional[str] = Field(None, max_length=10000, description="Full clause text")
    parent_clause_id: Optional[int] = Field(None, gt=0)
    sort_order: int = Field(default=0, ge=0)


class QMSClauseUpdate(BaseModel):
    clause_number: Optional[str] = Field(None, min_length=1, max_length=50)
    title: Optional[str] = Field(None, min_length=2, max_length=500)
    description: Optional[str] = Field(None, max_length=10000)
    parent_clause_id: Optional[int] = None
    sort_order: Optional[int] = Field(None, ge=0)
    compliance_status: Optional[str] = Field(None, pattern=r'^(not_assessed|compliant|partial|non_compliant|not_applicable)$')
    compliance_notes: Optional[str] = Field(None, max_length=5000)
    next_review_date: Optional[datetime] = None


class QMSClauseBulkCreate(BaseModel):
    """For importing multiple clauses at once (e.g., from a parsed standard document)"""
    clauses: List[QMSClauseCreate] = Field(..., min_length=1, max_length=500)


# ============ QMS Evidence Schemas ============

class QMSEvidenceCreate(BaseModel):
    evidence_type: str = Field(..., pattern=r'^(document|module|ncr|car|fai|calibration|training|procedure|spc|other)$')
    title: str = Field(..., min_length=2, max_length=500)
    description: Optional[str] = Field(None, max_length=5000)
    document_id: Optional[int] = Field(None, gt=0)
    module_reference: Optional[str] = Field(None, max_length=255)
    record_type: Optional[str] = Field(None, max_length=100)
    record_id: Optional[int] = Field(None, gt=0)


class QMSEvidenceUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=2, max_length=500)
    description: Optional[str] = Field(None, max_length=5000)
    document_id: Optional[int] = None
    module_reference: Optional[str] = Field(None, max_length=255)
    is_verified: Optional[bool] = None
    verification_notes: Optional[str] = Field(None, max_length=5000)


# ============ Response Schemas ============

class QMSEvidenceResponse(BaseModel):
    id: int
    clause_id: int
    evidence_type: str
    title: str
    description: Optional[str]
    document_id: Optional[int]
    module_reference: Optional[str]
    record_type: Optional[str]
    record_id: Optional[int]
    is_verified: bool
    verified_by: Optional[int]
    verified_date: Optional[datetime]
    verification_notes: Optional[str]
    created_by: Optional[int]
    created_at: datetime
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True


class QMSClauseResponse(BaseModel):
    id: int
    standard_id: int
    clause_number: str
    title: str
    description: Optional[str]
    parent_clause_id: Optional[int]
    sort_order: int
    compliance_status: str
    compliance_notes: Optional[str]
    last_assessed_date: Optional[datetime]
    last_assessed_by: Optional[int]
    next_review_date: Optional[datetime]
    evidence_links: List[QMSEvidenceResponse] = Field(default_factory=list)
    sub_clauses: List['QMSClauseResponse'] = Field(default_factory=list)
    created_at: datetime
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True


class QMSStandardResponse(BaseModel):
    id: int
    name: str
    version: Optional[str]
    description: Optional[str]
    standard_body: Optional[str]
    document_id: Optional[int]
    is_active: bool
    created_by: Optional[int]
    created_at: datetime
    updated_at: Optional[datetime]
    clauses: List[QMSClauseResponse] = Field(default_factory=list)

    class Config:
        from_attributes = True


class QMSStandardListResponse(BaseModel):
    """Lightweight response for listing standards (without full clause tree)"""
    id: int
    name: str
    version: Optional[str]
    description: Optional[str]
    standard_body: Optional[str]
    is_active: bool
    total_clauses: int = 0
    compliant_clauses: int = 0
    partial_clauses: int = 0
    non_compliant_clauses: int = 0
    not_assessed_clauses: int = 0
    created_at: datetime

    class Config:
        from_attributes = True


class QMSAuditReadinessSummary(BaseModel):
    """Dashboard summary for audit readiness across all active standards"""
    total_standards: int
    total_clauses: int
    compliant: int
    partial: int
    non_compliant: int
    not_assessed: int
    not_applicable: int
    compliance_percentage: float
    total_evidence_links: int
    verified_evidence: int
    unverified_evidence: int
    clauses_needing_review: int  # Past next_review_date
