from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

Clock = Callable[[], datetime]


def system_clock() -> datetime:
    """Return the current instant as a UTC-aware timestamp."""
    return datetime.now(timezone.utc)
