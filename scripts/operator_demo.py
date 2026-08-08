"""Throwaway operator-panel rehearsal seed: N horses, M races, a handful of
guests with bets already placed, so a full manual click-through can be
driven without a second terminal poking the API by hand. This is what the
pre-event rehearsal uses.

Usage:
    venv\\Scripts\\python.exe scripts\\operator_demo.py
        [--db-path PATH] [--horses N] [--races M] [--guests K]

Always starts from a FRESH database at --db-path: deletes and recreates it.
Two safety checks run first, both comparing Path(...).resolve() so relative
vs. absolute spelling can't slip past either one:

  1. Refuses if --db-path resolves to the same file as the DATABASE_PATH
     already configured in the environment — this script must never touch
     the real event database.
  2. Refuses if a file already exists at --db-path and contains any
     settled races (checked via a genuinely read-only sqlite connection,
     independent of app.db's write-capable connection helpers) — a
     second, independent guard in case --db-path points at a real,
     in-progress event db that simply isn't the currently configured
     DATABASE_PATH.

Leaves the seeded event mid-rehearsal: race 1 settled, race 2 OPEN with a
partial bet count (so /operator lands directly on the "open" view with a
non-empty who-hasn't-bet chip list when the operator logs in), race 3 with
one horse pre-scratched.
"""

from __future__ import annotations

import argparse
import os
import random
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.services import bets, guests, races  # noqa: E402

# A synthetic roster, not the real guest list — scripts/import_guests.py
# owns the CSV path for that. Kept short and alphabetically boring on
# purpose so demo usernames are easy to read off screen.
_FIRST_NAMES = [
    "Alex", "Bailey", "Casey", "Dana", "Ellis", "Frankie",
    "Gray", "Harper", "Isla", "Jordan", "Kit", "Logan",
    "Morgan", "Nico", "Oakley", "Parker", "Quinn", "Riley",
    "Sam", "Taylor",
]
_LAST_NAMES = [
    "Adler", "Bishop", "Carver", "Dunn", "Ellery", "Foss",
    "Grady", "Holt", "Ibarra", "Judge", "Knox", "Lang",
    "Marsh", "Novak", "Oberg", "Pace", "Quill", "Reyes",
    "Stern", "Turner",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guest_pool(n: int) -> list[guests.GuestName]:
    if n > len(_FIRST_NAMES):
        raise ValueError(f"at most {len(_FIRST_NAMES)} demo guests supported, got {n}")
    rng = random.Random(1234)  # deterministic across runs
    firsts, lasts = _FIRST_NAMES[:], _LAST_NAMES[:]
    rng.shuffle(firsts)
    rng.shuffle(lasts)
    return [
        guests.GuestName(first_name=f, last_name=l)
        for f, l in zip(firsts[:n], lasts[:n])
    ]


def _refuse_if_targets_configured_database(db_path: Path) -> None:
    configured = os.environ.get("DATABASE_PATH")
    if configured and Path(configured).resolve() == db_path.resolve():
        print(
            f"--db-path resolves to the currently configured DATABASE_PATH "
            f"({configured}) — refusing to run against the real event "
            f"database.",
            file=sys.stderr,
        )
        sys.exit(1)


_DEMO_MARKER_KEY = "operator_demo_seed"


def _refuse_if_existing_db_has_settled_races(db_path: Path) -> None:
    """Refuses to overwrite a file that already has settled races, UNLESS
    it's tagged as this script's own previous output (_DEMO_MARKER_KEY,
    written after a successful seed below) — otherwise the rehearsal
    script could never be re-run against its own default --db-path, since
    every successful run leaves race 1 settled."""
    if not db_path.exists():
        return
    settled_count, is_own_demo_db = 0, False
    try:
        uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            settled_count = conn.execute(
                "SELECT COUNT(*) FROM race WHERE status = 'SETTLED'"
            ).fetchone()[0]
            marker = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (_DEMO_MARKER_KEY,)
            ).fetchone()
            is_own_demo_db = marker is not None and marker[0] == "true"
        finally:
            conn.close()
    except sqlite3.Error:
        # Not a valid/initialised ngh database (or a stray empty file) —
        # nothing worth protecting.
        settled_count = 0
    if settled_count and not is_own_demo_db:
        print(
            f"{db_path} already exists and has {settled_count} settled "
            f"race(s) and wasn't created by this script — refusing to "
            f"overwrite what looks like a real, in-progress event. Pick a "
            f"different --db-path.",
            file=sys.stderr,
        )
        sys.exit(1)


