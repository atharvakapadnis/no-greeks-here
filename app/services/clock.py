"""Shared guard for the one invariant every race/bet function depends on:
`now` must be timezone-aware UTC. A naive datetime slipping through would
raise TypeError comparing offset-naive to offset-aware deep inside a clock
comparison, mid-event.
"""

from __future__ import annotations

from datetime import datetime


def require_aware(now: datetime) -> None:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (got a naive datetime)")
