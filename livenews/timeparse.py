from __future__ import annotations

import re
from datetime import datetime, timezone

_FRAC = re.compile(r"\.(\d+)")


def parse_ts(raw: str | None) -> datetime | None:
    """RFC3339 to aware datetime. Alpaca sends nanoseconds, Python wants <= 6 digits."""
    if not raw:
        return None
    s = raw.strip().replace("Z", "+00:00")
    s = _FRAC.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), s, count=1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
