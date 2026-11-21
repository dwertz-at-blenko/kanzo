#!/usr/bin/env python3
# Version: v1 · created 2026-11-21 13:26 EST · updated 2026-11-21 13:26 EST
# Changes: v1 tests for kanzo-dispatch against a throwaway board and a fake claude.
# This file: /home/claude/dev/kanzo/agent/dispatch/test_dispatch.py (repo dwertz-at-blenko/kanzo, branch public-url)
# Session: b1475961-06d7-40b0-9eff-f2b47896b6bb · vps as claude · cwd /home/claude/dev
"""kanzo-dispatch against a real, throwaway board and a fake claude.

The board is this worktree's debug build (built when missing), on 127.0.0.1:8198 with a temp
--home, stopped at the end. The fake claude keeps its sessions in a JSON file the tests edit to
play idle, busy, blocked or gone, and its `attach` prints the prompt, reads one line and
writes it to the session's transcript, like the real TUI does.

    python3 agent/dispatch/test_dispatch.py
"""
import json
import os
import pathlib
import shutil
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

PORT = 8198
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
if "--bg" in args:
    mode = open(os.path.join(d, "mode")).read().strip() if os.path.exists(os.path.join(d, "mode")) else "ok"
    if mode == "fail":
        print("Error: not logged in. Run /login"); sys.exit(1)
    short = "%08x" % (0xabc00000 + len(agents))
    sid = "sid-" + short
    name = args[args.index("--name") + 1]
    os.makedirs(os.path.join(d, ".claude", "jobs", short), exist_ok=True)
    json.dump({"sessionId": sid, "bridgeSessionId": "cse_TEST" + short, "state": "working", "name": name},
              open(os.path.join(d, ".claude", "jobs", short, "state.json"), "w"))
    os.makedirs(os.path.join(d, ".claude", "projects", "fake"), exist_ok=True)
    open(os.path.join(d, ".claude", "projects", "fake", sid + ".jsonl"), "a").close()
    agents.append({"id": short, "sessionId": sid, "kind": "background", "pid": 4242, "status": "busy",
                   "state": "working", "name": name, "cwd": os.getcwd()})
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
        self.state = os.path.join(self.fake_dir, "state", "state.json")
        self.d = self.dispatcher()
        self.d.tick()                                    # first run: the baseline

    def dispatcher(self, **kw):
        kw.setdefault("start_grace", 0)
        claude = kd.Claude(self.fake, home=self.fake_dir, sock=os.path.join(self.fake_dir, "tmux.sock"))
        return kd.Dispatcher(kd.Board(URL), claude, self.state, self.dev, os.path.join(self.kanzo_home, "kanzo.db"),
                             link_wait=1, **kw)

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

    def started(self, tid):
        self.dave(tid, "@claude please do it")
        self.d.tick()
        return self.d.st["cards"][str(tid)]["short"]


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

    def test_plain_comment_starts_nothing(self):
        tid = self.card()
        self.dave(tid, "just a note, claude is not called here")
        self.d.tick()
        self.assertEqual(self.calls("--bg"), [])
        self.assertEqual(self.mine(tid), [])

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
        self.dave(tid, "are you there")
        self.d.tick()
        self.assertEqual(len(self.calls("--bg")), 2)
        self.assertIn("are you there", self.calls("--bg")[1]["argv"][-1])
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
        self.dave(tid, "a plain note after the stop")
        self.d.tick()
        self.assertEqual(len(self.calls("--bg")), 1)

    def test_done_card_needs_at_claude_again(self):
        tid = self.card()
        self.started(tid)
        http("PATCH", f"/api/tasks/{tid}", {"status": "done"})
        self.dave(tid, "thanks")
        self.d.tick()
        self.assertEqual(self.calls("attach"), [])
        self.dave(tid, "@claude one more change")
        self.d.tick()
        self.assertEqual(len(self.calls("stop")), 1)     # never two sessions for one card
        self.assertEqual(len(self.calls("--bg")), 2)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
