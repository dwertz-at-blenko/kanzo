#!/usr/bin/env python3
# Version: v5 · created 2026-11-21 13:26 EST · updated 2026-11-28
# Changes: v1 tests for kanzo-dispatch against a throwaway board and a fake claude.
#          v2 2026-11-23 owner tests: adopt the live session already on a card, a fake herdr for panes.
#          v3 2026-11-24 status.json, receipts, nudge and flag, burst guard, resume (the fake claude
#             takes `--bg --resume`), idle stop, `@claude start`, pictures in a follow-up.
#          v4 2026-11-24 Web Push against a fake push service on loopback (class Push); the fake claude
#             records which key-like variable names it inherited.
#          v5 2026-11-28 asks (class Asks): blocked background sessions, their card, flag, inbox clear, resume.
# This file: /home/claude/dev/kanzo/agent/dispatch/test_dispatch.py (repo dwertz-at-blenko/kanzo, branch public-url)
# Session: b1475961-06d7-40b0-9eff-f2b47896b6bb · vps as claude · cwd /home/claude/dev
"""kanzo-dispatch against a real, throwaway board and a fake claude.

The board is this worktree's debug build (built when missing), on 127.0.0.1:8198 (or
$KANZO_TEST_PORT) with a temp --home, stopped at the end. The fake claude keeps its sessions in a
JSON file the tests edit to play idle, busy, blocked or gone, and its `attach` prints the prompt,
reads one line and writes it to the session's transcript, like the real TUI does. The fake herdr
lists panes from panes.json and its `agent prompt` writes to the transcript the same way.

    KANZO_TEST_PORT=8196 python3 -W error::ResourceWarning agent/dispatch/test_dispatch.py
"""
import base64
import functools
from http import client as http_client
from http import server as http_server
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True                           # no __pycache__ left in the repo
import kanzo_dispatch as kd  # noqa: E402

PORT = int(os.environ.get("KANZO_TEST_PORT", "8198"))
URL = f"http://127.0.0.1:{PORT}"

FAKE = r'''#!PYTHON
import json, os, sys
d = os.environ["FAKE_DIR"]
reg = os.path.join(d, "agents.json")
args = sys.argv[1:]
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": args, "cwd": os.getcwd(),
                        "env": sorted(k for k in os.environ if "VAPID" in k or "PRIVATE" in k)}) + "\n")
agents = json.load(open(reg)) if os.path.exists(reg) else []
def save():
    json.dump(agents, open(reg, "w"))
mode = open(os.path.join(d, "mode")).read().strip() if os.path.exists(os.path.join(d, "mode")) else "ok"
if "--bg" in args and "--resume" in args:
    # `claude --bg --resume <session id> <prompt>`: the same session runs again with the prompt.
    sid = args[args.index("--resume") + 1]
    a = next((a for a in agents if a["sessionId"] == sid), None)
    if mode in ("fail", "noresume") or a is None:
        print("Error: No conversation found with session ID: " + sid); sys.exit(1)
    a.update(pid=os.getppid(), status="busy", state="working")
    with open(os.path.join(d, ".claude", "projects", "fake", sid + ".jsonl"), "a") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": args[-1]}}) + "\n")
    save()
    print("Session backgrounded · " + a["id"])
elif "--bg" in args:
    if mode == "fail":
        print("Error: not logged in. Run /login"); sys.exit(1)
    short = "%08x" % (0xabc00000 + len(agents))
    sid = "sid-" + short
    name = args[args.index("--name") + 1]
    os.makedirs(os.path.join(d, ".claude", "jobs", short), exist_ok=True)
    json.dump({"sessionId": sid, "bridgeSessionId": "cse_TEST" + short, "state": "working", "name": name,
               "detail": "reading the card", "tokens": 1234},
              open(os.path.join(d, ".claude", "jobs", short, "state.json"), "w"))
    os.makedirs(os.path.join(d, ".claude", "projects", "fake"), exist_ok=True)
    open(os.path.join(d, ".claude", "projects", "fake", sid + ".jsonl"), "a").close()
    # the test process is the parent, so the pid is live and the session counts as running
    agents.append({"id": short, "sessionId": sid, "kind": "background", "pid": os.getppid(), "status": "busy",
                   "state": "working", "name": name, "cwd": os.getcwd(), "startedAt": 1795000000000})
    save()
    print("Session backgrounded · " + short)
elif args[:1] == ["agents"]:
    print(json.dumps(agents))
elif args[:1] == ["stop"]:
    for a in agents:
        if a["id"] == args[1]:
            a.update(pid=None, status=None, state="done")
    save()
elif args[:1] == ["attach"]:
    a = next(a for a in agents if a["id"] == args[1])
    print("❯", flush=True)
    line = sys.stdin.readline().strip()
    with open(os.path.join(d, ".claude", "projects", "fake", a["sessionId"] + ".jsonl"), "a") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": line}}) + "\n")
'''

# herdr: panes come from panes.json; `agent prompt` writes the text to that session's transcript.
FAKE_HERDR = r'''#!PYTHON
import json, os, sys
d = os.environ["FAKE_DIR"]
args = sys.argv[1:]
with open(os.path.join(d, "herdr_calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": args}) + "\n")
path = os.path.join(d, "panes.json")
panes = json.load(open(path)) if os.path.exists(path) else []
if args[:2] == ["agent", "list"]:
    print(json.dumps({"result": {"agents": panes}}))
elif args[:2] == ["agent", "prompt"]:
    p = next((p for p in panes if p["pane_id"] == args[2]), None)
    if not p:
        print("error: pane not found", file=sys.stderr); sys.exit(1)
    sid = p["agent_session"]["value"]
    with open(os.path.join(d, ".claude", "projects", "fake", sid + ".jsonl"), "a") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": args[3]}}) + "\n")
'''


