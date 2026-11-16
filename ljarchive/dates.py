"""Parse source dates without assigning a timezone to local journal dates."""
from datetime import datetime
from email.utils import parsedate_to_datetime
import re


def source_datetime(value):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    # getcomments uses e.g. '2005-04-18 04:15:04 UTC'; export XML uses ISO.
    iso = re.sub(r'\s+(?:UTC|GMT)$', '+00:00', value, flags=re.I).replace('Z', '+00:00')
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        try:
            return parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
