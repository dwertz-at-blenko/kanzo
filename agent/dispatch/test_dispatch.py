#!/usr/bin/env python3
# Version: v3 · created 2026-11-21 13:26 EST · updated 2026-11-24
# Changes: v1 tests for kanzo-dispatch against a throwaway board and a fake claude.
#          v2 2026-11-23 owner tests: adopt the live session already on a card, a fake herdr for panes.
#          v3 2026-11-24 status.json, receipts, nudge and flag, burst guard, resume (the fake claude
#             takes `--bg --resume`), idle stop, `@claude start`, pictures in a follow-up.
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
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
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
    f.write(json.dumps({"argv": args, "cwd": os.getcwd()}) + "\n")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