def http(method, path, body=None):
    req = urllib.request.Request(URL + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = REPO / "target" / "debug" / "kanzo"
        if not binary.exists():
            env = dict(os.environ, PATH=f"{os.path.expanduser('~/.cargo/bin')}:{os.environ['PATH']}")
            subprocess.run(["cargo", "build"], cwd=REPO, env=env, check=True)
        cls.tmp = tempfile.mkdtemp(prefix="kanzo-dispatch-test-")
        cls.kanzo_home = os.path.join(cls.tmp, "board")
        os.makedirs(cls.kanzo_home)
        cls.board_proc = subprocess.Popen([str(binary), "--home", cls.kanzo_home, "--port", str(PORT), "--no-open"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                urllib.request.urlopen(URL + "/api/ping", timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("the test board did not start")
        cls.dev = os.path.join(cls.tmp, "dev")
        os.makedirs(os.path.join(cls.dev, "widget"))
        http("POST", "/api/projects", {"slug": "nolink", "name": "No link"})
        http("POST", "/api/projects", {"slug": "widget", "name": "Widget", "repo": "acme/widget"})
        http("POST", "/api/projects", {"slug": "ghost", "name": "Ghost", "repo": "acme/ghost"})

    @classmethod
    def tearDownClass(cls):
        cls.board_proc.terminate()
        cls.board_proc.wait(10)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.fake_dir = tempfile.mkdtemp(dir=self.tmp)
        os.environ["FAKE_DIR"] = self.fake_dir
        self.fake = os.path.join(self.fake_dir, "claude")
        with open(self.fake, "w") as f:
            f.write(FAKE.replace("PYTHON", sys.executable))
        os.chmod(self.fake, 0o755)
        self.fake_herdr = os.path.join(self.fake_dir, "herdr")
        with open(self.fake_herdr, "w") as f:
            f.write(FAKE_HERDR.replace("PYTHON", sys.executable))
        os.chmod(self.fake_herdr, 0o755)
        os.makedirs(os.path.join(self.fake_dir, ".claude", "projects", "fake"))
        self.state = os.path.join(self.fake_dir, "state", "state.json")
        self.d = self.dispatcher()
        self.d.tick()                                    # first run: the baseline

    def dispatcher(self, **kw):
        kw.setdefault("start_grace", 0)
        kw.setdefault("refresh", 0)                      # status.json reads the session list every pass
        claude = kd.Claude(self.fake, home=self.fake_dir, sock=os.path.join(self.fake_dir, "tmux.sock"))
        return kd.Dispatcher(kd.Board(URL), claude, self.state, self.dev, os.path.join(self.kanzo_home, "kanzo.db"),
                             link_wait=1, herdr=kd.Herdr(self.fake_herdr), **kw)

    def card(self, project="widget", title="Make the thing"):
        r = http("POST", "/api/tasks", {"project": project, "title": title, "description": "Done when: it works."})
        return r["id"]

    def dave(self, tid, body):
        http("POST", f"/api/tasks/{tid}/comments", {"body": body})

    def comments(self, tid):
        return http("GET", f"/api/tasks/{tid}")["comments"]

    def mine(self, tid):
        return [c["body"] for c in self.comments(tid) if c["author"] == kd.AUTHOR]

    def human(self, tid):
        return http("GET", f"/api/tasks/{tid}")["task"]["human"]

    def calls(self, verb=None):
        try:
            with open(os.path.join(self.fake_dir, "calls.jsonl")) as f:
                rows = [json.loads(ln) for ln in f]
        except FileNotFoundError:
            return []
        if verb == "--bg":
            return [r for r in rows if "--bg" in r["argv"]]
        return [r for r in rows if verb is None or r["argv"][:1] == [verb]]

    def set_agent(self, short, **fields):
        path = os.path.join(self.fake_dir, "agents.json")
        with open(path) as f:
            agents = json.load(f)
        for a in agents:
            if a["id"] == short:
                a.update(fields)
        with open(path, "w") as f:
            json.dump(agents, f)
        job = os.path.join(self.fake_dir, ".claude", "jobs", short, "state.json")
        if "state" in fields and os.path.exists(job):    # the job file says the same, as with the real CLI
            data = json.loads(pathlib.Path(job).read_text())
            data["state"] = fields["state"]
            pathlib.Path(job).write_text(json.dumps(data))

    def started(self, tid):
        self.dave(tid, "@claude please do it")
        self.d.tick()
        return self.d.st["cards"][str(tid)]["short"]

    def session(self, short, kind="background", calls=(), pid=None, status="idle", name=None):
        """A session the dispatcher did not start, listed by `claude agents` and live (its pid is
        this test's), with a transcript holding `calls`: (tool, input, reply, minute of the hour)."""
        sid = "sid-" + short
        agent = {"id": short, "sessionId": sid, "kind": kind, "pid": pid or os.getpid(), "status": status,
                 "name": name or f"own work {short}", "cwd": self.dev, "startedAt": 1}
        if kind == "background":
            agent["state"] = "done" if status == "idle" else "working"
            os.makedirs(os.path.join(self.fake_dir, ".claude", "jobs", short))
            with open(os.path.join(self.fake_dir, ".claude", "jobs", short, "state.json"), "w") as f:
                json.dump({"sessionId": sid, "bridgeSessionId": "cse_OWN" + short}, f)
        path = os.path.join(self.fake_dir, "agents.json")
        agents = json.loads(pathlib.Path(path).read_text()) if os.path.exists(path) else []
        with open(path, "w") as f:
            json.dump(agents + [agent], f)
        with open(os.path.join(self.fake_dir, ".claude", "projects", "fake", sid + ".jsonl"), "a") as f:
            f.write(json.dumps({"type": "user", "cwd": self.dev, "message": {"content": "hello"}}) + "\n")
            for n, (tool, args, reply, minute) in enumerate(calls):
                at = f"2026-11-23T10:{minute:02d}:00.000Z"
                use = {"type": "tool_use", "id": f"tu{n}", "name": f"mcp__kanzo__{tool}", "input": args}
                res = {"type": "tool_result", "tool_use_id": f"tu{n}", "content": [{"type": "text", "text": reply}]}
                f.write(json.dumps({"type": "assistant", "timestamp": at, "message": {"content": [use]}}) + "\n")
                f.write(json.dumps({"type": "user", "timestamp": at, "message": {"content": [res]}}) + "\n")
        return sid

    def pane(self, sid, pane_id="w1:p1", status="idle"):
        with open(os.path.join(self.fake_dir, "panes.json"), "w") as f:
            json.dump([{"agent": "claude", "agent_session": {"kind": "id", "value": sid}, "agent_status": status,
                        "pane_id": pane_id, "terminal_title_stripped": "x"}], f)

    def herdr_calls(self, verb):
        try:
            with open(os.path.join(self.fake_dir, "herdr_calls.jsonl")) as f:
                return [json.loads(ln)["argv"] for ln in f if json.loads(ln)["argv"][1:2] == [verb]]
        except FileNotFoundError:
            return []

    def transcript(self, sid):
        return pathlib.Path(self.fake_dir, ".claude", "projects", "fake", f"{sid}.jsonl").read_text()

    def status(self):
        return json.loads(pathlib.Path(self.fake_dir, "state", "status.json").read_text())

    def starts(self):
        """Fresh starts only, not resumes."""
        return [r for r in self.calls("--bg") if "--resume" not in r["argv"]]

    def resumes(self):
        return [r for r in self.calls("--bg") if "--resume" in r["argv"]]

    def receipts(self, tid):
        """{comment id: state} from status.json."""
        return {int(k): v["state"] for k, v in self.status()["cards"][str(tid)]["comments"].items()}

    def wrote(self, tid, minute=10):
        return [("update_task", {"id": f"widget#{tid}", "status": "doing"}, f"Updated widget#{tid}: x", minute)]


class Dispatch(Base):
    def test_first_run_ignores_history(self):
        tid = self.card()
        self.dave(tid, "@claude this was here before")
        os.remove(self.state)
        d = self.dispatcher()
        d.tick()                                         # first run again
        d.tick()
        self.assertEqual(self.calls("--bg"), [])
        self.assertEqual(self.mine(tid), [])

    def test_at_claude_starts_exactly_one_session(self):
        tid = self.card()
        self.dave(tid, "  @Claude look into it")
        self.d.tick()
        self.d.tick()
        self.d.tick()                                    # its own comment is now in the activity log
        starts = self.calls("--bg")
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["cwd"], os.path.join(self.dev, "widget"))
        argv = starts[0]["argv"]
        self.assertTrue(argv[argv.index("--name") + 1].startswith(f"widget#{tid} "))
        prompt = argv[-1]
        for part in (f"widget#{tid}", "Done when: it works.", "look into it", "set human", "Dave reads only the card"):
            self.assertIn(part, prompt)
        mine = self.mine(tid)
        self.assertEqual(len(mine), 1)
        self.assertTrue(mine[0].startswith("Picked up."))
        self.assertIn("https://claude.ai/code/session_TEST", mine[0])
        self.assertIn(os.path.join(self.dev, "widget"), mine[0])

    def test_unlinked_project_runs_in_dev_root(self):
        tid = self.card("nolink")
        self.dave(tid, "@claude go")
        self.d.tick()
        self.assertEqual(self.calls("--bg")[0]["cwd"], self.dev)

    def test_plain_comment_starts_a_session(self):
        tid = self.card()
        self.dave(tid, "look into why the export is slow")
        self.d.tick()
        self.assertEqual(len(self.calls("--bg")), 1)

    def test_note_starts_nothing_and_is_not_delivered(self):
        tid = self.card()
        self.dave(tid, "[note] reminder to self, not for an agent")
        self.d.tick()
        self.assertEqual(self.calls("--bg"), [])
        self.assertEqual(self.mine(tid), [])
        self.started(tid)
        self.dave(tid, " [Note] another one for the record")
        self.d.tick()
        self.assertFalse(self.d.st["cards"][str(tid)].get("pending"))

    def test_follow_up_goes_to_the_attached_session(self):
        tid = self.card()
        short = self.started(tid)
        self.set_agent(short, status="idle", state="done")
        self.dave(tid, "also check the second\nline")
        self.d.tick()
        transcript = pathlib.Path(self.fake_dir, ".claude", "projects", "fake", f"sid-{short}.jsonl").read_text()
        self.assertIn(f"Dave commented on card widget#{tid}: also check the second line", transcript)
        self.assertEqual(len(self.calls("--bg")), 1)
        self.assertEqual(self.d.st["cards"][str(tid)]["pending"], [])

    def test_follow_up_waits_while_busy(self):
        tid = self.card()
        self.started(tid)
        self.dave(tid, "one more thing")
        self.d.tick()
        self.assertEqual(self.calls("attach"), [])
        self.assertEqual(len(self.d.st["cards"][str(tid)]["pending"]), 1)

    def test_dead_session_gets_a_new_one(self):
        tid = self.card()
        short = self.started(tid)
        self.set_agent(short, pid=None, status=None, state="done")
        pathlib.Path(self.fake_dir, "mode").write_text("noresume")   # v4 resumes first; this is the fallback
        self.dave(tid, "are you there")
        self.d.tick()
        self.assertEqual(len(self.starts()), 2)
        self.assertIn("are you there", self.starts()[1]["argv"][-1])
        self.assertTrue(any("earlier session was gone" in m for m in self.mine(tid)))
        self.assertNotEqual(self.d.st["cards"][str(tid)]["short"], short)

    def test_stop(self):
        tid = self.card()
        short = self.started(tid)
        self.dave(tid, "@claude stop")
        self.d.tick()
        self.assertEqual([c["argv"] for c in self.calls("stop")], [["stop", short]])
        self.assertIn("Stopped the session for this card.", self.mine(tid))
        self.assertNotIn(str(tid), self.d.st["cards"])
        self.dave(tid, "[note] a note after the stop")
        self.d.tick()
        self.assertEqual(len(self.calls("--bg")), 1)
        self.dave(tid, "pick this back up")
        self.d.tick()
        self.assertEqual(len(self.calls("--bg")), 2)

    def test_comment_on_a_done_card_reaches_its_session(self):
        tid = self.card()
        short = self.started(tid)
        http("PATCH", f"/api/tasks/{tid}", {"status": "done"})
        self.set_agent(short, status="idle", state="done")
        self.dave(tid, "one more change")
        self.d.tick()
        transcript = pathlib.Path(self.fake_dir, ".claude", "projects", "fake", f"sid-{short}.jsonl").read_text()
        self.assertIn("one more change", transcript)
        self.assertEqual(len(self.calls("--bg")), 1)     # never two sessions for one card

    def test_queue_at_the_cap(self):
        self.d = self.dispatcher(max_working=1)
        first, second = self.card(title="First"), self.card(title="Second")
        short = self.started(first)
        self.dave(second, "@claude and this one")
        self.d.tick()
        self.assertEqual(len(self.calls("--bg")), 1)
        self.assertTrue(self.mine(second)[0].startswith("Queued"))
        self.set_agent(short, status="idle", state="done")   # finished its turn: the slot is free
        self.d.tick()
        self.assertEqual(len(self.calls("--bg")), 2)
        self.assertIn(str(second), self.d.st["cards"])
        self.assertTrue(any(m.startswith("Picked up (from the queue)") for m in self.mine(second)))

    def test_no_checkout(self):
        tid = self.card("ghost")
        self.dave(tid, "@claude go")
        self.d.tick()
        self.assertEqual(self.calls("--bg"), [])
        self.assertIn("no checkout of acme/ghost", self.mine(tid)[0])
        self.assertTrue(self.human(tid))

    def test_start_failure_twice_stops_trying(self):
        pathlib.Path(self.fake_dir, "mode").write_text("fail")
        a, b, c = self.card(title="A"), self.card(title="B"), self.card(title="C")
        for tid in (a, b):
            self.dave(tid, "@claude go")
            self.d.tick()
        self.assertIn("Could not start a Claude session: Error: not logged in", self.mine(a)[0])
        self.assertTrue(self.human(a))
        self.assertIn("two in a row", self.mine(b)[0])
        self.dave(c, "@claude go")
        self.d.tick()
        self.assertEqual(len(self.calls("--bg")), 2)     # no third try
        self.assertTrue(self.mine(c)[0].startswith("Not started"))
        self.assertEqual(self.d.st["cards"], {})

    def test_watchdog_ended_without_update(self):
        tid = self.card()
        short = self.started(tid)
        self.set_agent(short, pid=None, status=None, state="done")
        self.d.tick()
        self.assertTrue(any("ended without an update" in m for m in self.mine(tid)))
        self.assertTrue(self.human(tid))
        self.assertNotIn(str(tid), self.d.st["cards"])

    def test_watchdog_quiet_when_the_agent_updated(self):
        tid = self.card()
        short = self.started(tid)
        kd.Board(URL, client="claude-code").comment(tid, "working on it")
        self.set_agent(short, status="idle", state="done")
        self.d.tick()
        self.assertFalse(any("without an update" in m for m in self.mine(tid)))
        self.assertFalse(self.human(tid))

    def test_watchdog_blocked_session(self):
        tid = self.card()
        short = self.started(tid)
        self.set_agent(short, status="idle", state="blocked")
        self.d.tick()
        self.assertTrue(any("waiting on a permission" in m for m in self.mine(tid)))

    def test_restart_does_not_double_start(self):
        tid = self.card()
        self.started(tid)
        d2 = self.dispatcher()
        d2.tick()
        d2.tick()
        self.assertEqual(len(self.calls("--bg")), 1)

    def test_crash_mid_start_does_not_start_again(self):
        tid = self.card()
        self.dave(tid, "@claude go")

        class Crash(Exception):
            pass

        def crash(*a):
            raise Crash()
        self.d.claude.start = crash
        with self.assertRaises(Crash):
            self.d.tick()                                # comment seen and intent saved, then the crash
        d2 = self.dispatcher()
        d2.tick()
        d2.tick()
        self.assertEqual(self.calls("--bg"), [])
        self.assertTrue(any("cut off by a dispatcher restart" in m for m in self.mine(tid)))
        self.assertEqual(d2.st["cards"], {})

    def test_own_comments_and_agent_comments_never_trigger(self):
        tid = self.card()
        kd.Board(URL).comment(tid, "@claude from the dispatcher itself")
        kd.Board(URL, client="claude-code").comment(tid, "@claude from an agent")
        self.d.tick()
        self.assertEqual(self.calls("--bg"), [])
        authors = {c["author"] for c in self.comments(tid)}
        self.assertEqual(authors, {"kanzo-dispatch", "claude-code"})

    def test_dry_run_changes_nothing(self):
        tid = self.card()
        d = self.dispatcher(dry_run=True)
        self.dave(tid, "@claude go")
        d.tick()
        self.assertEqual(self.calls("--bg"), [])
        self.assertEqual(self.mine(tid), [])


class Owner(Base):
    """A comment goes to whoever is working the card; if nobody is, a new agent starts."""

    def test_background_owner_is_adopted_and_gets_the_comment(self):
        tid = self.card()
        sid = self.session("0be00001", calls=self.wrote(tid))
        self.dave(tid, "also check the export")
        self.d.tick()
        self.assertEqual(self.calls("--bg"), [])                 # no second agent
        self.assertEqual([c["argv"] for c in self.calls("attach")], [["attach", "0be00001"]])
        self.assertIn(f"Dave commented on card widget#{tid} (Make the thing): also check the export",
                      self.transcript(sid))
        card = self.d.st["cards"][str(tid)]
        self.assertTrue(card["adopted"])
        self.assertEqual((card["short"], card["session_id"], card["kind"]), ("0be00001", sid, "background"))
        mine = self.mine(tid)
        self.assertEqual(len(mine), 1)
        self.assertTrue(mine[0].startswith("Sent to the session already on this card: `own work 0be00001`"))
        self.assertIn("https://claude.ai/code/session_OWN0be00001", mine[0])
        self.dave(tid, "and one more")                           # later comments: same session, no new note
        self.d.tick()
        self.assertIn("and one more", self.transcript(sid))
        self.assertEqual(len(self.mine(tid)), 1)
        self.assertEqual(self.calls("--bg"), [])

    def test_interactive_owner_gets_it_through_herdr(self):
        tid = self.card()
        sid = self.session("0be00002", kind="interactive",
                           calls=[("add_comment", {"id": f"widget#{tid}", "body": "x"}, f"Commented on widget#{tid}", 5)])
        self.pane(sid, "w9:p3")
        self.dave(tid, "use the new layout\nplease")
        self.d.tick()
        self.assertEqual(self.calls("--bg"), [])
        self.assertEqual(self.calls("attach"), [])
        prompts = self.herdr_calls("prompt")
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0][2], "w9:p3")
        self.assertTrue(prompts[0][3].startswith(f"Dave commented on card widget#{tid} (Make the thing): "
                                                 f"use the new layout please"))
        self.assertNotIn("\n", prompts[0][3])
        card = self.d.st["cards"][str(tid)]
        self.assertEqual((card["kind"], card["pane"]), ("interactive", "w9:p3"))
        self.assertIn("Terminal pane w9:p3", self.mine(tid)[0])

    def test_interactive_owner_waits_while_its_pane_works(self):
        tid = self.card()
        sid = self.session("0be0000a", kind="interactive", calls=self.wrote(tid))
        self.pane(sid, status="working")
        self.dave(tid, "when you are free")
        self.d.tick()
        self.assertEqual(self.herdr_calls("prompt"), [])
        self.pane(sid, status="idle")
        self.d.tick()
        self.assertEqual(len(self.herdr_calls("prompt")), 1)

    def test_unreachable_owner_gets_a_comment_and_no_start(self):
        tid = self.card()
        self.session("0be00003", kind="interactive", calls=self.wrote(tid), name="pane work")
        self.dave(tid, "are you on this")                        # no herdr pane runs it
        self.d.tick()
        self.assertEqual(self.mine(tid), [])                     # one failed try so far
        self.d.tick()
        self.d.tick()
        mine = self.mine(tid)
        self.assertEqual(mine, ["Session `pane work` is already on this card and I could not reach it. "
                                "Comment `@claude new` to start a separate agent."])
        self.assertTrue(self.human(tid))
        self.assertEqual(self.calls("--bg"), [])
        self.assertNotIn(str(tid), self.d.st["cards"])

    def test_at_claude_new_starts_a_separate_session(self):
        tid = self.card()
        self.session("0be00004", calls=self.wrote(tid))
        self.dave(tid, "@claude new try it from scratch")
        self.d.tick()
        starts = self.calls("--bg")
        self.assertEqual(len(starts), 1)
        self.assertIn("The ask: try it from scratch", starts[0]["argv"][-1])
        self.assertEqual(self.calls("attach"), [])
        self.assertEqual(self.calls("stop"), [])                 # the other session is never touched
        self.assertFalse(self.d.st["cards"][str(tid)].get("adopted"))

    def test_at_claude_new_stops_only_a_dispatcher_session(self):
        tid = self.card()
        short = self.started(tid)
        self.dave(tid, "@claude new")
        self.d.tick()
        self.assertEqual([c["argv"] for c in self.calls("stop")], [["stop", short]])
        self.assertEqual(len(self.calls("--bg")), 2)
        other = self.card(title="Other")
        self.session("0be00005", calls=self.wrote(other))
        self.dave(other, "hello")
        self.d.tick()                                            # adopted
        self.dave(other, "@claude new")
        self.d.tick()
        self.assertEqual(len(self.calls("stop")), 1)             # not run on the adopted one
        self.assertEqual(len(self.calls("--bg")), 3)

    def test_most_recent_writer_wins(self):
        tid = self.card()
        later = self.session("0be00007", calls=[("next_task", {"project": "widget"},
                                                 f"Moved to doing:\nwidget#{tid} x\nstatus: doing", 30)])
        self.session("0be00006", calls=[("create_task", {"project": "widget", "title": "x"},
                                         f"Created widget#{tid} in todo: x", 10)])
        self.session("0be00011", calls=[("update_task", {"id": f"widget#{tid}"}, f"Updated widget#{tid}: x", 20)])
        self.dave(tid, "who has it")
        self.d.tick()
        self.assertEqual(self.d.st["cards"][str(tid)]["session_id"], later)
        self.assertIn("who has it", self.transcript(later))

    def test_exited_sessions_are_not_candidates(self):
        tid = self.card()
        self.session("0be00008", calls=self.wrote(tid))
        self.set_agent("0be00008", pid=None, status=None, state="done")
        gone = subprocess.Popen(["true"])                        # listed with a status, but its pid is gone
        gone.wait()
        self.session("0be00009", calls=self.wrote(tid), pid=gone.pid)
        self.dave(tid, "anyone")
        self.d.tick()
        self.assertEqual(len(self.calls("--bg")), 1)
        self.assertFalse(self.d.st["cards"][str(tid)].get("adopted"))

    def test_a_session_that_only_read_the_card_is_not_its_owner(self):
        tid = self.card()
        self.session("0be0000b", calls=[("get_task", {"id": f"widget#{tid}"}, f"widget#{tid} Make the thing", 10),
                                        ("update_task", {"id": f"widget#{tid + 100}"}, f"Updated widget#{tid + 100}: y", 11)])
        self.dave(tid, "go")
        self.d.tick()
        self.assertEqual(len(self.calls("--bg")), 1)
        self.assertEqual(self.calls("attach"), [])

    def test_failed_write_does_not_make_an_owner(self):
        tid = self.card()
        sid = "sid-0be0000c"
        self.session("0be0000c")
        with open(os.path.join(self.fake_dir, ".claude", "projects", "fake", sid + ".jsonl"), "a") as f:
            use = {"type": "tool_use", "id": "e1", "name": "mcp__kanzo__update_task", "input": {"id": f"widget#{tid}"}}
            res = {"type": "tool_result", "tool_use_id": "e1", "is_error": True, "content": "failed"}
            f.write(json.dumps({"timestamp": "2026-11-23T10:00:00Z", "message": {"content": [use]}}) + "\n")
            f.write(json.dumps({"timestamp": "2026-11-23T10:00:01Z", "message": {"content": [res]}}) + "\n")
        self.dave(tid, "go")
        self.d.tick()
        self.assertEqual(len(self.calls("--bg")), 1)

    def test_stop_on_an_adopted_session_only_detaches(self):
        tid = self.card()
        self.session("0be0000d", calls=self.wrote(tid))
        self.dave(tid, "hello")
        self.d.tick()
        self.dave(tid, "@claude stop")
        self.d.tick()
        self.assertEqual(self.calls("stop"), [])
        self.assertTrue(self.mine(tid)[-1].startswith("Detached session `own work 0be0000d` from this card."))
        self.assertNotIn(str(tid), self.d.st["cards"])
        self.dave(tid, "start fresh then")                       # not adopted again: a new agent
        self.d.tick()
        self.assertEqual(len(self.calls("--bg")), 1)
        self.assertEqual(len(self.calls("attach")), 1)

    def test_adopted_session_ending_is_not_a_watchdog_case(self):
        tid = self.card()
        self.session("0be0000e", calls=self.wrote(tid))
        self.dave(tid, "hello")
        self.d.tick()
        self.set_agent("0be0000e", pid=None, status=None, state="done")
        self.d.tick()
        self.assertFalse(any("without an update" in m for m in self.mine(tid)))
        self.assertFalse(self.human(tid))
        self.assertNotIn(str(tid), self.d.st["cards"])

    def test_no_owner_starts_a_session_as_before(self):
        tid = self.card()
        self.session("0be0000f", calls=self.wrote(tid + 50))     # live, but on another card
        self.dave(tid, "please do it")
        self.d.tick()
        self.assertEqual(len(self.calls("--bg")), 1)
        self.assertTrue(self.mine(tid)[0].startswith("Picked up."))

    def test_dry_run_with_an_owner_changes_nothing(self):
        tid = self.card()
        self.session("0be00010", calls=self.wrote(tid))
        d = self.dispatcher(dry_run=True)
        self.dave(tid, "hello")
        d.tick()
        self.assertEqual(self.calls("attach"), [])
        self.assertEqual(self.mine(tid), [])


class Status(Base):
    """status.json: what the board shows."""

    def test_heartbeat_with_nothing_to_do(self):
        path = os.path.join(self.fake_dir, "state", "status.json")
        first, s = os.stat(path), self.status()
        self.assertEqual((s["version"], s["interval"], s["max"], s["sessions"], s["cards"], s["queue"]),
                         (1, 5, 3, [], {}, []))
        self.assertEqual(stat.S_IMODE(first.st_mode), 0o600)
        self.assertLess(abs(kd.stamp(s["updated"]) - time.time()), 5)
        self.d.tick()
        self.assertNotEqual(os.stat(path).st_ino, first.st_ino)        # rewritten: a temp file, then a rename
        self.assertEqual(self.status()["rev"], s["rev"])
        self.assertEqual([f for f in os.listdir(os.path.dirname(path)) if f.endswith(".tmp")], [])

    def test_rev_stable_when_idle_and_bumped_on_change(self):
        self.d.tick()
        rev = self.status()["rev"]
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.status()["rev"], rev)
        tid = self.card()
        self.dave(tid, "please do it")
        self.d.tick()
        s = self.status()
        self.assertGreater(s["rev"], rev)
        short = self.d.st["cards"][str(tid)]["short"]
        (sess,) = [x for x in s["sessions"] if x["id"] == short]
        self.assertEqual((sess["origin"], sess["cards"], sess["kind"], sess["state"], sess["detail"], sess["tokens"],
                          sess["session_id"], sess["started"]),
                         ("dispatch", [tid], "background", "working", "reading the card", 1234, f"sid-{short}",
                          "2026-11-18T11:06:40Z"))
        self.assertEqual(sess["link"], f"https://claude.ai/code/session_TEST{short}")
        card = s["cards"][str(tid)]
        self.assertEqual((card["session"], card["origin"], card["state"], card["note"]), (short, "dispatch", "working", ""))
        self.assertEqual(list(self.receipts(tid).values()), ["started"])
        self.d.tick()                                    # nothing new: same rev
        self.assertEqual(self.status()["rev"], s["rev"])

    def test_session_list_read_at_most_every_15_seconds(self):
        d = self.dispatcher(refresh=15)
        before = len(self.calls("agents"))
        for _ in range(3):
            d.tick()
        self.assertEqual(len(self.calls("agents")) - before, 1)

    def test_malformed_or_missing_job_file(self):
        self.session("0be00021")
        pathlib.Path(self.fake_dir, ".claude", "jobs", "0be00021", "state.json").write_text("{not json")
        self.session("0be00022", kind="interactive")     # no job file at all
        self.session("0be00023")
        pathlib.Path(self.fake_dir, ".claude", "jobs", "0be00023", "state.json").write_text("[1, 2]")
        self.d.tick()
        by = {x["id"]: x for x in self.status()["sessions"]}
        for short in ("0be00021", "0be00022", "0be00023"):
            self.assertEqual((by[short]["detail"], by[short]["tokens"], by[short]["link"], by[short]["state"],
                              by[short]["origin"]), ("", None, "", "idle", "other"))
        self.assertEqual(by["0be00022"]["kind"], "interactive")

    def test_owners_from_transcripts_latest_writer_and_no_done_cards(self):
        a, b = self.card(title="A"), self.card(title="B")
        self.session("0be00024", calls=self.wrote(a, 10) + [("update_task", {"id": f"widget#{b}"}, "ok", 11)])
        self.session("0be00025", calls=[("add_comment", {"id": f"widget#{a}", "body": "x"}, "ok", 20)])
        http("PATCH", f"/api/tasks/{b}", {"status": "done"})
        self.d.tick()
        s = self.status()
        self.assertEqual((s["cards"][str(a)]["session"], s["cards"][str(a)]["origin"]), ("0be00025", "other"))
        self.assertNotIn(str(b), s["cards"])             # done and not the dispatcher's: no owner shown
        self.assertEqual({x["id"]: x["cards"] for x in s["sessions"]}, {"0be00024": [], "0be00025": [a]})
        path = os.path.join(self.fake_dir, ".claude", "projects", "fake", "sid-0be00024.jsonl")
        use = {"type": "tool_use", "id": "late", "name": "mcp__kanzo__update_task", "input": {"id": f"widget#{a}"}}
        res = {"type": "tool_result", "tool_use_id": "late", "content": "ok"}
        with open(path, "a") as f:                       # a later write, and half of the next line
            f.write(json.dumps({"timestamp": "2026-11-23T10:30:00Z", "message": {"content": [use]}}) + "\n")
            f.write(json.dumps({"timestamp": "2026-11-23T10:30:01Z", "message": {"content": [res]}}) + "\n")
            f.write('{"type": "user", "mess')
        self.d.tick()
        self.assertEqual(self.status()["cards"][str(a)]["session"], "0be00024")
        cached = self.d.claude.transcripts.files[path]
        self.assertEqual(cached["offset"], os.path.getsize(path) - len('{"type": "user", "mess'))

    def test_receipts_survive_a_restart(self):
        tid = self.card()
        self.started(tid)
        self.dave(tid, "one more thing")                 # the session is busy: it waits
        self.d.tick()
        before = self.status()
        self.assertEqual(sorted(self.receipts(tid).values()), ["started", "waiting"])
        d2 = self.dispatcher()
        d2.tick()
        after = self.status()
        self.assertEqual(after["cards"][str(tid)]["comments"], before["cards"][str(tid)]["comments"])
        self.assertGreater(after["rev"], before["rev"])   # rev carries on from the file, never back to 1

    def test_stop_marks_a_waiting_comment_failed(self):
        tid = self.card()
        self.started(tid)
        self.dave(tid, "one more thing")                 # busy: waits
        self.d.tick()
        self.dave(tid, "@claude stop")
        self.d.tick()
        waiting, stop = [c["id"] for c in self.comments(tid) if c["author"] == "me"][1:3]
        self.assertEqual((self.receipts(tid)[waiting], self.receipts(tid)[stop]), ("failed", "command"))
        self.assertEqual(self.status()["cards"][str(tid)]["state"], "ended")

    def test_receipts_keep_the_newest_20_and_expire_after_7_days(self):
        for cid in range(1, 26):
            self.d.receipt("999", cid, "delivered")
        self.assertEqual(sorted(map(int, self.d.st["receipts"]["999"]["comments"])), list(range(6, 26)))
        self.d.st["receipts"]["999"]["touched"] = time.time() - 8 * 86400
        self.d.tick()
        self.assertNotIn("999", self.d.st["receipts"])
        self.assertNotIn("999", self.status()["cards"])

    def test_queue_entry(self):
        self.d = self.dispatcher(max_working=1)
        first, second = self.card(title="First"), self.card(title="Second")
        self.started(first)
        self.dave(second, "and this one")
        self.d.tick()
        self.dave(second, "also this")
        self.d.tick()
        s = self.status()
        ids = [c["id"] for c in self.comments(second) if c["author"] == "me"]
        self.assertEqual([(q["task"], q["comment"]) for q in s["queue"]], [(second, ids[0])])
        self.assertEqual((s["cards"][str(second)]["state"], s["cards"][str(second)]["session"]), ("queued", ""))
        self.assertEqual(self.receipts(second), {ids[0]: "queued", ids[1]: "queued"})


