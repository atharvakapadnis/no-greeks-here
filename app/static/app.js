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
});
