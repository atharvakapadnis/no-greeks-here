"""scripts/import_guests.py, exercised as a real subprocess against a real
sqlite file — this is a CLI tool, not app code, so there's no TestClient/app
fixture to hook into; running it exactly as an operator would is the most
direct way to verify it.
"""

from __future__ import annotations

import csv
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import db

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "import_guests.py"


def _write_csv(path: Path, rows: list[tuple[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["first_name", "last_name"])
        writer.writerows(rows)


def _run(
    csv_path: Path, db_path: Path, *extra_args: str
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DATABASE_PATH"] = str(db_path)
    env["SECRET_KEY"] = "test-secret-key"
    env["OPERATOR_PASSWORD"] = "hunter2"
    env["ENV"] = "dev"
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(csv_path), *extra_args],
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("OPERATOR_PASSWORD", "hunter2")
    monkeypatch.setenv("ENV", "dev")
    return path


def test_import_guests_dry_run_does_not_write(tmp_path, db_path):
    csv_path = tmp_path / "guests.csv"
    _write_csv(csv_path, [("Jane", "Doe"), ("Alice", "Smith")])

    result = _run(csv_path, db_path, "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "Dry run: 2 guest(s) would be inserted." in result.stdout
    assert db.fetch_guests() == []


def test_import_guests_normal_run_inserts_guests(tmp_path, db_path):
    csv_path = tmp_path / "guests.csv"
    _write_csv(csv_path, [("Jane", "Doe"), ("Alice", "Smith")])

    result = _run(csv_path, db_path)

    assert result.returncode == 0, result.stderr
    assert "Inserted 2 guest(s)." in result.stdout
    usernames = {r["username"] for r in db.fetch_guests()}
    assert usernames == {"jdoe", "asmith"}


def test_import_guests_prints_generated_usernames(tmp_path, db_path):
    csv_path = tmp_path / "guests.csv"
    _write_csv(csv_path, [("Jane", "Doe")])

    result = _run(csv_path, db_path, "--dry-run")

    assert "Generated usernames:" in result.stdout
    assert "Jane Doe -> jdoe" in result.stdout


def test_import_guests_prints_collision_report_for_campbell_pair(tmp_path, db_path):
    csv_path = tmp_path / "guests.csv"
    _write_csv(csv_path, [("Carolyn", "Campbell"), ("Chris", "Campbell")])

    result = _run(csv_path, db_path, "--dry-run")

    assert "Username collisions (extended to full name):" in result.stdout
    assert "Carolyn Campbell: ccampbell -> carolyncampbell" in result.stdout
    assert "Chris Campbell: ccampbell -> chriscampbell" in result.stdout


def test_import_guests_refuses_when_guests_exist_without_append(tmp_path, db_path):
    csv_path = tmp_path / "guests.csv"
    _write_csv(csv_path, [("Jane", "Doe")])
    _run(csv_path, db_path)

    csv_path2 = tmp_path / "guests2.csv"
    _write_csv(csv_path2, [("Bob", "Smith")])
    result = _run(csv_path2, db_path)

    assert result.returncode == 1
    assert "already exist" in result.stderr
    usernames = {r["username"] for r in db.fetch_guests()}
    assert usernames == {"jdoe"}


def test_import_guests_append_flag_allows_adding_when_guests_exist(tmp_path, db_path):
    csv_path = tmp_path / "guests.csv"
    _write_csv(csv_path, [("Jane", "Doe")])
    _run(csv_path, db_path)

    csv_path2 = tmp_path / "guests2.csv"
    _write_csv(csv_path2, [("Bob", "Smith")])
    result = _run(csv_path2, db_path, "--append")

    assert result.returncode == 0, result.stderr
    usernames = {r["username"] for r in db.fetch_guests()}
    assert usernames == {"jdoe", "bsmith"}
