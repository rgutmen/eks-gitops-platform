// Apply the saved theme before first paint to avoid a flash of the wrong one.
(function () {
  var choice = localStorage.getItem("sftp-console-theme") || "system";
  document.documentElement.setAttribute("data-theme", choice);
})();
