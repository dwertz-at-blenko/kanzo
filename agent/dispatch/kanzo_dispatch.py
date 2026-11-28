#!/usr/bin/env python3
# Version: v6 · created 2026-11-21 13:26 EST · updated 2026-11-28
# Changes: v1 watch the board for Dave's @claude comments and run one background Claude session per card.
#          v2 2026-11-23 any comment by Dave starts or reaches the session; "[note]" comments are skipped.
#          v3 2026-11-23 a comment goes to the live session already working the card (background or a
#             herdr pane); a new agent starts only when nobody is; `@claude new` forces one.
#          v4 2026-11-24 status.json for the board (live sessions, card owners, a receipt per comment,
#             heartbeat); `@claude start`; one nudge, then a flag, when the agent stays silent; burst
#             guard; resume an ended session before a cold start; stop idle sessions on done cards;
#             follow-ups say when a comment has pictures.
#          v5 2026-11-24 Web Push: one empty signed push per browser when an agent answers (class Push),
#             the key taken out of the environment at start, `push_key` and `push` in status.json, --no-push.
#          v6 2026-11-28 asks: a background session blocked on Dave gets one comment per episode on its card
#             (found or made), the human flag, and routing, so his answer on the card resumes or reaches it;
#             `asks` in state.json and status.json, blocked sessions listed even with no process,
#             --ask-after, --ask-project, --no-asks, Board.clear_inbox.
# This file: /home/claude/dev/kanzo/agent/dispatch/kanzo_dispatch.py (repo dwertz-at-blenko/kanzo, branch public-url)
# Session: b1475961-06d7-40b0-9eff-f2b47896b6bb · vps as claude · cwd /home/claude/dev
"""kanzo-dispatch: delegate a card to Claude by commenting on it.

Your comment goes to whoever is working the card; if nobody is, a new agent starts. A new
board comment by Dave (author `me`) on a card with no session first looks for a live Claude
session that already wrote to the card through the kanzo MCP tools (its transcript says so) and
hands the comment to it. Only when there is none does it start one background Claude Code
session for the card, in the card's repo folder. While the card has a session, every later
comment by Dave on it goes to the same session. `@claude stop` stops a session the dispatcher
started (and only detaches one it did not); `@claude new` starts a separate one. The dispatcher
comments on the card as `kanzo-dispatch` through the board's MCP endpoint, so its own comments
never look like Dave's and never trigger it.

Reads: the board's loopback HTTP API, live sessions' transcripts under ~/.claude/projects and
job files under ~/.claude/jobs, and the database file, read-only: once, on the very first run,
the highest comment id, so old comments are never replayed, and each pass which cards are done.
Runs: `claude --bg --name <name> <brief>`, `claude --bg --resume <session id> <text>`,
`claude agents --json --all`, `claude stop <id>`, `claude attach <id>` inside a private tmux to
type a follow-up into an idle background session, and `herdr agent list` /
`herdr agent prompt <pane> <text>` for a session in a terminal pane.
Writes: state.json and, every pass, status.json beside it for the board.
Sends: with KANZO_VAPID_PRIVATE_KEY set, an empty signed Web Push to each browser stored on the
board when a card newly waits on Dave (class Push); subscriptions read read-only from the database.
Asks: a background session that stops and waits on Dave (its job file says `blocked`, with a
`needs` line) gets one comment on its card, the human flag and the card's routing, so his answer
there reaches it, resuming it when its process is gone (Dispatcher.asks_pass).

    kanzo_dispatch.py --once                 # one pass
    kanzo_dispatch.py --loop --interval 5    # the service
    kanzo_dispatch.py --once --dry-run       # say what it would do, change nothing
"""
import argparse
import base64
import datetime
import glob
import hashlib
import http.client
import json
import logging
import os
import re
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

AUTHOR = "kanzo-dispatch"
DAVE = "me"
BOARD_ACTOR = "board"
TRIGGER = re.compile(r"^\s*@claude\b", re.I)
STOP = re.compile(r"^\s*@claude\s+stop\b", re.I)
NEW = re.compile(r"^\s*@claude\s+new\b", re.I)    # skip the owner search: a separate agent
START = re.compile(r"^\s*@claude\s+start\b", re.I)  # start an agent, the card is the brief
NOTE = re.compile(r"^\s*\[note\]", re.I)          # the board's "do not send to agent" box
BG_ID = re.compile(r"backgrounded\W+([0-9a-f]{8})")
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSW|CREDENTIAL|API_?KEY|PRIVATE_?KEY|VAPID|BEARER|COOKIE|BWS_", re.I)
LONG_OPAQUE = re.compile(r"[A-Za-z0-9_\-+/=]{32,}")
PROMPT_LINE = re.compile(r"^\s*❯\s*$", re.M)
CARD_REF = re.compile(r"[a-z0-9][a-z0-9._-]*#(\d+)")
# Kanzo writes that make a session the card's owner: the card is in the input or in the result.
CARD_IN_INPUT = {"mcp__kanzo__update_task", "mcp__kanzo__add_comment"}
CARD_IN_RESULT = {"mcp__kanzo__create_task", "mcp__kanzo__next_task"}

START_TIMEOUT = 60      # seconds for `claude --bg` to return
LINK_WAIT = 20          # seconds to wait for the app link after a start
START_GRACE = 60        # the watchdog leaves a fresh session alone this long
DELIVER_TIMEOUT = 1800  # a follow-up waits this long for a busy session, then Dave is told
ATTACH_READY = 25
ATTACH_VERIFY = 25
COMMENT_CLIP = 4000
BRIEF_CLIP = 100_000    # one argv string must stay under the kernel's 128 KiB
SESSIONS_EVERY = 15     # seconds between `claude agents` reads made only for status.json (one takes ~3 s)
SCAN_BUDGET = 64 << 20  # transcript bytes read per pass for status.json (~0.7 s); a restart catches up
NUDGE_AFTER = 20        # a turn that ends sooner than this after a delivery is not judged yet
IDLE_STOP = 3600        # an idle dispatcher session on a done card is stopped after this long
KEEP_RECEIPTS = 20      # comment receipts kept per card
KEEP_FOR = 7 * 86400    # a card's receipts are dropped after this long untouched
# Web Push: an empty, signed POST to each browser that asked for one (agent/dispatch/README.md, "Push").
PUSH_KEY_ENV = "KANZO_VAPID_PRIVATE_KEY"     # base64 of a SEC1 DER P-256 private key; taken out of os.environ at start
PUSH_SUBJECT = "https://kanzo.blenko.cloud"  # the VAPID `sub`: a URL, never an email address
PUSH_EVERY = 30         # seconds: at most one round of pushes this often
PUSH_TTL = 3600         # seconds the push service keeps a push for an offline phone; also how long one is owed
PUSH_TIMEOUT = 10
PUSH_JWT_FOR = 12 * 3600
PUSH_BACKOFF = (60, 3600)   # first and longest wait after a 429, a 5xx or no answer
PUSH_KEEP_DEAD = 50
PUSH_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com")
PUSH_HOST_SUFFIXES = (".notify.windows.com", ".push.apple.com")
# Asks: a background session blocked on Dave (agent/dispatch/README.md, "Sessions that wait on Dave").
ASK_AFTER = 900         # seconds blocked before the ask goes on the card: a session Dave is talking to stays quiet
ASK_PROJECT = "vps-ops"  # where a card is made when no project's repo matches the session's folder
ASKS_PER_PASS = 3       # new asks tried per pass
ASK_RETRY = 60          # seconds before a skipped or failed ask is tried again
ASK_NEEDS_CLIP = 700
ASK_EMPTY = "It stopped and asks for your input."
NUDGE = ("Card {key}: Dave has no answer on the card yet and he reads only the card. Post your answer or "
         "your question as a card comment now with the kanzo tools, and set the card status.")

log = logging.getLogger("kanzo-dispatch")


def now():
    return time.time()


def clip(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[: n - 3] + "..."


def scrub(text, n=160):
    """One short line of an error, with anything token-shaped taken out."""
    line = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "no output")
    return clip(LONG_OPAQUE.sub("[redacted]", line), n)


def child_env():
    """The environment for claude and tmux: everything but secret-named variables."""
    return {k: v for k, v in os.environ.items() if not SECRET_NAME.search(k)}


def take_push_key():
    """The VAPID key, removed from the environment at once: no child process can inherit it."""
    return os.environ.pop(PUSH_KEY_ENV, "")


# ---------------------------------------------------------------- push

def push_endpoint_ok(url):
    """https on a known push service, no user info, no port: the board's own check, again."""
    if not isinstance(url, str) or len(url) > 1024 or not url.startswith("https://"):
        return False
    if not all("!" <= ch <= "~" for ch in url):
        return False
    host = re.split(r"[/?#]", url[len("https://"):], maxsplit=1)[0].lower()
    if not host or any(ch in host for ch in "@:\\"):
        return False
    if host in PUSH_HOSTS:
        return True
    for suffix in PUSH_HOST_SUFFIXES:
        sub = host[:-len(suffix)] if host.endswith(suffix) else ""
        if sub and not sub.startswith(".") and re.fullmatch(r"[a-z0-9.-]+", sub):
            return True
    return False


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def digest(endpoint):
    """What state.json keeps of a dead endpoint: never the endpoint itself."""
    return hashlib.sha256(endpoint.encode()).hexdigest()[:32]


def push_post(url, headers, timeout, connect=None):
    """One POST with no body; (status, Retry-After). `connect` is the tests' way to a fake service."""
    u = urllib.parse.urlsplit(url)
    if connect is None:
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout,
                                           context=ssl.create_default_context())
    else:
        conn = connect(u.hostname, u.port, timeout)
    try:
        conn.request("POST", (u.path or "/") + (f"?{u.query}" if u.query else ""), body=b"", headers=headers)
        r = conn.getresponse()
        r.read(65536)
        return r.status, r.getheader("Retry-After")
    finally:
        conn.close()


