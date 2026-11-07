// Theme switch, loaded blocking in <head> so the stored choice applies before first paint.
// The server sets data-theme on <html> from config.toml; /?theme=lcars|kanzo overrides and
// is remembered; otherwise the choice saved by the footer toggle (localStorage kanzo.theme) wins.
(function () {
  var THEMES = ["kanzo", "lcars"];
  var pick = new URLSearchParams(location.search).get("theme");
  try {
    if (THEMES.indexOf(pick) >= 0) localStorage.setItem("kanzo.theme", pick);
    else pick = localStorage.getItem("kanzo.theme");
  } catch (e) { /* storage blocked: keep the server's default */ }
  if (THEMES.indexOf(pick) >= 0) document.documentElement.dataset.theme = pick;
})();
