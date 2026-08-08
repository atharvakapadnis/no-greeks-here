"""Bulk guest import ahead of the event: CSV -> usernames -> insert.

Usage:
    venv\\Scripts\\python.exe scripts\\import_guests.py guests.csv [--dry-run] [--append]

CSV must have first_name,last_name columns (extra columns are ignored).
Usernames are resolved via services.guests.assign_usernames, which extends
BOTH members of a colliding pair to their full name (e.g. the Campbell
pair: carolyncampbell / chriscampbell), never just one side.

Refuses to run if any guest rows already exist, unless --append is given.
Even under --append, a generated username colliding with an ALREADY-
INSERTED guest (assign_usernames only resolves collisions within this CSV's
own batch, not against the database) is checked for up front, before any
row is written, and every such collision is reported at once rather than
failing on the first insert.

--dry-run prints the same report and writes nothing.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.services import guests  # noqa: E402


def _read_guest_names(csv_path: Path) -> list[guests.GuestName]:
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or not {"first_name", "last_name"}.issubset(
            reader.fieldnames
        ):
            raise ValueError(
                f"CSV must have first_name,last_name columns, got {reader.fieldnames!r}"
            )
        return [
            guests.GuestName(
                first_name=row["first_name"].strip(), last_name=row["last_name"].strip()
            )
            for row in reader
        ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path, help="CSV with first_name,last_name columns")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the report, write nothing"
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="allow running when guest rows already exist",
    )
    args = parser.parse_args()

    db.run_migrations()

    try:
        guest_names = _read_guest_names(args.csv_path)
    except (OSError, ValueError) as exc:
        print(f"error reading {args.csv_path}: {exc}", file=sys.stderr)
        return 1

    existing_rows = db.fetch_guests()
    if existing_rows and not args.append:
        print(
            f"{len(existing_rows)} guest(s) already exist — refusing to run. "
            "Use --append to add more.",
            file=sys.stderr,
        )
        return 1

    try:
        result = guests.assign_usernames(guest_names)
    except ValueError as exc:
        print(f"error resolving usernames: {exc}", file=sys.stderr)
        return 1

    print("Generated usernames:")
    for u in result.usernames:
        print(f"  {u.guest.first_name} {u.guest.last_name} -> {u.username}")
    print()

    if result.extended:
        print("Username collisions (extended to full name):")
        for e in result.extended:
            print(
                f"  {e.guest.first_name} {e.guest.last_name}: "
                f"{e.short_username} -> {e.final_username}"
            )
    else:
        print("No username collisions.")
    print()

    # Checked up front, before any row is written, and all at once — a
    # generated username colliding with an already-inserted guest (only
    # reachable under --append, since assign_usernames only resolves
    # collisions within this CSV's own batch) is a data problem the
    # operator needs to see and fix in the CSV, not a partial insert to
    # clean up after.
    existing_usernames = {row["username"] for row in existing_rows}
    db_collisions = [u for u in result.usernames if u.username in existing_usernames]
    if db_collisions:
        for u in db_collisions:
            print(
                f"username already taken: {u.username} "
                f"({u.guest.first_name} {u.guest.last_name}) — "
                "resolve manually and re-run.",
                file=sys.stderr,
            )
        return 1

    if args.dry_run:
        print(f"Dry run: {len(result.usernames)} guest(s) would be inserted.")
        return 0

    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with db.transaction() as conn:
            for u in result.usernames:
                db.insert_guest(
                    u.username,
                    f"{u.guest.first_name} {u.guest.last_name}".strip(),
                    now_iso,
                    conn=conn,
                )
    except sqlite3.IntegrityError as exc:
        # Backstop only — the pre-check above should have already caught
        # every DB-level collision. Kept in case of a race against another
        # writer between the check and the insert.
        print(f"insert failed: {exc}", file=sys.stderr)
        return 1

    print(f"Inserted {len(result.usernames)} guest(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