class Delegation(Base):
    """Nudge then flag, burst guard, resume, idle stop, `@claude start`, pictures."""

    def test_nudge_once_then_flag(self):
        self.d = self.dispatcher(nudge_after=0)
        tid = self.card()
        short = self.started(tid)
        self.set_agent(short, status="idle", state="done")
        self.d.tick()                                    # the turn ended with no answer: one nudge
        nudge = f"Card widget#{tid}: Dave has no answer on the card yet and he reads only the card."
        self.assertEqual(self.transcript(f"sid-{short}").count(nudge), 1)
        self.assertEqual(len(self.mine(tid)), 1)         # only "Picked up"
        self.d.tick()                                    # the next turn ended without one too
        mine = self.mine(tid)
        self.assertEqual(mine[-1], f"The agent finished without answering here. Open the session to see what it "
                                   f"did: https://claude.ai/code/session_TEST{short}. Comment again to retry.")
        self.assertTrue(self.human(tid))
        self.assertEqual(self.status()["cards"][str(tid)]["state"], "silent")
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.transcript(f"sid-{short}").count(nudge), 1)   # never a second nudge
        self.assertEqual(len(self.mine(tid)), 2)
        self.assertFalse(any("without an update" in m for m in self.mine(tid)))
        kd.Board(URL, client="claude-code").comment(tid, "here is the answer")
        self.d.tick()
        self.assertEqual(self.status()["cards"][str(tid)]["state"], "idle")

    def test_no_nudge_when_the_card_moved_to_review(self):
        self.d = self.dispatcher(nudge_after=0)
        tid = self.card()
        short = self.started(tid)
        kd.Board(URL, client="claude-code").tool("update_task", {"id": tid, "status": "review"})
        self.set_agent(short, status="idle", state="done")
        self.d.tick()
        self.d.tick()
        self.assertNotIn("Dave has no answer", self.transcript(f"sid-{short}"))
        self.assertEqual(len(self.mine(tid)), 1)
        self.assertFalse(self.human(tid))

    def test_no_nudge_while_working_and_none_after_an_answer(self):
        self.d = self.dispatcher(nudge_after=0)
        tid = self.card()
        short = self.started(tid)
        self.d.tick()                                    # still working
        self.assertNotIn("Dave has no answer", self.transcript(f"sid-{short}"))
        kd.Board(URL, client="claude-code").comment(tid, "done: see the diff")
        self.set_agent(short, status="idle", state="done")
        self.d.tick()
        self.d.tick()
        self.assertNotIn("Dave has no answer", self.transcript(f"sid-{short}"))

    def test_burst_guard_at_5_and_6_cards(self):
        self.d = self.dispatcher(max_working=20)
        five = [self.card(title=f"Five {n}") for n in range(5)]
        for t in five:
            self.dave(t, "go")
        self.d.tick()
        self.assertEqual(len(self.starts()), 5)
        six = [self.card(title=f"Six {n}") for n in range(6)]
        for t in six:
            self.dave(t, "go")
        self.d.tick()
        self.assertEqual(len(self.starts()), 5)
        s = self.status()
        for t in six:
            self.assertEqual(self.mine(t), [])
            (c,) = s["cards"][str(t)]["comments"].values()
            self.assertEqual(c["state"], "held")
            self.assertIn("script", c["note"])
        self.dave(six[0], "go, really")                  # commented again: behaves normally
        self.d.tick()
        self.assertEqual(len(self.starts()), 6)

    def test_a_stopped_session_is_resumed_with_the_comment(self):
        tid = self.card()
        short = self.started(tid)
        self.dave(tid, "@claude stop")
        self.d.tick()
        self.dave(tid, "now do the second half")
        self.d.tick()
        self.assertEqual(len(self.starts()), 1)          # no cold start
        (r,) = self.resumes()
        self.assertEqual(r["argv"][r["argv"].index("--resume") + 1], f"sid-{short}")
        self.assertEqual(r["cwd"], os.path.join(self.dev, "widget"))
        self.assertIn(f"Dave commented on card widget#{tid}: now do the second half", self.transcript(f"sid-{short}"))
        card = self.d.st["cards"][str(tid)]
        self.assertEqual((card["short"], card["status"], card.get("adopted")), (short, "attached", None))
        self.assertTrue(self.mine(tid)[-1].startswith(f"Resumed. Your comment went to the card's earlier session"))
        last = self.comments(tid)[-2]["id"]              # Dave's, just before the dispatcher's reply
        self.assertEqual(self.receipts(tid)[last], "delivered")

    def test_a_dead_session_is_resumed(self):
        tid = self.card()
        short = self.started(tid)
        self.set_agent(short, pid=None, status=None, state="done")
        self.dave(tid, "are you there")
        self.d.tick()
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(len(self.resumes()), 1)
        self.assertEqual(self.d.st["cards"][str(tid)]["short"], short)
        self.assertTrue(self.mine(tid)[-1].startswith("Resumed (the earlier session was gone)."))

    def test_resume_fails_then_a_fresh_start_with_the_full_brief(self):
        tid = self.card()
        short = self.started(tid)
        self.dave(tid, "@claude stop")
        self.d.tick()
        pathlib.Path(self.fake_dir, "mode").write_text("noresume")
        self.dave(tid, "now do the second half")
        self.d.tick()
        self.assertEqual(len(self.resumes()), 1)
        self.assertEqual(len(self.starts()), 2)
        brief = self.starts()[1]["argv"][-1]
        for part in ("The ask: now do the second half", "@claude stop", "Done when: it works."):
            self.assertIn(part, brief)
        self.assertNotEqual(self.d.st["cards"][str(tid)]["short"], short)
        self.assertTrue(self.mine(tid)[-1].startswith("Picked up."))
        self.dave(tid, "@claude stop")                   # the new session is the one remembered now
        self.d.tick()
        pathlib.Path(self.fake_dir, "mode").write_text("ok")
        self.dave(tid, "and the third")
        self.d.tick()
        self.assertNotEqual(self.resumes()[-1]["argv"][2], f"sid-{short}")

    def test_new_never_resumes(self):
        tid = self.card()
        self.started(tid)
        self.dave(tid, "@claude stop")
        self.d.tick()
        self.dave(tid, "@claude new from scratch")
        self.d.tick()
        self.assertEqual(self.resumes(), [])
        self.assertEqual(len(self.starts()), 2)

    def test_idle_stop_on_done_cards_only_and_never_an_adopted_session(self):
        self.d = self.dispatcher(idle_stop=60, nudge_after=0)
        agent = kd.Board(URL, client="claude-code")
        done, review, theirs = self.card(title="Done"), self.card(title="Review"), self.card(title="Theirs")
        short = self.started(done)
        other = self.started(review)
        agent.comment(done, "finished, see the diff")
        agent.tool("update_task", {"id": done, "status": "done"})
        agent.tool("update_task", {"id": review, "status": "review"})
        self.session("0be00031", calls=self.wrote(theirs))
        self.dave(theirs, "hello")
        for s in (short, other):
            self.set_agent(s, status="idle", state="done")
        self.d.tick()                                    # adopted and delivered; idle clocks start
        agent.tool("update_task", {"id": theirs, "status": "done"})
        self.d.tick()
        for card in self.d.st["cards"].values():
            card["idle_since"] = time.time() - 120
        self.d.tick()
        self.assertEqual([c["argv"] for c in self.calls("stop")], [["stop", short]])
        self.assertNotIn(str(done), self.d.st["cards"])
        self.assertIn(str(review), self.d.st["cards"])
        self.assertIn(str(theirs), self.d.st["cards"])
        self.assertEqual(len(self.mine(done)), 1)        # no comment, only "Picked up"
        self.dave(done, "one more tweak")                # a later comment resumes it
        self.d.tick()
        self.assertEqual(self.resumes()[-1]["argv"][2], f"sid-{short}")

    def test_idle_stop_zero_is_off(self):
        self.d = self.dispatcher(idle_stop=0, nudge_after=0)
        tid = self.card()
        short = self.started(tid)
        kd.Board(URL, client="claude-code").tool("update_task", {"id": tid, "status": "done"})
        self.set_agent(short, status="idle", state="done")
        self.d.tick()
        self.d.st["cards"][str(tid)]["idle_since"] = time.time() - 10 ** 6
        self.d.tick()
        self.assertEqual(self.calls("stop"), [])

    def test_at_claude_start_with_and_without_an_owner(self):
        owned = self.card(title="Owned")
        self.session("0be00041", calls=self.wrote(owned), name="desk work")
        self.dave(owned, "@claude start")
        self.d.tick()
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.calls("attach"), [])
        self.assertEqual(self.mine(owned), ["Session `desk work` (id 0be00041) is already on this card, so nothing "
                                            "was started. Comment here to reach it."])
        self.assertEqual(list(self.receipts(owned).values()), ["command"])
        free = self.card(title="Free")
        self.dave(free, "@Claude start")
        self.d.tick()
        (s,) = self.starts()
        self.assertIn("The ask: (see the card and its comments)", s["argv"][-1])
        self.assertEqual(list(self.receipts(free).values()), ["command"])
        self.dave(free, "@claude start")                 # its own session is live: named, nothing new
        self.d.tick()
        self.assertEqual(len(self.starts()), 1)
        self.assertTrue(self.mine(free)[-1].startswith(f"Session `widget#{free} Free` (id "))

    def test_follow_up_counts_pictures_and_names_no_paths(self):
        tid = self.card()
        short = self.started(tid)
        self.set_agent(short, status="idle", state="done")
        req = urllib.request.Request(URL + "/api/images", data=b"\x89PNG not really", method="POST",
                                     headers={"Content-Type": "image/png"})
        with urllib.request.urlopen(req, timeout=10) as r:
            files = [json.load(r)["file"]]
        http("POST", f"/api/tasks/{tid}/comments", {"body": "see the screenshot", "images": files})
        self.d.tick()
        t = self.transcript(f"sid-{short}")
        self.assertIn("see the screenshot (Answer on the card with the kanzo tools; he reads only the card.) "
                      "(1 picture(s) attached to this comment: get_task on the card lists the file paths, read them.)", t)
        self.assertNotIn(files[0], t)
        self.assertNotIn("picture", kd.follow_up("x#1", [{"id": 5, "body": "b"}], images=[{"comment_id": 4}]))


