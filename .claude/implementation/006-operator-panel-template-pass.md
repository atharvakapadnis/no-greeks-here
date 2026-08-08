# 006 — Operator panel, template pass (Step 4b, continued)

2026-08-08

## What was built and why

This session was Step 4b's deferred template pass: turning session 005's
functional-but-crude operator templates (`<select>` dropdowns for results,
raw numeric guest-ID inputs, no CSS layout) into the tap-driven, laid-out,
tested panel the design doc specifies. A plan was written, reviewed, and
corrected by the user in four ways before implementation — see
"Corrections from the approved plan" below. The corrected plan is what's
implemented; see it in full at
`.claude/plans/read-claude-design-design-doc-md-claude-pure-deer.md`.

No changes to `races.py`/`bets.py`/`scoring.py`. No new routes. Two
existing routes changed their form-field contract (`guest_id` → `username`)
as a user-approved scope addition — see "Guest picker" below.

## Corrections from the approved plan

1. **Scratch checkbox posts its own desired state**, not a client-computed
   opposite. The old design had a hidden `scratched` field carrying the
   *inverse* of the current render-time state — exactly the "client
   asserts current server state" pattern Step 3 removed everywhere else,
   and it silently no-oped on a double-click (`set_scratched` no-ops when
   the flag already matches, so the box would visually re-render toggled
   while nothing changed). Fixed: the checkbox itself is
   `name="scratched" value="true"`, checked iff currently scratched; an
   unchecked box submits nothing. `post_race_scratch`'s signature changed
   from `scratched: bool = Form(...)` to `scratched: bool = Form(False)`.
2. **Scratch is available on scheduled, open, AND settled**, not just
   scheduled+settled. `races.set_scratched` deliberately permits SCHEDULED
   *or* OPEN (Step 3 added the OPEN case, with mid-race bet voiding,
   specifically so a horse pulling up lame after betting opens has a
   recovery path) — a UI omitting it from the open view made that path
   unreachable. On `open.html` it renders collapsed inside `<details>`
   ("Scratch a horse", with a voiding warning), so it never competes with
   "Lock betting".
3. **Template absence tests use `html.parser.HTMLParser`**, not a
   fixed-attribute-order regex, to extract `<form method=... action=...>`
   pairs — a regex anchored to `method` immediately followed by `action`
   would break silently the moment a template put a `class=` between them.
4. **`scripts/operator_demo.py`'s safety checks** compare
   `Path(...).resolve()` on both sides (not raw strings), and add a
   second, independent guard refusing to overwrite an existing target file
   that has settled races — unless that file carries this script's own
   `operator_demo_seed` meta marker (written after a successful seed),
   which is what lets the script be re-run repeatedly against its own
   default `--db-path` for rehearsal without tripping its own guard.

## Guest picker (approved scope addition beyond the original 6-item brief)

A numeric `guest_id` field was never something an operator could supply by
hand. `_secondary_actions.html`'s Fix-a-bet and Unlock forms now take a
`username` field, resolved server-side via `db.fetch_guest_by_username`
(existing primitive, unchanged) — `None` maps to the existing
`"guest_not_found"` flash message. `bets.GuestNotFoundError`'s except
clause in `post_bet_set` is now unreachable through the route (the lookup
already resolved username → id) and is kept as a documented backstop, same
convention as this file's other TOCTOU branches.

Two separate guest lists, not one, because they're genuinely different
sets:

- `unlock_guests` — only guests with `device_token IS NOT NULL`. Unlocking
  a guest who never claimed a phone is a no-op that would otherwise
  confuse the operator into thinking something failed.
- `fixbet_guests` — **all** guests, including unclaimed ones. The paper-
  fallback path exists precisely so the operator can enter a bet for
  someone who never logged in; `operator_set_bet` claims them as a side
  effect.

Both are native `<input list>` + `<datalist>` over **usernames** (not
display names) — what's printed on the guest's card, which is what the
operator reads off it. No JS.

Fix-a-bet also gained a horse picker: radio "chip" labels
(`.fixbet-horse-option`) over `fixbet_horses`, scratched horses `disabled`
— same exclusion principle as results entry, not a free-text horse number
field.