class Push:
    """Web Push without a payload (RFC 8030, VAPID RFC 8292) to the browsers stored on the board.

    The board keeps the subscriptions in its `meta` table; this reads that row read-only from the
    database file, so no HTTP route ever hands out an endpoint. Each pass it compares the inbox's
    `waiting` cards with the last pass: a card new there, or whose last comment moved, is news,
    and every live browser is owed one push. A round sends at most one push per browser, at most
    every PUSH_EVERY seconds, in a thread so a slow push service never holds up a pass."""

    def __init__(self, raw_key, db_path, post=push_post, allowed=push_endpoint_ok, every=PUSH_EVERY,
                 threaded=True, off=None):
        self.db_path, self.post, self.allowed, self.every, self.threaded = db_path, post, allowed, every, threaded
        self.on, self.key, self.public = False, None, ""
        self.seen = None                # {task id: last.at} of the last pass; None until the first
        self.owed = {}                  # browser hash -> since when it is owed a push
        self.backoff = {}               # browser hash -> (not before, last delay)
        self.dead = []                  # hashes of endpoints that answered 404 or 410
        self.tokens = {}                # origin -> (JWT, exp)
        self.count, self.last_round, self.last_sent, self.last_error = 0, 0, 0, ""
        self.lock = threading.Lock()
        self.thread = None
        if off:
            log.info("push off: %s", off)
            return
        if not raw_key:
            log.info("push off: %s is not set", PUSH_KEY_ENV)
            return
        try:                            # the one import outside the standard library, and only for push
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        except ImportError:
            log.info("push off: the python3 package cryptography is not installed")
            return
        try:
            key = serialization.load_der_private_key(base64.b64decode("".join(raw_key.split()), validate=True), None)
            if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
                raise ValueError
        except Exception:               # never the value, nor a parser message about it
            log.info("push off: %s is not a base64 DER P-256 private key", PUSH_KEY_ENV)
            return
        self.key, self.ec, self.hashes, self.decode = key, ec, hashes, decode_dss_signature
        self.public = b64url(key.public_key().public_bytes(serialization.Encoding.X962,
                                                           serialization.PublicFormat.UncompressedPoint))
        self.on = True
        log.info("push on")

    def subscriptions(self):
        """[(hash, endpoint)] of the stored browsers that pass the host check and are not dead."""
        try:
            con = sqlite3.connect(f"file:{urllib.parse.quote(self.db_path)}?mode=ro", uri=True, timeout=5)
            try:
                row = con.execute("SELECT value FROM meta WHERE key = 'push_subscriptions'").fetchone()
            finally:
                con.close()
            subs = json.loads(row[0]) if row else []
        except (sqlite3.Error, ValueError, TypeError):
            self.last_error = "cannot read the subscriptions from the board's database"
            return []
        out = []
        for sub in subs if isinstance(subs, list) else []:
            endpoint = sub.get("endpoint") if isinstance(sub, dict) else None
            if self.allowed(endpoint) and digest(endpoint) not in self.dead:
                out.append((digest(endpoint), endpoint))
        return out

    def token(self, origin, t):
        """The VAPID JWT for one push service, reused until an hour before it expires."""
        cached = self.tokens.get(origin)
        if cached and cached[1] - 3600 > t:
            return cached[0]
        exp = int(t) + PUSH_JWT_FOR
        part = lambda doc: b64url(json.dumps(doc, separators=(",", ":")).encode())  # noqa: E731
        signing = f"{part({'typ': 'JWT', 'alg': 'ES256'})}.{part({'aud': origin, 'exp': exp, 'sub': PUSH_SUBJECT})}"
        r, s = self.decode(self.key.sign(signing.encode(), self.ec.ECDSA(self.hashes.SHA256())))
        jwt = f"{signing}.{b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"   # raw r||s, not DER
        self.tokens[origin] = (jwt, exp)
        return jwt

    def check(self, inbox, dry=False):
        """One pass: note new answers in `inbox` (GET /api/inbox) and start a round when one is due."""
        t = now()
        waiting = {str(i["id"]): str((i.get("last") or {}).get("at", ""))
                   for i in (inbox or {}).get("waiting", []) if isinstance(i, dict) and "id" in i}
        news = self.seen is not None and any(self.seen.get(k) != at for k, at in waiting.items())
        self.seen = waiting                          # the first pass after a start only records
        if not self.on:
            return
        subs = self.subscriptions()
        with self.lock:
            self.count = len(subs)
            live = {h for h, _ in subs}
            if news:
                for h in live:
                    self.owed.setdefault(h, t)
            self.owed = {h: since for h, since in self.owed.items() if h in live and t - since < PUSH_TTL}
            due = [(h, e) for h, e in subs if h in self.owed and self.backoff.get(h, (0, 0))[0] <= t]
            busy = self.thread is not None and self.thread.is_alive()
        if busy or not due or t - self.last_round < self.every:
            return
        self.last_round = t
        if dry:
            log.info("dry run: would push to %d browser(s)", len(due))
            with self.lock:
                for h, _ in due:
                    self.owed.pop(h, None)
            return
        if self.threaded:
            self.thread = threading.Thread(target=self.send, args=(due,), name="kanzo-push", daemon=True)
            self.thread.start()
        else:
            self.send(due)

    def send(self, due):
        for h, endpoint in due:
            u = urllib.parse.urlsplit(endpoint)
            host, status, retry, err = u.hostname or "?", None, None, ""
            try:
                jwt = self.token(f"{u.scheme}://{u.netloc}", now())
                status, retry = self.post(endpoint, {
                    "Authorization": f"vapid t={jwt}, k={self.public}", "TTL": str(PUSH_TTL),
                    "Urgency": "normal", "Topic": "kanzo-inbox", "Content-Length": "0"}, PUSH_TIMEOUT)
            except Exception as e:      # a timeout, TLS, DNS: never the pass's problem
                err = type(e).__name__
            with self.lock:
                self.result(h, host, status, retry, err)

    def result(self, h, host, status, retry, err):
        t = now()
        if status is not None and 200 <= status < 300:
            self.owed.pop(h, None)
            self.backoff.pop(h, None)
            self.last_sent, self.last_error = t, ""
        elif status in (404, 410):
            self.owed.pop(h, None)
            self.dead = (self.dead + [h])[-PUSH_KEEP_DEAD:]
            log.info("push: a browser on %s is gone (HTTP %s), dropped", host, status)
        elif status is None or status == 429 or status >= 500:
            delay = min(max(self.backoff.get(h, (0, 0))[1] * 2, PUSH_BACKOFF[0]), PUSH_BACKOFF[1])
            if str(retry or "").isdigit():
                delay = min(max(delay, int(retry)), PUSH_BACKOFF[1])
            self.backoff[h] = (t + delay, delay)
            self.last_error = f"{host}: {err or f'HTTP {status}'}"
            log.warning("push to %s: %s, next try in %d s", host, err or f"HTTP {status}", delay)
        else:
            self.owed.pop(h, None)
            self.last_error = f"{host}: HTTP {status}"
            log.warning("push to %s: HTTP %s, not retried", host, status)

    def join(self, timeout=30):
        if self.thread is not None:
            self.thread.join(timeout)

    def status(self):
        with self.lock:
            return {"on": self.on, "subscriptions": self.count if self.on else 0,
                    "last_sent": iso(self.last_sent) if self.last_sent else "", "last_error": self.last_error}


# ---------------------------------------------------------------- board

class BoardError(Exception):
    pass


