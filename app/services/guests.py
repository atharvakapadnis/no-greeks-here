"""Username generation and collision resolution for guests.

Pure functions only: no I/O, no database, no network. Standard library only.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_STRIP_CHARS_RE = re.compile(r"[\s\-'’.]")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")


def _normalize(text: str) -> str:
    """Lowercase, strip accents to ASCII, and remove spaces, hyphens,
    apostrophes (straight and curly) and periods. Anything else that isn't
    ``[a-z0-9]`` is dropped too."""
    text = text.strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _STRIP_CHARS_RE.sub("", text)
    text = _NON_ALNUM_RE.sub("", text)
    return text


@dataclass(frozen=True)
class GuestName:
    first_name: str
    last_name: str


@dataclass(frozen=True)
class UsernameResult:
    guest: GuestName
    username: str


@dataclass(frozen=True)
class ExtendedName:
    guest: GuestName
    short_username: str
    final_username: str


@dataclass(frozen=True)
class BulkAssignmentResult:
    usernames: list[UsernameResult]
    extended: list[ExtendedName]


def _combine(first_norm: str, last_norm: str, first_name: str, last_name: str) -> str:
    if not first_norm and not last_norm:
        raise ValueError(
            f"name normalises to an empty username: first_name={first_name!r} last_name={last_name!r}"
        )
    return first_norm + last_norm


def base_username(first_name: str, last_name: str) -> str:
    """First initial + full last name, lowercased and normalised.

    Falls back to whichever of first_name/last_name is non-empty if only
    one is given. Raises ValueError if both are empty/whitespace-only, or
    if the result normalises to an empty string.
    """
    first_norm = _normalize(first_name)
    last_norm = _normalize(last_name)
    if not last_norm:
        username = first_norm
    elif not first_norm:
        username = last_norm
    else:
        username = first_norm[0] + last_norm
    if not username:
        raise ValueError(
            f"name normalises to an empty username: first_name={first_name!r} last_name={last_name!r}"
        )
    return username


def full_username(first_name: str, last_name: str) -> str:
    """Full first name + full last name, lowercased and normalised.

    This is the extended/collision form. Same emptiness validation as
    base_username.
    """
    first_norm = _normalize(first_name)
    last_norm = _normalize(last_name)
    return _combine(first_norm, last_norm, first_name, last_name)


def split_full_name(full_name: str) -> GuestName:
    """Best-effort split of a single "full name" text box into first/last.

    Last whitespace-separated token becomes last_name, everything before it
    becomes first_name (empty string for single-word names like
    "Madonna"). Raises ValueError if full_name is empty or whitespace-only.
    """
    if not full_name or not full_name.strip():
        raise ValueError("full_name cannot be empty or whitespace-only")
    tokens = full_name.strip().split()
    last_name = tokens[-1]
    first_name = " ".join(tokens[:-1])
    return GuestName(first_name=first_name, last_name=last_name)


def _resolve_collision(candidate: str, taken: set[str]) -> str:
    if candidate not in taken:
        return candidate
    n = 2
    while f"{candidate}{n}" in taken:
        n += 1
    return f"{candidate}{n}"


def assign_usernames(guests: list[GuestName]) -> BulkAssignmentResult:
    """Bulk import. Guests whose base_username collides are ALL extended to
    full_username (never just one side). If extended forms still collide,
    the smallest integer >= 2 is appended, resolved deterministically in
    input order. Guests with no collision keep the base form.
    """
    bases = [base_username(g.first_name, g.last_name) for g in guests]
    base_counts: dict[str, int] = {}
    for b in bases:
        base_counts[b] = base_counts.get(b, 0) + 1
    colliding_indices = {i for i, b in enumerate(bases) if base_counts[b] > 1}

    candidates: list[str] = []
    for i, g in enumerate(guests):
        if i in colliding_indices:
            candidates.append(full_username(g.first_name, g.last_name))
        else:
            candidates.append(bases[i])

    taken: set[str] = set()
    final_usernames: list[str] = []
    for candidate in candidates:
        username = _resolve_collision(candidate, taken)
        taken.add(username)
        final_usernames.append(username)

    usernames = [
        UsernameResult(guest=g, username=u) for g, u in zip(guests, final_usernames)
    ]
    extended = [
        ExtendedName(
            guest=guests[i],
            short_username=bases[i],
            final_username=final_usernames[i],
        )
        for i in sorted(colliding_indices)
    ]
    return BulkAssignmentResult(usernames=usernames, extended=extended)


def add_guest_username(new_guest: GuestName, existing_usernames: set[str]) -> str:
    """Incremental/mid-event registration.

    existing_usernames is never mutated or renamed: only the new guest is
    bumped to full_username, then suffixed with the smallest integer >= 2,
    if it still collides.
    """
    base = base_username(new_guest.first_name, new_guest.last_name)
    if base not in existing_usernames:
        return base
    full = full_username(new_guest.first_name, new_guest.last_name)
    return _resolve_collision(full, existing_usernames)
