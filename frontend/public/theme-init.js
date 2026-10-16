// Sets the colour theme before the first paint, so a dark-theme user never sees a flash of the
// light one. Loaded as a blocking same-origin script from index.html (the production CSP
// allows `script-src 'self'` only, so it cannot be inline). It must agree with
// src/app/theme.ts, which owns the preference after start-up.
(function () {
  var preference = "system";
  try {
    var stored = window.localStorage.getItem("sv-theme");
    if (stored === "light" || stored === "dark") preference = stored;
  } catch (error) {
    // Storage can be blocked; the system preference applies.
  }
  var dark =
    preference === "dark" ||
    (preference === "system" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
})();
