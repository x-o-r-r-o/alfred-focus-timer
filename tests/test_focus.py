#!/usr/bin/env python3
"""End-to-end tests: run the Script Filters and actions the way Alfred does and validate the JSON.

Pomodoro tests inject the clock with FT_NOW and run without the background waiter (FT_NO_WAITER),
except the few that start the real waiter with very short sessions; every test kills any process
that still refers to its temporary data folder. Toggl and Clockify are replaced by a local mock
HTTP server (FT_TOGGL_URL / FT_CLOCKIFY_URL) and the Keychain by a file (FT_KEYCHAIN_FILE), except
one test that uses a throwaway Keychain item and deletes it.
"""
import base64, json, os, plistlib, re, shutil, signal, subprocess, sys, tempfile, threading, time, unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
T0 = 1790000000  # a fixed "now" for the injected clock
TOGGL_TOKEN = "0123456789abcdef0123456789abcdef"
CLOCKIFY_TOKEN = "Y2xvY2tpZnktdGVzdC1rZXk="


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- mock services

class Mock:
    def reset(self):
        self.requests = []
        self.force = None  # (status, body[, headers]) returned for every request
        self.delay = 0  # seconds to wait before answering
        self.next_id = 1000
        self.toggl_entries = [
            {"id": 1, "workspace_id": 11, "description": "Writing docs", "project_id": 101, "project_name": "Website Redesign",
             "tags": ["writing"], "start": iso(T0 - 7200), "stop": iso(T0 - 3600), "duration": 3600},
            {"id": 2, "workspace_id": 11, "description": "Emails ✉️ \"urgent\"", "project_id": None, "project_name": None,
             "tags": None, "start": iso(T0 - 90000), "stop": iso(T0 - 88200), "duration": 1800},
            {"id": 3, "workspace_id": 11, "description": "Writing docs", "project_id": 101, "project_name": "Website Redesign",
             "tags": ["writing"], "start": iso(T0 - 180000), "stop": iso(T0 - 176400), "duration": 3600},
        ]
        self.toggl_projects = {11: [
            {"id": 101, "name": "Website Redesign", "billable": True, "active": True, "client_name": "Acme"},
            {"id": 102, "name": "Website Maintenance", "billable": False, "active": True},
            {"id": 103, "name": "Café Opening", "billable": False, "active": True},
        ], 12: [{"id": 201, "name": "Side Project", "billable": False, "active": True}]}
        self.c_entries = [
            {"id": "e1", "workspaceId": "w1", "description": "Planning", "projectId": "p1", "project": {"name": "Internal"},
             "tags": [{"id": "t1", "name": "meetings"}], "timeInterval": {"start": iso(T0 - 5000), "end": iso(T0 - 4000)}},
        ]
        self.c_tags = [{"id": "t1", "name": "meetings"}]
        self.c_projects = [{"id": "p1", "name": "Internal", "billable": True, "archived": False}]


MOCK = Mock()
MOCK.reset()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, status, body, headers=None):
        data = json.dumps(body).encode()
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self, method):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        MOCK.requests.append({"method": method, "path": u.path, "query": parse_qs(u.query), "headers": dict(self.headers), "body": body})
        if MOCK.delay:
            time.sleep(MOCK.delay)
        if MOCK.force:
            return self.send(*MOCK.force)
        if u.path.startswith("/toggl/"):
            want = "Basic " + base64.b64encode(f"{TOGGL_TOKEN}:api_token".encode()).decode()
            if self.headers.get("Authorization") != want:
                return self.send(403, "Incorrect username and/or password")
            return self.toggl(method, u.path[len("/toggl"):], body)
        if u.path.startswith("/clockify/"):
            if self.headers.get("X-Api-Key") != CLOCKIFY_TOKEN:
                return self.send(401, {"message": "Full authentication is required", "code": 1000})
            return self.clockify(method, u.path[len("/clockify"):], body)
        self.send(404, {})

    def toggl(self, method, path, body):
        m = MOCK
        if path == "/me":
            return self.send(200, {"id": 5, "fullname": "Ada Lovelace", "default_workspace_id": 11})
        if path == "/me/workspaces":
            return self.send(200, [{"id": 11, "name": "Personal"}, {"id": 12, "name": "Team"}])
        if path == "/me/time_entries":
            return self.send(200, m.toggl_entries)
        r = re.fullmatch(r"/workspaces/(\d+)/projects", path)
        if r:
            return self.send(200, m.toggl_projects.get(int(r.group(1)), []))
        r = re.fullmatch(r"/workspaces/(\d+)/tags", path)
        if r:
            return self.send(200, [{"id": 7, "name": "Deep Work"}, {"id": 8, "name": "writing"}])
        r = re.fullmatch(r"/workspaces/(\d+)/time_entries", path)
        if r and method == "POST":
            for e in m.toggl_entries:
                if e["duration"] < 0:
                    e["duration"], e["stop"] = 60, iso(T0)
            m.next_id += 1
            proj = next((p for ps in m.toggl_projects.values() for p in ps if p["id"] == body.get("project_id")), None)
            e = {"id": m.next_id, "workspace_id": body["workspace_id"], "description": body["description"], "project_id": body.get("project_id"),
                 "project_name": None, "tags": body.get("tags") or None, "start": body["start"], "stop": None, "duration": -1,
                 "billable": body.get("billable", False)}
            m.toggl_entries.insert(0, dict(e, project_name=proj["name"] if proj else None))
            return self.send(200, e)
        r = re.fullmatch(r"/workspaces/(\d+)/time_entries/(\d+)/stop", path)
        if r and method == "PATCH":
            for e in m.toggl_entries:
                if e["id"] == int(r.group(2)):
                    if e["duration"] >= 0:
                        return self.send(409, "Time entry already stopped")
                    e["duration"], e["stop"] = 100, iso(T0)
                    return self.send(200, e)
            return self.send(404, "not found")
        self.send(404, "no route")

    def clockify(self, method, path, body):
        m = MOCK
        if path == "/user":
            return self.send(200, {"id": "u1", "name": "Grace Hopper", "activeWorkspace": "w1", "defaultWorkspace": "w1"})
        if path == "/workspaces":
            return self.send(200, [{"id": "w1", "name": "Main"}])
        if path == "/workspaces/w1/projects":
            return self.send(200, m.c_projects)
        if path == "/workspaces/w1/tags":
            if method == "POST":
                t = {"id": f"t{len(m.c_tags) + 1}", "name": body["name"]}
                m.c_tags.append(t)
                return self.send(201, t)
            return self.send(200, m.c_tags)
        if path == "/workspaces/w1/user/u1/time-entries":
            if method == "PATCH":
                run = [e for e in m.c_entries if not e["timeInterval"]["end"]]
                if not run:
                    return self.send(404, {"message": "No running time entry"})
                run[0]["timeInterval"]["end"] = body["end"]
                return self.send(200, run[0])
            return self.send(200, m.c_entries)
        if path == "/workspaces/w1/time-entries" and method == "POST":
            for e in m.c_entries:
                if not e["timeInterval"]["end"]:
                    e["timeInterval"]["end"] = body["start"]
            m.next_id += 1
            tags = [t for t in m.c_tags if t["id"] in body.get("tagIds", [])]
            proj = next((p for p in m.c_projects if p["id"] == body.get("projectId")), None)
            e = {"id": f"e{m.next_id}", "workspaceId": "w1", "description": body["description"], "projectId": body.get("projectId"),
                 "tagIds": body.get("tagIds"), "billable": body.get("billable"), "timeInterval": {"start": body["start"], "end": None}}
            m.c_entries.insert(0, dict(e, project={"name": proj["name"]} if proj else None, tags=tags))
            return self.send(201, e)
        self.send(404, {"message": "no route"})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PATCH(self):
        self.handle_any("PATCH")


SERVER = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=SERVER.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{SERVER.server_address[1]}"


# ---------------------------------------------------------------- harness

def validate(data):
    assert isinstance(data.get("items"), list), data
    for it in data["items"]:
        assert isinstance(it.get("title"), str) and it["title"], it
        assert os.path.exists(os.path.join(SRC, it["icon"]["path"])), it["icon"]
        if it.get("valid", True) is not False:
            assert "arg" in it, it
        for m in (it.get("mods") or {}).values():
            assert "subtitle" in m and "arg" in m, m


