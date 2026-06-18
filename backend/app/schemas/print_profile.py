"""Pydantic v2 contracts for the per-company thermal-label print profile and the
manual reprint endpoint.

SECRETS are never accepted back nor returned: the ProxyBox API key is write-only on
update and exposed only as ``api_key_last4`` on read.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PrintProfileRead(BaseModel):
    """Print profile read shape. NEVER exposes the plaintext ProxyBox key."""

    id: int
    proxybox_base_url: Optional[str] = None
    proxybox_target: Optional[str] = None
    api_key_last4: Optional[str] = None
    has_api_key: bool = False
    default_paper_size: Optional[str] = None
    default_copies: Optional[int] = None
    auto_print_on_receipt: bool = False
    allow_print_egress: bool = False
    is_active: bool = True
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class PrintProfileUpdate(BaseModel):
    """Create/patch the company print profile. Omitted fields are left unchanged.

    Sending ``api_key`` rotates the stored ProxyBox key; omitting it (or sending an
    empty value -> handled by the endpoint) leaves the existing one. ``api_key`` is
    write-only: Fernet-encrypted at rest and NEVER returned.
    """

    proxybox_base_url: Optional[str] = Field(None, max_length=255, description="Full ProxyBox base URL incl. /api/v1.")
    proxybox_target: Optional[str] = Field(
        None, max_length=120, description="Target printer id on the ProxyBox device."
    )
    api_key: Optional[str] = Field(None, description="Write-only ProxyBox API key; encrypted at rest, never returned.")
    default_paper_size: Optional[str] = Field(None, max_length=20)
    default_copies: Optional[int] = Field(None, ge=1, le=20)
    auto_print_on_receipt: Optional[bool] = None
    # SAFETY: the outbound-egress kill switch defaults OFF on create; a human must
    # opt in explicitly before any label is transmitted to the ProxyBox bridge.
    allow_print_egress: Optional[bool] = None
    is_active: Optional[bool] = None

    @field_validator("proxybox_base_url")
    @classmethod
    def _require_https(cls, v: Optional[str]) -> Optional[str]:
        """Reject non-HTTPS ProxyBox URLs.

        The label carries CUI (part/lot/heat/serial, critical-characteristic marker).
        Forcing ``https://`` ensures it can never be transmitted to the bridge in
        cleartext, even if an admin fat-fingers an ``http://`` URL. The egress kill
        switch and admin-only gating already bound who can set this; this closes the
        cleartext path.
        """
        if v is None:
            return v
        v = v.strip()
        if v and not v.lower().startswith("https://"):
            raise ValueError("proxybox_base_url must use https:// (CUI label data must not be sent in cleartext)")
        return v


class PrintLabelRequest(BaseModel):
    """Manual reprint request body (all optional)."""

    copies: Optional[int] = Field(None, ge=1, le=20, description="Override the profile's default copy count.")


class PrintLabelResponse(BaseModel):
    """Outcome of a manual reprint."""

    receipt_id: int
    receipt_number: Optional[str] = None
    label_document_id: Optional[int] = None
    printed: bool
    message: Optional[str] = None
