(function () {
  const POLL_INTERVAL_MS = 5000;
  const STORAGE_KEY = "tigarsec-assess-last-in-progress-count";

  function showToast(message) {
    const container = document.getElementById("toast-container");
    if (!container) return;
    const toast = document.createElement("div");
    toast.textContent = message;
    toast.style.cssText =
      "background: var(--accent); color: var(--accent-text); padding: 12px 18px; " +
      "border-radius: 8px; box-shadow: var(--shadow); font-size: 0.9rem; max-width: 320px;";
    container.appendChild(toast);
    setTimeout(function () {
      toast.style.transition = "opacity 0.4s";
      toast.style.opacity = "0";
      setTimeout(function () { toast.remove(); }, 400);
    }, 6000);
  }

  function updateBadge(count) {
    const badge = document.getElementById("tasks-badge");
    if (!badge) return;
    if (count > 0) {
      badge.textContent = count;
      badge.style.display = "inline-block";
    } else {
      badge.style.display = "none";
    }
  }

  async function poll() {
    try {
      const res = await fetch("/tasks/summary");
      if (!res.ok) return;  // not logged in, or some other non-fatal issue - just skip this tick
      const data = await res.json();
      const current = data.in_progress || 0;

      updateBadge(current);

      const previous = parseInt(localStorage.getItem(STORAGE_KEY) || "0", 10);
      if (previous > 0 && current < previous) {
        showToast(
          current === 0
            ? "A task just finished - check Tasks for details."
            : `A task just finished (${previous - current} completed) - check Tasks.`
        );
      }
      localStorage.setItem(STORAGE_KEY, String(current));
    } catch (e) {
      // network hiccup - just try again next interval
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    poll();
    setInterval(poll, POLL_INTERVAL_MS);
  });
})();
