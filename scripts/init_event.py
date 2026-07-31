"""One-time event bootstrap: run migrations, then create the horses/races.

Usage:
    venv\\Scripts\\python.exe scripts\\init_event.py --horses 6 --races 10

Refuses to run twice — db.initialise_event raises EventAlreadyInitialisedError
if the event is already set up, and this script does not touch the database
any further in that case. There is deliberately no "reset" option here: see
the design doc's non-negotiable that the app never bootstraps a database on
its own, only this explicit, hand-run command does.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--horses", type=int, default=6, help="number of horses (default 6)")
    parser.add_argument("--races", type=int, default=10, help="total races (default 10)")
    args = parser.parse_args()

    db.run_migrations()
    try:
        db.initialise_event(horse_count=args.horses, total_races=args.races)
    except db.EventAlreadyInitialisedError:
        print("Event already initialised - refusing to run again.", file=sys.stderr)
        return 1

    print(
        f"Initialised event: {args.horses} horses, {args.races} races, "
        f"database at {os.environ.get('DATABASE_PATH')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
