// Kanzo's service worker: a notification when an agent answers, and the card on a tap.
// It caches nothing: a stale board is worse than none. The push carries no payload, on purpose,
// so nothing about a card leaves the server; this worker asks the board what is new.

const SHOWN = "kanzo-push";        // Cache API store for what was shown already
const SHOWN_KEY = "/__kanzo-shown";
const KEEP = 50;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

async function shownList() {
  try {
    const res = await (await caches.open(SHOWN)).match(SHOWN_KEY);
    const list = res ? await res.json() : [];
    return Array.isArray(list) ? list : [];
  } catch { return []; }
}

async function remember(list) {
  try {
    await (await caches.open(SHOWN)).put(SHOWN_KEY, new Response(JSON.stringify(list.slice(-KEEP))));
  } catch {}
}

// The card behind the newest answer not shown yet; null when the board cannot be read.
async function newest() {
  const res = await fetch("/api/inbox", { credentials: "include", redirect: "manual", cache: "no-store" });
  if (!res.ok) return null;                 // a login redirect is opaque, not ok
  const waiting = ((await res.json()).waiting || []).filter((t) => t.last);
  if (!waiting.length) return null;
  const shown = await shownList();
  const mark = (t) => `${t.id}@${t.last.at}`;
  const fresh = waiting.find((t) => !shown.includes(mark(t)));
  if (fresh) await remember([...shown, mark(fresh)]);
  // Nothing new (another device saw it first): the newest again, quietly, replacing itself.
  return { task: fresh || waiting[0], quiet: !fresh };
}

async function notify() {
  let found = null;
  try { found = await newest(); } catch {}
  if (!found) {
    // Chrome wants every push to end in a notification.
    return self.registration.showNotification("Kanzo: an agent answered", {
      body: "Open the inbox to read it.", tag: "kanzo-inbox", icon: "/icon-192.png", data: { url: "/#@inbox" },
    });
  }
  const t = found.task;
  const key = `${t.project}#${t.id}`;
  const excerpt = t.last.excerpt ? `: ${t.last.excerpt}` : " answered";
  return self.registration.showNotification(`${key} ${t.title}`, {
    body: `${t.last.author}${excerpt}`, tag: key, renotify: !found.quiet, silent: found.quiet,
    icon: "/icon-192.png", data: { url: `/#${encodeURIComponent(t.project)}/${t.id}` },
  });
}

self.addEventListener("push", (e) => e.waitUntil(notify()));

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || "/", self.location.origin).href;
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const win = wins.find((w) => new URL(w.url).origin === self.location.origin);
    if (!win) return self.clients.openWindow(url);
    await win.focus();
    try { await win.navigate(url); } catch { win.postMessage({ kanzo: "open", url }); }
  })());
});
