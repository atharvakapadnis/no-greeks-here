function newBetId() {
  if (window.crypto && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  // Insecure contexts (plain HTTP over LAN) have no crypto.randomUUID.
  // Uniqueness per tap is all that's required — this is an idempotency
  // key, not a security token.
  return "b-" + Date.now().toString(36) + "-" +
         Math.random().toString(36).slice(2, 10);
}

let countdownTimer = null;

function tickCountdown(evt) {
  // htmx:afterSwap bubbles from any polled container on the page (the
  // leaderboard tab has its own #leaderboard-table poll) — ignore swaps
  // that aren't the bet screen, or a leaderboard poll would stop the
  // countdown by clearing the interval and finding no .countdown element.
  if (evt && evt.target && evt.target.id !== "bet-state") return;

  if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
  const el = document.querySelector(".countdown[data-deadline-seconds]");
  if (!el) return;
  let remaining = parseInt(el.dataset.deadlineSeconds, 10);
  if (Number.isNaN(remaining)) return;

  function render() {
    if (remaining <= 0) {
      el.textContent = "Betting closed";
      if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
      return;
    }
    el.textContent = "Closes in " + remaining + "s";
  }
  render();
  // Display only — the server (next poll, later SSE) remains authoritative
  // on when betting actually closes.
  countdownTimer = setInterval(function () { remaining -= 1; render(); }, 1000);
}

// document.body doesn't exist yet when this script runs from <head>, so the
// htmx:afterSwap listener must be registered inside DOMContentLoaded too —
// registering it eagerly at parse time throws and silently drops the
// listener, leaving the countdown ticking once on load and never again.
document.addEventListener("DOMContentLoaded", function () {
  tickCountdown();
  document.addEventListener("htmx:afterSwap", tickCountdown);
  initResultsEntry();
});

// --- operator: results entry tap grid --------------------------------------
//
// Tap a position slot, tap a horse; used horses grey out. The operator
// panel does full-page POST/redirect/GET navigation, not htmx swaps, so
// this only needs to run once per page load — no afterSwap rewiring like
// tickCountdown needs. Guarded inert (early return) when the markup isn't
// on the page, same convention as tickCountdown's countdown-element guard,
// so this is harmless to call unconditionally on every page including
// guest pages once app.js is shared.
function initResultsEntry() {
  var root = document.querySelector("[data-results-entry]");
  if (!root) return;

  var positions = ["first", "second", "third"];
  var slots = Array.prototype.slice.call(root.querySelectorAll("[data-position]"));
  var horseButtons = Array.prototype.slice.call(root.querySelectorAll("[data-horse-number]"));
  var publishBtn = root.querySelector(".results-entry__publish");
  var assignments = {};

  slots.forEach(function (slot) {
    var pre = slot.dataset.prefill;
    if (pre) assignments[slot.dataset.position] = parseInt(pre, 10);
  });

  function activeSlot() {
    for (var i = 0; i < positions.length; i++) {
      if (!assignments[positions[i]]) {
        return slots[i];
      }
    }
    return null;
  }

  function render() {
    var active = activeSlot();
    var usedHorses = {};
    positions.forEach(function (pos) {
      if (assignments[pos]) usedHorses[assignments[pos]] = true;
    });

    slots.forEach(function (slot) {
      var pos = slot.dataset.position;
      var value = assignments[pos];
      slot.querySelector(".results-slot__value").textContent = value ? "#" + value : "—";
      slot.classList.toggle("results-slot--filled", !!value);
      slot.classList.toggle("results-slot--active", slot === active);

      var input = root.querySelector('[data-slot-input="' + pos + '"]');
      if (input) input.value = value || "";
    });

    horseButtons.forEach(function (btn) {
      if (btn.hasAttribute("data-scratched")) return; // never re-enabled
      var number = parseInt(btn.dataset.horseNumber, 10);
      var isUsed = !!usedHorses[number];
      btn.disabled = isUsed;
      btn.classList.toggle("horse-btn--used", isUsed);
    });

    if (publishBtn) {
      var allFilled = positions.every(function (pos) { return !!assignments[pos]; });
      publishBtn.disabled = !allFilled;
    }
  }

  slots.forEach(function (slot) {
    slot.addEventListener("click", function () {
      var pos = slot.dataset.position;
      if (assignments[pos]) {
        delete assignments[pos];
        render();
      }
    });
  });

  horseButtons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      if (btn.disabled) return;
      var slot = activeSlot();
      if (!slot) return;
      assignments[slot.dataset.position] = parseInt(btn.dataset.horseNumber, 10);
      render();
    });
  });

  render();
}