class Board:
    """The board's loopback HTTP API for reads; its MCP endpoint for writes, signed as an agent."""

    def __init__(self, url, timeout=15, client=AUTHOR):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.client = client
        self.session = None
        self.next_id = 1

    def get(self, path):
        try:
            with urllib.request.urlopen(self.url + path, timeout=self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise BoardError(f"GET {path}: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise BoardError(f"GET {path}: {type(e).__name__}") from None

    def changes(self):
        return self.get("/api/changes").get("changes")

    def latest_activity(self):
        rows = self.get("/api/activity")
        return rows[0]["id"] if rows else 0

    def activity_since(self, after):
        """Activity rows with id > after, oldest first. The API pages newest first, 100 a page."""
        rows, before = [], None
        while True:
            page = self.get("/api/activity" + (f"?before={before}" if before else ""))
            fresh = [r for r in page if r["id"] > after]
            rows.extend(fresh)
            if len(fresh) < len(page) or not page:
                break
            before = page[-1]["id"]
        return sorted(rows, key=lambda r: r["id"])

    def task(self, task_id):
        """{"task": {...}, "comments": [...]}, or None when the card is gone."""
        try:
            return self.get(f"/api/tasks/{int(task_id)}")
        except BoardError as e:
            if "HTTP 404" in str(e):
                return None
            raise

    def project(self, slug):
        data = self.get("/api/board?project=" + urllib.parse.quote(slug))
        return next((p for p in data.get("projects", []) if p.get("slug") == slug), None)

    def send(self, method, path, body):
        """A JSON write over the HTTP API (signed `me`, so never for comments)."""
        req = urllib.request.Request(self.url + path, method=method, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as e:
            raise BoardError(f"{method} {path}: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise BoardError(f"{method} {path}: {type(e).__name__}") from None

    def projects(self):
        """Every project with its linked repos. Only read when an ask needs a new card, which is rare."""
        return self.get("/api/board?project=all").get("projects") or []

    def create_task(self, project, title, description, **fields):
        """-> the new card's id. `fields` (status, human, tags) go in a PATCH right after."""
        task = self.send("POST", "/api/tasks", {"project": project, "title": title, "description": description})
        tid = int(task["id"])
        if fields:
            self.send("PATCH", f"/api/tasks/{tid}", fields)
        return tid

    def clear_inbox(self, task_ids):
        """Take these cards out of the Inbox (`POST /api/inbox/clear`). Best effort: an older board
        has no such route, so a 404 or any other error is only logged at debug level."""
        try:
            self.send("POST", "/api/inbox/clear", {"tasks": [int(t) for t in task_ids]})
            return True
        except BoardError as e:
            log.debug("inbox clear for %s: %s", task_ids, e)
            return False

    # MCP over streamable HTTP: initialize once, then tools/call. Replies come as SSE.
    def _post(self, payload):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url + "/mcp", data=json.dumps(payload).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                sid = r.headers.get("mcp-session-id")
                if sid:
                    self.session = sid
                if "id" not in payload:
                    return None
                if "json" in (r.headers.get("content-type") or ""):
                    return json.load(r)
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("data:") and line[5:].strip():
                        msg = json.loads(line[5:])
                        if msg.get("id") == payload["id"]:
                            return msg
                raise BoardError("MCP reply ended without an answer")
        except urllib.error.HTTPError as e:
            raise BoardError(f"MCP HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise BoardError(f"MCP {type(e).__name__}") from None

    def _rpc(self, method, params):
        self.next_id += 1
        return self._post({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})

    def _connect(self):
        self.session = None
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": self.client, "version": "1"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def tool(self, name, args):
        for attempt in (1, 2):
            if not self.session:
                self._connect()
            try:
                msg = self._rpc("tools/call", {"name": name, "arguments": args})
                break
            except BoardError:
                self.session = None       # the board restarted: one fresh session, then give up
                if attempt == 2:
                    raise
        if msg.get("error") or (msg.get("result") or {}).get("isError"):
            raise BoardError(f"{name} failed: {clip(json.dumps(msg.get('error') or msg.get('result')), 200)}")
        return msg["result"]

    def comment(self, task_id, body):
        self.tool("add_comment", {"id": int(task_id), "body": body, "author": self.client})

    def flag_human(self, task_id, on=True):
        self.tool("update_task", {"id": int(task_id), "human": bool(on)})


def highest_comment_id(db_path):
    """Read-only: the one value the API cannot give. Used once, on the first run."""
    con = sqlite3.connect(f"file:{urllib.parse.quote(db_path)}?mode=ro", uri=True, timeout=5)
    try:
        return con.execute("SELECT coalesce(max(id), 0) FROM comments").fetchone()[0]
    finally:
        con.close()


# ---------------------------------------------------------------- claude

def alive(agent):
    return bool(agent and agent.get("pid") and agent.get("status"))


def working(agent):
    return alive(agent) and (agent.get("status") in ("busy", "working") or agent.get("state") == "working")


def blocked(agent):
    return alive(agent) and agent.get("state") == "blocked"


def deliverable(agent):
    """An idle background session with its input box free (not on a permission or question)."""
    return (alive(agent) and agent.get("kind") == "background" and agent.get("status") == "idle"
            and agent.get("state") != "blocked")


def live(agent):
    """Listed as running and its process exists (an exited background item has no status)."""
    if not alive(agent):
        return False
    try:
        os.kill(int(agent["pid"]), 0)
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False
    return True


def asking(agent, job):
    """A background session that stopped and waits on Dave, whether or not its process runs. The
    job file decides when it has a state: it is read every pass, while the session list may be up
    to SESSIONS_EVERY old, and a stale `blocked` there next to a fresh updatedAt would look like a
    new episode."""
    if not agent or agent.get("kind") != "background":
        return False
    js = (job or {}).get("state")
    return js == "blocked" if js else agent.get("state") == "blocked"


def session_state(agent, job):
    """working, idle or blocked for status.json. A background session's job file is read every
    pass, so it is fresher than the session list, which is read at most every SESSIONS_EVERY."""
    js = job.get("state")
    if agent.get("kind") == "background" and js in ("working", "blocked"):
        return js
    if agent.get("kind") == "background" and js in ("done", "idle"):
        return "idle"
    if blocked(agent) or agent.get("status") == "waiting":
        return "blocked"
    return "working" if working(agent) else "idle"


def answered(data, after):
    """An agent commented on the card after comment `after`, or the card is in review or done."""
    return (data["task"].get("status") in ("review", "done")
            or any(c["id"] > after and c["author"] not in (DAVE, AUTHOR) for c in data["comments"]))


def card_number(ref):
    """`api#12`, 12 or "12" -> 12; anything else -> None."""
    m = CARD_REF.search(str(ref))
    if m:
        return int(m.group(1))
    return int(ref) if str(ref).strip().isdigit() else None


def stamp(text):
    try:
        return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_private(path, doc):
    """Atomically, mode 0600: a temp file in the same folder, then a rename."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def kanzo_writes(lines, pending, cards):
    """Fold the kanzo MCP writes that succeeded into `cards` {task id: epoch of the latest}. The
    parsing follows tools/activity-sign/sign.py; lines (bytes) without a kanzo call are skipped
    before any json parsing, since transcripts get large. `pending` holds the calls whose result
    has not been read yet: it may be in the next chunk."""
    for line in lines:
        if b"mcp__kanzo__" not in line and not (pending and b"tool_result" in line):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        content = (entry.get("message") or {}).get("content") if isinstance(entry, dict) else None
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            name = b.get("name")
            if b.get("type") == "tool_use" and name in CARD_IN_INPUT | CARD_IN_RESULT:
                ref = (b.get("input") or {}).get("id", "") if name in CARD_IN_INPUT else None
                pending[b.get("id")] = (ref, entry.get("timestamp"))
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                ref, at = pending.pop(b["tool_use_id"])
                if b.get("is_error"):
                    continue
                if ref is None:                          # create_task, next_task: the reply names the card
                    reply = b.get("content")
                    if isinstance(reply, list):
                        reply = " ".join(x.get("text", "") for x in reply if isinstance(x, dict))
                    ref = str(reply or "")[:400]
                n = card_number(ref)
                if n is not None:
                    cards[n] = max(cards.get(n, 0), stamp(at or entry.get("timestamp")))


class Transcripts:
    """The cards each session wrote to, read incrementally: per transcript file (the session's own
    and its subagents/), only the bytes added since the last read. In memory; a restart reads all."""

    def __init__(self, home):
        self.home = home
        self.files = {}           # path -> {"sid", "offset", "pending", "cards"}

    def writes(self, sid, budget=None):
        """-> ({task id: epoch of the latest successful write}, bytes read). With `budget`, reading
        stops after about that many bytes; the rest is read on a later call."""
        root = os.path.join(self.home, ".claude", "projects", "*")
        merged, used = {}, 0
        for path in glob.glob(os.path.join(root, f"{sid}.jsonl")) + glob.glob(os.path.join(root, sid, "subagents", "*.jsonl")):
            f = self.files.setdefault(path, {"sid": sid, "offset": 0, "pending": {}, "cards": {}})
            used += self._read(path, f, None if budget is None else max(budget - used, 0))
            for n, at in f["cards"].items():
                merged[n] = max(merged.get(n, 0), at)
        return merged, used

    @staticmethod
    def _read(path, f, limit):
        try:
            size = os.path.getsize(path)
            if size < f["offset"]:                       # rewritten: start over
                f.update(offset=0, pending={}, cards={})
            todo = size - f["offset"]
            if todo <= 0 or limit == 0:
                return 0
            with open(path, "rb") as fh:
                fh.seek(f["offset"])
                chunk = fh.read(todo if limit is None else min(todo, limit))
                if b"\n" not in chunk and len(chunk) < todo:  # one line longer than the budget
                    chunk += fh.readline()
        except OSError:
            return 0
        end = chunk.rfind(b"\n") + 1                     # whole lines only; a partial one waits
        if end and (f["pending"] or b"mcp__kanzo__" in chunk):
            kanzo_writes(chunk[:end].split(b"\n"), f["pending"], f["cards"])
        f["offset"] += end
        return end

    def keep(self, sids):
        """Forget files of sessions that are no longer live."""
        self.files = {p: f for p, f in self.files.items() if f["sid"] in sids}


class Claude:
    def __init__(self, binary="claude", home=None, tmux="tmux", sock=None):
        self.bin = shutil.which(binary) or binary
        self.home = home or os.path.expanduser("~")
        self.tmux_bin = tmux
        self.sock = sock or os.path.join(tempfile.gettempdir(), f"kanzo-dispatch-{os.getuid()}", "tmux.sock")
        self._agents = None
        self.listed, self.listed_at = None, 0           # the last list read, kept across passes
        self.transcripts = Transcripts(self.home)

    def run(self, args, cwd=None, timeout=30):
        return subprocess.run([self.bin, *args], cwd=cwd, env=child_env(), capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def start(self, cwd, name, prompt, resume=None):
        """-> (short id, None) or (None, short error). With `resume` (a session id) it continues
        that conversation in the background instead: `claude --bg --resume <id> <prompt>`."""
        self._agents = None                              # the list changes: read it again next time
        args = ["--bg", "--resume", resume, prompt] if resume else ["--bg", "--name", name, prompt]
        try:
            r = self.run(args, cwd=cwd, timeout=START_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            return None, f"claude did not run ({type(e).__name__})"
        out = (r.stdout or "") + "\n" + (r.stderr or "")
        m = BG_ID.search(out)
        if r.returncode != 0 or not (m or resume):
            return None, scrub(out)
        return (m.group(1) if m else resume[:8]), None   # resume keeps the session id, so its short id

    def stop(self, short):
        self._agents = None
        try:
            r = self.run(["stop", short])
            return r.returncode == 0, scrub(r.stderr or r.stdout)
        except (OSError, subprocess.SubprocessError) as e:
            return False, type(e).__name__

    def agents(self, fresh=False):
        """Cached for one pass; `fresh` reads again."""
        if self._agents is None or fresh:
            try:
                out = self.run(["agents", "--json", "--all"], timeout=20).stdout
                self._agents = json.loads(out or "[]")
                self.listed, self.listed_at = self._agents, now()
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                log.warning("claude agents --json failed: %s", type(e).__name__)
                return None           # unknown: the caller must not treat sessions as gone
        return self._agents

    def find(self, card, fresh=False):
        """The agent for a card, None when it is not listed, or "unknown" when listing failed."""
        listed = self.agents(fresh)
        if listed is None:
            return "unknown"
        for a in listed:
            if (card.get("session_id") and a.get("sessionId") == card["session_id"]) or a.get("id") == card.get("short"):
                return a
        return None

    def owner(self, task_id, skip=()):
        """The live session that last wrote to the card through Kanzo: an agent dict, None when
        no live session did, or "unknown" when listing failed. Sessions in `skip` are left out."""
        listed = self.agents(fresh=True)
        if listed is None:
            return "unknown"
        best, best_at = None, 0
        for a in listed:
            sid = a.get("sessionId")
            if not sid or sid in skip or not live(a):
                continue
            at = self.transcripts.writes(sid)[0].get(int(task_id), 0)
            if at > best_at:
                best, best_at = a, at
        return best

    def job(self, short):
        """The session's job file (background sessions have one), {} when missing or unreadable."""
        try:
            with open(os.path.join(self.home, ".claude", "jobs", str(short), "state.json")) as f:
                job = json.load(f)
        except (OSError, ValueError):
            return {}
        return job if isinstance(job, dict) else {}

    def session_and_link(self, short, wait=LINK_WAIT):
        deadline = now() + wait
        while True:
            job = self.job(short)
            sid, link = job.get("sessionId"), remote_url(job.get("bridgeSessionId"))
            if (sid and link) or now() >= deadline:
                return sid, link
            time.sleep(0.5)

    # Delivery: type into the session's own TUI, the way claude-slack-bridge does it. A detached
    # tmux on a private socket runs `claude attach <id>`, waits for the empty prompt, types the
    # text (one line: Enter would send early), presses Enter, checks the message landed in that
    # session's transcript, then Ctrl+Z ends the attach client only.
    def _tmux(self, *args):
        return subprocess.run([self.tmux_bin, "-f", "/dev/null", "-S", self.sock, *args],
                              capture_output=True, text=True, timeout=10, env=child_env())

    def _transcript(self, sid):
        hits = glob.glob(os.path.join(self.home, ".claude", "projects", "*", f"{sid}.jsonl"))
        return max(hits, key=os.path.getmtime) if hits else None

    @staticmethod
    def _landed(path, offset, text):
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return False
        for line in lines:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            msg = d.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
            if d.get("type") == "user" and str(content or "").strip() == text.strip():
                return True
        return False

    def deliver(self, card, text):
        """-> (ok, why)."""
        text = " ".join(str(text).split())
        agent = self.find(card, fresh=True)
        if not deliverable(agent if agent != "unknown" else None):
            return False, "session is not idle"
        os.makedirs(os.path.dirname(self.sock), mode=0o700, exist_ok=True)
        name = f"kd-{card['short']}-{int(now() * 1000)}"
        tpath = self._transcript(card.get("session_id") or "")
        offset = os.path.getsize(tpath) if tpath else 0
        cwd = agent.get("cwd") if os.path.isdir(agent.get("cwd") or "") else self.home
        try:
            r = self._tmux("new-session", "-d", "-s", name, "-x", "200", "-y", "50", "-c", cwd,
                           f"{self.bin} attach {card['short']}")
            if r.returncode != 0:
                return False, f"tmux failed ({scrub(r.stderr)})"
            deadline = now() + ATTACH_READY
            while not PROMPT_LINE.search(self._tmux("capture-pane", "-p", "-t", name).stdout or ""):
                if self._tmux("has-session", "-t", name).returncode != 0:
                    return False, "claude attach exited before its prompt"
                if now() > deadline:
                    return False, "attach prompt not ready in time"
                time.sleep(0.3)
            self._tmux("send-keys", "-t", name, "-l", text)
            time.sleep(0.4)
            self._tmux("send-keys", "-t", name, "Enter")
            deadline = now() + ATTACH_VERIFY
            while now() < deadline:
                time.sleep(0.5)
                if tpath and self._landed(tpath, offset, text):
                    return True, "transcript"
                if not tpath and working(self.find(card, fresh=True)):
                    return True, "session went busy"
            return False, "the message did not show up in the session"
        except (OSError, subprocess.SubprocessError) as e:
            return False, type(e).__name__
        finally:
            self._detach(name)

    def _detach(self, name):
        try:
            if self._tmux("has-session", "-t", name).returncode == 0:
                self._tmux("send-keys", "-t", name, "C-z")
                deadline = now() + 5
                while self._tmux("has-session", "-t", name).returncode == 0 and now() < deadline:
                    time.sleep(0.25)
            if self._tmux("has-session", "-t", name).returncode == 0:
                self._tmux("kill-session", "-t", name)
        except (OSError, subprocess.SubprocessError):
            pass


def remote_url(bridge_session_id):
    """cse_X or session_X -> https://claude.ai/code/session_X"""
    bid = str(bridge_session_id or "")
    if bid.startswith("cse_"):
        bid = "session_" + bid[4:]
    return f"https://claude.ai/code/{bid}" if bid.startswith("session_") else None


class Herdr:
    """An interactive session in a herdr pane, the way claude-slack-bridge reaches one."""

    def __init__(self, binary=None):
        self.bin = binary or os.path.expanduser("~/.local/bin/herdr")

    def agents(self):
        try:
            out = subprocess.run([self.bin, "agent", "list"], capture_output=True, text=True, timeout=10,
                                 env=child_env(), stdin=subprocess.DEVNULL).stdout
            return (json.loads(out or "{}").get("result") or {}).get("agents") or []
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            return []

    def pane_for(self, session_id, name=None):
        """herdr <= 0.9.4 names the session id (agent_session.value); later versions may not, so
        the fallback is the session name, which Claude Code writes into the terminal title."""
        agents = self.agents()
        for a in agents:
            if (a.get("agent_session") or {}).get("value") == session_id:
                return a
        if name:
            for a in agents:
                if a.get("terminal_title_stripped") == name:
                    return a
        return None

    def prompt(self, pane_id, text):
        """-> (ok, why). herdr types the text into the pane and presses Enter."""
        try:
            r = subprocess.run([self.bin, "agent", "prompt", pane_id, text], capture_output=True, text=True,
                               timeout=15, env=child_env(), stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"herdr did not run ({type(e).__name__})"
        return r.returncode == 0, scrub(r.stderr or r.stdout)


# ---------------------------------------------------------------- the brief

def card_key(task):
    return f"{task['project']}#{task['id']}"


def session_name(task):
    return clip(f"{card_key(task)} {' '.join(task['title'].split())}", 60)


def brief(data, cwd, ask_id=None):
    t, comments = data["task"], data["comments"]
    key = card_key(t)
    ask = next((c["body"] for c in comments if c["id"] == ask_id), "")
    ask = TRIGGER.sub("", START.sub("", NEW.sub("", ask, count=1), count=1), count=1).strip() or "(see the card and its comments)"
    lines = [f"- {'Dave (me)' if c['author'] == DAVE else c['author']}, {c['created_at']}: {clip(c['body'], COMMENT_CLIP)}"
             for c in comments]
    head = (f"You are working Kanzo card {key} for Dave, who asked for it on the card. "
            f"kanzo-dispatch started this session.\n\n"
            f"Card {key}: {t['title']}\nStatus: {t['status']}\nLink: {t.get('url') or 'none'}\nFolder: {cwd}\n\n"
            f"Description:\n{clip(t.get('description') or '(none)', COMMENT_CLIP * 2)}\n\n")
    rules = (f"\n\nThe ask: {clip(ask, COMMENT_CLIP)}\n\n"
             f"How to work this card:\n"
             f"1. You own {key} through the kanzo MCP tools (get_task, update_task, add_comment). Set it to doing now.\n"
             f"2. Comment your progress on the card as you go.\n"
             f"3. Put the final answer or result in a card comment: Dave reads only the card.\n"
             f"4. Move it to done only when the card's Done when is checkable and you verified it. Otherwise "
             f"move it to review, set human, and say in a comment what to look at.\n"
             f"5. A question for Dave goes in a card comment with human set. His reply arrives in this same session.\n"
             f"6. All standing guardrails apply: no outbound sends, no pushes to shared repos without Dave's "
             f"approval, nothing on prod.")
    # Keep the newest comments when the history is too long for one argument.
    room = BRIEF_CLIP - len(head) - len(rules) - 200
    kept = []
    for line in reversed(lines):
        if len(line) + 1 > room:
            kept.append(f"- ({len(lines) - len(kept)} older comments left out: read them with get_task)")
            break
        kept.append(line)
        room -= len(line) + 1
    history = "\n".join(reversed(kept)) or "(none)"
    return head + "Comments, oldest first:\n" + history + rules


def follow_up(key, comments, title=None, images=()):
    """One line. `title` is for an adopted session, which may work several cards. `images` is the
    card's image list from GET /api/tasks/{id}: the line counts the comments' pictures, no paths."""
    on = f"{key} ({' '.join(title.split())})" if title else key
    parts = [f"Dave commented on card {on}: {clip(c['body'], COMMENT_CLIP)}" for c in comments]
    ids = {c["id"] for c in comments}
    n = sum(1 for im in images or () if im.get("comment_id") in ids)
    pics = (f" ({n} picture(s) attached to this comment: get_task on the card lists the file paths, read them.)"
            if n else "")
    return " | ".join(parts) + " (Answer on the card with the kanzo tools; he reads only the card.)" + pics


# ---------------------------------------------------------------- the dispatcher

class Dispatcher:
    def __init__(self, board, claude, state_path, dev_root, db_path, max_working=3, dry_run=False,
                 deliver_timeout=DELIVER_TIMEOUT, start_grace=START_GRACE, link_wait=LINK_WAIT, herdr=None,
                 status_path=None, interval=5, burst=5, idle_stop=IDLE_STOP, nudge_after=NUDGE_AFTER,
                 refresh=SESSIONS_EVERY, push=None, asks=True, ask_after=ASK_AFTER, ask_project=ASK_PROJECT):
        self.board, self.claude, self.herdr = board, claude, herdr or Herdr()
        self.asks_on, self.ask_after, self.ask_project = asks, ask_after, ask_project
        self.ask_seen = {}              # session id -> when this run first saw it blocked (no updatedAt)
        self.ask_retry = {}             # session id -> not before: a skipped or failed ask waits ASK_RETRY
        self.state_path, self.dev_root, self.db_path = state_path, dev_root, db_path
        self.max, self.dry = max_working, dry_run
        self.deliver_timeout, self.start_grace, self.link_wait = deliver_timeout, start_grace, link_wait
        self.status_path = status_path or os.path.join(os.path.dirname(state_path), "status.json")
        self.interval, self.burst, self.idle_stop = interval, burst, idle_stop
        self.nudge_after, self.refresh = nudge_after, refresh
        self.failure = {"cause": None, "count": 0}
        self.halted = None              # in memory only: a restart is the reset
        self.st = self.load()
        self.push = push
        if push and self.st:
            push.dead = list(self.st.get("push_dead", []))[-PUSH_KEEP_DEAD:]
        try:                            # carry rev and each card's `since` over a restart
            with open(self.status_path) as f:
                old = json.load(f)
            self.rev = int(old["rev"])
            self.since = {t: (c["state"], stamp(c["since"])) for t, c in old["cards"].items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.rev, self.since = 0, {}
        self.shown = None               # the last status.json body, `updated` and `rev` left out

    # state
    def load(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def save(self):
        if self.dry:
            return
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = f"{self.state_path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(self.st, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)

    # writes, all skipped in a dry run
    def say(self, task_id, body, human=False):
        if self.dry:
            log.info("dry run: would comment on #%s%s: %s", task_id, " and set human" if human else "", clip(body, 200))
            return
        try:
            self.board.comment(task_id, body)
            if human:
                self.board.flag_human(task_id)
        except BoardError as e:
            log.error("could not write to card #%s: %s", task_id, e)

    # receipts: what happened to each of Dave's comments, for status.json, kept in state.json
    def receipt(self, tid, ids, state, note=""):
        rec = self.st.setdefault("receipts", {}).setdefault(str(tid), {"comments": {}})
        for cid in ids if isinstance(ids, list) else [ids]:
            rec["comments"][str(cid)] = {"state": state, "at": now(), "note": clip(note, 200)}
        for old in sorted(rec["comments"], key=int)[:-KEEP_RECEIPTS]:
            del rec["comments"][old]
        rec["touched"] = now()
        self.save()

    def mark(self, tid, state=None, note=""):
        """The card's own state when its sessions do not explain it: failed, unreachable or silent.
        No state clears it, as any new comment by Dave does."""
        recs = self.st.setdefault("receipts", {})
        if state is None and not recs.get(str(tid), {}).get("state"):
            return
        recs.setdefault(str(tid), {"comments": {}}).update(state=state, note=clip(note, 200) if state else "",
                                                           touched=now())
        self.save()

    def first_run(self):
        activity = self.board.latest_activity()      # first, so nothing between the two reads replays
        comment = highest_comment_id(self.db_path)
        self.st = {"version": 1, "activity_seen": activity, "comment_seen": comment, "cards": {}, "queue": []}
        self.save()
        log.info("first run: acting only on comments after id %s (activity after %s)", comment, activity)

    def recover(self):
        """A start that was recorded but not finished: adopt the session if it shows up by name,
        or tell Dave. Never start again for that comment."""
        for tid, card in list(self.st["cards"].items()):
            if card.get("status") != "starting":
                continue
            listed = self.claude.agents(fresh=True)
            if listed is None:
                return                                   # cannot tell yet; the next pass tries again
            found = next((a for a in listed if a.get("name") == card["name"] and a.get("id")), None)
            if found:
                card.update(status="attached", short=found["id"], session_id=found.get("sessionId"),
                            link=self.claude.session_and_link(found["id"], 0)[1])
                log.info("adopted %s for card #%s after a restart", found["id"], tid)
            else:
                del self.st["cards"][tid]
                self.mark(tid, "failed", "a start was cut off by a dispatcher restart")
                self.say(tid, "A start for this card was cut off by a dispatcher restart and no session "
                              "showed up. Comment again to retry.", human=True)
            self.save()

    # one pass
    def tick(self):
        try:
            self.step()
        finally:
            self.push_pass()
            self.publish()                               # the heartbeat, even when the pass failed

    def push_pass(self):
        """New answers in the inbox: a push to each browser (class Push). Never fails the pass."""
        if self.push is None or self.st is None:
            return
        try:
            self.push.check(self.board.get("/api/inbox"), dry=self.dry)
            with self.push.lock:
                dead = list(self.push.dead)
            if dead != self.st.get("push_dead", []):
                self.st["push_dead"] = dead
                self.save()
        except BoardError:
            pass                                         # the pass reports the board itself
        except Exception as e:
            log.warning("push pass failed: %s", type(e).__name__)

    def step(self):
        self.claude._agents = None
        if self.st is None:
            self.first_run()
            return
        st = self.st
        if any(c.get("status") == "starting" for c in st["cards"].values()):
            self.recover()
        rows = self.board.activity_since(st["activity_seen"])
        commented = []
        for r in rows:
            if r.get("task_id") is None:
                continue
            tid = str(r["task_id"])
            card = st["cards"].get(tid)
            if r.get("actor") not in (BOARD_ACTOR, AUTHOR):
                if card:
                    card["updated"] = True               # an agent wrote or moved the card
                if st.get("receipts", {}).get(tid, {}).get("state") == "silent":
                    self.mark(tid)                       # it spoke up after all
            if r.get("action") == "commented" and tid not in commented:
                commented.append(tid)
        new = []
        for tid in commented:
            data = self.board.task(tid)
            for c in (data or {}).get("comments", []):
                if c["id"] > st["comment_seen"]:
                    new.append((c, data))
        # Other tools post through the HTTP API and are signed `me` too: comments on many cards in
        # one pass look like a script, so none of them starts or reaches a session.
        asks = {str(d["task"]["id"]) for c, d in new if c["author"] == DAVE and not NOTE.match(c["body"])}
        burst = bool(self.burst) and len(asks) > self.burst
        if burst:
            log.warning("held comments on %d cards from one pass: that looks like a script, nothing sent", len(asks))
        for c, data in sorted(new, key=lambda x: x[0]["id"]):
            st["comment_seen"] = max(st["comment_seen"], c["id"])
            self.save()                                  # seen before acted: at most once
            if c["author"] == DAVE and burst and not NOTE.match(c["body"]):
                self.receipt(data["task"]["id"], c["id"], "held",
                             f"held: comments on {len(asks)} cards at once looked like a script; comment again to send")
            elif c["author"] == DAVE:
                self.handle(c, data)
        if rows:
            st["activity_seen"] = max(st["activity_seen"], rows[-1]["id"])
            self.save()
        self.deliver_pending()
        self.watch()
        self.start_queued()
        self.asks_pass()

    def handle(self, c, data):
        t = data["task"]
        tid, body, cid = str(t["id"]), c["body"], c["id"]
        card = self.st["cards"].get(tid)
        queued = next((q for q in self.st["queue"] if q["task"] == tid), None)
        if NOTE.match(body):                             # a note to self: never reaches an agent
            return
        self.mark(tid)                                   # a new ask: an earlier failure or silence is old news
        held = (card or {}).get("pending") or []         # comments still waiting for the session
        if STOP.match(body):
            if held:
                self.receipt(tid, held, "failed", "not delivered: the session was stopped first")
            if card and card.get("adopted"):             # not ours to stop: let it go
                self.detach(tid, card)
                note = f"detached session {card['name']}; it was not stopped"
                self.say(tid, f"Detached session `{card['name']}` from this card. The dispatcher did not "
                              f"start it, so it was not stopped and keeps running. Your next comment here "
                              f"starts a new agent.")
            elif card:
                self.stop_card(tid, card)
                note = f"stopped session {card['name']}"
                self.say(tid, "Stopped the session for this card.")
            elif queued:
                self.st["queue"] = [q for q in self.st["queue"] if q["task"] != tid]
                self.save()
                note = "taken off the queue"
                self.say(tid, "Taken off the queue; no session was started.")
            else:
                note = "no session was attached"
                self.say(tid, "No session is attached to this card.")
            self.receipt(tid, cid, "command", note)
            return
        if NEW.match(body):                              # a separate agent, whoever else is on the card
            if card and card.get("adopted"):
                self.detach(tid, card)
            elif card:
                self.stop_card(tid, card)
            if queued:
                self.receipt(tid, cid, "queued")
            else:
                self.request(tid, cid, fresh=True, ids=[cid, *held])   # the brief holds them all
            return
        if queued:                                       # its brief will hold this comment too
            queued.setdefault("also", []).append(cid)
            self.receipt(tid, cid, "queued")
            return
        if START.match(body) and card:
            agent = self.claude.find(card)
            if agent == "unknown" or alive(agent):       # someone is on it: name them, start nothing
                self.on_it(tid, cid, card["name"], card.get("short"))
                return
        if card:                                         # one session per card, done or not
            card.setdefault("pending", []).append(cid)
            card.setdefault("pending_since", now())
            self.receipt(tid, cid, "waiting")
            return
        self.route(tid, cid, start=bool(START.match(body)))

    def on_it(self, tid, cid, name, short):
        """`@claude start` on a card a live session already works: one comment naming it."""
        self.say(tid, f"Session `{name}` (id {short}) is already on this card, so nothing was started. "
                      f"Comment here to reach it.")
        self.receipt(tid, cid, "command", f"already on the card: {name}")

    def forget(self, tid, card):
        """Take the card off the dispatcher's list. A session it started is remembered, so the
        next comment can resume that conversation instead of starting cold."""
        if card.get("session_id") and not card.get("adopted"):
            rec = self.st.setdefault("receipts", {}).setdefault(tid, {"comments": {}})
            rec.update(resume={k: card.get(k) for k in ("session_id", "short", "name", "cwd", "link")},
                       touched=now())
        self.st["cards"].pop(tid, None)
        self.save()

    def stop_card(self, tid, card):
        """Stop a session the dispatcher started; one it adopted is only let go."""
        if card.get("short") and not card.get("adopted") and not self.dry:
            ok, why = self.claude.stop(card["short"])
            if not ok:
                log.warning("claude stop %s: %s", card["short"], why)
        self.forget(tid, card)

    def detach(self, tid, card):
        """Let an adopted session go, and keep it from being adopted for this card again."""
        if self.dry:
            return
        gone = self.st.setdefault("detached", {}).setdefault(tid, [])
        if card.get("session_id") and card["session_id"] not in gone:
            gone.append(card["session_id"])
        self.st["cards"].pop(tid, None)
        self.save()

    def route(self, tid, comment_id, why=None, skip=(), ids=None, start=False):
        """Your comment goes to whoever is working the card; if nobody is, a new agent starts.
        `ids` are all the comments this carries (default: the one); `start` is `@claude start`."""
        ids = ids or [comment_id]
        skip = set(skip) | set(self.st.get("detached", {}).get(tid, []))
        owner = self.claude.owner(int(tid), skip)
        if owner == "unknown":
            self.receipt(tid, ids, "failed", "could not list the Claude sessions")
            self.mark(tid, "failed", "could not list the Claude sessions")
            self.say(tid, "Could not list the Claude sessions to check whether one is already on this card, "
                          "so nothing was started. Comment again to retry.", human=True)
        elif owner and start:
            self.on_it(tid, comment_id, owner.get("name") or owner.get("sessionId"),
                       owner.get("id") or str(owner.get("sessionId"))[:8])
        elif owner:
            self.adopt(tid, owner, ids)
        else:
            self.request(tid, comment_id, why, ids=ids)

    def adopt(self, tid, agent, ids):
        """Attach a session the dispatcher did not start, then deliver through the usual path."""
        data = self.board.task(tid)
        if not data:
            return
        sid = agent.get("sessionId")
        short = agent.get("id") or str(sid)[:8]          # an interactive session has no short id: its first 8
        name = agent.get("name") or short
        kind = "interactive" if agent.get("kind") == "interactive" else "background"
        if self.dry:
            log.info("dry run: would send the comment on %s to %s session %s (%s)", card_key(data["task"]),
                     kind, short, name)
            return
        card = {"key": card_key(data["task"]), "name": name, "cwd": agent.get("cwd"), "status": "attached",
                "short": short, "session_id": sid, "adopted": True, "kind": kind, "since": now(),
                "updated": False, "warned": False, "pending": list(ids), "pending_since": now(),
                "failures": 0, "announce": True,
                "link": self.claude.session_and_link(short, 0)[1] if kind == "background" and short else None}
        if kind == "interactive":
            pane = self.herdr.pane_for(sid, name)
            card["pane"] = pane.get("pane_id") if pane else None
        self.st["cards"][tid] = card
        self.receipt(tid, list(ids), "waiting")
        log.info("adopted %s session %s for %s", kind, short, card["key"])

    def unreachable(self, tid, card, why):
        log.warning("could not reach %s for %s: %s", card.get("short"), card["key"], why)
        self.st["cards"].pop(tid, None)
        self.receipt(tid, card.get("pending") or [], "failed", f"could not reach the session: {why}")
        self.mark(tid, "unreachable", f"session {card['name']} could not be reached: {why}")
        self.say(tid, f"Session `{card['name']}` is already on this card and I could not reach it. "
                      f"Comment `@claude new` to start a separate agent.", human=True)

    def working_count(self):
        n = 0
        for card in self.st["cards"].values():
            if card.get("adopted"):                      # not started here: not in the cap
                continue
            if card.get("status") == "starting":
                n += 1
            elif card.get("status") == "attached":
                a = self.claude.find(card)
                n += a == "unknown" or working(a)
        return n

    def request(self, tid, comment_id, why=None, fresh=False, ids=None):
        """`fresh`: never resume the card's earlier session (`@claude new`)."""
        ids = ids or [comment_id]
        if self.halted:
            self.receipt(tid, ids, "failed", "the dispatcher stopped starting sessions after two failures")
            self.mark(tid, "failed", f"no starts after two failures: {self.halted}")
            self.say(tid, f"Not started: the dispatcher stopped starting sessions after two failures in a row "
                          f"({self.halted}). It retries after a fix and a restart of kanzo-dispatch.", human=True)
            return
        if self.working_count() >= self.max:
            self.st["queue"].append({"task": tid, "comment": comment_id, "why": why, "at": now(),
                                     "fresh": fresh, "also": ids[1:]})
            self.receipt(tid, ids, "queued")
            self.say(tid, f"Queued: {self.max} sessions are already working. This card starts when one finishes.")
            return
        self.start(tid, comment_id, why, fresh, ids)

    def folder(self, slug):
        """-> (dir, None) or (None, why)."""
        project = self.board.project(slug) or {}
        repos = project.get("repos") or []
        if not repos:
            return self.dev_root, None
        for repo in repos:
            d = os.path.join(self.dev_root, repo.split("/")[-1])
            if os.path.isdir(d):
                return d, None
        want = os.path.join(self.dev_root, repos[0].split("/")[-1])
        return None, (f"There is no checkout of {repos[0]} on this box (looked for {want}), so no session "
                      f"was started. Clone it there, then comment again.")

    def start(self, tid, comment_id, why=None, fresh=False, ids=None):
        ids = ids or [comment_id]
        data = self.board.task(tid)
        if not data:
            return
        t = data["task"]
        if not fresh and self.resume(tid, data, ids, why):
            return
        cwd, problem = self.folder(t["project"])
        if problem:
            self.receipt(tid, ids, "failed", "no checkout of the card's repo on this box")
            self.mark(tid, "failed", "no checkout of the card's repo on this box")
            self.say(tid, problem, human=True)
            return
        name = session_name(t)
        if self.dry:
            log.info("dry run: would start %r in %s", name, cwd)
            return
        card = {"key": card_key(t), "name": name, "cwd": cwd, "comment": comment_id,
                "status": "starting", "since": now(), "updated": False, "warned": False}
        self.st["cards"][tid] = card
        self.save()                                      # the intent is on disk before claude runs
        short, err = self.claude.start(cwd, name, brief(data, cwd, comment_id))
        if err:
            self.st["cards"].pop(tid, None)
            self.save()
            self.receipt(tid, ids, "failed", f"claude did not start: {err}")
            self.fail(tid, err)
            return
        self.failure = {"cause": None, "count": 0}
        sid, link = self.claude.session_and_link(short, self.link_wait)
        card.update(status="attached", short=short, session_id=sid, link=link,
                    wait={"comment": max(ids), "at": now(), "nudged": False})
        self.st.get("receipts", {}).get(tid, {}).pop("resume", None)   # a new conversation replaces the old one
        self.carried(tid, data, ids, "started", f"started session {short}")
        log.info("started %s for %s in %s", short, card["key"], cwd)
        lead = f"Picked up ({why}). " if why else "Picked up. "
        self.say(tid, f"{lead}Session `{name}` (id {short}) in {cwd}. "
                      + (f"App: {link}" if link else "The app link was not ready yet; `claude agents` lists it."))

    def carried(self, tid, data, ids, state, note):
        """Receipts for the comments a start or resume carried; `@claude start|new` is a command."""
        bodies = {c["id"]: c["body"] for c in data["comments"]}
        for cid in ids:
            command = START.match(bodies.get(cid, "")) or NEW.match(bodies.get(cid, ""))
            self.receipt(tid, cid, "command" if command else state, note)

    def resume(self, tid, data, ids, why=None):
        """The card's earlier dispatcher session ended or was stopped: continue it with the new
        comments (`claude --bg --resume`), so the card keeps one conversation. -> True when it runs."""
        prev = self.st.get("receipts", {}).get(tid, {}).get("resume")
        if not prev or not prev.get("session_id"):
            return False
        key = card_key(data["task"])
        if self.dry:
            log.info("dry run: would resume %s for %s", prev.get("short"), key)
            return True
        comments = [c for c in data["comments"] if c["id"] in ids]
        card = {"key": key, "name": prev.get("name") or session_name(data["task"]), "cwd": prev.get("cwd"),
                "comment": ids[0], "status": "starting", "since": now(), "updated": False, "warned": False}
        self.st["cards"][tid] = card
        self.save()                                      # the intent is on disk before claude runs
        short, err = self.claude.start(prev.get("cwd") or self.dev_root, card["name"],
                                       follow_up(key, comments, images=data.get("images")), prev["session_id"])
        self.st["receipts"][tid].pop("resume", None)     # one try per ended session
        if err:
            log.warning("could not resume %s for %s (%s): starting a new session", prev.get("short"), key, err)
            self.st["cards"].pop(tid, None)
            self.save()
            return False
        sid, link = self.claude.session_and_link(short, self.link_wait)
        card.update(status="attached", short=short, session_id=sid or prev["session_id"],
                    link=link or prev.get("link"), wait={"comment": max(ids), "at": now(), "nudged": False})
        self.carried(tid, data, ids, "delivered", f"resumed session {short}")
        log.info("resumed %s for %s", short, key)
        lead = f"Resumed ({why}). " if why else "Resumed. "
        self.say(tid, f"{lead}Your comment went to the card's earlier session `{card['name']}` (id {short}), "
                      f"so it keeps the conversation. " + (f"App: {card['link']}" if card.get("link") else ""))
        return True

    def fail(self, tid, err):
        cause = re.sub(r"\d+", "N", err)
        same = cause == self.failure["cause"]
        self.failure = {"cause": cause, "count": self.failure["count"] + 1 if same else 1}
        log.error("claude did not start for card #%s: %s", tid, err)
        msg = f"Could not start a Claude session: {err}"
        if self.failure["count"] >= 2:
            self.halted = err
            log.error("two start failures in a row (%s): no new starts until kanzo-dispatch restarts", err)
            msg += " That is two in a row, so the dispatcher stops starting sessions until it is fixed and restarted."
        self.mark(tid, "failed", f"claude did not start: {err}")
        self.say(tid, msg, human=True)

    def replace(self, tid, card, why):
        """The card's session is gone or would not take the comment: whoever else works the card
        gets it, else the dropped session is resumed with them, else a new one starts from the full
        history. The dropped session is never re-adopted."""
        self.stop_card(tid, card)
        ids = card.get("pending") or [card.get("comment")]
        self.route(tid, ids[0], why, skip={card.get("session_id")}, ids=ids)

    def ready(self, card, agent):
        """-> (ready, pane id or None, why not). A pane is re-resolved on every try."""
        if card.get("kind") != "interactive":
            return deliverable(agent), None, "session is not idle"
        pane = self.herdr.pane_for(card.get("session_id"), agent.get("name") or card.get("name"))
        if not pane:
            return False, None, "no herdr pane runs it"
        card["pane"] = pane.get("pane_id")
        busy = working(agent) or pane.get("agent_status") in ("working", "blocked")
        return not busy, card["pane"], "session is busy"

    def deliver_pending(self):
        for tid, card in list(self.st["cards"].items()):
            if card.get("status") != "attached" or not card.get("pending"):
                continue
            agent = self.claude.find(card)
            if agent == "unknown" or (agent is None and not card.get("adopted")
                                      and now() - card.get("since", 0) < self.start_grace):
                continue
            if card.get("ask") and not alive(agent):
                self.resume_ask(tid, card)               # it waits on Dave with no process: resume it
                continue
            if not alive(agent):
                self.replace(tid, card, "the earlier session was gone")
                continue
            go, pane, why = self.ready(card, agent)
            if not go and card.get("ask") and not working(agent):   # a permission or a question: never typed into
                self.note_waiting(tid, card, "the session waits on a permission or a question; open it in "
                                             f"the app to answer: {card.get('link') or card.get('short')}")
            if not go and card.get("adopted") and why == "no herdr pane runs it":
                ok = False                               # nothing to wait for: a failed try
            elif go:
                data = self.board.task(tid) or {"task": {}, "comments": []}
                comments = [c for c in data["comments"] if c["id"] in card["pending"]]
                if self.dry:
                    log.info("dry run: would deliver %d comment(s) to %s", len(comments), card["short"])
                    continue
                text = follow_up(card["key"], comments, data["task"].get("title") if card.get("adopted") else None,
                                 data.get("images"))
                if not comments:
                    ok, why = True, "none left"
                elif pane:
                    ok, why = self.herdr.prompt(pane, " ".join(text.split()))
                else:
                    ok, why = self.claude.deliver(card, text)
            else:
                ok = None                                # busy: wait, up to the deadline below
            if ok:
                log.info("delivered %d comment(s) on %s to %s", len(comments), card["key"], card["short"])
                if comments:
                    self.receipt(tid, [c["id"] for c in comments], "delivered")
                    card["wait"] = {"comment": max(c["id"] for c in comments), "at": now(), "nudged": False}
                card.update(pending=[], updated=False, warned=False, since=now(), failures=0)
                card.pop("pending_since", None)
                announce = card.pop("announce", False)   # once, on the first delivery after adopting
                self.save()
                if announce:
                    where = (f"App: {card['link']}" if card.get("link") else
                             f"Terminal pane {pane}, no app link." if pane else "No app link yet.")
                    self.say(tid, f"Sent to the session already on this card: `{card['name']}` "
                                  f"(id {card['short']}). {where}")
            elif ok is False and card.get("adopted"):
                card["failures"] = card.get("failures", 0) + 1
                log.warning("delivery to %s failed (%d): %s", card["short"], card["failures"], why)
                if card["failures"] >= 2:
                    self.unreachable(tid, card, why)
                else:
                    self.save()                          # the next pass tries once more
            elif ok is False:
                log.warning("delivery to %s failed: %s", card["short"], why)
                self.replace(tid, card, f"the earlier session did not take the comment: {why}")
            elif now() - card.get("pending_since", now()) > self.deliver_timeout:
                self.say(tid, f"Your comment did not reach the session: it stayed busy for "
                              f"{self.deliver_timeout // 60} minutes. App: {card.get('link') or card['short']}",
                         human=True)
                self.receipt(tid, card["pending"], "failed",
                             f"the session stayed busy for {self.deliver_timeout // 60} minutes")
                card["pending"] = []
                card.pop("pending_since", None)
                self.save()

    def note_waiting(self, tid, card, note):
        """Say on the waiting receipts why they wait, once: rewriting them every pass would move `rev`."""
        rec = self.st.get("receipts", {}).get(str(tid), {}).get("comments", {})
        ids = [cid for cid in card.get("pending") or [] if rec.get(str(cid), {}).get("note") != clip(note, 200)]
        if ids and not self.dry:
            self.receipt(tid, ids, "waiting", note)

    def resume_ask(self, tid, card):
        """Dave answered on an ask card and the session has no process: `claude --bg --resume` it with
        his comments, as resume() does for a dispatcher session, with the same follow-up line. No card
        comment: the receipt says delivered, and a dispatcher comment on a card flagged for Dave would
        put it back in his Inbox and push his phone about his own answer."""
        data = self.board.task(tid)
        if not data:
            return
        ids = list(card.get("pending") or [])
        comments = [c for c in data["comments"] if c["id"] in ids]
        if self.dry:
            log.info("dry run: would resume %s with %d comment(s) on %s", card.get("short"), len(comments), card["key"])
            return
        card.pop("pending_since", None)
        if not comments:
            card["pending"] = []
            self.save()
            return
        text = follow_up(card["key"], comments, data["task"].get("title") if card.get("adopted") else None,
                         data.get("images"))
        card.update(status="starting", pending=[])        # on disk before claude runs: a crash never resends
        self.save()
        short, err = self.claude.start(card.get("cwd") or self.dev_root, card["name"], text, card["session_id"])
        if err:
            log.warning("could not resume %s for %s (%s)", card.get("short"), card["key"], err)
            card.update(status="attached", pending=ids, pending_since=now())
            self.replace(tid, card, f"the waiting session could not be resumed: {err}")
            return
        link = self.claude.session_and_link(short, 0)[1]
        card.update(status="attached", short=short, link=link or card.get("link"), since=now(), updated=False,
                    warned=False, failures=0, wait={"comment": max(c["id"] for c in comments), "at": now(),
                                                    "nudged": False})
        self.carried(tid, data, [c["id"] for c in comments], "delivered", f"resumed session {short}")
        log.info("resumed waiting session %s for %s", short, card["key"])

    def watch(self):
        for tid, card in list(self.st["cards"].items()):
            if card.get("status") != "attached":
                continue
            agent = self.claude.find(card)
            if agent == "unknown":
                continue
            if card.get("ask"):                          # waits on Dave: the asks pass has it, never a nudge
                continue
            if self.asks_on and agent and not alive(agent) and asking(agent, self.claude.job(card.get("short"))):
                continue                                 # no process, blocked: an ask comes, keep the card
            if card.get("adopted"):                      # not started for this card: only the nudge
                if not alive(agent) and not card.get("pending"):
                    self.st["cards"].pop(tid, None)
                    self.save()
                elif alive(agent) and not working(agent):
                    self.nudge(tid, card, agent)
                continue
            idle = alive(agent) and not working(agent) and not blocked(agent)
            if not idle:
                card.pop("idle_since", None)
            elif "idle_since" not in card:
                card["idle_since"] = now()
                self.save()
            if working(agent) or now() - card.get("since", 0) < self.start_grace:
                continue
            if idle and self.nudge(tid, card, agent):
                continue
            if not card.get("updated") and not card.get("warned"):
                what = ("is waiting on a permission or a question in the app" if blocked(agent)
                        else "ended without an update")
                self.say(tid, f"The session {what}; nothing on this card from it since pickup. "
                              f"App: {card.get('link') or card.get('short')}", human=True)
                card["warned"] = True
                self.save()
            if not alive(agent) and not card.get("pending"):
                self.forget(tid, card)                   # gone: the slot is free; a comment resumes it
            elif (idle and self.idle_stop and not card.get("pending") and not self.dry
                  and now() - card["idle_since"] >= self.idle_stop and int(tid) in self.done_cards([int(tid)])):
                log.info("stopping %s: idle %d minutes on done card %s", card["short"],
                         (now() - card["idle_since"]) // 60, card["key"])
                self.stop_card(tid, card)                # no comment; the next one resumes it

    def nudge(self, tid, card, agent):
        """The turn ended and Dave's last comment has no answer on the card: type one reminder into
        the session; when the next turn ends without one too, say so on the card and set human.
        -> True while this is in hand, so the plain watchdog waits."""
        wait = card.get("wait")
        if not wait or card.get("pending") or blocked(agent):
            return False
        if card.get("short") and asking(agent, self.claude.job(card["short"])):
            return False                                 # its job file says it waits on Dave: never typed into
        if now() - wait["at"] < self.nudge_after:
            return True
        go, pane, _ = self.ready(card, agent)
        if not go:                                       # never into a session that is working
            return False
        data = self.board.task(tid)
        if not data or answered(data, wait["comment"]):
            card.pop("wait")
            self.save()
            return True
        if self.dry:
            log.info("dry run: would nudge %s on %s", card["short"], card["key"])
            return True
        if not wait["nudged"]:
            text = NUDGE.format(key=card["key"])
            ok, why = self.herdr.prompt(pane, text) if pane else self.claude.deliver(card, text)
            log.info("nudged %s on %s: %s", card["short"], card["key"], "ok" if ok else why)
            wait.update(nudged=True, at=now())           # once per delivered comment, even when it failed
            self.save()
            return True
        card.pop("wait")
        card["warned"] = True
        self.mark(tid, "silent", "the agent finished twice without answering on the card")
        self.say(tid, f"The agent finished without answering here. Open the session to see what it did: "
                      f"{card.get('link') or card.get('short')}. Comment again to retry.", human=True)
        return True

    def start_queued(self):
        while self.st["queue"] and not self.halted and self.working_count() < self.max:
            item = self.st["queue"].pop(0)
            self.save()
            if item["task"] not in self.st["cards"]:
                self.start(item["task"], item["comment"], item.get("why") or "from the queue",
                           item.get("fresh", False), [item["comment"], *item.get("also", [])])

    # asks: a background session that waits on Dave (agent/dispatch/README.md, "Sessions that wait on Dave")
    def listing(self):
        """The session list read this pass, else a fresh one at most every `refresh` seconds, else the
        last one read. None when there is none: the caller must not treat sessions as gone."""
        claude = self.claude
        if claude._agents is not None:
            return claude._agents
        if now() - claude.listed_at >= self.refresh:
            return claude.agents(fresh=True) or claude.listed
        return claude.listed

    def asks_pass(self):
        """Every pass: end the episodes that are over, then ask for the sessions blocked long enough.
        An episode is (session id, the job's updatedAt); it gets one comment, never two."""
        if not self.asks_on or self.st is None:
            return
        listed = self.listing()
        if listed is None:
            return
        asks, t = self.st.setdefault("asks", {}), now()
        waiting = {}
        for a in listed:
            sid = a.get("sessionId")
            if not sid or a.get("kind") != "background":
                continue
            short = a.get("id") or sid[:8]
            job = self.claude.job(short)
            if asking(a, job):
                waiting[sid] = (a, job, short)
        for sid in list(asks):
            if sid not in waiting:
                self.end_ask(sid)
        self.ask_seen = {sid: self.ask_seen.get(sid, t) for sid in waiting}
        self.ask_retry = {sid: at for sid, at in self.ask_retry.items() if sid in waiting and at > t}
        tried = 0
        for sid, (a, job, short) in sorted(waiting.items(), key=lambda x: self.ask_since(x[0], x[1][1])):
            since, entry = self.ask_since(sid, job), asks.get(sid)
            if entry and (entry.get("since") == since or not job.get("updatedAt")):
                continue                                 # this episode was asked already
            if t - since < self.ask_after or self.ask_retry.get(sid, 0) > t:
                continue
            if tried >= ASKS_PER_PASS:
                break
            tried += 1
            self.ask_retry[sid] = t + ASK_RETRY          # cleared by success; a skip or failure waits
            try:
                self.ask(sid, a, job, short, since, entry)
            except (BoardError, KeyError, TypeError, ValueError) as e:   # never the loop's problem
                log.warning("ask for session %s not posted: %s", short, e)
        self.forget_ask_cards()

    def ask_since(self, sid, job):
        """When the episode began: the job's updatedAt, else when this run first saw it blocked."""
        return stamp(job.get("updatedAt")) if job.get("updatedAt") else self.ask_seen.get(sid, now())

    def project_for(self, cwd, projects):
        """The reverse of folder(): a cwd inside <dev_root>/<dir> (a worktree under it too) maps to the
        project that links a repo named <dir>."""
        try:
            rel = os.path.relpath(os.path.abspath(cwd), self.dev_root) if cwd else ".."
        except ValueError:
            return None
        top = rel.split(os.sep)[0]
        if top in ("", ".", ".."):
            return None
        hits = [p for p in projects if any(str(r).split("/")[-1] == top for r in p.get("repos") or [])]
        best = next((p for p in hits if p.get("slug") == top), hits[0] if hits else None)
        return best.get("slug") if best else None

    def ask_card(self, sid, short, name, cwd, link):
        """-> (task id, created) or (None, False). Order: the card the dispatcher holds for the session;
        the card of its earlier ask; the open card it last wrote to through Kanzo; a new card in the
        project of its folder, else in --ask-project."""
        st = self.st
        held = next((t for t, c in st["cards"].items() if c.get("session_id") == sid), None)
        if held:
            return held, False
        prev = st.get("ask_cards", {}).get(sid) or {}
        writes = self.claude.transcripts.writes(sid)[0]
        order = ([str(prev["task"])] if prev.get("task") else []) + [
            str(n) for n, _ in sorted(writes.items(), key=lambda x: (-x[1], -x[0]))]
        done = self.done_cards([int(x) for x in order])
        queued = {q["task"] for q in st["queue"]}
        for tid in list(dict.fromkeys(order))[:10]:
            other = st["cards"].get(tid)
            if (int(tid) in done or tid in queued or (other and other.get("session_id") != sid)
                    or sid in st.get("detached", {}).get(tid, [])):
                continue
            data = self.board.task(tid)
            if data and data["task"].get("status") != "done":
                return tid, bool(prev.get("created")) and str(prev.get("task")) == tid
        projects = self.board.projects()
        slug = self.project_for(cwd, projects)
        if not slug and not any(p.get("slug") == self.ask_project for p in projects):
            log.warning("ask for session %s skipped: no project for %s and the fallback project %s does not "
                        "exist (--ask-project)", short, cwd or "its folder", self.ask_project)
            return None, False
        slug = slug or self.ask_project
        if self.dry:
            log.info("dry run: would make a card in %s for session %s", slug, short)
            return None, False
        desc = (f"Claude session `{name}` (id {short}) stopped and waits on Dave.\n\n"
                f"Folder: {cwd or 'unknown'}\nApp: {link or 'none'}\n\n"
                f"kanzo-dispatch made this card for it. Answer in a comment and it goes to that session.")
        tid = str(self.board.create_task(slug, clip(f"Answer: {' '.join(name.split())}", 80), desc,
                                         status="doing", human=True, tags="agent,ask"))
        st.setdefault("ask_cards", {})[sid] = {"task": tid, "created": True, "at": now()}
        self.save()
        log.info("made card #%s in %s for waiting session %s", tid, slug, short)
        return tid, True

    def ask(self, sid, agent, job, short, since, entry):
        """Post the ask on the session's card, set human, and route the card to the session."""
        name = " ".join(str(agent.get("name") or job.get("name") or short).split())
        cwd = agent.get("cwd") or job.get("cwd") or ""
        link = remote_url(job.get("bridgeSessionId"))
        tid, created = self.ask_card(sid, short, name, cwd, link)
        if tid is None:
            return
        needs = scrub(job.get("needs"), ASK_NEEDS_CLIP) if str(job.get("needs") or "").strip() else ASK_EMPTY
        body = f"**{name} waits on you.**\n\n{needs}\n\n"
        if str(job.get("suggestedReply") or "").strip():
            body += f"Suggested reply: {scrub(job['suggestedReply'], ASK_NEEDS_CLIP)}\n\n"
        body += "Answer in a comment here and it goes to that session." + (f" [Open in Claude]({link})" if link else "")
        if self.dry:
            log.info("dry run: would ask on #%s for session %s: %s", tid, short, clip(needs, 120))
            return
        data = self.board.task(tid)
        if not data:
            return
        asks = self.st.setdefault("asks", {})
        flagged = created or bool(entry and entry.get("flagged") and data["task"].get("human"))
        asks[sid] = {"task": tid, "comment": None, "since": since, "at": now(), "flagged": flagged,
                     "created": created or bool(entry and entry.get("created"))}
        self.save()                                      # on disk before the comment: never two for one episode
        try:
            self.board.comment(tid, body)
        except BoardError:
            if entry:
                asks[sid] = entry
            else:
                asks.pop(sid, None)
            self.save()
            raise
        self.ask_retry.pop(sid, None)
        if not data["task"].get("human"):
            try:
                self.board.flag_human(tid)
                asks[sid]["flagged"] = True
            except BoardError as e:
                log.error("could not set human on card #%s: %s", tid, e)
        # Routing: the card is attached to the session as an adopted one. The dispatcher did not start
        # it, so `@claude stop` only detaches it, it stays outside the --max cap and the idle stop, and
        # a failed delivery is counted like any adopted session's; `ask` adds the one exception, a
        # resume when its process is gone (deliver_pending). A card already held for the session keeps
        # its entry and only gets `ask`.
        card = self.st["cards"].get(tid)
        if card is None:
            card = self.st["cards"][tid] = {
                "key": card_key(data["task"]), "name": name, "cwd": cwd, "status": "attached", "short": short,
                "session_id": sid, "adopted": True, "kind": "background", "since": now(), "updated": False,
                "warned": False, "pending": [], "failures": 0, "link": link}
        card["ask"] = True
        after = self.board.task(tid) or {"comments": []}  # add_comment does not return the comment's id
        asks[sid]["comment"] = max((c["id"] for c in after["comments"] if c["author"] == AUTHOR), default=None)
        rec = self.st.setdefault("ask_cards", {}).setdefault(sid, {"task": tid, "created": created})
        rec.update(task=tid, at=now())
        self.save()
        log.info("asked on #%s for waiting session %s", tid, short)

    def end_ask(self, sid):
        """The session no longer waits: drop the episode, take the human flag off when the dispatcher
        set it, and take the card out of the Inbox. A real agent answer Dave has not seen stays put."""
        entry = self.st["asks"][sid]
        tid = str(entry.get("task"))
        if self.dry:
            log.info("dry run: would end the ask on #%s", tid)
            return
        try:
            data = self.board.task(tid)
        except BoardError as e:
            log.warning("ask on #%s not ended yet: %s", tid, e)
            return                                       # the next pass tries again
        del self.st["asks"][sid]
        card = self.st["cards"].get(tid)
        if card and card.get("session_id") == sid:
            card.pop("ask", None)
        self.save()
        if not data:
            return
        last = max(data["comments"], key=lambda c: c["id"], default=None)
        if last and last["author"] not in (DAVE, AUTHOR):
            log.info("ask on #%s ended; an agent answered last, so the card stays in the Inbox", tid)
            return
        if entry.get("flagged") and data["task"].get("human") and data["task"].get("status") != "done":
            try:
                self.board.flag_human(tid, False)
            except BoardError as e:
                log.error("could not clear human on card #%s: %s", tid, e)
        self.board.clear_inbox([tid])
        log.info("ask on #%s ended", tid)

    def forget_ask_cards(self):
        """Drop the remembered ask card of a session not seen asking for KEEP_FOR."""
        cards, open_ = self.st.get("ask_cards", {}), self.st.get("asks", {})
        old = [sid for sid, r in cards.items() if sid not in open_ and r.get("at", 0) < now() - KEEP_FOR]
        for sid in old:
            del cards[sid]
        if old:
            self.save()

    # status.json: what the board shows (the contract is agent/dispatch/README.md, "What the board shows")
    def publish(self):
        """Every pass, as a heartbeat; `rev` moves only when something besides `updated` did."""
        if self.dry or self.st is None:
            return
        try:
            body = self.status()
            key = json.dumps(body, sort_keys=True)
            if key != self.shown:
                self.rev, self.shown = self.rev + 1, key
            write_private(self.status_path, {"version": 1, "updated": iso(now()), "rev": self.rev, **body})
        except (BoardError, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as e:
            log.warning("status.json not written: %s", e)

    def status(self):
        st, claude = self.st, self.claude
        listed = self.listing()                          # read this pass already, else at most every `refresh` s
        agents = sorted((a for a in listed or [] if a.get("sessionId")),
                        key=lambda a: (a.get("startedAt") or 0, a["sessionId"]))
        mine = {c["session_id"]: c for c in st["cards"].values() if c.get("session_id")}
        detached = st.get("detached", {})
        budget, sessions, best, needs = SCAN_BUDGET, {}, {}, {}
        for a in agents:
            sid = a["sessionId"]
            short = a.get("id") or sid[:8]
            job, card = claude.job(short), mine.get(sid)
            waits = self.asks_on and asking(a, job)      # listed even with no process: it waits on Dave
            if not live(a) and not waits:
                continue
            if live(a):                                  # an exited session owns only its ask card
                writes, used = claude.transcripts.writes(sid, budget)
                budget = max(budget - used, 0)
                for n, at in writes.items():             # latest writer wins, a detached one never
                    if at > best.get(str(n), (0,))[0] and sid not in detached.get(str(n), []):
                        best[str(n)] = (at, sid)
            if waits and str(job.get("needs") or "").strip():
                needs[sid] = job["needs"]
            tokens = job.get("tokens")
            sessions[sid] = {
                "id": short, "session_id": sid, "name": a.get("name") or job.get("name") or "",
                "kind": "interactive" if a.get("kind") == "interactive" else "background",
                "state": "blocked" if waits else session_state(a, job),
                "detail": scrub(needs[sid], 160) if sid in needs else clip(" ".join(str(job.get("detail") or "").split()), 160),
                "folder": a.get("cwd") or "",
                "started": iso(a["startedAt"] / 1000) if isinstance(a.get("startedAt"), (int, float)) else "",
                "tokens": tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else None,
                "link": remote_url(job.get("bridgeSessionId")) or (card or {}).get("link") or "",
                "origin": ("adopted" if card.get("adopted") else "dispatch") if card else "other",
                "cards": []}
        claude.transcripts.keep(set(sessions))
        done = self.done_cards([int(t) for t in best if t not in st["cards"]])
        owner = {t: sid for t, (at, sid) in best.items() if int(t) not in done}
        for t, c in st["cards"].items():                 # the dispatcher's own session comes first
            if c.get("status") == "starting":
                owner.pop(t, None)
            elif c.get("session_id") in sessions:
                owner[t] = c["session_id"]
        asked = {str(e.get("task")): sid for sid, e in st.get("asks", {}).items()
                 if self.asks_on and sid in sessions and sessions[sid]["state"] == "blocked"}
        owner.update(asked)                              # the ask's card belongs to the waiting session
        for t, sid in sorted(owner.items(), key=lambda x: int(x[0])):
            sessions[sid]["cards"].append(int(t))
        current = {}                                     # a session works on one card: its latest write
        for t, sid in owner.items():
            if t in best and best[t][1] == sid and best[t][0] >= best.get(current.get(sid), (0,))[0]:
                current[sid] = t
        queued = {q["task"] for q in st["queue"]}
        recs = st.setdefault("receipts", {})
        stale = [t for t, r in recs.items()
                 if r.get("touched", 0) < now() - KEEP_FOR and t not in st["cards"] and t not in queued]
        for t in stale:
            del recs[t]
        if stale:
            self.save()
        cards = {}
        for t in set(owner) | queued | set(st["cards"]) | set(recs):
            c, rec, sid = st["cards"].get(t) or {}, recs.get(t, {}), owner.get(t)
            sticky, note = rec.get("state"), None
            if t in asked:
                state = "blocked"
                note = scrub(needs[asked[t]], 200) if asked[t] in needs else ASK_EMPTY
            elif t in queued:
                state = "queued"
            elif c.get("status") == "starting":
                state = "starting"
            elif sticky == "silent" or (sticky == "unreachable" and sid):
                state = sticky
            elif sid:
                state = sessions[sid]["state"]
                if state == "working" and t not in st["cards"] and current.get(sid) != t:
                    state = "idle"                       # it owns this card too, but is busy on another
            else:
                state = sticky or "ended"
            seen = self.since.get(t)
            if not seen or seen[0] != state:
                seen = self.since[t] = (state, now())
            cards[t] = {"session": sessions[sid]["id"] if sid else "", "origin": sessions[sid]["origin"] if sid else "",
                        "state": state, "since": iso(seen[1]),
                        "note": note if note is not None else rec.get("note", "") if state == sticky else "",
                        "comments": {i: {"state": r["state"], "at": iso(r["at"]), "note": r.get("note", "")}
                                     for i, r in rec.get("comments", {}).items()}}
        self.since = {t: v for t, v in self.since.items() if t in cards}
        push = self.push.status() if self.push else {"on": False, "subscriptions": 0, "last_sent": "", "last_error": ""}
        return {"interval": self.interval, "max": self.max, "sessions": list(sessions.values()), "cards": cards,
                "push_key": self.push.public if self.push else "", "push": push,
                "asks": {"on": bool(self.asks_on),
                         "open": sum(1 for e in st.get("asks", {}).values() if e.get("at")) if self.asks_on else 0},
                "queue": [{"task": int(q["task"]), "comment": q["comment"], "at": iso(q.setdefault("at", now()))}
                          for q in st["queue"]]}

    def done_cards(self, ids):
        """Which of these cards are done: one read-only query on the board's database (`--db`), not
        one HTTP call per card."""
        if not ids:
            return set()
        try:
            con = sqlite3.connect(f"file:{urllib.parse.quote(self.db_path)}?mode=ro", uri=True, timeout=5)
            try:
                done = set()
                for i in range(0, len(ids), 500):
                    part = ids[i:i + 500]
                    done.update(r[0] for r in con.execute(
                        f"SELECT id FROM tasks WHERE status = 'done' AND id IN ({','.join('?' * len(part))})", part))
                return done
            finally:
                con.close()
        except sqlite3.Error as e:
            log.warning("cannot read card statuses from %s: %s", self.db_path, e)
            return set()


def main(argv=None):
    raw_key = take_push_key()                            # first: nothing below, and no child, sees it in the environment
    p = argparse.ArgumentParser(description="Start a Claude session from a card when Dave comments on it.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="one pass, then exit")
    mode.add_argument("--loop", action="store_true", help="keep watching")
    p.add_argument("--interval", type=float, default=5, help="seconds between passes (default 5)")
    p.add_argument("--dry-run", action="store_true", help="log what it would do; no board writes, no sessions")
    p.add_argument("--board", default="http://127.0.0.1:8111", help="board URL (loopback)")
    home = os.environ.get("KANZO_HOME") or os.path.expanduser("~/.local/share/kanzo")
    p.add_argument("--state", default=os.path.join(home, "dispatch", "state.json"))
    p.add_argument("--status", help="status.json for the board (default: beside --state)")
    p.add_argument("--db", default=os.path.join(home, "kanzo.db"),
                   help="read-only: the highest comment id on the first run, done cards for status.json")
    p.add_argument("--claude", default="claude", help="claude binary")
    p.add_argument("--herdr", default=os.path.expanduser("~/.local/bin/herdr"), help="herdr binary")
    p.add_argument("--dev-root", default=os.path.expanduser("~/dev"))
    p.add_argument("--max", type=int, default=3, help="sessions working at once (default 3)")
    p.add_argument("--burst", type=int, default=5,
                   help="comments on more cards than this in one pass are held as a script (default 5, 0 off)")
    p.add_argument("--idle-stop", type=float, default=IDLE_STOP,
                   help="stop a dispatcher session idle this many seconds on a done card (default 3600, 0 off)")
    p.add_argument("--no-push", action="store_true", help=f"send no Web Push, even with {PUSH_KEY_ENV} set")
    p.add_argument("--no-asks", action="store_true", help="do not put a waiting background session's ask on a card")
    p.add_argument("--ask-after", type=float, default=ASK_AFTER,
                   help=f"seconds a background session waits on Dave before the ask goes on a card (default {ASK_AFTER})")
    p.add_argument("--ask-project", default=ASK_PROJECT,
                   help=f"project for a new ask card when no project's repo matches the folder (default {ASK_PROJECT})")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    push = Push(raw_key, a.db, off="--no-push" if a.no_push else None)
    del raw_key
    d = Dispatcher(Board(a.board), Claude(a.claude), a.state, os.path.abspath(a.dev_root), a.db,
                   max_working=a.max, dry_run=a.dry_run, herdr=Herdr(a.herdr), status_path=a.status,
                   interval=int(a.interval) if a.interval == int(a.interval) else a.interval,
                   burst=a.burst, idle_stop=a.idle_stop, push=push, asks=not a.no_asks, ask_after=a.ask_after,
                   ask_project=a.ask_project)
    while True:
        try:
            d.tick()
        except BoardError as e:
            log.warning("board not reachable: %s", e)   # next pass tries again
            if a.once:
                return 1
        except sqlite3.Error as e:
            log.error("first run cannot read the highest comment id from %s: %s", a.db, e)
            return 1                                     # never start without it: old comments would replay
        if a.once:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