def _delete_existing(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=Path("operator_demo.db"))
    parser.add_argument("--horses", type=int, default=6, help="default 6")
    parser.add_argument("--races", type=int, default=10, help="default 10")
    parser.add_argument("--guests", type=int, default=12, help="default 12 (a handful)")
    args = parser.parse_args()

    if args.horses < 3:
        print("--horses must be at least 3.", file=sys.stderr)
        return 1
    if args.races < 3:
        print(
            "--races must be at least 3 (this script settles race 1, opens "
            "race 2, and scratches a horse in race 3).",
            file=sys.stderr,
        )
        return 1

    db_path = args.db_path
    _refuse_if_targets_configured_database(db_path)
    _refuse_if_existing_db_has_settled_races(db_path)
    _delete_existing(db_path)

    os.environ["DATABASE_PATH"] = str(db_path)

    db.run_migrations()
    db.initialise_event(horse_count=args.horses, total_races=args.races)
    db.set_meta(_DEMO_MARKER_KEY, "true")

    guest_names = _guest_pool(args.guests)
    assignment = guests.assign_usernames(guest_names)

    now = datetime.now(timezone.utc)
    now_iso = _now_iso()
    claimed_guest_ids: list[int] = []
    unclaimed_usernames: set[str] = set()
    for i, u in enumerate(assignment.usernames):
        guest_id = db.insert_guest(
            u.username, f"{u.guest.first_name} {u.guest.last_name}", now_iso
        )
        # Roughly three-quarters claimed, so the rest exercise the
        # unclaimed-guest paths (excluded from who-hasn't-bet and the
        # unlock picker, included in the fix-a-bet picker).
        if i % 4 != 3:
            db.claim_guest_device(guest_id, f"demo-token-{u.username}", now_iso)
            claimed_guest_ids.append(guest_id)
        else:
            unclaimed_usernames.add(u.username)

    # Race 1: open, most claimed guests bet, lock, settle.
    races.open_race(1, now)
    for n, guest_id in enumerate(claimed_guest_ids):
        if n % 5 != 4:  # ~80% bet
            horse = (n % args.horses) + 1
            bets.place_bet(guest_id, 1, horse, f"demo-r1-{guest_id}", now)
    races.lock_race(1, now)
    races.settle_race(1, 1, 2, 3, now)

    # Race 2: open, only about half of claimed guests bet, left OPEN — so
    # /operator lands directly on the "open" view with a non-empty
    # who-hasn't-bet chip list when the operator logs in.
    races.open_race(2, now)
    for n, guest_id in enumerate(claimed_guest_ids):
        if n % 2 == 0:
            horse = (n % args.horses) + 1
            bets.place_bet(guest_id, 2, horse, f"demo-r2-{guest_id}", now)

    # Race 3: still SCHEDULED — scratch one horse so the scratch
    # checkboxes have a pre-scratched entry to look at.
    races.set_scratched(3, args.horses, True, now)

    print(f"Database: {db_path.resolve()}")
    password = os.environ.get("OPERATOR_PASSWORD")
    if password:
        print(f"Operator password: {password}")
    else:
        print(
            "Operator password: OPERATOR_PASSWORD is not set in this shell "
            "— export it before starting uvicorn."
        )
    print()
    print(f"Guests ({len(assignment.usernames)}):")
    for u in assignment.usernames:
        status = "unclaimed" if u.username in unclaimed_usernames else "claimed"
        print(f"  {u.username:20s} {u.guest.first_name} {u.guest.last_name} ({status})")
    print()
    print("Race 1: SETTLED (1st #1, 2nd #2, 3rd #3)")
    print("Race 2: OPEN — some claimed guests have bet, some haven't")
    print(f"Race 3: horse #{args.horses} scratched, otherwise SCHEDULED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
