from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

IST = timezone(timedelta(hours=5, minutes=30))

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def normalize_header(header: str) -> str:
    """Lower-case, trim, and collapse any run of underscores or whitespace to a single space."""
    return re.sub(r"[_\s]+", " ", header.strip().lower())


def normalize_phone(raw: str | None) -> str | None:
    """
    Indian mobile number normalization:
    - Strip every non-digit.
    - If 12 digits starting with 91, drop 91.
    - If 11 digits starting with 0, drop 0.
    - Valid iff 10 digits starting with 6, 7, 8, or 9.
    - Canonical format: +91XXXXXXXXXX.
    """
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in {"6", "7", "8", "9"}:
        return f"+91{digits}"
    return None


def normalize_date(raw: str | None) -> str | None:
    """
    Accept YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, and DD-Mon-YYYY (case-insensitive).
    Dates are day-first. Impossible dates (like 31/02) return None.
    Returns ISO YYYY-MM-DD.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None

    # 1. YYYY-MM-DD
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _build_iso_date(y, mo, d)

    # 2. DD/MM/YYYY or DD-MM-YYYY
    m = re.fullmatch(r"(\d{2})[/-](\d{2})[/-](\d{4})", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _build_iso_date(y, mo, d)

    # 3. DD-Mon-YYYY
    m = re.fullmatch(r"(\d{2})-([A-Za-z]{3})-(\d{4})", s)
    if m:
        d = int(m.group(1))
        mon_str = m.group(2).lower()
        y = int(m.group(3))
        if mon_str not in MONTH_MAP:
            return None
        return _build_iso_date(y, MONTH_MAP[mon_str], d)

    return None


def _build_iso_date(year: int, month: int, day: int) -> str | None:
    if not (1 <= month <= 12):
        return None
    max_day = calendar.monthrange(year, month)[1]
    if not (1 <= day <= max_day):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def normalize_decimal(raw: str | None) -> str | None:
    """
    Optionally prefixed by ₹, Rs, Rs. or INR (case-insensitive, optional space).
    Commas are thousands separators. Must be >= 0. Round to 2 decimal places.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    s = re.sub(r"^(?:₹|rs\.?|inr)\s*", "", s, flags=re.IGNORECASE)
    s = s.replace(",", "").strip()
    if not s:
        return None
    try:
        val = Decimal(s)
    except InvalidOperation:
        return None
    if not val.is_finite() or val < 0:
        return None
    rounded = val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{rounded:.2f}"


def normalize_enum(raw: str | None, allowed_values: list[str]) -> str | None:
    if raw is None:
        return None
    s = raw.strip().lower()
    if not s:
        return None
    allowed_lower = {v.lower() for v in allowed_values}
    return s if s in allowed_lower else None


def parse_timestamp_to_ist(raw: str) -> tuple[datetime, datetime]:
    """
    Parse UTC (...Z), offset (...+05:30), or naive (YYYY-MM-DD HH:MM:SS -> IST) timestamp.
    Returns (utc_dt, ist_dt).
    """
    s = raw.strip()
    if s.endswith("Z") or s.endswith("z"):
        dt = datetime.fromisoformat(s[:-1] + "+00:00")
    else:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
    utc_dt = dt.astimezone(timezone.utc)
    ist_dt = dt.astimezone(IST)
    return utc_dt, ist_dt
