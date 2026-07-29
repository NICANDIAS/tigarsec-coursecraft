(function () {
  const STORAGE_KEY = "tigarsec-assess-admin-nav-expanded";

  document.addEventListener("DOMContentLoaded", function () {
    const toggle = document.getElementById("admin-nav-toggle");
    const items = document.getElementById("admin-nav-items");
    if (!toggle || !items) return;

    const hasActiveChild = items.querySelector("a.active") !== null;
    const stored = localStorage.getItem(STORAGE_KEY);
    // Auto-expand if we've landed directly on an admin sub-page (e.g. a bookmark) even if
    // it was collapsed last time - never hide the page someone's actually looking at.
    const expanded = hasActiveChild || stored === "true";

    function setExpanded(value) {
      toggle.setAttribute("aria-expanded", String(value));
      items.setAttribute("data-expanded", String(value));
      localStorage.setItem(STORAGE_KEY, String(value));
    }

    setExpanded(expanded);
    if (hasActiveChild) toggle.classList.add("has-active-child");

    toggle.addEventListener("click", function () {
      setExpanded(toggle.getAttribute("aria-expanded") !== "true");
    });
  });
})();