class Base(unittest.TestCase):
    extra_env = {}

    def setUp(self):
        MOCK.reset()
        self.tmp = tempfile.mkdtemp(prefix="focus-timer-test-")
        self.data = os.path.join(self.tmp, "data")
        self.cache = os.path.join(self.tmp, "cache")
        self.log = os.path.join(self.tmp, "effects.log")
        self.keys = os.path.join(self.tmp, "keys.json")
        self.env = dict(os.environ, alfred_workflow_data=self.data, alfred_workflow_cache=self.cache, FT_TEST_LOG=self.log,
                        FT_NO_WAITER="1", FT_NO_BACKGROUND="1", FT_KEYCHAIN_FILE=self.keys, FT_NOW=str(T0),
                        FT_TOGGL_URL=BASE + "/toggl", FT_CLOCKIFY_URL=BASE + "/clockify",
                        alfred_workflow_bundleid="io.github.x-o-r-r-o.focus-timer", TZ="Europe/Paris")
        self.env.update(self.extra_env)
        for k in ("tracker", "link_tracker", "auto_breaks", "auto_focus", "shortcut_start", "shortcut_end", "show_alfred"):
            if k not in self.extra_env:
                self.env.pop(k, None)

    def tearDown(self):
        # Detached helpers (effects, track-refresh, shortcut-failed) name the script by its full path
        # rather than the temporary folder: give them a moment to finish, then make sure.
        helpers = f"{SRC}/focus.js "
        self.wait_for(lambda: subprocess.run(["pgrep", "-f", helpers], capture_output=True).returncode == 1, 10)
        for pattern in (self.tmp, helpers):
            subprocess.run(["pkill", "-f", pattern], capture_output=True)
        time.sleep(0.1)
        left = subprocess.run(["pgrep", "-fl", self.tmp], capture_output=True, text=True).stdout
        left += subprocess.run(["pgrep", "-fl", helpers], capture_output=True, text=True).stdout
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual(left, "", "background processes left running")

    def run_js(self, cmd, arg="", now=None, **env):
        e = dict(self.env, **{k: str(v) for k, v in env.items()})
        if now is not None:
            e["FT_NOW"] = str(now)
        out = subprocess.run(["osascript", "-l", "JavaScript", "./focus.js", cmd, arg], cwd=SRC, env=e, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def sf(self, cmd, query="", now=None, full=False, **env):
        data = json.loads(self.run_js(cmd, query, now, **env))
        validate(data)
        return data if full else data["items"]

    def act(self, arg, now=None, **env):
        return self.run_js("action", arg if isinstance(arg, str) else json.dumps(arg), now, **env).strip()

    def keychain(self):
        with open(self.keys) as f:
            return json.load(f)

    def state(self):
        with open(os.path.join(self.data, "state.json")) as f:
            return json.load(f)

    def sessions(self):
        p = os.path.join(self.data, "sessions.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(l) for l in f if l.strip()]

    def effects(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return f.read().splitlines()

    def find(self, items, prefix):
        for i in items:
            if i["title"].startswith(prefix):
                return i
        raise AssertionError(f"no item starting with {prefix!r}: {[i['title'] for i in items]}")

    def arg(self, it, mod=None):
        return json.loads(it["mods"][mod]["arg"] if mod else it["arg"])

    def start(self, secs=1500, kind="focus", label="", now=T0, **env):
        return self.act({"a": "start", "kind": kind, "secs": secs, "label": label, "expect": self.current()}, now, **env)

    def current(self):
        try:
            s = self.state()
        except FileNotFoundError:
            return ""
        return s["id"] if s["status"] in ("running", "paused") else ""

    def wait_for(self, cond, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.1)
        return False


# ---------------------------------------------------------------- Pomodoro

class PomodoroTests(Base):
    def test_idle_menu(self):
        it = self.sf("pomo")
        self.assertEqual([i["title"] for i in it[:3]], ["Start Focus · 25 min", "Start Short Break · 5 min", "Start Long Break · 15 min"])
        self.assertEqual(self.arg(it[0]), {"a": "start", "kind": "focus", "secs": 1500, "label": "", "expect": ""})
        self.assertTrue(it[3]["title"].startswith("Today: 0 pomodoros of 8"))
        it = self.sf("pomo", focus_minutes="50", short_minutes="abc", daily_goal="0")
        self.assertEqual(it[0]["title"], "Start Focus · 50 min")
        self.assertEqual(it[1]["title"], "Start Short Break · 5 min")
        self.assertEqual(it[3]["title"], "Today: 0 pomodoros · 0 min")

    def test_durations_and_labels(self):
        cases = {"50": (3000, ""), "1h30": (5400, ""), "1h 30m": (5400, ""), "1 h 30": (5400, ""), "90s": (90, ""),
                 "1.5h": (5400, ""), "45 min write it": (2700, "write it"), "25 5 things": (1500, "5 things"),
                 "write report": (1500, "write report"), "2h deep work": (7200, "deep work"), "10m": (600, ""), "1h30m20s": (5420, ""), "2 hours": (7200, "")}
        for q, (secs, label) in cases.items():
            a = self.arg(self.sf("pomo", q)[-1])
            self.assertEqual((a["kind"], a["secs"], a["label"]), ("focus", secs, label), q)
        a = self.arg(self.sf("pomo", "short 10")[0])
        self.assertEqual((a["kind"], a["secs"]), ("short", 600))
        a = self.arg(self.sf("pomo", "long")[0])
        self.assertEqual((a["kind"], a["secs"]), ("long", 900))
        self.assertEqual(self.sf("pomo", "25h")[0]["title"], "Sessions can be up to 24 hours")
        self.assertEqual(self.sf("pomo", "0")[0]["title"], "Sessions can be up to 24 hours")

    def test_unicode_quotes_newlines(self):
        label = 'Write "the" café 📝 report\\n'
        a = self.arg(self.sf("pomo", f"20 {label}\n  second line")[-1])
        self.assertEqual(a["label"], 'Write "the" café 📝 report\\n second line')
        self.assertIn("started", self.act(dict(a, expect="")))
        self.assertEqual(self.state()["label"], a["label"])
        self.act({"a": "stop", "id": self.state()["id"]}, T0 + 60)
        self.assertEqual(self.sessions()[0]["label"], a["label"])

    def test_long_label_is_cut_between_characters(self):
        a = self.arg(self.sf("pomo", "10 " + "a" * 195 + "😀😀😀")[-1])  # UTF-16 cut would split the 2nd emoji
        self.assertEqual(a["label"], "a" * 195 + "😀…")
        a["label"].encode("utf-8")  # no lone surrogate

    def test_running_view_and_rerun(self):
        self.assertIn("Focus started: 25 min", self.start(label="Deep work"))
        data = self.sf("pomo", now=T0 + 60, full=True)
        self.assertEqual(data["rerun"], 1)
        head = data["items"][0]
        self.assertEqual(head["title"], "Focus · 24:00 left · Deep work")
        self.assertIn("Pomodoro 1 of 4", head["subtitle"])
        self.assertEqual(self.arg(head)["a"], "pause")
        self.assertEqual(self.arg(head, "cmd")["a"], "stop")
        self.assertEqual(self.arg(head, "alt")["a"], "skip")
        self.assertEqual(self.find(data["items"], "Skip to")["title"], "Skip to Short Break")

    def test_double_start_is_refused(self):
        stale = {"a": "start", "kind": "focus", "secs": 1500, "label": "", "expect": ""}
        self.act(stale)
        first = self.state()["id"]
        self.assertIn("changed", self.act(stale, T0 + 1))
        self.assertEqual(self.state()["id"], first)
        self.assertEqual(self.sessions(), [])
        # replacing on purpose works and logs the replaced session as stopped
        self.assertIn("started", self.act(dict(stale, secs=600, expect=first), T0 + 120))
        self.assertEqual(self.sessions()[0]["outcome"], "stopped")
        self.assertEqual(self.sessions()[0]["focused"], 120)

    def test_parallel_starts_only_one_wins(self):
        arg = json.dumps({"a": "start", "kind": "focus", "secs": 1500, "label": "", "expect": ""})
        e = dict(self.env)
        procs = [subprocess.Popen(["osascript", "-l", "JavaScript", "./focus.js", "action", arg], cwd=SRC, env=e,
                                  stdout=subprocess.PIPE, text=True) for _ in range(6)]
        outs = [p.communicate(timeout=60)[0] for p in procs]
        self.assertEqual(sum("Focus started" in o for o in outs), 1, outs)
        self.assertEqual(sum("changed" in o for o in outs), 5, outs)

    def test_pause_resume_stop(self):
        self.start()
        sid = self.state()["id"]
        self.assertIn("paused with 23:20 left", self.act({"a": "pause", "id": sid}, T0 + 100))
        it = self.sf("pomo", now=T0 + 5000)
        self.assertEqual(it[0]["title"], "Paused · Focus · 23:20 left")
        self.assertEqual(self.arg(it[0])["a"], "resume")
        self.act({"a": "resume", "id": sid}, T0 + 1000)
        self.assertEqual(self.state()["end"], T0 + 1000 + 1400)
        self.assertIn("stopped after 17 min", self.act({"a": "stop", "id": sid}, T0 + 1000 + 900))
        self.assertEqual(self.sessions()[0]["focused"], 1000)
        self.assertEqual(self.state()["status"], "idle")
        self.assertIn("already ended", self.act({"a": "pause", "id": sid}, T0 + 2000))

    def test_completion_when_waiter_is_gone(self):
        self.start(label="Report", shortcut_start="Focus On")
        self.assertIn("shortcut:Focus On", self.effects())
        it = self.sf("pomo", now=T0 + 1501, shortcut_end="Focus Off")
        self.assertTrue(it[0]["title"].startswith("Start Short Break"), it[0])
        self.assertTrue(it[0]["subtitle"].startswith("Focus complete"))
        s = self.state()
        self.assertEqual((s["status"], s["count"], s["next"]), ("done", 1, "short"))
        self.assertEqual(self.sessions()[0]["outcome"], "completed")
        fx = self.effects()
        self.assertIn("sound:Glass", fx)
        self.assertIn("trigger:notify:Focus complete: Report. Time for a short break.", fx)
        self.assertIn("shortcut:Focus Off", fx)
        # idempotent: nothing happens twice
        self.sf("pomo", now=T0 + 1600)
        self.assertEqual(len(self.sessions()), 1)

    def test_late_completion_is_quiet(self):
        self.start(label="Late", auto_breaks="1")
        self.sf("pomo", now=T0 + 1500 + 3600, auto_breaks="1")
        fx = self.effects()
        self.assertNotIn("sound:Glass", fx)
        self.assertTrue(any(f.startswith("trigger:notify:Focus ended at") for f in fx), fx)
        self.assertEqual(self.state()["status"], "done")  # no auto-start after a long sleep

    def test_long_break_cycle_and_reset(self):
        t = T0
        for n in range(4):
            self.start(secs=60, now=t)
            t += 61
            self.sf("pomo", now=t)
            if n < 3:
                self.start(secs=60, kind="short", now=t)
                t += 61
                self.sf("pomo", now=t)
        s = self.state()
        self.assertEqual((s["count"], s["next"]), (4, "long"))
        self.assertTrue(self.sf("pomo", now=t)[0]["title"].startswith("Start Long Break"))
        self.start(secs=60, kind="long", now=t)
        self.sf("pomo", now=t + 61)
        self.assertEqual((self.state()["count"], self.state()["next"]), (0, "focus"))
        # after 3 idle hours the count starts over
        self.start(secs=60, now=t + 100)
        self.sf("pomo", now=t + 161)
        self.assertEqual(self.state()["next"], "short")
        it = self.sf("pomo", now=t + 161 + 4 * 3600)
        self.assertTrue(it[0]["title"].startswith("Start Focus"))
        self.assertIn("Pomodoro 1 of 4", it[0]["subtitle"])

    def test_skip_and_extend(self):
        self.start()
        sid = self.state()["id"]
        self.assertEqual(self.sf("pomo", "+10", now=T0 + 10)[1]["title"], "Add 10 min")
        self.assertIn("Added 5 min", self.act({"a": "extend", "id": sid, "secs": 300}, T0 + 10))
        self.assertEqual(self.state()["end"], T0 + 1800)
        msg = self.act({"a": "skip", "id": sid}, T0 + 20)
        self.assertIn("Focus skipped. Short Break started", msg)
        s = self.state()
        self.assertEqual((s["kind"], s["status"], s["count"]), ("short", "running", 1))
        self.assertEqual(self.sessions()[0]["outcome"], "skipped")
        self.assertEqual(self.sf("pomo", "stats")[0]["title"], "Today: 0 pomodoros · 20 s focused")

    def test_auto_start_break(self):
        self.start(label="A", auto_breaks="1")
        self.sf("pomo", now=T0 + 1500, auto_breaks="1")
        s = self.state()
        self.assertEqual((s["kind"], s["status"], s["start"]), ("short", "running", T0 + 1500))
        self.assertTrue(any("Short Break started" in f for f in self.effects()))
        # and back to focus with the same label
        self.sf("pomo", now=T0 + 1800, auto_focus="1")
        self.assertEqual((self.state()["kind"], self.state()["label"]), ("focus", "A"))

    def test_show_alfred_at_end(self):
        self.start(show_alfred="1")
        self.sf("pomo", now=T0 + 1500, show_alfred="1")
        self.assertIn("trigger:pomo:", self.effects())

    def test_breaks_do_not_run_shortcuts(self):
        self.start(kind="short", secs=300, shortcut_start="On", shortcut_end="Off")
        self.sf("pomo", now=T0 + 300, shortcut_start="On", shortcut_end="Off")
        self.assertFalse(any(f.startswith("shortcut:") for f in self.effects()))

    def test_command_prefixes(self):
        self.start()
        titles = [i["title"] for i in self.sf("pomo", "st", now=T0 + 5)]
        self.assertIn("Stop", titles)
        self.assertTrue(any(t.startswith("Today:") for t in titles))
        self.assertIn("Pause", [i["title"] for i in self.sf("pomo", "pa", now=T0 + 5)])

    def test_corrupt_files(self):
        os.makedirs(self.data)
        with open(os.path.join(self.data, "state.json"), "w") as f:
            f.write("{not json")
        with open(os.path.join(self.data, "sessions.jsonl"), "w") as f:
            f.write('{"kind":"focus","end":%d,"focused":1500,"outcome":"completed"}\n{"kind":"foc' % T0)
        it = self.sf("pomo")
        self.assertTrue(it[0]["title"].startswith("Start Focus"))
        self.assertEqual(self.sf("pomo", "stats")[0]["title"], "Today: 1 pomodoro · 25 min focused")

    # --- audit pass 1 regressions ---

    def test_lock_left_by_a_killed_script_filter_is_taken_over(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        os.makedirs(os.path.join(self.data, "lock"))
        with open(os.path.join(self.data, "lock", "pid"), "w") as f:
            f.write(str(dead.pid))
        t = time.time()
        self.assertIn("Focus started", self.start())
        self.assertLess(time.time() - t, 3)

    def test_lock_with_a_reused_pid_is_taken_over(self):
        other = subprocess.Popen(["sleep", "30"])
        try:
            os.makedirs(os.path.join(self.data, "lock"))
            with open(os.path.join(self.data, "lock", "pid"), "w") as f:
                f.write(str(other.pid))
            self.assertIn("Focus started", self.start())
        finally:
            other.kill()
            other.wait()

    def test_lock_held_by_a_live_owner_waits(self):
        holder = subprocess.Popen(["/bin/bash", "-c", "sleep 30; true", "focus.js"])
        time.sleep(0.2)
        try:
            os.makedirs(os.path.join(self.data, "lock"))
            with open(os.path.join(self.data, "lock", "pid"), "w") as f:
                f.write(str(holder.pid))
            self.assertIn("busy", self.start(FT_LOCK_TIMEOUT="0.5"))
            self.assertIn("busy", self.sf("pomo", FT_LOCK_TIMEOUT="0.5")[0]["subtitle"])
        finally:
            holder.kill()
            holder.wait()

    def test_pause_at_the_end_completes_instead(self):
        self.start()
        sid = self.state()["id"]
        self.assertEqual(self.act({"a": "pause", "id": sid}, T0 + 1500), "That timer has already ended. Focus complete. Time for a short break.")
        self.assertEqual(self.state()["status"], "done")
        self.assertEqual(len(self.sessions()), 1)

    def test_durations_round_to_hours(self):
        self.start(secs=7200)
        self.assertIn("stopped after 1 h.", self.act({"a": "stop", "id": self.state()["id"]}, T0 + 3570))

    # --- audit pass 3 regressions ---

    def test_lock_with_our_own_reused_pid_after_reboot(self):
        # After a reboot, the PID in a leftover lock can be the very process asking for it.
        # bash writes its PID and then execs osascript, which keeps that PID.
        os.makedirs(os.path.join(self.data, "lock"))
        arg = json.dumps({"a": "start", "kind": "focus", "secs": 60, "label": "", "expect": ""})
        e = dict(self.env, FT_LOCK_TIMEOUT="1")
        out = subprocess.run(["/bin/bash", "-c", 'echo $$ > "$0/lock/pid"; exec osascript -l JavaScript ./focus.js action "$1"', self.data, arg],
                             cwd=SRC, env=e, capture_output=True, text=True, timeout=30).stdout
        self.assertIn("Focus started", out)

    def test_plus_without_a_session(self):
        it = self.sf("pomo", "+10")
        self.assertEqual([i["title"] for i in it], ["No session is running"])

    def test_stale_lock_is_recovered(self):
        os.makedirs(os.path.join(self.data, "lock"))
        old = time.time() - 60
        os.utime(os.path.join(self.data, "lock"), (old, old))
        self.assertIn("started", self.start())

    # --- audit pass 4 regressions ---

    def test_damaged_state_falls_back_to_idle(self):
        os.makedirs(self.data)
        bad = ['{"status":"running"}', '{"status":"running","id":"abc","end":null}', '{"status":"running","id":"0123456789abcdef0123","end":"zz","secs":"q","kind":"focus","start":1}',
               '{"status":"paused","id":"0123456789abcdef0123","left":"x","secs":100,"kind":"bogus","start":1}', '[1,2]', 'null', '"str"',
               '{"status":"done","lastKind":"zzz","count":"7","next":"zz","track":5}', '{"status":"weird","count":-3,"lastDone":"x"}']
        for text in bad:
            with open(os.path.join(self.data, "state.json"), "w") as f:
                f.write(text)
            with open(os.path.join(self.data, "running"), "w") as f:
                f.write("abc 1\n")
            it = self.sf("pomo")
            self.assertTrue(it[0]["title"].startswith("Start Focus"), (text, it[0]))
            self.assertIn("Pomodoro 1 of 4", it[0]["subtitle"], text)
            self.assertFalse(any("NaN" in i["title"] + i["subtitle"] for i in it), text)
            self.assertEqual(self.sf("pomo", "+5")[0]["title"], "No session is running", text)
            self.assertFalse(os.path.exists(os.path.join(self.data, "running")), text)
            self.assertIn("Focus started", self.act({"a": "start", "kind": "focus", "secs": 60, "label": "", "expect": ""}), text)
            self.assertEqual(self.state()["status"], "running")
            os.remove(os.path.join(self.data, "state.json"))

    def test_valid_paused_state_is_kept(self):
        self.start()
        sid = self.state()["id"]
        self.act({"a": "pause", "id": sid}, T0 + 100)
        self.assertEqual(self.sf("pomo", now=T0 + 200)[0]["title"], "Paused · Focus · 23:20 left")

    def test_shortcut_that_cannot_run_is_reported(self):
        fake = os.path.join(self.tmp, "shortcuts")
        with open(fake, "w") as f:
            f.write('#!/bin/bash\nprintf "%s\\n" "$@" > "$0.args"\nexit 1\n')
        os.chmod(fake, 0o755)
        self.start(shortcut_start="-Focus On", FT_SHORTCUTS=fake)
        self.assertTrue(self.wait_for(lambda: any(f.startswith("trigger:notify:The Shortcut “-Focus On” didn’t run") for f in self.effects())), self.effects())
        with open(fake + ".args") as f:
            self.assertEqual(f.read().splitlines(), ["run", "--", "-Focus On"])  # a name, never an option


class StatsTests(Base):
    extra_env = {"TZ": "America/New_York", "FT_FIRST_WEEKDAY": "2"}

    def write_log(self, entries):
        os.makedirs(self.data, exist_ok=True)
        with open(os.path.join(self.data, "sessions.jsonl"), "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def local(self, y, m, d, h=12):
        # epoch for a local New York time (the environment's TZ, via time.mktime)
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        return int(time.mktime((y, m, d, h, 0, 0, 0, 0, -1)))

    def test_streak_across_dst(self):
        # DST starts in New York on 2026-03-08: that day has 23 hours
        days = [(2026, 3, 5), (2026, 3, 7), (2026, 3, 8), (2026, 3, 9)]
        ent = [{"kind": "focus", "end": self.local(*d, h=0 if i == 2 else 23), "focused": 1500, "outcome": "completed"} for i, d in enumerate(days)]
        ent.append({"kind": "focus", "end": self.local(2026, 3, 9, 1), "focused": 600, "outcome": "stopped"})
        ent.append({"kind": "short", "end": self.local(2026, 3, 9, 2), "focused": 300, "outcome": "completed"})
        self.write_log(ent)
        it = self.sf("pomo", "stats", now=self.local(2026, 3, 9, 23, ) + 1800)
        self.assertEqual(it[0]["title"], "Today: 1 pomodoro · 35 min focused")
        self.assertEqual(it[2]["title"], "Streak: 3 days")
        self.assertIn("Best streak: 3 days · 4 pomodoros in total", it[2]["subtitle"])
        # the streak survives until the end of the next day, then breaks
        self.assertEqual(self.sf("pomo", "stats", now=self.local(2026, 3, 10, 22))[2]["title"], "Streak: 3 days")
        self.assertEqual(self.sf("pomo", "stats", now=self.local(2026, 3, 11, 9))[2]["title"], "Streak: 0 days")

    def test_week_start_follows_locale(self):
        # 2026-09-20 is a Sunday
        self.write_log([{"kind": "focus", "end": self.local(2026, 9, 20), "focused": 1500, "outcome": "completed"},
                        {"kind": "focus", "end": self.local(2026, 9, 22), "focused": 1500, "outcome": "completed"}])
        now = self.local(2026, 9, 23)
        self.assertTrue(self.sf("pomo", "stats", now=now)[1]["title"].startswith("This week: 1 pomodoro"))
        self.assertTrue(self.sf("pomo", "stats", now=now, FT_FIRST_WEEKDAY="1")[1]["title"].startswith("This week: 2 pomodoros"))
        self.assertIn("This month: 2 pomodoros", self.sf("pomo", "stats", now=now)[1]["subtitle"])

    def test_goal_and_recent(self):
        self.write_log([{"kind": "focus", "end": self.local(2026, 9, 23, h), "focused": 1500, "outcome": "completed", "label": f"Task {h}"} for h in (9, 10)])
        it = self.sf("pomo", "stats", now=self.local(2026, 9, 23, 18), daily_goal="4")
        self.assertIn("●●○○", it[0]["subtitle"])
        self.assertEqual(it[3]["title"], "Focus: Task 10")
        self.assertEqual(self.arg(it[-1]), {"a": "reveal"})
        self.assertEqual(self.sf("pomo", "stats", now=self.local(2026, 9, 23, 18), daily_goal="2")[0]["subtitle"].split("  ")[1], "Daily goal reached")

    def test_streak_across_months_and_years(self):
        days = [(2026, 12, 30), (2026, 12, 31), (2027, 1, 1), (2027, 1, 2), (2027, 2, 27), (2027, 2, 28), (2027, 3, 1)]
        self.write_log([{"kind": "focus", "end": self.local(*d), "focused": 1500, "outcome": "completed"} for d in days])
        it = self.sf("pomo", "stats", now=self.local(2027, 3, 1, 20))
        self.assertEqual(it[2]["title"], "Streak: 3 days")
        self.assertIn("Best streak: 4 days · 7 pomodoros in total", it[2]["subtitle"])
        self.assertTrue(it[1]["subtitle"].startswith("This month: 1 pomodoro"))
        it = self.sf("pomo", "stats", now=self.local(2027, 1, 2, 20))
        self.assertEqual(it[2]["title"], "Streak: 4 days")
        self.assertTrue(it[1]["title"].startswith("This week: 4 pomodoros"), it[1])  # Mon 2026-12-28 to Sat 2027-01-02
        self.assertTrue(it[1]["subtitle"].startswith("This month: 2 pomodoros"), it[1])

    def test_days_follow_the_current_time_zone(self):
        # 23:30 in New York is 04:30 the next day in London (after flying, or changing the zone)
        self.write_log([{"kind": "focus", "end": self.local(2026, 9, 22, 23) + 1800, "focused": 1500, "outcome": "completed"}])
        now = self.local(2026, 9, 23, 12)
        self.assertEqual(self.sf("pomo", "stats", now=now)[0]["title"], "Today: 0 pomodoros · 0 min focused")
        self.assertEqual(self.sf("pomo", "stats", now=now, TZ="Europe/London")[0]["title"], "Today: 1 pomodoro · 25 min focused")
        self.assertIn("at 04:30", self.sf("pomo", "stats", now=now, TZ="Europe/London")[3]["subtitle"].replace("\u202f", " ").replace("4:30 AM", "04:30"))

    def test_first_weekday_saturday(self):
        # 2026-09-19 is a Saturday
        self.write_log([{"kind": "focus", "end": self.local(2026, 9, d), "focused": 1500, "outcome": "completed"} for d in (18, 19, 23)])
        self.assertTrue(self.sf("pomo", "stats", now=self.local(2026, 9, 23), FT_FIRST_WEEKDAY="7")[1]["title"].startswith("This week: 2 pomodoros"))


class WaiterTests(Base):
    """Real background waiters with very short sessions (no injected clock)."""

    def setUp(self):
        super().setUp()
        for k in ("FT_NO_WAITER", "FT_NOW"):
            self.env.pop(k)
        self.env["FT_WAITER_STEP"] = "1"

    def waiter_on_record(self):
        try:
            return self.state().get("pid", 0) != 0
        except (OSError, ValueError):
            return False

    def waiters(self):
        return subprocess.run(["pgrep", "-f", f"waiter.sh .* {self.data}"], capture_output=True, text=True).stdout.split()

    def test_waiter_fires_and_exits(self):
        self.act({"a": "start", "kind": "focus", "secs": 2, "label": "Quick", "expect": ""})
        self.assertEqual(len(self.waiters()), 1)
        self.assertTrue(self.wait_for(lambda: any("Focus complete: Quick" in f for f in self.effects())), self.effects())
        self.assertTrue(self.wait_for(lambda: not self.waiters(), 5))
        self.assertEqual(self.state()["status"], "done")
        self.assertIn("sound:Glass", self.effects())

    def test_pause_and_stop_kill_the_waiter(self):
        self.act({"a": "start", "kind": "focus", "secs": 60, "label": "", "expect": ""})
        sid = self.state()["id"]
        self.act({"a": "pause", "id": sid})
        self.assertTrue(self.wait_for(lambda: not self.waiters(), 3))
        self.act({"a": "resume", "id": sid})
        self.assertEqual(len(self.waiters()), 1)
        self.act({"a": "stop", "id": sid})
        self.assertTrue(self.wait_for(lambda: not self.waiters(), 3))

    def test_extend_is_seen_by_the_waiter(self):
        self.act({"a": "start", "kind": "focus", "secs": 2, "label": "", "expect": ""})
        self.act({"a": "extend", "id": self.state()["id"], "secs": 2})
        time.sleep(2.5)
        self.assertEqual(self.state()["status"], "running")
        self.assertTrue(self.wait_for(lambda: self.state()["status"] == "done", 6))

    def test_overdue_session_completes_once_with_a_live_waiter(self):
        self.act({"a": "start", "kind": "focus", "secs": 60, "label": "", "expect": ""})
        self.assertEqual(len(self.waiters()), 1)
        self.sf("pomo", now=int(time.time()) + 61)
        self.assertEqual(self.state()["status"], "done")
        self.assertTrue(self.wait_for(lambda: not self.waiters(), 5))
        self.assertEqual(len(self.sessions()), 1)
        self.assertEqual(sum(f.startswith("trigger:notify:") for f in self.effects()), 1)

    def test_missing_running_file_is_restored(self):
        # a run killed between writing state.json and the running file
        self.act({"a": "start", "kind": "focus", "secs": 60, "label": "", "expect": ""})
        subprocess.run(["pkill", "-f", f"waiter.sh .* {self.data}"])
        os.remove(os.path.join(self.data, "running"))
        self.sf("pomo")
        self.assertTrue(os.path.exists(os.path.join(self.data, "running")))
        time.sleep(1.5)
        self.assertEqual(len(self.waiters()), 1)

    def test_waiter_has_its_own_process_group(self):
        # Alfred may kill a terminated Script Filter's process group; the waiter must not be in it
        self.act({"a": "start", "kind": "focus", "secs": 30, "label": "", "expect": ""})
        pid = self.state()["pid"]
        pgid = int(subprocess.run(["ps", "-o", "pgid=", "-p", str(pid)], capture_output=True, text=True).stdout)
        self.assertEqual(pgid, pid)

    def test_stale_pid_after_reboot(self):
        # A PID recorded before a reboot may now belong to an unrelated process: it must not be
        # killed, and the session must get a new waiter (or finish if it's overdue).
        other = subprocess.Popen(["sleep", "30"])
        try:
            self.act({"a": "start", "kind": "focus", "secs": 30, "label": "", "expect": ""})
            subprocess.run(["pkill", "-f", f"waiter.sh .* {self.data}"])
            time.sleep(0.2)
            s = self.state()
            s["pid"] = other.pid
            with open(os.path.join(self.data, "state.json"), "w") as f:
                json.dump(s, f)
            self.sf("pomo")
            self.assertIsNone(other.poll(), "unrelated process was killed")
            self.assertEqual(len(self.waiters()), 1)
            self.assertNotEqual(self.state()["pid"], other.pid)
            self.act({"a": "pause", "id": s["id"]})
            self.assertIsNone(other.poll(), "unrelated process was killed on pause")
            # overdue session with a dead waiter finishes on the next look
            self.act({"a": "resume", "id": s["id"]}, )
            s = self.state()
            subprocess.run(["pkill", "-f", f"waiter.sh .* {self.data}"])
            s["end"] = int(time.time()) - 5
            s["pid"] = other.pid
            with open(os.path.join(self.data, "state.json"), "w") as f:
                json.dump(s, f)
            with open(os.path.join(self.data, "running"), "w") as f:
                f.write(f"{s['id']} {s['end']}\n")
            self.sf("pomo")
            self.assertEqual(self.state()["status"], "done")
            self.assertIsNone(other.poll())
        finally:
            other.kill()
            other.wait()

    # --- audit pass 4 regressions ---

    def test_waiter_survives_alfred_killing_the_script_filter(self):
        # Alfred terminates a running Script Filter when you type. Kill the Script Filter's whole
        # process group as soon as it has started a waiter (a session whose waiter died): the waiter
        # must survive and fire. Whatever the moment of the kill, the session must never be lost.
        killed_mid_run = 0
        for n in range(4):
            sid = f"{n:020x}"
            end = int(time.time()) + 2
            os.makedirs(self.data, exist_ok=True)
            with open(os.path.join(self.data, "state.json"), "w") as f:
                json.dump({"status": "running", "id": sid, "kind": "focus", "label": f"K{n}", "secs": 60, "start": end - 60, "end": end,
                           "pid": 0, "count": 0, "next": "focus", "lastDone": 0}, f)
            with open(os.path.join(self.data, "running"), "w") as f:
                f.write(f"{sid} {end}\n")
            p = subprocess.Popen(["/bin/bash", "-c", 'osascript -l JavaScript ./focus.js pomo "$1"', "sf", ""], cwd=SRC, env=self.env,
                                 start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            while p.poll() is None and not self.waiter_on_record():
                time.sleep(0.001)
            try:
                os.killpg(p.pid, signal.SIGKILL if n % 2 else signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            p.wait()
            killed_mid_run += p.returncode < 0
            if not self.waiters():
                self.sf("pomo")  # killed before it got that far: the next look starts the waiter
            self.assertEqual(len(self.waiters()), 1)
            self.assertTrue(self.wait_for(lambda: any(f"Focus complete: K{n}." in f for f in self.effects()), 8), self.effects())
            self.assertTrue(self.wait_for(lambda: not self.waiters(), 5))
        self.assertGreater(killed_mid_run, 0)
        self.assertEqual(len(self.sessions()), 4)

    def test_time_added_as_the_session_ends(self):
        # The waiter read the old end time, then time was added before it asked focus.js to complete
        # the session: it must keep watching and fire at the new end, not exit.
        self.act({"a": "start", "kind": "focus", "secs": 60, "label": "Late add", "expect": ""})
        subprocess.run(["pkill", "-f", f"waiter.sh .* {self.data}"])
        s = self.state()
        new_end = int(time.time()) + 3
        s.update(end=new_end, secs=s["secs"] + 3, pid=0)
        with open(os.path.join(self.data, "state.json"), "w") as f:
            json.dump(s, f)
        with open(os.path.join(self.data, "running"), "w") as f:
            f.write(f"{s['id']} {int(time.time()) - 1}\n")  # what the waiter saw before the extension
        w = subprocess.Popen(["/bin/bash", os.path.join(SRC, "waiter.sh"), s["id"], self.data], env=self.env)
        time.sleep(1.5)
        with open(os.path.join(self.data, "running"), "w") as f:
            f.write(f"{s['id']} {new_end}\n")
        self.assertIsNone(w.poll(), "the waiter gave up after a no-op completion")
        self.assertTrue(self.wait_for(lambda: self.state()["status"] == "done", 8))
        self.assertEqual(w.wait(timeout=5), 0)

    def test_rapid_start_stop_skip_leaves_one_waiter(self):
        for step in range(3):
            self.start(secs=30, now=None)
            sid = self.state()["id"]
            self.act({"a": "skip", "id": sid})
            sid = self.state()["id"]
            self.act({"a": "pause", "id": sid})
            self.act({"a": "resume", "id": sid})
            self.act({"a": "extend", "id": sid, "secs": 60})
            self.act({"a": "skip", "id": sid})
            s = self.state()
            self.assertEqual(self.waiters(), [str(s["pid"])])  # exactly one, and it is the one on record
            self.act({"a": "stop", "id": s["id"]})
            self.assertTrue(self.wait_for(lambda: not self.waiters(), 3))


# ---------------------------------------------------------------- Time tracking

class TrackBase(Base):
    provider = "toggl"

    def setUp(self):
        super().setUp()
        self.env["tracker"] = self.provider
        with open(self.keys, "w") as f:
            json.dump({"toggl": TOGGL_TOKEN, "clockify": CLOCKIFY_TOKEN}, f)

    def tearDown(self):
        # the token must never end up in files, logs or Script Filter output
        for base, _, files in os.walk(self.tmp):
            for name in files:
                p = os.path.join(base, name)
                if p == self.keys:
                    continue
                with open(p, "rb") as f:
                    blob = f.read()
                for tok in (TOGGL_TOKEN, CLOCKIFY_TOKEN, base64.b64encode(f"{TOGGL_TOKEN}:api_token".encode()).decode()):
                    self.assertNotIn(tok.encode(), blob, p)
        super().tearDown()

    def paths(self, method=None):
        return [r["path"] for r in MOCK.requests if method is None or r["method"] == method]


class TogglTests(TrackBase):
    def test_setup_states(self):
        self.assertEqual(self.sf("track", tracker="none")[0]["title"], "Choose Toggl Track or Clockify")
        os.remove(self.keys)
        it = self.sf("track")
        self.assertEqual(it[0]["title"], "Set your Toggl Track API token")
        self.assertEqual(self.arg(it[0]), {"a": "token-set"})
        self.assertEqual(self.arg(it[0], "cmd")["url"], "https://track.toggl.com/profile")
        self.assertEqual(MOCK.requests, [])
        self.assertIn("doesn't look like", self.act({"a": "token-set"}, FT_TOKEN_INPUT='bad token"; rm -rf ~'))
        self.assertIn("Connected to Toggl Track as Ada Lovelace", self.act({"a": "token-set"}, FT_TOKEN_INPUT=TOGGL_TOKEN))
        self.assertEqual(self.keychain()["toggl"], TOGGL_TOKEN)

    def test_list_running_recent(self):
        MOCK.toggl_entries.insert(0, {"id": 9, "workspace_id": 11, "description": "Café ☕️ planning", "project_id": 103,
                                      "project_name": "Café Opening", "tags": [], "start": iso(T0 - 3725), "stop": None, "duration": -1})
        data = self.sf("track", full=True)
        it = data["items"]
        self.assertEqual(data["rerun"], 1)
        self.assertEqual(it[0]["title"], "▶ Café ☕️ planning · 1:02:05")
        self.assertIn("Today: 2 h 2 min tracked", it[0]["subtitle"])  # 1 h earlier today + the running 1:02:05
        self.assertEqual(self.arg(it[0])["a"], "track-stop")
        self.assertEqual([i["title"] for i in it[1:3]], ["Writing docs", 'Emails ✉️ "urgent"'])  # duplicates merged
        self.assertIn("@ Website Redesign · #writing · 1 h", it[1]["subtitle"])
        self.assertEqual(self.arg(it[1]), {"a": "track-start", "description": "Writing docs", "projectId": 101, "project": "Website Redesign", "tags": ["writing"]})
        self.assertTrue(self.arg(it[1], "cmd")["pomo"])
        auth = MOCK.requests[0]["headers"]["Authorization"]
        self.assertEqual(auth, "Basic " + base64.b64encode(f"{TOGGL_TOKEN}:api_token".encode()).decode())
        q = next(r for r in MOCK.requests if r["path"] == "/toggl/me/time_entries")["query"]
        self.assertEqual(q["meta"], ["true"])
        self.assertEqual(q["start_date"], [iso(T0 - 14 * 86400)])
        # a second look within the TTL uses the cache only, so reruns cost no API calls
        n = len(MOCK.requests)
        self.sf("track", now=T0 + 1)
        self.assertEqual(len(MOCK.requests), n)
        self.assertEqual(self.sf("track", now=T0 + 10)[0]["title"], "▶ Café ☕️ planning · 1:02:15")

    def test_start_with_project_and_tags(self):
        it = self.sf("track", "Fix the “login” bug @website_red #deep_work #new")
        start = it[0]
        self.assertEqual(start["title"], "Start “Fix the “login” bug”")
        a = self.arg(start)
        self.assertEqual((a["projectId"], a["tags"]), (101, ["Deep Work", "new"]))
        msg = self.act(a)
        self.assertIn("Started “Fix the “login” bug” in Toggl.", msg)
        body = MOCK.requests[-1]["body"]
        self.assertEqual(MOCK.requests[-1]["path"], "/toggl/workspaces/11/time_entries")
        self.assertEqual(body["project_id"], 101)
        self.assertEqual(body["billable"], True)
        self.assertEqual(body["duration"], -1)
        self.assertEqual(body["workspace_id"], 11)
        self.assertEqual(body["created_with"], "Alfred Focus Timer")
        self.assertEqual(body["start"], iso(T0))
        # the cache already shows the new running entry, without another request
        n = len(MOCK.requests)
        it = self.sf("track", now=T0 + 5)
        self.assertEqual(it[0]["title"], "▶ Fix the “login” bug · 00:05")
        self.assertEqual(len(MOCK.requests), n)
        self.assertIn("Stopped “Fix the “login” bug” after 00:30", self.act(self.arg(it[0]), T0 + 30))
        self.assertEqual(MOCK.requests[-1]["method"], "PATCH")
        self.assertEqual(self.sf("track", now=T0 + 31)[0]["title"], "Nothing is being tracked")

    def test_known_tag_names_are_reused(self):
        a = self.arg(self.sf("track", "Draft #deep_work #WRITING #brand-new")[0])
        self.assertEqual(a["tags"], ["Deep Work", "writing", "brand-new"])
        self.act(a)
        self.assertEqual(MOCK.requests[-1]["body"]["tags"], ["Deep Work", "writing", "brand-new"])
        self.assertEqual(sum(r["path"].endswith("/tags") for r in MOCK.requests), 1)

    def test_today_total_when_idle(self):
        self.assertTrue(self.sf("track")[0]["subtitle"].startswith("Today: 1 h tracked · "))
        self.assertTrue(self.sf("track", tracker="clockify")[0]["subtitle"].startswith("Today: 17 min tracked · "))

    def test_entries_without_start_are_skipped(self):
        MOCK.toggl_entries.append({"id": 50, "workspace_id": 11, "description": "Broken", "start": None, "stop": None, "duration": -1})
        titles = [i["title"] for i in self.sf("track")]
        self.assertEqual(titles[0], "Nothing is being tracked")
        self.assertNotIn("Broken", titles)

    def test_description_cannot_inject_curl_options(self):
        desc = 'C:\\temp "quoted" \\" url = "http://evil.invalid/" header = "X-Evil: 1'
        a = self.arg(self.sf("track", desc)[0])
        self.act(a)
        r = MOCK.requests[-1]
        self.assertEqual(r["body"]["description"], desc)
        self.assertEqual(r["path"], "/toggl/workspaces/11/time_entries")
        self.assertNotIn("X-Evil", r["headers"])

    def test_forbidden_is_not_a_bad_token(self):
        self.sf("track")  # caches /me
        MOCK.force = (403, {"message": "No access"})
        shutil.rmtree(self.cache)
        it = self.sf("track")
        self.assertEqual(it[0]["title"], "Toggl Track rejected the API token")  # /me: bad token
        MOCK.force = None
        self.sf("track")
        MOCK.force = (403, {"message": "No access"})
        it = self.sf("track", now=T0 + 3600)  # stale entries, /me still cached
        self.assertEqual(it[0]["title"], "Toggl Track refused the request (403)")
        self.assertEqual(self.arg(it[0]), {"a": "token-set"})

    def test_cmd_warns_about_replacing_a_pomodoro(self):
        self.assertNotIn("replaces", self.sf("track", "Docs")[0]["mods"]["cmd"]["subtitle"])
        self.start()
        self.assertIn("replaces the running session", self.sf("track", "Docs")[0]["mods"]["cmd"]["subtitle"])

    def test_workspace_word_in_a_description(self):
        self.assertEqual(self.sf("track", "ws review")[0]["title"], "Start “ws review”")

    def test_stopping_an_entry_stopped_elsewhere(self):
        self.assertEqual(self.act({"a": "track-stop", "id": 1, "ws": 11, "description": "Writing docs"}), "“Writing docs” was already stopped.")

    def test_linked_entry_stopped_elsewhere(self):
        self.act(self.arg(self.sf("track", "Deep dive")[0], "cmd"))
        MOCK.toggl_entries[0]["duration"] = 60
        msg = self.act({"a": "stop", "id": self.state()["id"]}, T0 + 60)
        self.assertEqual(msg, "Focus stopped after 1 min.")

    def test_failed_refresh_backs_off(self):
        self.sf("track")
        MOCK.force = (429, "")
        env = {"FT_NO_BACKGROUND": "0"}
        self.sf("track", now=T0 + 3600, **env)
        err = os.path.join(self.cache, "toggl-error.json")
        self.assertTrue(self.wait_for(lambda: os.path.exists(err)))
        self.assertTrue(self.wait_for(lambda: not os.path.exists(os.path.join(self.data, "refresh.lock")), 5))
        n = len(MOCK.requests)
        data = self.sf("track", now=T0 + 3601, full=True, **env)
        self.assertEqual(data["items"][0]["title"], "Too many requests to Toggl Track")
        self.assertNotIn("rerun", data)
        time.sleep(1)
        self.assertEqual(len(MOCK.requests), n)

    def test_project_picker(self):
        it = self.sf("track", "writing @web")
        self.assertEqual([i["title"] for i in it], ["Website Redesign", "Website Maintenance"])
        self.assertEqual(it[0]["autocomplete"], "writing @Website_Redesign ")
        self.assertFalse(it[0]["valid"])
        self.assertEqual(self.sf("track", "@cafe")[0]["title"], "Café Opening")
        self.assertEqual(self.sf("track", "@zzz")[0]["title"], "No matching project")
        it = self.sf("track", "writing @web more")
        self.assertTrue(it[0]["title"].startswith("“web” matches 2 projects"))
        self.assertFalse(any(i["title"].startswith("Start") for i in it))
        it = self.sf("track", "@Café_Opening ")
        self.assertEqual(it[0]["title"], "Start @ Café Opening")

    def test_errors(self):
        for status, body, title in [(401, "", "Toggl Track rejected the API token"), (402, "Requires a paid plan", "Toggl Track error 402"),
                                    (429, "", "Too many requests to Toggl Track"), (500, "", "Toggl Track is having problems (500)"),
                                    (400, "Invalid project_id", "Toggl Track error 400")]:
            MOCK.reset()
            shutil.rmtree(self.cache, ignore_errors=True)
            MOCK.force = (status, body)
            it = self.sf("track")
            if status == 402:
                self.assertEqual(it[0]["subtitle"], "Requires a paid plan")  # a paid feature, not the quota
            self.assertEqual(it[0]["title"], title)
            if status == 401:
                self.assertEqual(self.arg(it[0]), {"a": "token-set"})
            if status == 400:
                self.assertEqual(it[0]["subtitle"], "Invalid project_id")
        shutil.rmtree(self.cache, ignore_errors=True)
        it = self.sf("track", FT_TOGGL_URL="http://127.0.0.1:9/api")
        self.assertEqual(it[0]["title"], "Can't reach Toggl Track")
        msg = self.act({"a": "track-start", "description": "x", "tags": []}, FT_TOGGL_URL="http://127.0.0.1:9/api")
        self.assertEqual(msg, "Can't reach Toggl Track. Check your internet connection")

    def test_stale_cache_with_failing_refresh(self):
        self.sf("track")
        MOCK.force = (429, "")
        it = self.sf("track", now=T0 + 3600)
        self.assertEqual(it[0]["title"], "Too many requests to Toggl Track")
        self.assertIn("Writing docs", [i["title"] for i in it])

    def test_background_refresh(self):
        self.sf("track")
        MOCK.toggl_entries[0]["description"] = "Renamed"
        env = {"FT_NO_BACKGROUND": "0"}
        data = self.sf("track", now=T0 + 3600, full=True, **env)
        self.assertEqual(data["items"][1]["title"], "Writing docs")  # stale data at once
        self.assertEqual(data["rerun"], 1)
        self.assertTrue(self.wait_for(lambda: "Renamed" in [i["title"] for i in self.sf("track", now=T0 + 3600, **env)]))
        self.assertTrue(self.wait_for(lambda: subprocess.run(["pgrep", "-f", "focus.js track-refresh"], capture_output=True).returncode == 1, 10))

    def test_workspaces(self):
        it = self.sf("track", "workspace")
        self.assertEqual([i["title"] for i in it], ["✓ Personal", "Team"])
        self.assertIn("Team workspace", self.act(self.arg(it[1])))
        it = self.sf("track", "@")
        self.assertEqual([i["title"] for i in it], ["Side Project"])
        self.assertIn("/toggl/workspaces/12/projects", self.paths())

    def test_track_with_pomodoro(self):
        a = dict(self.arg(self.sf("track", "Deep dive")[0], "cmd"))
        msg = self.act(a, shortcut_start="Focus On")
        self.assertIn("Started “Deep dive” in Toggl. Focus started: 25 min", msg)
        s = self.state()
        self.assertEqual((s["status"], s["label"], s["track"]["owned"]), ("running", "Deep dive", True))
        self.assertIn("shortcut:Focus On", self.effects())
        self.assertIn("Tracking in Toggl", self.sf("pomo", now=T0 + 5)[0]["subtitle"])
        n = len(self.paths("POST"))
        # the focus session ends -> the entry is stopped (by a separate process, see below)
        self.sf("pomo", now=T0 + 1500)
        self.assertTrue(self.wait_for(lambda: any("Stopped the Toggl timer" in f for f in self.effects())), self.effects())
        self.assertEqual(self.paths("PATCH")[-1], f"/toggl/workspaces/11/time_entries/{s['track']['id']}/stop")
        self.assertEqual(len(self.paths("POST")), n)

    def test_stopping_entry_releases_pomodoro(self):
        self.act(self.arg(self.sf("track", "Deep dive")[0], "cmd"))
        run = self.sf("track", now=T0 + 5)[0]
        self.act(self.arg(run), T0 + 10)
        self.assertIsNone(self.state()["track"])
        n = len(MOCK.requests)
        self.act({"a": "stop", "id": self.state()["id"]}, T0 + 20)
        self.assertEqual(len(MOCK.requests), n)  # nothing left to stop

    def test_link_tracker(self):
        self.start(label="Écrire ✍️", link_tracker="1")
        post = [r for r in MOCK.requests if r["method"] == "POST"][-1]
        self.assertEqual(post["body"]["description"], "Écrire ✍️")
        self.assertTrue(self.state()["track"]["owned"])
        self.act({"a": "stop", "id": self.state()["id"]}, T0 + 60, link_tracker="1")
        self.assertEqual(self.paths("PATCH")[-1].split("/")[-1], "stop")
        # breaks are not tracked
        n = len(MOCK.requests)
        self.start(kind="short", secs=300, link_tracker="1")
        self.assertEqual(len(MOCK.requests), n)

    def test_link_tracker_leaves_running_entry_alone(self):
        MOCK.toggl_entries.insert(0, {"id": 9, "workspace_id": 11, "description": "Meeting", "project_id": None, "tags": [],
                                      "start": iso(T0 - 60), "stop": None, "duration": -1})
        msg = self.start(link_tracker="1")
        self.assertIn("already tracking “Meeting”", msg)
        self.assertEqual(self.paths("POST"), [])
        self.act({"a": "stop", "id": self.state()["id"]}, T0 + 60, link_tracker="1")
        self.assertEqual(self.paths("PATCH"), [])

    def test_link_tracker_offline(self):
        msg = self.start(link_tracker="1", FT_TOGGL_URL="http://127.0.0.1:9/api")
        self.assertIn("Focus started", msg)
        self.assertIn("Toggl: Can't reach Toggl Track.", msg)
        self.assertEqual(self.state()["status"], "running")

    def test_universal_action_text(self):
        it = self.sf("track", "Review PR\n#123 from Jane")
        self.assertEqual(it[0]["title"], "Start “Review PR from Jane”")
        self.assertEqual(self.arg(it[0])["tags"], ["123"])

    def test_token_items(self):
        it = self.sf("track", "token")
        self.assertEqual([i["title"] for i in it], ["Replace the Toggl Track API token", "Remove the saved token"])
        self.assertIn("Removed", self.act(self.arg(it[1])))
        self.assertNotIn("toggl", self.keychain())

    # --- audit pass 4 regressions ---

    def test_quota_blocks_requests_until_it_resets(self):
        self.sf("track")
        MOCK.force = (402, "You have hit your hourly limit for API calls.", {"X-Toggl-Quota-Remaining": "0", "X-Toggl-Quota-Resets-In": "1200"})
        it = self.sf("track", now=T0 + 3600)  # stale cache: refresh in the foreground (FT_NO_BACKGROUND)
        self.assertEqual(it[0]["title"], "Toggl Track API limit reached")
        self.assertIn("Try again in 20 min", it[0]["subtitle"])
        self.assertIn("Writing docs", [i["title"] for i in it])
        n = len(MOCK.requests)
        for dt in (1, 5, 600):
            it = self.sf("track", "some words", now=T0 + 3600 + dt)
            self.assertEqual(it[0]["title"], "Toggl Track API limit reached")
        self.assertIn("Try again in 10 min", it[0]["subtitle"])
        self.assertIn("API limit", self.act({"a": "track-start", "description": "x", "tags": []}, T0 + 3700))
        self.assertEqual(len(MOCK.requests), n)  # nothing went out while blocked
        MOCK.force = None
        self.assertEqual(self.sf("track", now=T0 + 3600 + 1201)[0]["title"], "Nothing is being tracked")
        self.assertGreater(len(MOCK.requests), n)

    def test_session_end_does_not_wait_for_the_tracker(self):
        # The pomo Script Filter that notices the end must not block on Toggl (Alfred would kill it
        # when you type, and the entry would never be stopped).
        self.act(self.arg(self.sf("track", "Deep dive")[0], "cmd"))
        tid = self.state()["track"]["id"]
        MOCK.delay = 3
        t = time.time()
        it = self.sf("pomo", now=T0 + 1500)
        self.assertLess(time.time() - t, 2)
        self.assertTrue(it[0]["title"].startswith("Start Short Break"))
        self.assertTrue(self.wait_for(lambda: f"/toggl/workspaces/11/time_entries/{tid}/stop" in self.paths("PATCH"), 10))
        self.assertTrue(self.wait_for(lambda: any("Stopped the Toggl timer" in f for f in self.effects()), 5))


class ClockifyTests(TrackBase):
    provider = "clockify"

    def test_list_start_stop(self):
        it = self.sf("track")
        self.assertEqual(it[0]["title"], "Nothing is being tracked")
        self.assertEqual(it[1]["title"], "Planning")
        self.assertIn("@ Internal · #meetings", it[1]["subtitle"])
        self.assertEqual(MOCK.requests[0]["headers"]["X-Api-Key"], CLOCKIFY_TOKEN)
        self.assertNotIn("Authorization", MOCK.requests[0]["headers"])
        a = self.arg(self.sf("track", "Standup @internal #meetings #brand_new")[0])
        self.assertIn("Started “Standup” in Clockify.", self.act(a))
        tag_post = [r for r in MOCK.requests if r["method"] == "POST" and r["path"].endswith("/tags")]
        self.assertEqual([r["body"]["name"] for r in tag_post], ["brand_new"])
        body = MOCK.requests[-1]["body"]
        self.assertEqual(body["tagIds"], ["t1", "t2"])
        self.assertEqual(body["projectId"], "p1")
        self.assertIs(body["billable"], True)  # Clockify ignores the project's default
        it = self.sf("track", now=T0 + 65)
        self.assertEqual(it[0]["title"], "▶ Standup · 01:05")
        self.assertIn("#brand_new", it[0]["subtitle"])
        self.act(self.arg(it[0]), T0 + 70)
        self.assertEqual(MOCK.requests[-1]["method"], "PATCH")
        self.assertEqual(MOCK.requests[-1]["body"], {"end": iso(T0 + 70)})

    def test_stop_checks_the_running_entry(self):
        # Clockify's stop endpoint stops whatever is running: never stop someone else's entry
        self.act(self.arg(self.sf("track", "Deep dive")[0], "cmd"))
        track = self.state()["track"]
        MOCK.c_entries[0]["timeInterval"]["end"] = iso(T0 + 1)
        MOCK.c_entries.insert(0, {"id": "other", "workspaceId": "w1", "description": "Call", "timeInterval": {"start": iso(T0 + 2), "end": None}})
        self.act({"a": "stop", "id": self.state()["id"]}, T0 + 60)
        self.assertEqual(self.paths("PATCH"), [])
        self.assertIsNone(MOCK.c_entries[0]["timeInterval"]["end"])
        self.assertTrue(track["owned"])

    def test_auth_error(self):
        MOCK.force = (401, {"message": "Unauthorized"})
        self.assertEqual(self.sf("track")[0]["title"], "Clockify rejected the API token")

    def test_no_permission_to_create_tags(self):
        self.sf("track")
        MOCK.force = None
        orig = Handler.clockify

        def no_tags(handler, method, path, body):
            if method == "POST" and path.endswith("/tags"):
                return handler.send(403, {"message": "Only admins can create tags"})
            return orig(handler, method, path, body)
        Handler.clockify = no_tags
        try:
            msg = self.act(self.arg(self.sf("track", "Standup #brand_new")[0]))
        finally:
            Handler.clockify = orig
        self.assertEqual(msg, "Clockify refused the request (403). Check your access to this workspace, or press ↩ to set a new token")


class KeychainTests(unittest.TestCase):
    """Uses a throwaway Keychain item under a test service name and always deletes it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="focus-timer-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_real_keychain_roundtrip(self):
        service = f"io.github.x-o-r-r-o.focus-timer.test-{os.getpid()}"
        env = dict(os.environ, FT_KEYCHAIN_SERVICE=service, tracker="toggl", FT_TOGGL_URL=BASE + "/toggl", FT_TOKEN_INPUT=TOGGL_TOKEN,
                   alfred_workflow_data=self.tmp + "/d", alfred_workflow_cache=self.tmp + "/c")
        env.pop("FT_KEYCHAIN_FILE", None)
        try:
            out = subprocess.run(["osascript", "-l", "JavaScript", "./focus.js", "action", '{"a":"token-set"}'], cwd=SRC, env=env,
                                 capture_output=True, text=True, timeout=60)
            self.assertIn("Connected to Toggl Track", out.stdout, out.stderr)
            got = subprocess.run(["security", "find-generic-password", "-s", service, "-a", "toggl", "-w"], capture_output=True, text=True)
            self.assertEqual(got.stdout.strip(), TOGGL_TOKEN)
            out = subprocess.run(["osascript", "-l", "JavaScript", "./focus.js", "action", '{"a":"token-remove"}'], cwd=SRC, env=env,
                                 capture_output=True, text=True, timeout=60)
            gone = subprocess.run(["security", "find-generic-password", "-s", service, "-a", "toggl"], capture_output=True)
            self.assertNotEqual(gone.returncode, 0)
        finally:
            subprocess.run(["security", "delete-generic-password", "-s", service, "-a", "toggl"], capture_output=True)

    def test_keychain_input_cannot_inject_commands(self):
        service = f"io.github.x-o-r-r-o.focus-timer.test-{os.getpid()}-inj"
        victim = service + "-victim"
        env = dict(os.environ, FT_KEYCHAIN_SERVICE=service, tracker="clockify", FT_CLOCKIFY_URL=BASE + "/clockify",
                   alfred_workflow_data=self.tmp + "/d", alfred_workflow_cache=self.tmp + "/c")
        env.pop("FT_KEYCHAIN_FILE", None)

        def token_set(tok, **extra):
            return subprocess.run(["osascript", "-l", "JavaScript", "./focus.js", "action", '{"a":"token-set"}'], cwd=SRC,
                                  env=dict(env, FT_TOKEN_INPUT=tok, **extra), capture_output=True, text=True, timeout=60).stdout

        def exists(svc, acct):
            return subprocess.run(["security", "find-generic-password", "-s", svc, "-a", acct], capture_output=True).returncode == 0
        try:
            evil = [f'abcdefgh" \nadd-generic-password -s "{victim}" -a x -w "pwned', 'abc def ghij', 'abcdefgh\\" -a other',
                    "abcdefgh'quote", 'abcdefgh\\', 'a' * 300, "abcdefgh\tij"]
            for tok in evil:
                self.assertIn("doesn't look like", token_set(tok), tok)
            self.assertFalse(exists(victim, "x"))
            self.assertFalse(exists(service, "clockify"))
            self.assertIn("doesn't look like", token_set("abcdefghij", FT_KEYCHAIN_SERVICE=f'{service}" -a y'))
            # every character a real token may have survives the round trip
            self.assertIn("Connected to Clockify", token_set(CLOCKIFY_TOKEN))
            for tok in ("Ab+/=:._-09xyzXYZ", "ZmFrZS1rZXk+/w=="):
                token_set(tok)
                got = subprocess.run(["security", "find-generic-password", "-s", service, "-a", "clockify", "-w"], capture_output=True, text=True)
                self.assertEqual(got.stdout.strip(), tok)
        finally:
            for svc, acct in ((service, "clockify"), (victim, "x"), (f'{service}" -a y', "clockify")):
                subprocess.run(["security", "delete-generic-password", "-s", svc, "-a", acct], capture_output=True)


class PlistTests(unittest.TestCase):
    def test_build_and_plist(self):
        subprocess.run([sys.executable, "tools/build.py"], cwd=ROOT, check=True, capture_output=True)
        with open(os.path.join(SRC, "info.plist"), "rb") as f:
            p = plistlib.load(f)
        uids = [o["uid"] for o in p["objects"]]
        self.assertEqual(len(uids), len(set(uids)))
        for src, conns in p["connections"].items():
            self.assertIn(src, uids)
            for c in conns:
                self.assertIn(c["destinationuid"], uids)
        for o in p["objects"]:
            kw = o["config"].get("keyword")
            if kw:
                self.assertRegex(kw, r"^\{var:keyword_\w+\}$")
        triggers = {o["config"]["triggerid"] for o in p["objects"] if o["type"] == "alfred.workflow.trigger.external"}
        self.assertEqual(triggers, {"notify", "pomo", "track"})
        self.assertTrue(p["readme"].startswith("## Usage"))
        out = subprocess.run(["sips", "-g", "pixelWidth", os.path.join(SRC, "icon.png")], capture_output=True, text=True).stdout
        self.assertGreaterEqual(int(out.split()[-1]), 256)
        sounds = next(c for c in p["userconfigurationconfig"] if c["variable"] == "sound")["config"]["pairs"]
        for _, v in sounds[1:]:
            self.assertTrue(os.path.exists(f"/System/Library/Sounds/{v}.aiff"), v)


if __name__ == "__main__":
    unittest.main(verbosity=1)
