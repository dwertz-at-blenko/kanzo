// Version: v1 · created 2026-11-14 17:15 EST · updated 2026-11-14 17:15 EST
// Changes: v1 v1 initial.
// This file: /home/claude/dev/kanzo/.claude/worktrees/warp-splash/deploy/warp-addon/stars.js
// Session: 1434c01e-24f9-406f-856d-7015b5dd7b69 · vps as claude · cwd /home/claude/dev
// Resume: ssh -t blenko-claude 'cd /home/claude/dev && claude --resume 1434c01e-24f9-406f-856d-7015b5dd7b69'   (from the Mac)
// Kanzo warp login: draws moving warp stars on one canvas placed behind the Cloudflare login page.
// Cosmetic only. It adds a canvas and draws on it; it reads no field, sends nothing, changes no form.
// Light by design: at most 260 stars, 3 stroke calls a frame, 60 fps cap, pixel ratio cap 1.5,
// paused by the browser when the tab is hidden, a still sky under prefers-reduced-motion.
// Same starfield as deploy/warp/index.html.
(() => {
  const TAIL = 0.14, SPEED = 0.55;
  const TIERS = [[0.3, 2.4, 'rgba(255,255,255,1)'], [0.6, 1.7, 'rgba(205,222,255,.9)'], [9, 1.2, 'rgba(175,195,255,.65)']];
  const c = document.createElement('canvas');
  c.id = 'kanzo-warp-sky'; c.setAttribute('aria-hidden', 'true');
  document.documentElement.appendChild(c);
  const g = c.getContext('2d', { alpha: false });
  const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const stars = [];
  let w = 0, h = 0, cx = 0, cy = 0, k = 0, d = 1, last = 0;

  const spawn = (s, far) => {
    s.x = Math.random() * 2 - 1; s.y = Math.random() * 2 - 1;
    s.z = far ? 1 : 0.1 + Math.random() * 0.9;
    return s;
  };

  function size() {
    d = Math.min(window.devicePixelRatio || 1, 1.5);
    w = c.width = Math.round(innerWidth * d); h = c.height = Math.round(innerHeight * d);
    cx = w / 2; cy = h / 2; k = Math.max(w, h) * 0.6;
    const n = Math.min(260, Math.max(120, Math.round(innerWidth * innerHeight / 6000)));
    while (stars.length < n) stars.push(spawn({}, false));
    stars.length = n;
    frame(0);
  }

  function frame(dt) {
    const v = still ? 0 : SPEED;
    g.fillStyle = '#02030a'; g.fillRect(0, 0, w, h);
    g.lineCap = 'round';
    for (const s of stars) {
      s.z -= v * dt;
      const x = cx + s.x / s.z * k, y = cy + s.y / s.z * k;
      if (s.z < 0.02 || x < -50 || x > w + 50 || y < -50 || y > h + 50) spawn(s, true);
    }
    let lo = 0;
    for (const [hi, width, color] of TIERS) {
      g.beginPath();
      for (const s of stars) {
        if (s.z < lo || s.z >= hi) continue;
        const t = s.z + v * TAIL + 0.0015;
        g.moveTo(cx + s.x / t * k, cy + s.y / t * k); g.lineTo(cx + s.x / s.z * k, cy + s.y / s.z * k);
      }
      g.lineWidth = width * d; g.strokeStyle = color; g.stroke();
      lo = hi;
    }
  }

  function tick(now) {
    requestAnimationFrame(tick);
    const dt = (now - last) / 1000;
    if (dt < 0.012) return;               // 60 fps cap on faster displays
    last = now; frame(Math.min(dt, 0.05));
  }

  addEventListener('resize', size); size();
  if (!still) requestAnimationFrame(now => { last = now; requestAnimationFrame(tick); });
})();