class Asks(Base):
    """A background session that waits on Dave: one ask per episode on its card, his answer reaches it."""

    NEEDS = "Pick A or B for the export format."

    def job(self, short, **fields):
        path = pathlib.Path(self.fake_dir, ".claude", "jobs", short, "state.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(path.read_text()) if path.exists() else {}
        data.update(fields)
        path.write_text(json.dumps(data))

    def waiting(self, short, needs=NEEDS, ago=300, alive=False, calls=(), cwd=None, **job):
        """A background session blocked on Dave since `ago` seconds; with no process unless `alive`."""
        sid = self.session(short, calls=calls)
        fields = {"state": "blocked"} if alive else {"pid": None, "status": None, "state": "blocked"}
        if cwd:
            fields["cwd"] = cwd
        self.set_agent(short, **fields)
        self.job(short, state="blocked", needs=needs, updatedAt=kd.iso(time.time() - ago), sessionId=sid,
                 name=f"own work {short}", cwd=cwd or self.dev, **job)
        return sid

    def unblock(self, short):
        self.job(short, state="working", updatedAt=kd.iso(time.time()))

    def ask_body(self, short, needs=NEEDS):
        return (f"**own work {short} waits on you.**\n\n{needs}\n\nAnswer in a comment here and it goes to that "
                f"session. [Open in Claude](https://claude.ai/code/session_OWN{short})")

    def asked(self, short):
        return self.d.st["asks"][f"sid-{short}"]

    def record_clears(self):
        cleared, real = [], self.d.board.clear_inbox
        self.d.board.clear_inbox = lambda ids: (cleared.append([int(i) for i in ids]), real(ids))[1]
        return cleared

    def test_one_ask_per_episode_and_not_before_ask_after(self):
        tid = self.card()
        self.waiting("0a500001", ago=30, calls=self.wrote(tid))
        self.d.tick()
        self.assertEqual(self.mine(tid), [])             # blocked 30 s: too soon
        self.assertFalse(self.human(tid))
        self.job("0a500001", updatedAt=kd.iso(time.time() - 121))
        self.d.tick()
        self.assertEqual(self.mine(tid), [self.ask_body("0a500001")])
        self.assertTrue(self.human(tid))
        self.d.tick()
        self.d.tick()
        self.assertEqual(len(self.mine(tid)), 1)         # never twice for one episode
        a = self.asked("0a500001")
        self.assertEqual((a["task"], a["flagged"], a["created"]), (str(tid), True, False))
        self.assertEqual(a["comment"], [c["id"] for c in self.comments(tid) if c["author"] == kd.AUTHOR][0])
        self.assertAlmostEqual(a["since"], time.time() - 121, delta=5)
        d2 = self.dispatcher()                            # a restart keeps the episode
        d2.tick()
        self.assertEqual(len(self.mine(tid)), 1)
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.resumes(), [])

    def test_ask_after_falls_back_to_first_seen(self):
        tid = self.card()
        self.waiting("0a500002", calls=self.wrote(tid))
        self.job("0a500002", updatedAt=None)
        self.d = self.dispatcher(ask_after=0.5)
        self.d.tick()
        self.assertEqual(self.mine(tid), [])
        time.sleep(0.6)
        self.d.tick()
        self.d.tick()
        self.assertEqual(len(self.mine(tid)), 1)

    def test_empty_needs_suggested_reply_long_needs_and_no_link(self):
        a, b = self.card(title="A"), self.card(title="B")
        self.waiting("0a500003", needs="", suggestedReply="Use A.", calls=self.wrote(a))
        long_needs = "Which branch? " + "x " * 400
        self.waiting("0a500004", needs=long_needs, calls=self.wrote(b))
        self.job("0a500004", bridgeSessionId=None)
        self.d.tick()
        self.assertEqual(self.mine(a), ["**own work 0a500003 waits on you.**\n\nIt stopped and asks for your input.\n\n"
                                        "Suggested reply: Use A.\n\nAnswer in a comment here and it goes to that "
                                        "session. [Open in Claude](https://claude.ai/code/session_OWN0a500003)"])
        (body,) = self.mine(b)
        self.assertIn(kd.clip(long_needs.strip(), 700), body)
        self.assertTrue(body.endswith("Answer in a comment here and it goes to that session."))

    def test_card_held_by_the_dispatcher_comes_first(self):
        tid, other = self.card(), self.card(title="Other")
        short = self.started(tid)
        with open(os.path.join(self.fake_dir, ".claude", "projects", "fake", f"sid-{short}.jsonl"), "a") as f:
            for n, (tool, args, reply, minute) in enumerate(self.wrote(other, 50)):
                use = {"type": "tool_use", "id": f"h{n}", "name": f"mcp__kanzo__{tool}", "input": args}
                res = {"type": "tool_result", "tool_use_id": f"h{n}", "content": reply}
                f.write(json.dumps({"timestamp": f"2026-11-23T10:{minute}:00Z", "message": {"content": [use]}}) + "\n")
                f.write(json.dumps({"timestamp": f"2026-11-23T10:{minute}:00Z", "message": {"content": [res]}}) + "\n")
        self.set_agent(short, pid=None, status=None, state="blocked")
        self.job(short, needs=self.NEEDS, updatedAt=kd.iso(time.time() - 300))
        self.d.tick()
        mine = self.mine(tid)
        self.assertEqual(len(mine), 2)                   # "Picked up", then the ask
        self.assertTrue(mine[1].startswith(f"**widget#{tid} Make the thing waits on you.**"))
        self.assertEqual(self.mine(other), [])
        card = self.d.st["cards"][str(tid)]
        self.assertEqual((card["ask"], card.get("adopted"), card["short"]), (True, None, short))
        self.assertFalse(any("without an update" in m for m in mine))

    def test_open_card_with_the_latest_write(self):
        done, newest_open, older = self.card(title="Done"), self.card(title="Newest open"), self.card(title="Older")
        http("PATCH", f"/api/tasks/{done}", {"status": "done"})
        self.waiting("0a500005", calls=self.wrote(older, 10) + self.wrote(newest_open, 20) + self.wrote(done, 30))
        self.d.tick()
        self.assertEqual((self.mine(done), self.mine(older)), ([], []))
        self.assertEqual(self.mine(newest_open), [self.ask_body("0a500005")])
        card = self.d.st["cards"][str(newest_open)]
        self.assertEqual((card["adopted"], card["ask"], card["session_id"]), (True, True, "sid-0a500005"))

    def test_new_card_in_the_project_of_the_folder(self):
        cwd = os.path.join(self.dev, "widget", ".claude", "worktrees", "feature")
        self.waiting("0a500006", cwd=cwd)
        self.d.tick()
        a = self.asked("0a500006")
        self.assertEqual((a["created"], a["flagged"]), (True, True))
        t = http("GET", f"/api/tasks/{a['task']}")
        task = t["task"]
        self.assertEqual((task["project"], task["title"], task["status"], task["human"]),
                         ("widget", "Answer: own work 0a500006", "doing", True))
        self.assertEqual(sorted(task["tags"].split(",") if isinstance(task["tags"], str) else task["tags"]),
                         ["agent", "ask"])
        for part in ("own work 0a500006", cwd, "https://claude.ai/code/session_OWN0a500006"):
            self.assertIn(part, task["description"])
        self.assertEqual([c["body"] for c in t["comments"]], [self.ask_body("0a500006")])

    def test_new_card_in_the_fallback_project_and_title_clipped(self):
        self.d = self.dispatcher(ask_project="nolink")
        self.waiting("0a500007", cwd=os.path.join(self.dev, "not-a-repo"))
        self.set_agent("0a500007", name="n" * 120)
        self.d.tick()
        task = http("GET", f"/api/tasks/{self.asked('0a500007')['task']}")["task"]
        self.assertEqual(task["project"], "nolink")
        self.assertEqual(len(task["title"]), 80)
        self.assertTrue(task["title"].startswith("Answer: nnn") and task["title"].endswith("..."))

    def test_missing_fallback_project_skips_and_retries_once_a_minute(self):
        self.d = self.dispatcher(ask_project="no-such-project")
        reads, real = [], self.d.board.projects
        self.d.board.projects = lambda: (reads.append(1), real())[1]
        self.waiting("0a500008", cwd=self.dev)
        with self.assertLogs("kanzo-dispatch", "WARNING") as logs:
            self.d.tick()
        self.assertIn("no-such-project", "\n".join(logs.output))
        self.d.tick()
        self.assertEqual(len(reads), 1)                  # not again within the minute
        self.assertEqual(self.d.st.get("asks"), {})
        self.d.ask_retry["sid-0a500008"] = 0
        self.d.tick()
        self.assertEqual(len(reads), 2)
        self.assertEqual(self.status()["asks"], {"on": True, "open": 0})

    def test_flag_off_at_the_end_only_when_the_dispatcher_set_it(self):
        mine_flag, theirs = self.card(title="Mine"), self.card(title="Theirs")
        http("PATCH", f"/api/tasks/{theirs}", {"human": True})
        self.waiting("0a500009", calls=self.wrote(mine_flag))
        self.waiting("0a50000a", calls=self.wrote(theirs))
        self.d.tick()
        self.assertEqual((self.asked("0a500009")["flagged"], self.asked("0a50000a")["flagged"]), (True, False))
        self.assertTrue(self.human(mine_flag) and self.human(theirs))
        self.unblock("0a500009")
        self.unblock("0a50000a")
        self.d.tick()
        self.assertEqual((self.human(mine_flag), self.human(theirs)), (False, True))
        self.assertEqual(self.d.st["asks"], {})
        self.assertEqual((len(self.mine(mine_flag)), len(self.mine(theirs))), (1, 1))   # nothing posted at the end
        self.assertNotIn("ask", self.d.st["cards"].get(str(mine_flag), {}))

    def test_inbox_cleared_at_the_end_and_a_404_is_fine(self):
        self.assertFalse(kd.Board(URL).clear_inbox([1]))  # the route is not on this board: no exception
        tid = self.card()
        cleared = self.record_clears()
        self.waiting("0a50000b", calls=self.wrote(tid))
        self.d.tick()
        self.assertEqual(cleared, [])
        self.set_agent("0a50000b", state="done")         # gone from the waiting list (job says done)
        self.job("0a50000b", state="done")
        with self.assertNoLogs("kanzo-dispatch", "WARNING"):
            self.d.tick()
        self.assertEqual(cleared, [[tid]])
        self.assertFalse(self.human(tid))

    def test_clear_inbox_posts_the_task_ids(self):
        seen = []

        class Handler(http_server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass
        srv = http_server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.assertTrue(kd.Board(f"http://127.0.0.1:{srv.server_port}").clear_inbox(["7", 9]))
        self.assertEqual(seen, [("/api/inbox/clear", {"tasks": [7, 9]})])

    def test_not_cleared_when_an_agent_answered_last(self):
        tid = self.card()
        cleared = self.record_clears()
        self.waiting("0a50000c", calls=self.wrote(tid))
        self.d.tick()
        kd.Board(URL, client="claude-code").comment(tid, "Went with A, see the diff.")
        self.unblock("0a50000c")
        self.d.tick()
        self.assertEqual(cleared, [])
        self.assertTrue(self.human(tid))                 # still waits on Dave in the Inbox
        self.assertEqual(self.d.st["asks"], {})

    def test_a_second_episode_comments_again_on_the_same_card(self):
        self.d = self.dispatcher(ask_project="nolink")
        self.waiting("0a50000d")
        self.d.tick()
        tid = self.asked("0a50000d")["task"]
        self.unblock("0a50000d")
        self.d.tick()
        self.d.tick()                                    # the adopted entry goes: no process, not waiting
        self.assertNotIn(tid, self.d.st["cards"])
        self.job("0a50000d", state="blocked", needs="Now the second question.", updatedAt=kd.iso(time.time() - 200))
        self.d.tick()
        self.assertEqual(self.asked("0a50000d")["task"], tid)
        self.assertEqual(self.mine(int(tid)), [self.ask_body("0a50000d"),
                                               self.ask_body("0a50000d", "Now the second question.")])
        tasks = http("GET", "/api/board?project=nolink")["tasks"]
        self.assertEqual(len([t for t in tasks if t["title"] == "Answer: own work 0a50000d"]), 1)
        self.assertTrue(self.human(int(tid)))

    def test_daves_answer_resumes_the_exited_session(self):
        tid = self.card()
        sid = self.waiting("0a50000e", calls=self.wrote(tid))
        self.d.tick()
        self.dave(tid, "Go with A.")
        self.d.tick()
        self.assertEqual(self.starts(), [])              # nothing new started
        (r,) = self.resumes()
        self.assertEqual(r["argv"][:3], ["--bg", "--resume", sid])
        self.assertEqual(r["argv"][-1], f"Dave commented on card widget#{tid} (Make the thing): Go with A. "
                                        f"(Answer on the card with the kanzo tools; he reads only the card.)")
        self.assertIn("Go with A.", self.transcript(sid))
        dave = [c["id"] for c in self.comments(tid) if c["author"] == "me"][-1]
        self.assertEqual(self.receipts(tid)[dave], "delivered")
        self.assertEqual(self.mine(tid), [self.ask_body("0a50000e")])   # no "Resumed" or "Picked up"
        self.d.tick()
        self.assertEqual(len(self.resumes()), 1)

    def test_a_session_on_a_permission_prompt_is_not_typed_into(self):
        tid = self.card()
        sid = self.waiting("0a50000f", alive=True, calls=self.wrote(tid))   # listed idle and blocked
        self.d.tick()
        self.assertEqual(len(self.mine(tid)), 1)
        self.dave(tid, "Yes, allow it.")
        self.d.tick()
        self.d.tick()
        self.assertEqual((self.calls("attach"), self.resumes(), self.starts()), ([], [], []))
        self.assertNotIn("Yes, allow it.", self.transcript(sid))
        dave = [c["id"] for c in self.comments(tid) if c["author"] == "me"][-1]
        c = self.status()["cards"][str(tid)]["comments"][str(dave)]
        self.assertEqual(c["state"], "waiting")
        self.assertIn("open it in the app", c["note"])
        self.assertIn("https://claude.ai/code/session_OWN0a50000f", c["note"])

    def test_a_live_idle_session_gets_the_typed_delivery(self):
        tid = self.card()
        sid = self.waiting("0a500010", alive=True, calls=self.wrote(tid))
        self.set_agent("0a500010", state="done")         # input box free; the job file still says blocked
        self.job("0a500010", state="blocked")
        self.d.tick()
        self.dave(tid, "Use B.")
        self.d.tick()
        self.assertEqual([c["argv"] for c in self.calls("attach")], [["attach", "0a500010"]])
        self.assertIn(f"Dave commented on card widget#{tid} (Make the thing): Use B.", self.transcript(sid))
        self.assertEqual(self.resumes(), [])

    def test_a_blocked_session_is_never_nudged(self):
        self.d = self.dispatcher(nudge_after=0)
        tid = self.card()
        short = self.started(tid)
        self.set_agent(short, status="idle", state="done")
        self.job(short, state="blocked", needs=self.NEEDS, updatedAt=kd.iso(time.time()))
        self.d.tick()
        self.d.tick()
        self.assertNotIn("Dave has no answer", self.transcript(f"sid-{short}"))

    def test_at_most_three_new_asks_per_pass(self):
        cards = [self.card(title=f"C{n}") for n in range(4)]
        for n, t in enumerate(cards):
            self.waiting(f"0a50002{n}", ago=300 - n, calls=self.wrote(t))
        self.d.tick()
        self.assertEqual([len(self.mine(t)) for t in cards], [1, 1, 1, 0])   # oldest first
        self.d.tick()
        self.assertEqual([len(self.mine(t)) for t in cards], [1, 1, 1, 1])

    def test_no_asks_is_off(self):
        self.d = self.dispatcher(asks=False)
        tid = self.card()
        self.waiting("0a500030", calls=self.wrote(tid))
        self.d.tick()
        self.assertEqual(self.mine(tid), [])
        s = self.status()
        self.assertEqual(s["asks"], {"on": False, "open": 0})
        self.assertEqual(s["sessions"], [])              # exited: not listed when asks are off
        self.dave(tid, "hello?")                         # and the old rules: a new agent starts
        self.d.tick()
        self.assertEqual(len(self.starts()), 1)

    def test_dry_run_writes_nothing(self):
        tid = self.card()
        self.waiting("0a500031", calls=self.wrote(tid))
        self.waiting("0a500032", cwd=os.path.join(self.dev, "widget"))
        state = pathlib.Path(self.state).read_bytes()
        status = pathlib.Path(self.state).with_name("status.json").read_bytes()
        before = len(http("GET", "/api/board?project=widget")["tasks"])
        d = self.dispatcher(dry_run=True)
        d.tick()
        d.tick()
        self.assertEqual(self.mine(tid), [])
        self.assertFalse(self.human(tid))
        self.assertEqual(len(http("GET", "/api/board?project=widget")["tasks"]), before)
        self.assertEqual(pathlib.Path(self.state).read_bytes(), state)
        self.assertEqual(pathlib.Path(self.state).with_name("status.json").read_bytes(), status)

    def test_status_json_lists_the_waiting_session_and_its_card(self):
        tid = self.card()
        needs = "Choose the column order. " * 12
        self.waiting("0a500033", needs=needs, calls=self.wrote(tid))
        self.d.tick()
        s = self.status()
        (sess,) = s["sessions"]
        self.assertEqual((sess["id"], sess["state"], sess["detail"], sess["cards"], sess["origin"]),
                         ("0a500033", "blocked", kd.clip(needs.strip(), 160), [tid], "adopted"))
        card = s["cards"][str(tid)]
        self.assertEqual((card["state"], card["note"], card["session"]), ("blocked", kd.clip(needs.strip(), 200), "0a500033"))
        self.assertEqual(s["asks"], {"on": True, "open": 1})
        rev = s["rev"]
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.status()["rev"], rev)      # no churn while nothing changes
        self.unblock("0a500033")
        self.d.tick()
        s = self.status()
        self.assertGreater(s["rev"], rev)
        self.assertEqual(s["asks"], {"on": True, "open": 0})
        self.assertEqual(s["sessions"], [])              # no process and no longer waiting


