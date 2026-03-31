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


