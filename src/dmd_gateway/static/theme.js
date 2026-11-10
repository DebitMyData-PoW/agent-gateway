// Light or dark. Runs before the page is drawn: the visitor's choice, else light. The switch in
// the top bar changes it; the choice is kept in this site's own "dmd-theme-id" cookie (DebitMyData's
// other sites, the explorer, the pool and the website, each keep their own).
(function () {
  "use strict";
  var root = document.documentElement;
  var theme = null;
  try {
    var saved = document.cookie.match(/(?:^|;\s*)dmd-theme-id=(light|dark)/);
    theme = saved ? saved[1] : localStorage.getItem("dmd-theme-id");
  } catch (e) {}
  if (theme !== "light" && theme !== "dark") theme = "light";
  root.setAttribute("data-theme", theme);

  function isDark() {
    return root.getAttribute("data-theme") === "dark";
  }
  function label() {
    var button = document.getElementById("theme-toggle");
    if (!button) return;
    button.setAttribute("aria-label", isDark() ? "Switch to the light theme" : "Switch to the dark theme");
    button.title = isDark() ? "Light theme" : "Dark theme";
  }
  function save(value) {
    try {
      localStorage.setItem("dmd-theme-id", value);
    } catch (e) {}
    var secure = location.protocol === "https:" ? "; secure" : "";
    document.cookie = "dmd-theme-id=" + value + "; path=/; max-age=31536000; samesite=lax" + secure;
  }
  document.addEventListener("DOMContentLoaded", label);
  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target || !target.closest || !target.closest("#theme-toggle")) return;
    var value = isDark() ? "light" : "dark";
    root.setAttribute("data-theme", value);
    save(value);
    label();
  });
})();
