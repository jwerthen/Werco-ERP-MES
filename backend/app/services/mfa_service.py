"""
Multi-Factor Authentication (MFA) Service

CMMC Level 2 Control: AC-3.1.1 - Use multi-factor authentication for access
to privileged and non-privileged accounts.

Implements TOTP (Time-based One-Time Password) using RFC 6238.
"""
import secrets
import hashlib
import base64
from io import BytesIO
from typing import Optional, List, Tuple
from datetime import datetime

from app.core.logging import get_logger

logger = get_logger(__name__)

# Lazy import pyotp and qrcode to handle missing dependencies gracefully
_pyotp = None
_qrcode = None

def _ensure_pyotp():
    global _pyotp
    if _pyotp is None:
        try:
            import pyotp
            _pyotp = pyotp
        except ImportError:
            raise ImportError("pyotp is required for MFA. Install with: pip install pyotp")
    return _pyotp

def _ensure_qrcode():
    global _qrcode
    if _qrcode is None:
        try:
            import qrcode
            _qrcode = qrcode
        except ImportError:
            raise ImportError("qrcode is required for MFA. Install with: pip install qrcode[pil]")
    return _qrcode

# MFA Configuration
MFA_ISSUER = "Werco ERP"
MFA_DIGITS = 6
MFA_INTERVAL = 30  # seconds
MFA_BACKUP_CODE_COUNT = 10
MFA_BACKUP_CODE_LENGTH = 8


def generate_mfa_secret() -> str:
    """
    Generate a new TOTP secret for MFA setup.
    Returns a base32-encoded secret string.
    """
    pyotp = _ensure_pyotp()
    return pyotp.random_base32()


def get_totp(secret: str):
    """Create a TOTP object for verification."""
    pyotp = _ensure_pyotp()
    return pyotp.TOTP(secret, digits=MFA_DIGITS, interval=MFA_INTERVAL)


def verify_totp(secret: str, code: str) -> bool:
    """
    Verify a TOTP code.
    
    Args:
        secret: The user's MFA secret
        code: The 6-digit code to verify
        
    Returns:
        True if valid, False otherwise
    """
    if not secret or not code:
        return False
    
    try:
        totp = get_totp(secret)
        # Allow 1 interval of clock drift (30 seconds before/after)
        return totp.verify(code, valid_window=1)
    except Exception as e:
        logger.error(f"TOTP verification error: {e}")
        return False


def generate_provisioning_uri(secret: str, email: str) -> str:
    """
    Generate the provisioning URI for authenticator apps.
    
    This URI is used to set up the account in apps like:
    - Google Authenticator
    - Microsoft Authenticator
    - Authy
    - 1Password
    """
    totp = get_totp(secret)
    return totp.provisioning_uri(name=email, issuer_name=MFA_ISSUER)


def generate_qr_code_base64(provisioning_uri: str) -> str:
    """
    Generate a QR code image as a base64-encoded PNG.
    
    Returns:
        Base64 encoded PNG image string for embedding in HTML/JSON
    """
    qrcode = _ensure_qrcode()
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(provisioning_uri)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    
    return base64.b64encode(buffer.getvalue()).decode('utf-8')


def generate_backup_codes() -> List[str]:
    """
    Generate a set of one-time backup codes.
    
    Backup codes are used when the user doesn't have access to their
    authenticator app (lost phone, etc.).
    
    Returns:
        List of backup codes (plain text for display to user)
    """
    codes = []
    for _ in range(MFA_BACKUP_CODE_COUNT):
        # Generate random alphanumeric code
        code = secrets.token_hex(MFA_BACKUP_CODE_LENGTH // 2).upper()
        # Format as XXXX-XXXX for readability
        formatted = f"{code[:4]}-{code[4:]}"
        codes.append(formatted)
    return codes


def hash_backup_code(code: str) -> str:
    """
    Hash a backup code for storage.
    We store hashes, not plain text, for security.
    """
    # Remove formatting (dashes)
    clean_code = code.replace("-", "").upper()
    return hashlib.sha256(clean_code.encode()).hexdigest()


def hash_backup_codes(codes: List[str]) -> List[str]:
    """Hash all backup codes for database storage."""
    return [hash_backup_code(code) for code in codes]


def verify_backup_code(code: str, hashed_codes: List[str]) -> Tuple[bool, Optional[int]]:
    """
    Verify a backup code against stored hashes.
    
    Args:
        code: The backup code to verify
        hashed_codes: List of hashed backup codes
        
    Returns:
        (is_valid, index_of_used_code) - index is None if invalid
    """
    if not code or not hashed_codes:
        return False, None
    
    hashed_input = hash_backup_code(code)
    
    for i, stored_hash in enumerate(hashed_codes):
        if stored_hash and hashed_input == stored_hash:
            return True, i
    
    return False, None


def get_current_totp(secret: str) -> str:
    """
    Get the current TOTP code (for testing/debugging only).
    DO NOT use in production authentication flow.
    """
    totp = get_totp(secret)
    return totp.now()


class MFASetupResult:
    """Result object for MFA setup."""
    def __init__(
        self,
        secret: str,
        provisioning_uri: str,
        qr_code_base64: str,
        backup_codes: List[str],
        backup_codes_hashed: List[str]
    ):
        self.secret = secret
        self.provisioning_uri = provisioning_uri
        self.qr_code_base64 = qr_code_base64
        self.backup_codes = backup_codes  # Plain text for user
        self.backup_codes_hashed = backup_codes_hashed  # For storage


def setup_mfa(email: str) -> MFASetupResult:
    """
    Initialize MFA setup for a user.
    
    Returns all the data needed for the user to set up their authenticator
    app and store backup codes.
    
    Note: The secret should NOT be saved to the database until the user
    verifies they can generate valid codes (confirm_mfa_setup).
    """
    secret = generate_mfa_secret()
    provisioning_uri = generate_provisioning_uri(secret, email)
    qr_code = generate_qr_code_base64(provisioning_uri)
    backup_codes = generate_backup_codes()
    backup_codes_hashed = hash_backup_codes(backup_codes)
    
    return MFASetupResult(
        secret=secret,
        provisioning_uri=provisioning_uri,
        qr_code_base64=qr_code,
        backup_codes=backup_codes,
        backup_codes_hashed=backup_codes_hashed
    )
