from bleach import clean
from typing import Any


def sanitize_string(value: str, allow_tags: list = None) -> str:
    """
    Remove HTML/script content from strings

    Args:
        value: Input string to sanitize
        allow_tags: List of allowed HTML tags (default: none)

    Returns:
        Sanitized string
    """
    if not isinstance(value, str):
        return str(value) if value is not None else ""

    return clean(value, tags=allow_tags or [], strip=True)


def sanitize_dict(data: dict, keys: list[str] = None) -> dict:
    """
    Sanitize all string values in a dict

    Args:
        data: Dictionary to sanitize
        keys: Specific keys to sanitize (default: all string values)

    Returns:
        Dictionary with sanitized string values
    """
    result = {}
    for key, value in data.items():
        if keys is None or key in keys:
            if isinstance(value, str):
                result[key] = sanitize_string(value)
            elif isinstance(value, dict):
                result[key] = sanitize_dict(value, keys)
            elif isinstance(value, list):
                result[key] = [
                    sanitize_string(item) if isinstance(item, str) else item
                    for item in value
                ]
            else:
                result[key] = value
        else:
            result[key] = value
    return result


def validate_file_upload(
    filename: str,
    content_type: str,
    file_size: int,
    max_size_mb: int = 10
) -> list[str]:
    """
    Validate file upload properties

    Args:
        filename: File name with extension
        content_type: MIME type
        file_size: File size in bytes
        max_size_mb: Maximum allowed size in MB

    Returns:
        List of error messages (empty if valid)
    """
    errors = []

    # Allowed extensions and their expected MIME types
    ALLOWED_EXTENSIONS = {
        '.pdf': 'application/pdf',
        '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.txt': 'text/plain',
    }

    MAX_FILE_SIZE = max_size_mb * 1024 * 1024  # Convert to bytes

    # Check extension
    if not filename:
        errors.append("Filename required")
        return errors

    ext = '.' + filename.split('.')[-1].lower() if '.' in filename else ''
    if ext.lower() not in ALLOWED_EXTENSIONS:
        errors.append(
            f"File type {ext} not allowed. "
            f"Allowed: {', '.join(ALLOWED_EXTENSIONS.keys())}"
        )

    # Check size
    if file_size > MAX_FILE_SIZE:
        errors.append(
            f"File too large ({file_size / (1024 * 1024):.2f}MB). "
            f"Maximum size: {max_size_mb}MB"
        )

    # Check MIME type matches extension
    if ext.lower() in ALLOWED_EXTENSIONS:
        expected_mime = ALLOWED_EXTENSIONS[ext.lower()]
        if content_type != expected_mime:
            errors.append(
                f"File content doesn't match extension. "
                f"Expected: {expected_mime}, Got: {content_type}"
            )

    return errors


def validate_phone_number(phone: str) -> tuple[bool, str]:
    """
    Validate phone number format

    Args:
        phone: Phone number string

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not phone:
        return True, ""  # Optional field

    # Remove all non-digit characters for validation
    digits_only = re.sub(r'[^\d]', '', phone)

    # US phone number (10 or 11 digits, 11 must start with 1)
    if len(digits_only) == 11 and not digits_only.startswith('1'):
        return False, "Invalid US phone number"
    if len(digits_only) not in [10, 11]:
        return False, "Phone number must be 10-11 digits"

    return True, ""


import re
