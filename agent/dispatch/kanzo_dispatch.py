#!/usr/bin/env python3
# Version: v3 · created 2026-11-21 13:26 EST · updated 2026-11-23 14:59 EST
# Changes: v1 watch the board for Dave's @claude comments and run one background Claude session per card.
#          v2 2026-11-23 any comment by Dave starts or reaches the session; "[note]" comments are skipped.
#          v3 2026-11-23 a comment goes to the live session already working the card (background or a
#             herdr pane); a new agent starts only when nobody is; `@claude new` forces one.
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

Reads: the board's loopback HTTP API, live sessions' transcripts under ~/.claude/projects, and
once, on the very first run, the highest comment id from the database file (read-only), so old
comments are never replayed.
Runs: `claude --bg --name <name> <brief>`, `claude agents --json --all`, `claude stop <id>`,
`claude attach <id>` inside a private tmux to type a follow-up into an idle background session,
and `herdr agent list` / `herdr agent prompt <pane> <text>` for a session in a terminal pane.

    kanzo_dispatch.py --once                 # one pass
    kanzo_dispatch.py --loop --interval 5    # the service
    kanzo_dispatch.py --once --dry-run       # say what it would do, change nothing
"""
import argparse
import datetime
import glob
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
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
NOTE = re.compile(r"^\s*\[note\]", re.I)          # the board's "do not send to agent" box
BG_ID = re.compile(r"backgrounded\W+([0-9a-f]{8})")
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSW|CREDENTIAL|API_?KEY|BEARER|COOKIE|BWS_", re.I)
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

log = logging.getLogger("kanzo-dispatch")


def now():
    return time.time()


def clip(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[: n - 3] + "..."


def scrub(text):
    """One short line of an error, with anything token-shaped taken out."""
    line = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "no output")
    return clip(LONG_OPAQUE.sub("[redacted]", line), 160)


def child_env():
    """The environment for claude and tmux: everything but secret-named variables."""
    return {k: v for k, v in os.environ.items() if not SECRET_NAME.search(k)}


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

    def flag_human(self, task_id):
        self.tool("update_task", {"id": int(task_id), "human": True})


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


def last_write(path, task_id):
    """When this transcript last wrote to card `task_id` through a kanzo MCP write that
    succeeded (epoch seconds), or 0. The parsing follows tools/activity-sign/sign.py; lines
    without a kanzo call are skipped before any json parsing, since transcripts get large."""
    last, pending = 0, {}
    try:
        f = open(path, errors="replace")
    except OSError:
        return 0
    with f:
        for line in f:
            if "mcp__kanzo__" not in line and not (pending and "tool_result" in line):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            content = (entry.get("message") or {}).get("content")
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
                    if ref is None:                      # create_task, next_task: the reply names the card
                        reply = b.get("content")
                        if isinstance(reply, list):
                            reply = " ".join(x.get("text", "") for x in reply if isinstance(x, dict))
                        ref = str(reply or "")[:400]
                    if card_number(ref) == int(task_id):
                        last = max(last, stamp(at or entry.get("timestamp")))
    return last


class Claude:
    def __init__(self, binary="claude", home=None, tmux="tmux", sock=None):
        self.bin = shutil.which(binary) or binary
        self.home = home or os.path.expanduser("~")
        self.tmux_bin = tmux
        self.sock = sock or os.path.join(tempfile.gettempdir(), f"kanzo-dispatch-{os.getuid()}", "tmux.sock")
        self._agents = None

    def run(self, args, cwd=None, timeout=30):
        return subprocess.run([self.bin, *args], cwd=cwd, env=child_env(), capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def start(self, cwd, name, prompt):
        """-> (short id, None) or (None, short error)."""
        self._agents = None                              # the list changes: read it again next time
        try:
            r = self.run(["--bg", "--name", name, prompt], cwd=cwd, timeout=START_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            return None, f"claude did not run ({type(e).__name__})"
        out = (r.stdout or "") + "\n" + (r.stderr or "")
        m = BG_ID.search(out)
        if r.returncode != 0 or not m:
            return None, scrub(out)
        return m.group(1), None

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
            files = (glob.glob(os.path.join(self.home, ".claude", "projects", "*", f"{sid}.jsonl"))
                     + glob.glob(os.path.join(self.home, ".claude", "projects", "*", sid, "subagents", "*.jsonl")))
            at = max((last_write(p, task_id) for p in files), default=0)
            if at > best_at:
                best, best_at = a, at
        return best

    def job(self, short):
        try:
            with open(os.path.join(self.home, ".claude", "jobs", short, "state.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

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
    ask = TRIGGER.sub("", NEW.sub("", ask, count=1), count=1).strip() or "(see the card and its comments)"
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


def follow_up(key, comments, title=None):
    """One line. `title` is for an adopted session, which may work several cards."""
    on = f"{key} ({' '.join(title.split())})" if title else key
    parts = [f"Dave commented on card {on}: {clip(c['body'], COMMENT_CLIP)}" for c in comments]
    return " | ".join(parts) + " (Answer on the card with the kanzo tools; he reads only the card.)"


# ---------------------------------------------------------------- the dispatcher

class Dispatcher:
    def __init__(self, board, claude, state_path, dev_root, db_path, max_working=3, dry_run=False,
                 deliver_timeout=DELIVER_TIMEOUT, start_grace=START_GRACE, link_wait=LINK_WAIT, herdr=None):
        self.board, self.claude, self.herdr = board, claude, herdr or Herdr()
        self.state_path, self.dev_root, self.db_path = state_path, dev_root, db_path
        self.max, self.dry = max_working, dry_run
        self.deliver_timeout, self.start_grace, self.link_wait = deliver_timeout, start_grace, link_wait
        self.failure = {"cause": None, "count": 0}
        self.halted = None              # in memory only: a restart is the reset
        self.st = self.load()

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
                self.say(tid, "A start for this card was cut off by a dispatcher restart and no session "
                              "showed up. Comment again to retry.", human=True)
            self.save()

    # one pass
    def tick(self):
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
            if card and r.get("actor") not in (BOARD_ACTOR, AUTHOR):
                card["updated"] = True                   # an agent wrote or moved the card
            if r.get("action") == "commented" and tid not in commented:
                commented.append(tid)
        new = []
        for tid in commented:
            data = self.board.task(tid)
            for c in (data or {}).get("comments", []):
                if c["id"] > st["comment_seen"]:
                    new.append((c, data))
        for c, data in sorted(new, key=lambda x: x[0]["id"]):
            st["comment_seen"] = max(st["comment_seen"], c["id"])
            self.save()                                  # seen before acted: at most once
            if c["author"] == DAVE:
                self.handle(c, data)
        if rows:
            st["activity_seen"] = max(st["activity_seen"], rows[-1]["id"])
            self.save()
        self.deliver_pending()
        self.watch()
        self.start_queued()

    def handle(self, c, data):
        t = data["task"]
        tid, body = str(t["id"]), c["body"]
        card = self.st["cards"].get(tid)
        queued = any(q["task"] == tid for q in self.st["queue"])
        if NOTE.match(body):                             # a note to self: never reaches an agent
            return
        if STOP.match(body):
            if card and card.get("adopted"):             # not ours to stop: let it go
                self.detach(tid, card)
                self.say(tid, f"Detached session `{card['name']}` from this card. The dispatcher did not "
                              f"start it, so it was not stopped and keeps running. Your next comment here "
                              f"starts a new agent.")
            elif card:
                self.stop_card(tid, card)
                self.say(tid, "Stopped the session for this card.")
            elif queued:
                self.st["queue"] = [q for q in self.st["queue"] if q["task"] != tid]
                self.save()
                self.say(tid, "Taken off the queue; no session was started.")
            else:
                self.say(tid, "No session is attached to this card.")
            return
        if NEW.match(body):                              # a separate agent, whoever else is on the card
            if card and card.get("adopted"):
                self.detach(tid, card)
            elif card:
                self.stop_card(tid, card)
            if not queued:
                self.request(tid, c["id"])
            return
        if card:                                         # one session per card, done or not
            card.setdefault("pending", []).append(c["id"])
            card.setdefault("pending_since", now())
            self.save()
            return
        if queued:
            return
        self.route(tid, c["id"])

    def stop_card(self, tid, card):
        """Stop a session the dispatcher started; one it adopted is only let go."""
        if card.get("short") and not card.get("adopted") and not self.dry:
            ok, why = self.claude.stop(card["short"])
            if not ok:
                log.warning("claude stop %s: %s", card["short"], why)
        self.st["cards"].pop(tid, None)
        self.save()

    def detach(self, tid, card):
        """Let an adopted session go, and keep it from being adopted for this card again."""
        if self.dry:
            return
        gone = self.st.setdefault("detached", {}).setdefault(tid, [])
        if card.get("session_id") and card["session_id"] not in gone:
            gone.append(card["session_id"])
        self.st["cards"].pop(tid, None)
        self.save()

    def route(self, tid, comment_id, why=None, skip=()):
        """Your comment goes to whoever is working the card; if nobody is, a new agent starts."""
        skip = set(skip) | set(self.st.get("detached", {}).get(tid, []))
        owner = self.claude.owner(int(tid), skip)
        if owner == "unknown":
            self.say(tid, "Could not list the Claude sessions to check whether one is already on this card, "
                          "so nothing was started. Comment again to retry.", human=True)
        elif owner:
            self.adopt(tid, owner, comment_id)
        else:
            self.request(tid, comment_id, why)

    def adopt(self, tid, agent, comment_id):
        """Attach a session the dispatcher did not start, then deliver through the usual path."""
        data = self.board.task(tid)
        if not data:
            return
        short, sid, name = agent.get("id"), agent.get("sessionId"), agent.get("name") or agent.get("id")
        kind = "interactive" if agent.get("kind") == "interactive" else "background"
        if self.dry:
            log.info("dry run: would send the comment on %s to %s session %s (%s)", card_key(data["task"]),
                     kind, short, name)
            return
        card = {"key": card_key(data["task"]), "name": name, "cwd": agent.get("cwd"), "status": "attached",
                "short": short, "session_id": sid, "adopted": True, "kind": kind, "since": now(),
                "updated": False, "warned": False, "pending": [comment_id], "pending_since": now(),
                "failures": 0, "announce": True,
                "link": self.claude.session_and_link(short, 0)[1] if kind == "background" and short else None}
        if kind == "interactive":
            pane = self.herdr.pane_for(sid, name)
            card["pane"] = pane.get("pane_id") if pane else None
        self.st["cards"][tid] = card
        self.save()
        log.info("adopted %s session %s for %s", kind, short, card["key"])

    def unreachable(self, tid, card, why):
        log.warning("could not reach %s for %s: %s", card.get("short"), card["key"], why)
        self.st["cards"].pop(tid, None)
        self.save()
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

    def request(self, tid, comment_id, why=None):
        if self.halted:
            self.say(tid, f"Not started: the dispatcher stopped starting sessions after two failures in a row "
                          f"({self.halted}). It retries after a fix and a restart of kanzo-dispatch.", human=True)
            return
        if self.working_count() >= self.max:
            self.st["queue"].append({"task": tid, "comment": comment_id, "why": why})
            self.save()
            self.say(tid, f"Queued: {self.max} sessions are already working. This card starts when one finishes.")
            return
        self.start(tid, comment_id, why)

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

    def start(self, tid, comment_id, why=None):
        data = self.board.task(tid)
        if not data:
            return
        t = data["task"]
        cwd, problem = self.folder(t["project"])
        if problem:
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
            self.fail(tid, err)
            return
        self.failure = {"cause": None, "count": 0}
        sid, link = self.claude.session_and_link(short, self.link_wait)
        card.update(status="attached", short=short, session_id=sid, link=link)
        self.save()
        log.info("started %s for %s in %s", short, card["key"], cwd)
        lead = f"Picked up ({why}). " if why else "Picked up. "
        self.say(tid, f"{lead}Session `{name}` (id {short}) in {cwd}. "
                      + (f"App: {link}" if link else "The app link was not ready yet; `claude agents` lists it."))

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
        self.say(tid, msg, human=True)

    def replace(self, tid, card, why):
        """The card's session is gone or would not take the comment: whoever else works the card
        gets it, or a new one from the full history. The dropped session is never re-adopted."""
        self.stop_card(tid, card)
        first = (card.get("pending") or [card.get("comment")])[0]
        self.route(tid, first, why, skip={card.get("session_id")})

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
            if not alive(agent):
                self.replace(tid, card, "the earlier session was gone")
                continue
            go, pane, why = self.ready(card, agent)
            if not go and card.get("adopted") and why == "no herdr pane runs it":
                ok = False                               # nothing to wait for: a failed try
            elif go:
                data = self.board.task(tid) or {"task": {}, "comments": []}
                comments = [c for c in data["comments"] if c["id"] in card["pending"]]
                if self.dry:
                    log.info("dry run: would deliver %d comment(s) to %s", len(comments), card["short"])
                    continue
                text = follow_up(card["key"], comments, data["task"].get("title") if card.get("adopted") else None)
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
                card["pending"] = []
                card.pop("pending_since", None)
                self.save()

    def watch(self):
        for tid, card in list(self.st["cards"].items()):
            if card.get("status") != "attached":
                continue
            agent = self.claude.find(card)
            if agent != "unknown" and card.get("adopted"):  # not started for this card: no watchdog
                if not alive(agent) and not card.get("pending"):
                    self.st["cards"].pop(tid, None)
                    self.save()
                continue
            if agent == "unknown" or working(agent) or now() - card.get("since", 0) < self.start_grace:
                continue
            if not card.get("updated") and not card.get("warned"):
                what = ("is waiting on a permission or a question in the app" if blocked(agent)
                        else "ended without an update")
                self.say(tid, f"The session {what}; nothing on this card from it since pickup. "
                              f"App: {card.get('link') or card.get('short')}", human=True)
                card["warned"] = True
                self.save()
            if not alive(agent) and not card.get("pending"):
                self.st["cards"].pop(tid, None)          # gone: the slot is free
                self.save()

    def start_queued(self):
        while self.st["queue"] and not self.halted and self.working_count() < self.max:
            item = self.st["queue"].pop(0)
            self.save()
            if item["task"] not in self.st["cards"]:
                self.start(item["task"], item["comment"], item.get("why") or "from the queue")


def main(argv=None):
    p = argparse.ArgumentParser(description="Start a Claude session from a card when Dave comments on it.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="one pass, then exit")
    mode.add_argument("--loop", action="store_true", help="keep watching")
    p.add_argument("--interval", type=float, default=5, help="seconds between passes (default 5)")
    p.add_argument("--dry-run", action="store_true", help="log what it would do; no board writes, no sessions")
    p.add_argument("--board", default="http://127.0.0.1:8111", help="board URL (loopback)")
    home = os.environ.get("KANZO_HOME") or os.path.expanduser("~/.local/share/kanzo")
    p.add_argument("--state", default=os.path.join(home, "dispatch", "state.json"))
    p.add_argument("--db", default=os.path.join(home, "kanzo.db"), help="read once, read-only, on the first run")
    p.add_argument("--claude", default="claude", help="claude binary")
    p.add_argument("--herdr", default=os.path.expanduser("~/.local/bin/herdr"), help="herdr binary")
    p.add_argument("--dev-root", default=os.path.expanduser("~/dev"))
    p.add_argument("--max", type=int, default=3, help="sessions working at once (default 3)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    d = Dispatcher(Board(a.board), Claude(a.claude), a.state, os.path.abspath(a.dev_root), a.db,
                   max_working=a.max, dry_run=a.dry_run, herdr=Herdr(a.herdr))
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