`fixbet_horses` needed a context subtlety: in every view except "settled",
`state.race_number` **is** the race `operator_set_bet` will act on
(`races.current_state(now)`, fresh, in the route). But the reconstructed
"settled" view's `state` describes the just-settled **previous** race
(`_effective_state`'s gap logic) — the real current race for betting is
`next_race_number`, whose horses are already computed as `next_horses` for
the scratch list. `_panel_context` defaults `fixbet_horses = state.horses`
and overrides it to `next_horses` specifically in the `settled` branch.
Verified end-to-end by
`test_fixbet_horses_use_next_race_on_settled_view`.

## Results entry: tap-a-position, tap-a-horse

`_results_entry.html`'s `<select>` triplet is now three position-slot
buttons plus a horse tap grid (reusing `.horse-btn`), driven by
`initResultsEntry()` in `app/static/app.js` — the first script tag
`operator/base.html` has ever had. The operator panel does full-page
POST/redirect/GET navigation (not htmx swaps), so the function runs once
from `DOMContentLoaded`, no `afterSwap` rewiring needed.

Server-rendered disabled state carries real information, not just cosmetic
JS hooks: a horse already in the placings (`results_prefill`, populated
only on the settled/correction view) renders `disabled` with class
`horse-btn--used`; a scratched horse renders `disabled data-scratched`
with class `horse-btn--scratched`. The two are mutually exclusive and
distinguishable purely in server output — no JS execution needed to assert
it, which is what `test_results_entry_distinguishes_assigned_from_scratched_horse`
checks. The Publish/Correct button carries `disabled` in the initial
markup unconditionally (even on the pre-filled correction view) — JS
removes it once all three slots are filled, so a JS failure can never
submit a partial result.

`locked.html` passes `results_button_class="btn--primary
operator-primary-btn"` (Publish **is** the dominant action there);
`settled.html`'s correction block passes `results_button_class="btn--secondary"`
(a recovery path, subordinate to "Open race N+1").

## Layout pass

Every view partial now has a full-sentence `<h1 class="state__title">`
plus an optional `<p class="state__subtitle">` second line (countdown, or
the next step), one `.operator-primary-btn` (visually dominant — 64px
min-height, larger font, full-width) per view, and `.btn--secondary` on
every subordinate/recovery action (Reopen, Correct result, and the three
secondary-action submit buttons) — replacing bare `.btn`, which previously
had no background/color of its own at all.

Open view: `.bet-count` renders "N of M" large (2.5rem/800), with the
who-hasn't-bet list restyled from `<ul><li>` to `.chip-list`/`.chip` pills.
Add-guest's generated username moved out of the flash `<p>` into its own
`.added-guest-callout` block (accent-filled, 3.5rem). Confirm page wrapped
in `.confirm-page` (vertically centered), placings reformatted to the
design doc's mid-dot style (`1st #6 · 2nd #2 · 3rd #1`). Backup footer
stays in `panel.html` (renders on every view unconditionally) with an
`operator-footer--stale` class hook wired but inert until Step 6 gives
`_backup_status()` something other than `"not configured"` to return.

No new CSS custom properties — everything reuses the existing
`--color-*` tokens from `app.css`.

## Test coverage

342 tests passing total (312 from Steps 1–4b's python pass, +30 this
session): new `tests/test_operator_templates.py` (the parametrized
present/absent action-form table across all 5 views using an
`html.parser`-based extractor, destructive-string absence, auto-lock
defaults, scratch-checkbox presence across scheduled/open/settled,
settled-view scratch copy, assigned-vs-scratched markup distinguishability,
publish-button-disabled, who-hasn't-bet chips, backup-footer-on-every-view)
plus additions to `tests/test_operator_routes.py` (scratch
check/uncheck/no-op-twice/open-view-voids-bets, the two picker lists, the
`fixbet_horses` settled-view override) plus the `guest_id` → `username`
field-name rename across ~10 existing tests.

## Two test bugs found and fixed during verification (not production bugs)

Both surfaced as failures in this session's own new tests, diagnosed
before any assertion was touched, per the project's test-failure rule:

- **`test_fixbet_horses_use_next_race_on_settled_view`**: the original
  regex assumed single-line `name="horse_number" value="3"` markup with a
  literal single space between attributes. `_secondary_actions.html`'s
  radio inputs are multi-line/multi-attribute (matching the rest of this
  codebase's form markup), so the regex never matched the fixbet grid at
  all — it accidentally matched the scratch-list's single-line hidden
  `horse_number` inputs instead (same field name, same 1–6 range, same
  race), producing a false-positive first assertion and a genuine failure
  on the second (that hidden input never carries `disabled`). Verified via
  a manual debug render that the actual fixbet markup is correct (horse 3
  does carry `disabled` and `fixbet-horse-option--scratched`). Fixed by
  scoping the regex to the `fixbet-horse-grid` container and using `\s+`
  between attributes instead of a literal space — the same whitespace-
  fragility lesson the review's correction #3 already flagged for the
  html.parser-based test, applied here too.
- **`test_who_hasnt_bet_excludes_unclaimed_guests`** (pre-existing, from
  session 005) **and its new sibling
  `test_who_hasnt_bet_excludes_unclaimed_guests_renders_as_chips`**: both
  asserted an unclaimed guest's display name appears nowhere on the page.
  That stopped being true the moment this session's Fix-a-bet picker
  shipped — the picker's datalist deliberately includes unclaimed guests
  (see "Guest picker" above), so `Bob Smith` now legitimately appears
  there. The who-hasn't-bet chip list itself still correctly excludes
  unclaimed guests. Fixed by narrowing both assertions to the chip markup
  specifically (`'<span class="chip">Bob Smith</span>' not in html`)
  rather than a whole-page substring check.

## `scripts/operator_demo.py`

Seeds a throwaway db (default `./operator_demo.db`, 6 horses, 10 races, 12
guests from a synthetic name pool — not `import_guests.py`'s CSV path)
with race 1 settled, race 2 OPEN with a partial bet count (so `/operator`
lands directly on the "open" view with a non-empty who-hasn't-bet chip
list), and one horse pre-scratched in race 3. Prints the resolved db path,
`OPERATOR_PASSWORD` from the environment (or a reminder to set it), and
every generated username with claimed/unclaimed status.

Two safety checks before it ever deletes anything: refuses if `--db-path`
resolves to the configured `DATABASE_PATH`, and refuses if the target file
already exists with settled races **and** wasn't created by this script
itself (checked via a genuinely read-only `sqlite3` connection with
`mode=ro`, and a `operator_demo_seed` meta marker this script writes after
a successful seed) — the marker is what makes the script safely re-runnable
against its own default path for repeated rehearsal, rather than
permanently locking itself out after the first run. Manually verified: two
consecutive runs against the same `--db-path` both succeed; pointing
`--db-path` at the currently-configured `DATABASE_PATH` is refused.

## Verification

`venv\Scripts\Activate.ps1` then `pytest -q` — 342 passed. `app.js`
syntax-checked with `node --check`. Manually rendered the fixbet grid,
scratch checkboxes, and datalist pickers via ad hoc `TestClient` scripts to
confirm markup shape (no browser available in this environment, consistent
with 4a/4b's notes — a real mobile/laptop-browser click-through, including
the tap-grid JS interaction itself, is still owed once a browser is
available; `scripts/operator_demo.py` is what that rehearsal will use).

## Carry-forward notes for Step 5 / Step 6

- `app/static/app.js` is now shared between guest and operator pages
  (`operator/base.html` loads it). `initResultsEntry()` follows
  `tickCountdown()`'s guard convention (early-return when its markup isn't
  present) so it's inert on guest pages — keep that convention for
  anything Step 5 adds here.
- `_backup_status()` is still session 005's stub (`"not configured"`,
  always). `.operator-footer--stale`'s class hook
  (`{% if 'stale' in backup_status %}`) is wired in `panel.html` but has
  never fired — Step 6 needs to give `_backup_status()` real freshness
  text for it to do anything.
- The results-entry tap grid's JS interaction (the actual sequence of taps
  advancing through slots, freeing a horse on clearing a slot) has only
  been verified by reading the code and syntax-checking it with Node, not
  by driving it in a real browser — worth an explicit pass in the first
  real rehearsal.
