// Console behaviour: theme switch, folder rows, PGP reference field, meters.
(function () {
  var KEY = "sftp-console-theme";
  var root = document.documentElement;

  function setTheme(choice) {
    root.setAttribute("data-theme", choice);
    localStorage.setItem(KEY, choice);
    document.querySelectorAll("[data-theme-choice]").forEach(function (button) {
      button.setAttribute("aria-checked", String(button.dataset.themeChoice === choice));
    });
  }

  function syncPgp(row) {
    var mode = row.querySelector("[data-pgp-mode]");
    var ref = row.querySelector(".pgp-ref");
    if (!mode || !ref) return;
    var custom = mode.value === "custom";
    ref.hidden = !custom;
    var input = ref.querySelector("input");
    input.required = custom;
    if (custom && document.activeElement === mode) input.focus();
  }

  function renumber() {
    var next = document.getElementById("next-index");
    if (!next) return;
    var highest = -1;
    document.querySelectorAll("#mappings [name$='-entry']").forEach(function (input) {
      var match = input.name.match(/^mappings-(\d+)-/);
      if (match) highest = Math.max(highest, Number(match[1]));
    });
    next.value = String(highest + 1);
    var rows = document.querySelectorAll("#mappings [data-row]");
    rows.forEach(function (row) {
      var remove = row.querySelector("[data-remove-row]");
      if (remove) remove.disabled = rows.length === 1;
    });
  }

  function init(scope) {
    scope.querySelectorAll("[data-row]").forEach(syncPgp);
    scope.querySelectorAll(".meter-fill[data-fill]").forEach(function (fill) {
      fill.style.setProperty("--fill", fill.dataset.fill + "%");
    });
    renumber();
  }

  document.addEventListener("click", function (event) {
    var theme = event.target.closest("[data-theme-choice]");
    if (theme) setTheme(theme.dataset.themeChoice);

    var remove = event.target.closest("[data-remove-row]");
    if (remove && !remove.disabled) {
      var row = remove.closest("[data-row]");
      var form = row.closest("form");
      var focusTarget = row.nextElementSibling || row.previousElementSibling;
      row.remove();
      renumber();
      if (focusTarget) focusTarget.querySelector("input")?.focus();
      if (form) form.dispatchEvent(new Event("change", { bubbles: true }));
    }
  });

  document.addEventListener("change", function (event) {
    if (event.target.matches("[data-pgp-mode]")) syncPgp(event.target.closest("[data-row]"));
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    init(event.detail.target);
    if (event.detail.target.id === "mappings") {
      var rows = event.detail.target.querySelectorAll("[data-row]");
      rows[rows.length - 1]?.querySelector("input")?.focus();
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    setTheme(localStorage.getItem(KEY) || "system");
    init(document);
  });
})();
