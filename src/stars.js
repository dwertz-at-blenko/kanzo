// Version: v1 · created 2026-11-15 05:45 EST · updated 2026-11-15 05:45 EST
// Changes: v1 warp stars behind the board.
// This file: /home/claude/dev/kanzo/.claude/worktrees/warp-splash/src/stars.js
// Session: 1434c01e-24f9-406f-856d-7015b5dd7b69 · vps as claude · cwd /home/claude/dev · slug unified-imagining-globe
// Resume: ssh -t blenko-claude 'cd /home/claude/dev && claude --resume 1434c01e-24f9-406f-856d-7015b5dd7b69'   (from the Mac)
// Warp stars behind the board: one canvas at the back of the page, faint (the page gives it
// 50% opacity), in the color of the theme's text so it works on light and dark looks.
// A per-browser switch: localStorage "kanzo.stars" = "off" turns it off; app.js draws the
// controls and calls window.kanzoStars.set(). Off means no canvas and no animation frame.
// Light by design: at most 200 stars, 3 stroke calls a frame, 60 fps cap, pixel ratio cap
// 1.5, paused by the browser when the tab is hidden, a still sky under prefers-reduced-motion.
// The same starfield as deploy/warp-addon/stars.js (the sign-in page add-on), slowed down.
(() => {
  const KEY = "kanzo.stars", TAIL = 0.06, SPEED = 0.16;
  // Nearest first: depth limit, line width, alpha.
  const TIERS = [[0.3, 1.6, 0.9], [0.6, 1.2, 0.7], [9, 1, 0.5]];
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const stars = [];
  let c = null, g = null, raf = 0, w = 0, h = 0, cx = 0, cy = 0, k = 0, d = 1, last = 0, color = "#fff", age = 0;

  const saved = () => { try { return localStorage.getItem(KEY) !== "off"; } catch { return true; } };
  const spawn = (s, far) => {
    s.x = Math.random() * 2 - 1; s.y = Math.random() * 2 - 1;
    s.z = far ? 1 : 0.1 + Math.random() * 0.9;
    return s;
  };

  function size() {
    d = Math.min(window.devicePixelRatio || 1, 1.5);
    w = c.width = Math.round(innerWidth * d); h = c.height = Math.round(innerHeight * d);
    cx = w / 2; cy = h / 2; k = Math.max(w, h) * 0.6;
    const n = Math.min(200, Math.max(90, Math.round(innerWidth * innerHeight / 8000)));
    while (stars.length < n) stars.push(spawn({}, false));
    stars.length = n;
    age = 0; frame(0);
  }

  function frame(dt) {
    const v = still ? 0 : SPEED;
    // The theme can change under us; look at the text color about once a second.
    if (age-- <= 0) { color = getComputedStyle(document.body).color; age = 60; }
    g.clearRect(0, 0, w, h);
    g.lineCap = "round"; g.strokeStyle = color;
    for (const s of stars) {
      s.z -= v * dt;
      const x = cx + s.x / s.z * k, y = cy + s.y / s.z * k;
      if (s.z < 0.02 || x < -50 || x > w + 50 || y < -50 || y > h + 50) spawn(s, true);
    }
    let lo = 0;
    for (const [hi, width, alpha] of TIERS) {
      g.beginPath();
      for (const s of stars) {
        if (s.z < lo || s.z >= hi) continue;
        const t = s.z + v * TAIL + 0.0015;
        g.moveTo(cx + s.x / t * k, cy + s.y / t * k); g.lineTo(cx + s.x / s.z * k, cy + s.y / s.z * k);
      }
      g.lineWidth = width * d; g.globalAlpha = alpha; g.stroke();
      lo = hi;
    }
  }

  function tick(now) {
    raf = requestAnimationFrame(tick);
    const dt = (now - last) / 1000;
    if (dt < 0.012) return;               // 60 fps cap on faster displays
    last = now; frame(Math.min(dt, 0.05));
  }

  function start() {
    if (c) return;
    c = document.createElement("canvas");
    c.id = "stars"; c.setAttribute("aria-hidden", "true");
    document.body.prepend(c);
    g = c.getContext("2d");
    addEventListener("resize", size); size();
    if (!still) raf = requestAnimationFrame((now) => { last = now; raf = requestAnimationFrame(tick); });
  }

  function stop() {
    if (!c) return;
    cancelAnimationFrame(raf); removeEventListener("resize", size);
    c.remove(); c = g = null;
  }

  window.kanzoStars = {
    get on() { return !!c; },
    set(on) {
      try { localStorage.setItem(KEY, on ? "on" : "off"); } catch { /* private window: this visit only */ }
      if (on) start(); else stop();
    },
  };
  if (saved()) start();
})();
