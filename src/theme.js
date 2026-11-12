// Previews a look for one visit: /?theme=kanzo|lcars|skeletal. Loaded blocking in <head>
// so it applies before first paint. The page already carries the board's saved theme;
// the switcher at the bottom of the sidebar, in app.js, is what saves a pick.
(function () {
  var pick = new URLSearchParams(location.search).get("theme");
  if (pick && ["kanzo", "lcars", "skeletal"].indexOf(pick) >= 0) {
    document.documentElement.dataset.theme = pick;
    document.documentElement.dataset.themePreview = "";
  }
})();