class FakePushService:
    """A push service on a loopback port: records each request, answers 201 or what `status` says."""

    def __init__(self):
        self.requests, self.status = [], {}
        outer = self

        class Handler(http_server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                       "body": self.rfile.read(n) if n else b""})
                self.send_response(outer.status.get(self.path, 201))
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http_server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        # The dispatcher's own sender, with only the connection pointed here instead of the real host.
        self.post = functools.partial(kd.push_post, connect=lambda host, port, timeout: http_client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=timeout))

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def new_key(curve=None):
    """A throwaway P-256 key, made in this process only: SEC1 DER, base64, as the README says."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(curve or ec.SECP256R1())
    der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption())
    return base64.b64encode(der).decode()


def unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Push(Base):
    """Web Push: content-free, signed, at most one per browser per pass and one round per 30 s."""

    def setUp(self):
        super().setUp()
        self.fake_push = FakePushService()
        self.addCleanup(self.fake_push.close)
        self.key = new_key()
        self.p = kd.Push(self.key, os.path.join(self.kanzo_home, "kanzo.db"), post=self.fake_push.post, threaded=False)
        self.d = self.dispatcher(push=self.p)
        self.subscribe(1)
        self.d.tick()                                    # the first pass after a start only records

    def subscribe(self, n):
        http("POST", "/api/push/subscribe", {"endpoint": f"https://fcm.googleapis.com/fcm/send/fake{n}",
                                             "keys": {"p256dh": "BFake", "auth": "fake"}, "label": "Test phone"})

    def answer(self, title="Answer me"):
        """A card in review whose last comment is an agent's: it waits on Dave."""
        tid = self.card(title=title)
        http("PATCH", f"/api/tasks/{tid}", {"status": "review"})
        kd.Board(URL, client="claude").comment(tid, "Done, see the pull request.")
        return tid

    def status(self):
        return json.loads(pathlib.Path(self.state).with_name("status.json").read_text())

    def test_first_pass_is_silent(self):
        self.answer()
        p = kd.Push(self.key, os.path.join(self.kanzo_home, "kanzo.db"), post=self.fake_push.post, threaded=False)
        d = self.dispatcher(push=p)
        d.tick()
        self.assertEqual(self.fake_push.requests, [])
        self.assertEqual(self.status()["push"]["subscriptions"], 1)

    def test_a_new_answer_sends_one_signed_empty_push(self):
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        self.answer()
        self.d.tick()
        self.assertEqual(len(self.fake_push.requests), 1)
        r = self.fake_push.requests[0]
        h = r["headers"]
        self.assertEqual((r["path"], r["body"]), ("/fcm/send/fake1", b""))
        self.assertEqual((h["ttl"], h["urgency"], h["topic"], h["content-length"]), ("3600", "normal", "kanzo-inbox", "0"))
        self.assertNotIn("content-type", h)
        auth = h["authorization"]
        self.assertTrue(auth.startswith("vapid t="), auth)
        jwt, k = auth[len("vapid t="):].split(", k=")
        self.assertEqual(k, self.p.public)
        self.assertEqual(self.status()["push_key"], k)
        head, claims, sig = jwt.split(".")
        self.assertEqual(json.loads(unb64(head)), {"typ": "JWT", "alg": "ES256"})
        c = json.loads(unb64(claims))
        self.assertEqual((c["aud"], c["sub"]), ("https://fcm.googleapis.com", "https://kanzo.blenko.cloud"))
        self.assertAlmostEqual(c["exp"], time.time() + 12 * 3600, delta=60)
        raw = unb64(sig)
        self.assertEqual(len(raw), 64, "raw r||s, not DER")
        public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64(k))
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        public.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
        with self.assertRaises(InvalidSignature):
            public.verify(der, f"{head}.{claims}x".encode(), ec.ECDSA(hashes.SHA256()))
        st = self.status()["push"]
        self.assertEqual((st["on"], st["subscriptions"], st["last_error"]), (True, 1, ""))
        self.assertTrue(st["last_sent"])
        for path in (self.state, pathlib.Path(self.state).with_name("status.json")):
            self.assertNotIn("fake1", pathlib.Path(path).read_text(), "an endpoint never leaves the database")

    def test_two_answers_in_one_pass_send_one_push(self):
        self.answer("One")
        self.answer("Two")
        self.d.tick()
        self.assertEqual(len(self.fake_push.requests), 1)
        self.d.tick()                                    # nothing new: nothing sent
        self.assertEqual(len(self.fake_push.requests), 1)

    def test_one_round_per_30_seconds(self):
        self.answer("First")
        self.d.tick()
        self.answer("Second")
        self.d.tick()
        self.assertEqual(len(self.fake_push.requests), 1, "held by the 30 s floor")
        self.p.last_round -= 31
        self.d.tick()
        self.assertEqual(len(self.fake_push.requests), 2, "then sent, once")
        self.p.last_round -= 31
        self.d.tick()
        self.assertEqual(len(self.fake_push.requests), 2)

    def test_one_push_per_browser(self):
        self.subscribe(2)
        self.addCleanup(http, "POST", "/api/push/unsubscribe", {"endpoint": "https://fcm.googleapis.com/fcm/send/fake2"})
        self.answer()
        self.d.tick()
        self.assertEqual(sorted(r["path"] for r in self.fake_push.requests), ["/fcm/send/fake1", "/fcm/send/fake2"])

    def test_410_drops_the_browser(self):
        self.fake_push.status["/fcm/send/fake1"] = 410
        self.answer()
        self.d.tick()
        self.assertEqual(len(self.fake_push.requests), 1)
        st = json.loads(pathlib.Path(self.state).read_text())
        self.assertEqual(st["push_dead"], [kd.digest("https://fcm.googleapis.com/fcm/send/fake1")])
        self.d.tick()
        self.assertEqual(self.status()["push"]["subscriptions"], 0)
        self.answer("Another")
        self.p.last_round -= 31
        self.d.tick()
        self.assertEqual(len(self.fake_push.requests), 1, "never sent to again")
        again = self.dispatcher(push=kd.Push(self.key, os.path.join(self.kanzo_home, "kanzo.db"),
                                             post=self.fake_push.post, threaded=False))
        self.assertEqual(again.push.dead, st["push_dead"], "remembered across a restart")

    def test_429_backs_off(self):
        self.fake_push.status["/fcm/send/fake1"] = 429
        self.answer()
        self.d.tick()
        self.assertEqual(len(self.fake_push.requests), 1)
        self.assertIn("HTTP 429", self.status()["push"]["last_error"])
        self.p.last_round -= 31                          # past the floor, inside the 60 s backoff
        self.d.tick()
        self.assertEqual(len(self.fake_push.requests), 1)
        self.fake_push.status.clear()
        self.p.last_round -= 31
        h = kd.digest("https://fcm.googleapis.com/fcm/send/fake1")
        self.p.backoff[h] = (0, 60)                      # the backoff has run out
        self.d.tick()
        self.assertEqual(len(self.fake_push.requests), 2, "still owed, sent once the wait is over")
        self.assertEqual(self.status()["push"]["last_error"], "")

    def test_no_key_or_a_bad_key_means_off(self):
        db = os.path.join(self.kanzo_home, "kanzo.db")
        from cryptography.hazmat.primitives.asymmetric import ec
        for raw in ("", "not base64 at all", base64.b64encode(b"not a key").decode(), new_key(ec.SECP384R1())):
            p = kd.Push(raw, db, post=self.fake_push.post, threaded=False)
            self.assertFalse(p.on)
            d = self.dispatcher(push=p)
            d.tick()
            self.answer()
            d.tick()
            st = self.status()
            self.assertEqual((st["push_key"], st["push"]["on"]), ("", False))
        self.assertEqual(self.fake_push.requests, [])
        self.assertFalse(kd.Push(self.key, db, off="--no-push").on)

    def test_dry_run_sends_nothing(self):
        d = self.dispatcher(push=self.p, dry_run=True)
        self.answer()
        d.tick()
        self.assertEqual(self.fake_push.requests, [])

    def test_a_hanging_push_service_does_not_hold_up_the_pass(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        def slow(url, headers, timeout):
            gate.wait(timeout)
            return 201, None
        p = kd.Push(self.key, os.path.join(self.kanzo_home, "kanzo.db"), post=slow)
        d = self.dispatcher(push=p)
        d.tick()
        self.answer()
        began = time.time()
        d.tick()
        self.assertLess(time.time() - began, 5)
        gate.set()
        p.join()
        self.assertTrue(self.status()["push"]["on"])

    def test_the_key_leaves_the_environment_and_never_reaches_claude(self):
        os.environ[kd.PUSH_KEY_ENV] = self.key
        self.addCleanup(os.environ.pop, kd.PUSH_KEY_ENV, None)
        self.assertNotIn(kd.PUSH_KEY_ENV, kd.child_env(), "filtered by name even before it is taken")
        rc = kd.main(["--once", "--dry-run", "--board", URL, "--state", os.path.join(self.fake_dir, "main", "state.json"),
                      "--db", os.path.join(self.kanzo_home, "kanzo.db"), "--claude", self.fake])
        self.assertEqual(rc, 0)
        self.assertNotIn(kd.PUSH_KEY_ENV, os.environ, "main() takes it out first")
        os.environ[kd.PUSH_KEY_ENV] = self.key           # as if it were still there: take it, then start claude
        self.assertEqual(kd.take_push_key(), self.key)
        tid = self.card()
        self.started(tid)
        calls = self.calls("--bg")
        self.assertTrue(calls)
        self.assertTrue(all(c["env"] == [] for c in calls), calls)
        self.assertNotIn(self.key, pathlib.Path(self.fake_dir, "calls.jsonl").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
