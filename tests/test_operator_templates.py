"""Step 4b template pass: per-view markup presence/absence assertions.

Separate from test_operator_routes.py's behavioral/transition tests, same
split as test_operator_auth.py / test_operator_routes.py. These tests parse
rendered HTML (never execute the client-side tap-grid JS) and assert what a
future change could quietly break: an invalid action's <form> genuinely
absent (not merely disabled), scratched-vs-assigned markup distinguishable,
pickers scoped to the right guest set, no destructive copy anywhere.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, db
from app.services import races

TEMPLATES_DIR = (
    Path(__file__).resolve().parent.parent / "app" / "templates" / "operator"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("OPERATOR_PASSWORD", "hunter2")
    monkeypatch.setenv("ENV", "dev")
    return db_path


@pytest.fixture
def initialised_db(app_env):
    db.run_migrations()
    db.initialise_event(horse_count=6, total_races=3)
    return app_env


@pytest.fixture
def client(initialised_db):
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    auth.record_operator_login_success()
    yield
    auth.record_operator_login_success()


def _login_operator(client) -> None:
    client.post("/operator/login", data={"password": "hunter2"})


def _add_guest(username: str, display_name: str = "Guest", *, claimed: bool = True) -> int:
    guest_id = db.insert_guest(username, display_name, _now_iso())
    if claimed:
        db.claim_guest_device(guest_id, f"token-{username}", _now_iso())
    return guest_id


def _view_html(client, view: str) -> str:
    """Drives real state transitions directly against the db (not through
    routes — same convention test_operator_routes.py's
    test_effective_state_* tests use) and returns the rendered /operator
    page for the requested view.

    "settled" scratches horse 4 in race 1 while it's still OPEN (the only
    legal window — set_scratched forbids LOCKED/SETTLED) before locking
    and settling with placings 1/2/3, so that view has both a genuinely
    scratched horse and three "assigned" placings to distinguish.
    """
    now = _now()
    if view == "scheduled":
        pass
    elif view == "open":
        races.open_race(1, now)
    elif view == "locked":
        races.open_race(1, now)
        races.lock_race(1, now)
    elif view == "settled":
        races.open_race(1, now)
        races.set_scratched(1, 4, True, now)
        races.lock_race(1, now)
        races.settle_race(1, 1, 2, 3, now)
    elif view == "complete":
        for n in range(1, 4):  # initialised_db: 3 races
            races.open_race(n, now)
            races.lock_race(n, now)
            races.settle_race(n, 1, 2, 3, now)
    else:
        raise ValueError(f"unknown view {view!r}")

    _login_operator(client)
    return client.get("/operator").text


class _FormActionCollector(HTMLParser):
    """Collects (method, action) from every <form> tag's attribute dict,
    independent of attribute order or an intervening class= — a fixed-
    order regex breaks silently the moment a template's attributes get
    reordered or gain a class, and a brittle absence-assertion is worse
    than none."""

    def __init__(self):
        super().__init__()
        self.actions: set[tuple[str, str]] = set()

    def handle_starttag(self, tag, attrs):
        if tag != "form":
            return
        attrs_dict = dict(attrs)
        action = attrs_dict.get("action")
        if action is None:
            return
        method = (attrs_dict.get("method") or "get").lower()
        self.actions.add((method, action))


def _form_actions(html: str) -> set[tuple[str, str]]:
    collector = _FormActionCollector()
    collector.feed(html)
    return collector.actions


_HORSE_BTN_RE = re.compile(
    r'<button\s+type="button"\s+class="([^"]*results-horse-btn[^"]*)"\s+'
    r'data-horse-number="(\d+)"([^>]*)>',
    re.DOTALL,
)


def _horse_buttons(html: str) -> dict[int, tuple[str, str]]:
    """horse number -> (class attribute value, trailing attribute text) for
    every results-entry tap button on the page."""
    return {
        int(number): (class_attr, trailing)
        for class_attr, number, trailing in _HORSE_BTN_RE.findall(html)
    }


# --- absence-asserting action table -----------------------------------------

_open = ("post", "/operator/race/open")
_lock = ("post", "/operator/race/lock")
_reopen = ("post", "/operator/race/reopen")
_settle = ("post", "/operator/race/settle")
_correct = ("post", "/operator/race/correct")
_scratch = ("post", "/operator/race/scratch")

VIEW_ACTION_TABLE = [
    ("scheduled", {_open, _scratch}, {_lock, _reopen, _settle, _correct}),
    ("open", {_lock, _scratch}, {_open, _reopen, _settle, _correct}),
    ("locked", {_settle, _reopen}, {_open, _lock, _scratch, _correct}),
    ("settled", {_open, _correct, _scratch}, {_lock, _settle, _reopen}),
    ("complete", set(), {_open, _lock, _reopen, _settle, _correct, _scratch}),
]


@pytest.mark.parametrize(
    "view,expected_present,expected_absent",
    VIEW_ACTION_TABLE,
    ids=[row[0] for row in VIEW_ACTION_TABLE],
)
def test_view_shows_only_valid_action_forms(client, view, expected_present, expected_absent):
    html = _view_html(client, view)
    actions = _form_actions(html)

    for action in expected_present:
        assert action in actions, f"{action} missing from {view} view"
    for action in expected_absent:
        assert action not in actions, f"{action} present on {view} view but shouldn't be"


# --- no destructive copy -----------------------------------------------------


def test_no_destructive_action_strings_in_templates():
    forbidden = ["reset", "delete", "wipe", "re-initialise"]
    for path in TEMPLATES_DIR.rglob("*.html"):
        text = path.read_text(encoding="utf-8").lower()
        for word in forbidden:
            assert word not in text, f"{word!r} found in {path}"


# --- auto-lock selector -------------------------------------------------


@pytest.mark.parametrize("view", ["scheduled", "settled"])
def test_auto_lock_selector_renders_on_scheduled_and_settled_defaults_none(client, view):
    html = _view_html(client, view)

    assert 'name="auto_lock_seconds"' in html
    assert re.search(r'<option value=""\s*selected', html) is not None


# --- scratch checkboxes ---------------------------------------------------


@pytest.mark.parametrize("view", ["scheduled", "open", "settled"])
def test_scratch_checkboxes_render_on_scheduled_open_and_settled(client, view):
    html = _view_html(client, view)

    assert re.search(
        r'<input[^>]*type="checkbox"[^>]*name="scratched"[^>]*value="true"', html
    ) is not None
    # posts its own desired state — no client-computed-opposite hidden field
    assert '<input type="hidden" name="scratched"' not in html


def test_settled_view_scratch_copy_labelled_for_next_race(client):
    html = _view_html(client, "settled")

    assert "Race 2" in html

    race_numbers = set(
        re.findall(
            r'<form method="post" action="/operator/race/scratch" class="[^"]*">\s*'
            r'<input type="hidden" name="race_number" value="(\d+)">',
            html,
        )
    )
    assert race_numbers == {"2"}


# --- results entry: assigned vs scratched, publish disabled ---------------


def test_results_entry_distinguishes_assigned_from_scratched_horse(client):
    html = _view_html(client, "settled")
    buttons = _horse_buttons(html)

    used_class, used_trailing = buttons[1]  # 1st placing
    scratched_class, scratched_trailing = buttons[4]  # scratched, never placed

    assert "horse-btn--used" in used_class
    assert "horse-btn--scratched" not in used_class
    assert "disabled" in used_trailing
    assert "data-scratched" not in used_trailing

    assert "horse-btn--scratched" in scratched_class
    assert "horse-btn--used" not in scratched_class
    assert "data-scratched" in scratched_trailing
    assert "disabled" in scratched_trailing

    for class_attr, _ in buttons.values():
        assert not ("horse-btn--used" in class_attr and "horse-btn--scratched" in class_attr)


@pytest.mark.parametrize("view", ["locked", "settled"])
def test_publish_button_disabled_in_initial_markup(client, view):
    html = _view_html(client, view)

    match = re.search(r'<button class="btn[^"]*results-entry__publish"[^>]*>', html)
    assert match is not None
    assert "disabled" in match.group(0)


# --- who-hasn't-bet chips ----------------------------------------------


def test_who_hasnt_bet_excludes_unclaimed_guests_renders_as_chips(client):
    _login_operator(client)
    _add_guest("jdoe", "Jane Doe", claimed=True)
    _add_guest("bsmith", "Bob Smith", claimed=False)
    races.open_race(1, _now())

    html = client.get("/operator").text

    # Scoped to the chip specifically, not the whole page — Bob Smith
    # legitimately appears elsewhere now, in the Fix-a-bet username
    # datalist (which deliberately includes unclaimed guests for the
    # paper-fallback path).
    assert '<span class="chip">Jane Doe</span>' in html
    assert '<span class="chip">Bob Smith</span>' not in html
    assert "<li>" not in html


# --- backup footer -------------------------------------------------------


@pytest.mark.parametrize("view", ["scheduled", "open", "locked", "settled", "complete"])
def test_backup_footer_renders_on_every_view(client, view):
    html = _view_html(client, view)

    assert "Backup:" in html
