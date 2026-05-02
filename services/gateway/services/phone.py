"""
Phone number normalization using the phonenumbers library.

Handles international formats properly — not just India like HelmTech.
All numbers stored in E.164 format (e.g., +919876543210).
"""

import phonenumbers


def normalize_phone(phone: str, default_region: str = "IN") -> str:
    """Normalize a phone number to E.164 format.

    Args:
        phone: Raw phone number (any format)
        default_region: ISO country code if number has no country prefix

    Returns:
        E.164 formatted number without + prefix (e.g., "919876543210")

    Raises:
        ValueError: If the number cannot be parsed
    """
    phone = phone.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    try:
        parsed = phonenumbers.parse(phone, default_region)
        if not phonenumbers.is_valid_number(parsed):
            raise ValueError(f"Invalid phone number: {phone}")
        # E.164 without the + prefix (Meta expects this)
        return phonenumbers.format_number(
            parsed, phonenumbers.PhoneNumberFormat.E164
        ).lstrip("+")
    except phonenumbers.NumberParseException as e:
        raise ValueError(f"Cannot parse phone number '{phone}': {e}") from e


def get_country_code(phone: str, default_region: str = "IN") -> str:
    """Extract the ISO country code from a phone number.

    Args:
        phone: Phone number (any format)
        default_region: Fallback if no country code in number

    Returns:
        ISO country code (e.g., "IN", "US")
    """
    phone = phone.strip().replace(" ", "").replace("-", "")
    try:
        parsed = phonenumbers.parse(phone, default_region)
        return phonenumbers.region_code_for_number(parsed) or default_region
    except phonenumbers.NumberParseException:
        return default_region
