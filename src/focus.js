#!/usr/bin/osascript -l JavaScript
// Focus Timer for Alfred: Pomodoro timer plus Toggl Track / Clockify time tracking.
// Usage: osascript -l JavaScript focus.js <command> [arg]
//   pomo <query>       Script Filter for the Pomodoro timer
//   track <query>      Script Filter for time tracking
//   action <json>      run the action chosen in a Script Filter (prints a notification message)
//   complete <id>      called by waiter.sh when a session reaches its end time
//   track-refresh      background refresh of the time-entry cache
ObjC.import("Foundation");

const ENV = $.NSProcessInfo.processInfo.environment;
function env(name, fallback) {
  const v = ENV.objectForKey(name);
  return v.isNil() ? fallback : v.js;
}
const FM = $.NSFileManager.defaultManager;
const BUNDLE = env("alfred_workflow_bundleid", "io.github.x-o-r-r-o.focus-timer");
const ALFRED = "com.runningwithcrayons.Alfred";

// ---------- small helpers ----------

// Wall-clock seconds. FT_NOW injects a fixed clock for the test suite.
function now() {
  const f = env("FT_NOW", "");
  return f !== "" && isFinite(Number(f)) ? Math.floor(Number(f)) : Math.floor(Date.now() / 1000);
}

function num(name, def, min, max) {
  const v = parseFloat(String(env(name, "")).replace(",", "."));
  return isFinite(v) && v >= min && v <= max ? v : def;
}
const flag = (name) => env(name, "0") === "1";

const pad = (n) => String(n).padStart(2, "0");
// Collapse whitespace and shorten by characters (not UTF-16 units, so emoji are never cut in half).
const oneLine = (s, max = 200) => {
  const t = String(s || "").replace(/\s+/g, " ").trim();
  if (t.length <= max) return t;
  const chars = Array.from(t);
  return chars.length > max ? chars.slice(0, max - 1).join("") + "…" : t;
};
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

function fmtClock(secs) {
  secs = Math.max(0, Math.round(secs));
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = secs % 60;
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function fmtDur(secs) {
  secs = Math.max(0, Math.round(secs));
  if (secs === 0) return "0 min";
  if (secs < 60) return `${secs} s`;
  const mins = Math.round(secs / 60);
  const h = Math.floor(mins / 60), m = mins % 60;
  if (!h) return `${m} min`;
  return m ? `${h} h ${m} min` : `${h} h`;
}

// Local wall-clock time like "14:35" or "2:35 PM", following the user's locale.
function clockTime(t) {
  const f = $.NSDateFormatter.alloc.init;
  f.dateStyle = 0;
  f.timeStyle = 1;
  return f.stringFromDate($.NSDate.dateWithTimeIntervalSince1970(t)).js;
}

function dayKey(t) {
  const d = new Date(t * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// Step a day key by n calendar days (DST-safe: works on dates, not on 86400-second steps).
function shiftDay(key, n) {
  const [y, m, d] = key.split("-").map(Number);
  const x = new Date(y, m - 1, d + n, 12);
  return `${x.getFullYear()}-${pad(x.getMonth() + 1)}-${pad(x.getDate())}`;
}

function relDay(t) {
  const today = dayKey(now());
  const k = dayKey(t);
  if (k === today) return "today";
  if (k === shiftDay(today, -1)) return "yesterday";
  for (let i = 2; i < 7; i++) if (k === shiftDay(today, -i)) return `${i} days ago`;
  return k;
}

function isoUTC(t) {
  return new Date(t * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
}

function parseTime(s) {
  const t = Date.parse(s);
  return isFinite(t) ? Math.floor(t / 1000) : null;
}

function newId() {
  return $.NSUUID.UUID.UUIDString.js.replace(/-/g, "").toLowerCase().slice(0, 20);
}

function b64(s) {
  return $(s).dataUsingEncoding($.NSUTF8StringEncoding).base64EncodedStringWithOptions(0).js;
}

// ---------- files ----------

function mkdirp(d) {
  FM.createDirectoryAtPathWithIntermediateDirectoriesAttributesError(d, true, $(), $());
  return d;
}
function dataDir() {
  return mkdirp(env("alfred_workflow_data", `${$.NSTemporaryDirectory().js}focus-timer-data`));
}
function cacheDir() {
  return mkdirp(env("alfred_workflow_cache", `${$.NSTemporaryDirectory().js}focus-timer-cache`));
}
function readText(p) {
  const s = $.NSString.stringWithContentsOfFileEncodingError(p, $.NSUTF8StringEncoding, $());
  return s.isNil() ? null : s.js;
}
function writeText(p, t) {
  $(t).writeToFileAtomicallyEncodingError(p, true, $.NSUTF8StringEncoding, $());
}
function readJSON(p, fallback) {
  const t = readText(p);
  if (t === null) return fallback;
  try {
    return JSON.parse(t);
  } catch (e) {
    return fallback;
  }
}
const writeJSON = (p, v) => writeText(p, JSON.stringify(v));
const exists = (p) => FM.fileExistsAtPath(p);
function removeFile(p) {
  FM.removeItemAtPathError(p, $());
}
function mtime(p) {
  const a = FM.attributesOfItemAtPathError(p, $());
  if (a.isNil()) return null;
  const d = a.objectForKey($.NSFileModificationDate);
  return d.isNil() ? null : d.timeIntervalSince1970;
}
function appendLine(p, line) {
  if (!exists(p)) FM.createFileAtPathContentsAttributes(p, $(), $());
  const fh = $.NSFileHandle.fileHandleForWritingAtPath(p);
  if (fh.isNil()) return;
  fh.seekToEndOfFile;
  fh.writeData($(line + "\n").dataUsingEncoding($.NSUTF8StringEncoding));
  fh.closeFile;
}

// A cross-process mutex: mkdir is atomic, and the owner's PID is written inside. Alfred terminates
// a Script Filter that is still running when you type, so a lock whose owner is gone (or no
// longer a focus.js process, as PIDs get reused) is stale and taken over at once.
const MY_PID = Number($.NSProcessInfo.processInfo.processIdentifier);
function withLock(fn, timeout) {
  const lock = `${dataDir()}/lock`;
  const deadline = Date.now() + 1000 * (timeout || num("FT_LOCK_TIMEOUT", 5, 0.1, 60));
  let lastCheck = 0;
  while (!FM.createDirectoryAtPathWithIntermediateDirectoriesAttributesError(lock, false, $(), $())) {
    const m = mtime(lock);
    const age = m === null ? 0 : Date.now() / 1000 - m;
    const owner = parseInt(readText(`${lock}/pid`) || "", 10);
    let stale = m !== null && (age > 30 || (!owner && age > 2));
    if (!stale && owner && Date.now() - lastCheck > 100) {
      lastCheck = Date.now();
      stale = !processCommand(owner).includes("focus.js") && parseInt(readText(`${lock}/pid`) || "", 10) === owner;
    }
    if (stale) {
      removeFile(lock);
      continue;
    }
    if (Date.now() > deadline) throw new Error("Focus Timer is busy, try again");
    $.NSThread.sleepForTimeInterval(0.02);
  }
  writeText(`${lock}/pid`, String(MY_PID));
  try {
    return fn();
  } finally {
    removeFile(lock);
  }
}

// ---------- processes ----------

// Run a program with argv (never through a shell string) and optional stdin.
function exec(path, args, input) {
  const task = $.NSTask.alloc.init;
  task.executableURL = $.NSURL.fileURLWithPath(path);
  task.arguments = args;
  const inP = $.NSPipe.pipe, outP = $.NSPipe.pipe, errP = $.NSPipe.pipe;
  task.standardInput = inP;
  task.standardOutput = outP;
  task.standardError = errP;
  if (!task.launchAndReturnError($())) return { status: -1, out: "", err: "launch failed" };
  if (input) inP.fileHandleForWriting.writeData($(input).dataUsingEncoding($.NSUTF8StringEncoding));
  inP.fileHandleForWriting.closeFile;
  const out = outP.fileHandleForReading.readDataToEndOfFile;
  const err = errP.fileHandleForReading.readDataToEndOfFile;
  task.waitUntilExit;
  const str = (d) => {
    const s = $.NSString.alloc.initWithDataEncoding(d, $.NSUTF8StringEncoding);
    return s.isNil() ? "" : s.js;
  };
  return { status: task.terminationStatus, out: str(out), err: str(err) };
}

// Start a program detached from Alfred (stdio to /dev/null) and return its PID. `set -m` puts it
// in its own process group, so it survives Alfred terminating the Script Filter that started it.
function spawnDetached(args) {
  const r = exec("/bin/bash", ["-c", 'set -m; nohup "$@" </dev/null >/dev/null 2>&1 & echo $!', "spawn", ...args]);
  const pid = parseInt(r.out, 10);
  return isFinite(pid) ? pid : 0;
}

function processCommand(pid) {
  if (!pid) return "";
  const r = exec("/bin/ps", ["-p", String(pid), "-o", "command="]);
  return r.status === 0 ? r.out.trim() : "";
}

// ---------- side effects (notifications, sound, Shortcuts, Alfred) ----------

// FT_TEST_LOG makes the test suite record side effects instead of performing them.
function testLog(line) {
  const f = env("FT_TEST_LOG", "");
  if (!f) return false;
  appendLine(f, line);
  return true;
}

function alfredTrigger(trigger, argument) {
  if (testLog(`trigger:${trigger}:${argument}`)) return;
  spawnDetached([
    "/usr/bin/osascript",
    "-e", "on run argv",
    "-e", `tell application id "${ALFRED}" to run trigger (item 1 of argv) in workflow (item 2 of argv) with argument (item 3 of argv)`,
    "-e", "end run",
    trigger, BUNDLE, argument,
  ]);
}

function notify(msg) {
  if (msg) alfredTrigger("notify", msg);
}

function playSound() {
  const name = env("sound", "Glass");
  if (!/^[A-Za-z ]{1,30}$/.test(name) || name === "none") return;
  if (testLog(`sound:${name}`)) return;
  const path = `/System/Library/Sounds/${name}.aiff`;
  if (exists(path)) spawnDetached(["/usr/bin/afplay", path]);
}

function runShortcut(name) {
  name = String(name || "").trim();
  if (!name) return;
  if (testLog(`shortcut:${name}`)) return;
  if (exists("/usr/bin/shortcuts")) spawnDetached(["/usr/bin/shortcuts", "run", name]);
}

// ---------- Script Filter output ----------

function item(title, subtitle, icon, action, extra = {}) {
  const it = { title, subtitle: subtitle || "", icon: { path: `icons/${icon}.png` }, text: { copy: title, largetype: title } };
  if (action) {
    it.arg = JSON.stringify(action);
    it.valid = true;
  } else it.valid = false;
  return Object.assign(it, extra);
}
function mod(subtitle, action) {
  return action ? { subtitle, arg: JSON.stringify(action), valid: true } : { subtitle, arg: "", valid: false };
}
function output(items, extra = {}) {
  return JSON.stringify(Object.assign({ skipknowledge: true, items }, extra));
}

// =====================================================================
// Pomodoro
// =====================================================================

const KINDS = {
  focus: { name: "Focus", icon: "focus" },
  short: { name: "Short Break", icon: "short" },
  long: { name: "Long Break", icon: "long" },
};
const MAX_SECS = 24 * 3600;
const CYCLE_RESET = 3 * 3600; // the Pomodoro count restarts after 3 idle hours

function lengthOf(kind) {
  const def = { focus: 25, short: 5, long: 15 }[kind];
  return Math.round(num(`${kind}_minutes`, def, 1, 1440) * 60);
}
const longEvery = () => Math.round(num("long_every", 4, 1, 20));

// "25", "1h30", "1h 30m", "90s", "1.5h", "45 min" -> seconds (null if not a duration)
function durationOf(s) {
  s = s.toLowerCase().replace(/,/g, ".");
  if (/^\d+(\.\d+)?$/.test(s)) return Math.round(parseFloat(s) * 60);
  s = s.replace(/hours?|hrs?/g, "h").replace(/minutes?|mins?/g, "m").replace(/seconds?|secs?/g, "s");
  // A bare number is only allowed last ("1h30"), so "90s" can't split into "9" minutes + "0s".
  const m = s.match(/^(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)(?:m|$))?(?:(\d+(?:\.\d+)?)s)?$/);
  if (!m || (!m[1] && !m[2] && !m[3])) return null;
  return Math.round((parseFloat(m[1] || 0) * 3600) + (parseFloat(m[2] || 0) * 60) + parseFloat(m[3] || 0));
}

// Split "1h 30 write report" into { secs: 5400, label: "write report" }.
function parseDuration(q) {
  const tokens = q.trim().split(/\s+/).filter(Boolean);
  const unit = /^(h|hrs?|hours?|m|mins?|minutes?|s|secs?|seconds?)$/i;
  for (let n = Math.min(3, tokens.length); n >= 1; n--) {
    const part = tokens.slice(0, n);
    // Join several tokens only when they are clearly one duration ("1h 30", "45 min"), so
    // "pomo 25 5 things" stays 25 minutes labelled "5 things".
    if (n > 1 && !part.slice(1).every((t, i) => unit.test(t) || /[a-z]$/i.test(part[i]) || /^\d+[a-z]+$/i.test(t))) continue;
    const secs = durationOf(part.join(""));
    if (secs !== null) return { secs, label: tokens.slice(n).join(" ") };
  }
  return { secs: null, label: tokens.join(" ") };
}

// ----- state -----
// state.json: { status: idle|running|paused|done, id, kind, label, secs, start, end, left, pid,
//               count, next, lastDone, lastKind, focusLabel, track }
// running:    "<id> <end>\n" while a session runs; the waiter polls it.

const statePath = () => `${dataDir()}/state.json`;
const runningPath = () => `${dataDir()}/running`;
const logPath = () => `${dataDir()}/sessions.jsonl`;

function loadState() {
  const s = readJSON(statePath(), null);
  return Object.assign({ status: "idle", count: 0, next: "focus", lastDone: 0, id: null, pid: 0, track: null }, s && typeof s === "object" ? s : {});
}

// The running file is written before state.json, so a run interrupted in between never leaves a
// "running" state without it (the waiter would exit at once and the session would never end).
function saveState(s) {
  if (s.status === "running") {
    writeText(runningPath(), `${s.id} ${s.end}\n`);
    writeJSON(statePath(), s);
  } else {
    writeJSON(statePath(), s);
    removeFile(runningPath());
  }
}

const isActive = (s) => s.status === "running" || s.status === "paused";

function cycle(s) {
  if (!isActive(s) && s.lastDone && now() - s.lastDone > CYCLE_RESET) return { count: 0, next: "focus", reset: true };
  return { count: s.count || 0, next: KINDS[s.next] ? s.next : "focus", reset: false };
}

function remaining(s) {
  if (s.status === "running") return Math.max(0, s.end - now());
  if (s.status === "paused") return Math.max(0, s.left || 0);
  return 0;
}

function waiterAlive(s) {
  const c = processCommand(s.pid);
  return !!s.id && c.includes("waiter.sh") && c.includes(s.id);
}

function spawnWaiter(id) {
  if (flag("FT_NO_WAITER")) return 0;
  const dir = FM.currentDirectoryPath.js;
  return spawnDetached(["/bin/bash", `${dir}/waiter.sh`, id, dataDir()]);
}

// Kill the waiter only if the PID still belongs to this session's waiter (PIDs get reused).
function killWaiter(s) {
  if (s.pid && waiterAlive(s)) exec("/bin/kill", ["-TERM", String(s.pid)]);
}

function appendLog(entry) {
  appendLine(logPath(), JSON.stringify(entry));
}

function readLog() {
  const t = readText(logPath());
  if (!t) return [];
  const out = [];
  for (const line of t.split("\n")) {
    if (!line.trim()) continue;
    try {
      const e = JSON.parse(line);
      if (e && typeof e.end === "number" && KINDS[e.kind]) out.push(e);
    } catch (e) {
      // skip a partially written line
    }
  }
  return out;
}

function newEffects() {
  return { msgs: [], sound: false, focusStarted: null, focusEnded: false, trackStop: null, show: false };
}

// Start a session (caller holds the lock).
function begin(s, kind, secs, label, fx, track) {
  const c = cycle(s);
  s.count = c.count;
  if (c.reset) s.next = "focus";
  const t = now();
  Object.assign(s, { status: "running", id: newId(), kind, label: oneLine(label), secs, start: t, end: t + secs, left: null, pid: 0, track: track || null });
  if (kind === "focus") s.focusLabel = s.label;
  saveState(s); // write the running file before the waiter looks for it
  s.pid = spawnWaiter(s.id);
  saveState(s);
  if (kind === "focus") fx.focusStarted = { id: s.id, label: s.label, linked: !!track };
}

// End the current session (caller holds the lock). outcome: completed | stopped | skipped
function finish(s, outcome, fx, fromWaiter) {
  const t = now();
  const left = remaining(s);
  const focused = outcome === "completed" ? s.secs : Math.max(0, s.secs - left);
  appendLog({ id: s.id, kind: s.kind, label: s.label || "", start: s.start, end: t, planned: s.secs, focused, outcome });
  if (!fromWaiter) killWaiter(s);
  const kind = s.kind, label = s.label;
  if (outcome === "stopped") s.next = "focus";
  else if (kind === "focus") {
    s.count = (s.count || 0) + 1;
    s.next = s.count % longEvery() === 0 ? "long" : "short";
  } else {
    if (kind === "long") s.count = 0;
    s.next = "focus";
  }
  if (kind === "focus") {
    fx.focusEnded = true;
    if (s.track && s.track.owned) fx.trackStop = s.track;
  }
  Object.assign(s, { status: outcome === "completed" ? "done" : "idle", lastDone: t, lastKind: kind, lastOutcome: outcome, id: null, pid: 0, end: null, left: null, track: null });
  saveState(s);
  return { kind, label, focused };
}

// A session reached its end time (caller holds the lock).
function completeLocked(s, fx) {
  const endAt = s.end;
  const late = now() - endAt;
  const r = finish(s, "completed", fx, true);
  const k = KINDS[r.kind].name;
  let msg = r.kind === "focus" ? `Focus complete${r.label ? `: ${r.label}` : ""}.` : `${k} over.`;
  if (late >= 60) {
    // The Mac was asleep or off when the timer ended: report it quietly, don't auto-start.
    fx.msgs.push(`${k} ended at ${clockTime(endAt)}${r.label ? `: ${r.label}` : ""}.`);
    return r;
  }
  fx.sound = true;
  const auto = r.kind === "focus" ? flag("auto_breaks") : flag("auto_focus");
  if (auto) {
    const next = s.next;
    begin(s, next, lengthOf(next), next === "focus" ? s.focusLabel || "" : "", fx);
    msg += ` ${KINDS[next].name} started (${fmtDur(lengthOf(next))}).`;
  } else {
    msg += r.kind === "focus" ? ` Time for a ${KINDS[s.next].name.toLowerCase()}.` : " Ready to focus?";
    fx.show = flag("show_alfred");
  }
  fx.msgs.push(msg);
  return r;
}

// Finish an overdue session, whoever notices first (the waiter, a Script Filter or an action; it
// only happens once because it runs under the lock), and restart a missing waiter after a crash
// or reboot. Caller holds the lock.
function reconcile(s, fx) {
  if (s.status !== "running") return;
  if (now() >= s.end) completeLocked(s, fx);
  else if (!waiterAlive(s)) {
    saveState(s); // restores the running file too
    s.pid = spawnWaiter(s.id);
    saveState(s);
  }
}

// Perform side effects after the lock is released.
// background: called from the waiter, so messages go to Alfred's notification.
function runEffects(fx, background) {
  if (fx.focusEnded && !fx.focusStarted) runShortcut(env("shortcut_end", ""));
  if (fx.focusStarted && !fx.focusEnded) runShortcut(env("shortcut_start", ""));
  if (fx.trackStop) {
    const m = trackerStopOwned(fx.trackStop);
    if (m) fx.msgs.push(m);
  }
  if (fx.focusStarted && !fx.focusStarted.linked && flag("link_tracker")) {
    const m = trackerStartForFocus(fx.focusStarted);
    if (m) fx.msgs.push(m);
  }
  if (fx.sound) playSound();
  const msg = fx.msgs.join(" ");
  if (background) {
    notify(msg);
    if (fx.show) alfredTrigger("pomo", "");
  }
  return msg;
}

function complete(id) {
  const fx = newEffects();
  let done = false;
  withLock(() => {
    const s = loadState();
    if (s.status !== "running" || s.id !== id || now() < s.end) return;
    completeLocked(s, fx);
    done = true;
  }, 20);
  if (done) runEffects(fx, true);
  return "";
}

// ----- actions -----

function pomoAction(a) {
  const fx = newEffects();
  let msg = "";
  withLock(() => {
    const s = loadState();
    reconcile(s, fx);
    const current = isActive(s) ? s.id : "";
    const kindName = KINDS[s.kind] ? KINDS[s.kind].name : "";
    if (a.a === "start") {
      // Refuse if the timer changed since the Script Filter was shown (double start, other window).
      if ((a.expect || "") !== current) {
        msg = "The timer changed in the meantime. Nothing was started.";
        return;
      }
      const kind = KINDS[a.kind] ? a.kind : "focus";
      const secs = Math.min(MAX_SECS, Math.max(1, Math.round(Number(a.secs) || lengthOf(kind))));
      if (isActive(s)) finish(s, "stopped", fx, false);
      begin(s, kind, secs, a.label || "", fx, a.track || null);
      msg = `${KINDS[kind].name} started: ${fmtDur(secs)}, ends at ${clockTime(s.end)}.`;
      return;
    }
    if (!a.id || a.id !== current) {
      msg = "That timer has already ended.";
      return;
    }
    switch (a.a) {
      case "pause":
        if (s.status !== "running") return;
        killWaiter(s);
        Object.assign(s, { left: remaining(s), status: "paused", end: null, pid: 0 });
        saveState(s);
        msg = `${kindName} paused with ${fmtClock(s.left)} left.`;
        break;
      case "resume":
        if (s.status !== "paused") return;
        Object.assign(s, { status: "running", end: now() + s.left, left: null });
        saveState(s);
        s.pid = spawnWaiter(s.id);
        saveState(s);
        msg = `${kindName} resumed, ends at ${clockTime(s.end)}.`;
        break;
      case "stop": {
        const r = finish(s, "stopped", fx, false);
        msg = `${KINDS[r.kind].name} stopped after ${fmtDur(r.focused)}.`;
        break;
      }
      case "skip": {
        const r = finish(s, "skipped", fx, false);
        const next = s.next;
        begin(s, next, lengthOf(next), next === "focus" ? s.focusLabel || "" : "", fx);
        msg = `${KINDS[r.kind].name} skipped. ${KINDS[next].name} started: ${fmtDur(s.secs)}.`;
        break;
      }
      case "extend": {
        const add = Math.max(1, Math.round(Number(a.secs) || 300));
        if (s.secs + add > MAX_SECS) {
          msg = "A session can't be longer than 24 hours.";
          return;
        }
        s.secs += add;
        if (s.status === "running") s.end += add;
        else s.left += add;
        saveState(s);
        msg = `Added ${fmtDur(add)}. ${fmtClock(remaining(s))} left.`;
        break;
      }
    }
  });
  const extra = runEffects(fx, false);
  return [msg, extra].filter(Boolean).join(" ");
}

// ----- stats -----

function firstWeekday() {
  const f = parseInt(env("FT_FIRST_WEEKDAY", ""), 10);
  if (f >= 1 && f <= 7) return f;
  return Number($.NSCalendar.currentCalendar.firstWeekday) || 2;
}

function computeStats(t) {
  const log = readLog();
  const days = {};
  for (const e of log) {
    if (e.kind !== "focus") continue;
    const k = dayKey(e.end);
    const d = (days[k] = days[k] || { pomos: 0, focused: 0 });
    if (e.outcome === "completed") d.pomos++;
    d.focused += Math.max(0, Number(e.focused) || 0);
  }
  const today = dayKey(t);
  const d = new Date(t * 1000);
  const back = (d.getDay() - (firstWeekday() - 1) + 7) % 7;
  const weekStart = dayKey(new Date(d.getFullYear(), d.getMonth(), d.getDate() - back, 12).getTime() / 1000);
  const monthStart = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-01`;
  const sum = (from) => {
    const r = { pomos: 0, focused: 0 };
    for (const [k, v] of Object.entries(days)) {
      if (k >= from && k <= today) {
        r.pomos += v.pomos;
        r.focused += v.focused;
      }
    }
    return r;
  };
  const has = (k) => days[k] && days[k].pomos > 0;
  let cur = has(today) ? today : shiftDay(today, -1);
  let streak = 0;
  while (has(cur)) {
    streak++;
    cur = shiftDay(cur, -1);
  }
  let best = 0, run = 0, prev = null;
  for (const k of Object.keys(days).filter(has).sort()) {
    run = prev && shiftDay(prev, 1) === k ? run + 1 : 1;
    best = Math.max(best, run);
    prev = k;
  }
  const total = Object.values(days).reduce((a, v) => a + v.pomos, 0);
  return { today: days[today] || { pomos: 0, focused: 0 }, week: sum(weekStart), month: sum(monthStart), streak, best, total, log };
}

function goalBar(n, goal) {
  if (!goal) return "";
  const cells = Math.min(goal, 12);
  const filled = Math.min(cells, Math.round((n / goal) * cells));
  return "●".repeat(filled) + "○".repeat(cells - filled);
}

function statsItems() {
  const st = computeStats(now());
  const goal = Math.round(num("daily_goal", 8, 0, 100));
  const pomos = (n) => plural(n, "pomodoro");
  const items = [
    item(`Today: ${pomos(st.today.pomos)} · ${fmtDur(st.today.focused)} focused`,
      goal ? `${goalBar(st.today.pomos, goal)}  ${st.today.pomos >= goal ? "Daily goal reached" : `Daily goal: ${goal}`}` : "", "stats"),
    item(`This week: ${pomos(st.week.pomos)} · ${fmtDur(st.week.focused)} focused`, `This month: ${pomos(st.month.pomos)} · ${fmtDur(st.month.focused)}`, "stats"),
    item(`Streak: ${plural(st.streak, "day")}`, `Best streak: ${plural(st.best, "day")} · ${pomos(st.total)} in total`, "streak"),
  ];
  const recent = st.log.slice(-8).reverse();
  for (const e of recent) {
    const k = KINDS[e.kind].name;
    const what = e.outcome === "completed" ? "" : ` (${e.outcome})`;
    items.push(item(`${k}${e.label ? `: ${e.label}` : ""}${what}`, `${fmtDur(e.focused)} · ${relDay(e.end)} at ${clockTime(e.end)}`, e.outcome === "completed" ? "done" : KINDS[e.kind].icon));
  }
  if (st.log.length) items.push(item("Reveal the session log in Finder", "sessions.jsonl: one JSON object per session", "info", { a: "reveal" }));
  return items;
}

// ----- Script Filter -----

function progressBar(done, total) {
  const n = 10, f = Math.max(0, Math.min(n, Math.round((done / Math.max(1, total)) * n)));
  return "▰".repeat(f) + "▱".repeat(n - f);
}

function startItem(s, kind, secs, label, primary) {
  const k = KINDS[kind];
  const title = `${primary ? "Start " : ""}${k.name} · ${fmtDur(secs)}${label ? ` · ${label}` : ""}`;
  const c = cycle(s);
  let sub = kind === "focus" ? `Pomodoro ${(c.count % longEvery()) + 1} of ${longEvery()}` : `Then a focus session`;
  if (isActive(s)) sub = `Replaces the current ${KINDS[s.kind].name.toLowerCase()} · ${sub}`;
  else sub += ` · Ends at ${clockTime(now() + secs)}`;
  return item(title, sub, k.icon, { a: "start", kind, secs, label, expect: isActive(s) ? s.id : "" }, kind === "focus" ? {} : { autocomplete: kind });
}

function statusItems(s) {
  const k = KINDS[s.kind] || KINDS.focus;
  const left = remaining(s);
  const running = s.status === "running";
  const label = s.label ? ` · ${s.label}` : "";
  const pomo = s.kind === "focus" ? `Pomodoro ${((s.count || 0) % longEvery()) + 1} of ${longEvery()}` : `Next: focus`;
  const tracked = s.track ? ` · Tracking in ${s.track.prov === "clockify" ? "Clockify" : "Toggl"}` : "";
  const bar = progressBar(s.secs - left, s.secs);
  const id = s.id;
  const nextKind = s.kind === "focus" ? (((s.count || 0) + 1) % longEvery() === 0 ? "long" : "short") : "focus";
  const skipName = KINDS[nextKind].name;
  const mods = {
    cmd: mod("Stop and log the time so far", { a: "stop", id }),
    alt: mod(`Skip to the ${skipName.toLowerCase()}`, { a: "skip", id }),
  };
  const items = [];
  if (running) {
    items.push(item(`${k.name} · ${fmtClock(left)} left${label}`, `${bar}  Ends at ${clockTime(s.end)} · ${pomo}${tracked}`, k.icon, { a: "pause", id }, { mods }));
    items.push(item("Pause", `Resume later with ${fmtClock(left)} left`, "pause", { a: "pause", id }, { mods }));
  } else {
    items.push(item(`Paused · ${k.name} · ${fmtClock(left)} left${label}`, `${bar}  ↩ Resume · ${pomo}${tracked}`, "pause", { a: "resume", id }, { mods }));
    items.push(item("Resume", `Ends at ${clockTime(now() + left)}`, "play", { a: "resume", id }, { mods }));
  }
  items.push(item("Add 5 Minutes", `Type +10 to add another amount`, "plus", { a: "extend", id, secs: 300 }));
  items.push(item("Stop", "End this session and log the time so far", "stop", { a: "stop", id }));
  items.push(item(`Skip to ${skipName}`, `Start the ${skipName.toLowerCase()} now (${fmtDur(lengthOf(nextKind))})`, "skip", { a: "skip", id }));
  return items;
}

function statsLine() {
  const st = computeStats(now());
  const goal = Math.round(num("daily_goal", 8, 0, 100));
  const sub = `This week: ${st.week.pomos} · Streak: ${plural(st.streak, "day")}`;
  return item(`Today: ${plural(st.today.pomos, "pomodoro")}${goal ? ` of ${goal}` : ""} · ${fmtDur(st.today.focused)}`, sub, "stats", null, { autocomplete: "stats" });
}

const COMMANDS = ["stats", "pause", "resume", "stop", "skip", "short", "long", "break", "focus"];

function pomoItems(query) {
  const fx = newEffects();
  let s;
  withLock(() => {
    s = loadState();
    reconcile(s, fx);
  });
  if (fx.msgs.length || fx.focusEnded) runEffects(fx, true);
  const q = oneLine(query);
  const lower = q.toLowerCase();
  const active = isActive(s);
  const rerun = s.status === "running" ? 1 : undefined;

  if (/^stats?$/.test(lower)) return output(statsItems());

  if (!q) {
    const items = [];
    if (active) items.push(...statusItems(s));
    else {
      const c = cycle(s);
      const order = [c.next, ...["focus", "short", "long"].filter((k) => k !== c.next)];
      const first = startItem(s, order[0], lengthOf(order[0]), "", true);
      if (s.status === "done" && !c.reset) {
        const doneK = KINDS[s.lastKind] ? KINDS[s.lastKind].name : "Session";
        first.subtitle = `${doneK} complete · ${first.subtitle}`;
      }
      if (order[0] === "focus") first.subtitle += " · Type a length like 50 or 1h30, then a label";
      items.push(first);
      for (const k of order.slice(1)) items.push(startItem(s, k, lengthOf(k), "", true));
    }
    items.push(statsLine());
    return output(items, { rerun });
  }

  const items = [];
  if (active) items.push(statusItems(s)[0]);
  const word = lower.split(" ")[0];
  const rest = q.slice(word.length).trim();

  // "+10" adds time to the running session
  const plus = lower.match(/^\+\s*(.+)$/);
  if (plus && active) {
    const secs = durationOf(plus[1].replace(/\s+/g, ""));
    if (secs) items.push(item(`Add ${fmtDur(secs)}`, `${fmtClock(remaining(s) + secs)} left afterwards`, "plus", { a: "extend", id: s.id, secs }));
    else items.push(item("Type a length to add, like +10 or +1h", "", "info"));
    return output(items, { rerun });
  }

  // a break or explicit focus: "short", "long 20", "focus 50 label"
  const kindWord = { short: "short", break: "short", long: "long", focus: "focus" }[word];
  if (kindWord) {
    const p = parseDuration(rest);
    const secs = p.secs || lengthOf(kindWord);
    if (p.secs !== null && (p.secs < 1 || p.secs > MAX_SECS)) items.push(item("Sessions can be up to 24 hours", "", "error"));
    else items.push(startItem(s, kindWord, secs, kindWord === "focus" ? p.label : p.label, true));
    return output(items, { rerun });
  }

  // command prefixes: "st" -> stats, stop
  if (word.length >= 2 && !rest) {
    const byCmd = {
      stats: () => statsLine(),
      pause: () => s.status === "running" && item("Pause", "", "pause", { a: "pause", id: s.id }),
      resume: () => s.status === "paused" && item("Resume", "", "play", { a: "resume", id: s.id }),
      stop: () => active && item("Stop", "End this session and log the time so far", "stop", { a: "stop", id: s.id }),
      skip: () => active && statusItems(s).find((i) => i.title.startsWith("Skip")),
    };
    for (const c of COMMANDS) if (c.startsWith(word) && byCmd[c]) {
      const it = byCmd[c]();
      if (it) items.push(it);
    }
  }

  // anything else: a focus session with an optional length and label
  const p = parseDuration(q);
  if (p.secs !== null && (p.secs < 1 || p.secs > MAX_SECS)) items.push(item("Sessions can be up to 24 hours", "Type a length like 25, 50, 1h30 or 90s", "error"));
  else items.push(startItem(s, "focus", p.secs || lengthOf("focus"), p.label, true));
  return output(items, { rerun });
}

// =====================================================================
// Time tracking: Toggl Track (API v9) and Clockify (API v1)
// =====================================================================

class ApiError extends Error {
  constructor(title, subtitle, kind, status) {
    super(title);
    this.subtitle = subtitle || "";
    this.kind = kind || "api"; // api | auth | network | quota | token
    this.status = status || 0;
  }
}

// HTTP through /usr/bin/curl. The whole request, including the auth header and body, goes in a
// curl config on stdin, so the token never appears in the process list.
function http(method, url, headers, body) {
  const q = (s) => '"' + String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\n/g, "\\n").replace(/\r/g, "\\r").replace(/\t/g, "\\t") + '"';
  const lines = [
    `url = ${q(url)}`, `request = ${q(method)}`, "silent", "show-error",
    `max-time = ${q(env("FT_HTTP_TIMEOUT", "12"))}`, 'connect-timeout = "6"',
    'write-out = "\\n%{http_code}"', 'user-agent = "AlfredFocusTimer/1.0"', 'header = "Accept: application/json"',
  ];
  for (const h of headers) lines.push(`header = ${q(h)}`);
  if (body !== undefined) {
    lines.push('header = "Content-Type: application/json"');
    lines.push(`data-raw = ${q(JSON.stringify(body))}`);
  }
  const r = exec("/usr/bin/curl", ["-K", "-"], lines.join("\n") + "\n");
  if (r.status !== 0) return { status: 0, curl: r.status };
  const i = r.out.lastIndexOf("\n");
  const status = parseInt(r.out.slice(i + 1), 10) || 0;
  const text = r.out.slice(0, i);
  let json = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch (e) {
    json = null;
  }
  return { status, json, text };
}

function checkResponse(prov, r) {
  const n = prov.name;
  if (r.status === 0) {
    if (r.curl === 28) throw new ApiError(`${n} didn't respond in time`, "Check your connection and try again", "network");
    throw new ApiError(`Can't reach ${n}`, "Check your internet connection", "network");
  }
  if (r.status >= 200 && r.status < 300) return r.json;
  if (r.status === 401 || r.status === 403) throw new ApiError(`${n} rejected the API token`, "Press ↩ to set a new token", "auth");
  if (r.status === 402) throw new ApiError(`${n} API limit reached`, "Toggl limits API calls per hour on your plan. Try again later.", "quota");
  if (r.status === 429) throw new ApiError(`Too many requests to ${n}`, "Wait a minute and try again", "quota");
  if (r.status >= 500) throw new ApiError(`${n} is having problems (${r.status})`, "Try again later", "api");
  let detail = "";
  if (r.json && typeof r.json === "object") detail = r.json.message || r.json.error || "";
  else if (typeof r.json === "string") detail = r.json;
  else detail = r.text || "";
  throw new ApiError(`${n} error ${r.status}`, oneLine(detail, 120), "api", r.status);
}

// Keychain: service = bundle id, account = provider. FT_KEYCHAIN_FILE (tests only) swaps in a file.
const KC_SERVICE = env("FT_KEYCHAIN_SERVICE", BUNDLE);
const TOKEN_RE = /^[A-Za-z0-9._+\/=:-]{8,256}$/;

function getToken(acct) {
  const f = env("FT_KEYCHAIN_FILE", "");
  if (f) return String(readJSON(f, {})[acct] || "");
  const r = exec("/usr/bin/security", ["find-generic-password", "-s", KC_SERVICE, "-a", acct, "-w"]);
  return r.status === 0 ? r.out.trim() : "";
}

function setToken(acct, token) {
  if (!TOKEN_RE.test(token) || !/^[\w.-]+$/.test(KC_SERVICE)) throw new ApiError("That doesn't look like an API token", "Copy it again from your profile page", "token");
  const f = env("FT_KEYCHAIN_FILE", "");
  if (f) {
    const all = readJSON(f, {});
    all[acct] = token;
    writeJSON(f, all);
    return;
  }
  // `security -i` reads the command from stdin, so the token is never a process argument.
  // Both values were validated above, so they cannot break out of the quotes.
  exec("/usr/bin/security", ["-i"], `add-generic-password -U -s "${KC_SERVICE}" -a "${acct}" -l "Focus Timer (${acct})" -w "${token}"\n`);
  if (getToken(acct) !== token) throw new ApiError("Couldn't save the token in the Keychain", "", "token");
}

function deleteToken(acct) {
  const f = env("FT_KEYCHAIN_FILE", "");
  if (f) {
    const all = readJSON(f, {});
    delete all[acct];
    writeJSON(f, all);
    return;
  }
  exec("/usr/bin/security", ["delete-generic-password", "-s", KC_SERVICE, "-a", acct]);
}

const qs = (o) => Object.entries(o).map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&");
const enc = encodeURIComponent;

function call(ctx, method, path, body) {
  return checkResponse(ctx.prov, http(method, ctx.prov.base() + path, ctx.prov.headers(ctx.token), body));
}

const Toggl = {
  key: "toggl",
  name: "Toggl Track",
  short: "Toggl",
  tokenURL: "https://track.toggl.com/profile",
  tokenHelp: "Find it at the bottom of your Toggl Track profile page",
  base: () => env("FT_TOGGL_URL", "https://api.track.toggl.com/api/v9").replace(/\/+$/, ""),
  headers: (token) => [`Authorization: Basic ${b64(`${token}:api_token`)}`],
  me(ctx) {
    const m = call(ctx, "GET", "/me");
    return { id: m.id, name: m.fullname || m.email || "", defaultWs: m.default_workspace_id };
  },
  workspaces(ctx) {
    return (call(ctx, "GET", "/me/workspaces") || []).map((w) => ({ id: w.id, name: w.name }));
  },
  projects(ctx) {
    const out = [];
    for (let page = 1; page <= 10; page++) {
      const batch = call(ctx, "GET", `/workspaces/${enc(ctx.ws)}/projects?${qs({ active: "true", per_page: 200, page })}`) || [];
      for (const p of batch) out.push({ id: p.id, name: p.name, billable: p.billable === true, client: p.client_name || "" });
      if (batch.length < 200) break;
    }
    return out;
  },
  tags(ctx) {
    return (call(ctx, "GET", `/workspaces/${enc(ctx.ws)}/tags`) || []).map((t) => ({ id: t.id, name: t.name }));
  },
  entries(ctx) {
    const t = now();
    const list = call(ctx, "GET", `/me/time_entries?${qs({ meta: "true", start_date: isoUTC(t - 14 * 86400), end_date: isoUTC(t + 86400) })}`) || [];
    return list.map(Toggl.norm).filter((e) => e.start).sort((a, b) => b.start - a.start);
  },
  norm(e) {
    return {
      id: e.id, ws: e.workspace_id, description: e.description || "", projectId: e.project_id || null,
      project: e.project_name || null, tags: e.tags || [], start: parseTime(e.start), stop: e.stop ? parseTime(e.stop) : null,
      running: e.duration < 0 || !e.stop,
    };
  },
  start(ctx, e) {
    const body = { created_with: "Alfred Focus Timer", description: e.description, workspace_id: Number(ctx.ws) || ctx.ws, start: isoUTC(now()), duration: -1, tags: e.tags };
    if (e.projectId) body.project_id = e.projectId;
    if (e.billable !== undefined) body.billable = e.billable;
    const r = Toggl.norm(call(ctx, "POST", `/workspaces/${enc(ctx.ws)}/time_entries`, body));
    if (!r.project && e.project) r.project = e.project;
    return r;
  },
  // Returns false when the entry was already stopped (or deleted) elsewhere.
  stop(ctx, entry) {
    try {
      call(ctx, "PATCH", `/workspaces/${enc(entry.ws || ctx.ws)}/time_entries/${enc(entry.id)}/stop`);
      return true;
    } catch (e) {
      if (e instanceof ApiError && (e.status === 409 || e.status === 404)) return false;
      throw e;
    }
  },
};

const CLOCKIFY_REGIONS = {
  global: "https://api.clockify.me/api/v1",
  usa: "https://use2.clockify.me/api/v1",
  eu: "https://euc1.clockify.me/api/v1",
  uk: "https://euw2.clockify.me/api/v1",
  au: "https://apse2.clockify.me/api/v1",
};

const Clockify = {
  key: "clockify",
  name: "Clockify",
  short: "Clockify",
  tokenURL: "https://app.clockify.me/user/preferences#advanced",
  tokenHelp: "Generate one in Clockify under Preferences → Advanced",
  base: () => env("FT_CLOCKIFY_URL", CLOCKIFY_REGIONS[env("clockify_region", "global")] || CLOCKIFY_REGIONS.global).replace(/\/+$/, ""),
  headers: (token) => [`X-Api-Key: ${token}`],
  me(ctx) {
    const m = call(ctx, "GET", "/user");
    return { id: m.id, name: m.name || m.email || "", defaultWs: m.activeWorkspace || m.defaultWorkspace };
  },
  workspaces(ctx) {
    return (call(ctx, "GET", "/workspaces") || []).map((w) => ({ id: w.id, name: w.name }));
  },
  projects(ctx) {
    return (call(ctx, "GET", `/workspaces/${enc(ctx.ws)}/projects?${qs({ archived: "false", "page-size": 500 })}`) || [])
      .map((p) => ({ id: p.id, name: p.name, billable: p.billable === true, client: p.clientName || "" }));
  },
  tags(ctx) {
    return (call(ctx, "GET", `/workspaces/${enc(ctx.ws)}/tags?${qs({ archived: "false", "page-size": 500 })}`) || []).map((t) => ({ id: t.id, name: t.name }));
  },
  entries(ctx) {
    const list = call(ctx, "GET", `/workspaces/${enc(ctx.ws)}/user/${enc(ctx.me.id)}/time-entries?${qs({ hydrated: "true", "page-size": 50 })}`) || [];
    return list.map(Clockify.norm).filter((e) => e.start).sort((a, b) => b.start - a.start);
  },
  norm(e) {
    const ti = e.timeInterval || {};
    return {
      id: e.id, ws: e.workspaceId, description: e.description || "", projectId: e.projectId || null,
      project: (e.project && e.project.name) || null, tags: (e.tags || []).map((t) => (typeof t === "string" ? t : t.name)).filter(Boolean),
      start: parseTime(ti.start), stop: ti.end ? parseTime(ti.end) : null, running: !ti.end,
    };
  },
  start(ctx, e) {
    // Clockify wants tag ids: reuse existing tags by name and create the missing ones.
    let tagIds = [];
    if (e.tags.length) {
      const known = cached(ctx, `tags-${ctx.ws}`, DAY, () => Clockify.tags(ctx));
      for (const name of e.tags) {
        let t = known.find((x) => norm(x.name) === norm(name));
        if (!t) {
          const created = call(ctx, "POST", `/workspaces/${enc(ctx.ws)}/tags`, { name });
          t = { id: created.id, name: created.name };
          known.push(t);
          cacheWrite(ctx, `tags-${ctx.ws}`, known);
        }
        tagIds.push(t.id);
      }
    }
    const body = { start: isoUTC(now()), description: e.description, tagIds };
    if (e.projectId) body.projectId = e.projectId;
    if (e.billable !== undefined) body.billable = e.billable; // Clockify ignores the project default
    const r = Clockify.norm(call(ctx, "POST", `/workspaces/${enc(ctx.ws)}/time-entries`, body));
    if (!r.project && e.project) r.project = e.project;
    if (!r.tags.length) r.tags = e.tags;
    return r;
  },
  stop(ctx, entry) {
    // This endpoint stops whatever is running, so make sure it is still the entry we mean.
    const running = Clockify.entries(ctx).find((x) => x.running);
    if (!running || running.id !== entry.id) return false;
    call(ctx, "PATCH", `/workspaces/${enc(entry.ws || ctx.ws)}/user/${enc(ctx.me.id)}/time-entries`, { end: isoUTC(now()) });
    return true;
  },
};

const PROVIDERS = { toggl: Toggl, clockify: Clockify };
const provider = () => PROVIDERS[env("tracker", "none")] || null;

const DAY = 86400;
const ERROR_BACKOFF = 120;
const ENTRY_TTL = () => Math.round(num("FT_ENTRY_TTL", 300, 0, 86400));

// Cache files are per provider: <cache>/<provider>-<name>.json = { t, v }
const cachePath = (ctx, name) => `${cacheDir()}/${ctx.prov.key}-${String(name).replace(/[^\w.-]/g, "_")}.json`;
function cacheRead(ctx, name) {
  const c = readJSON(cachePath(ctx, name), null);
  return c && typeof c.t === "number" ? c : null;
}
function cacheWrite(ctx, name, v) {
  writeJSON(cachePath(ctx, name), { t: now(), v });
}
function cached(ctx, name, ttl, fetch) {
  const c = cacheRead(ctx, name);
  if (c && now() - c.t < ttl && now() >= c.t) return c.v;
  const v = fetch();
  cacheWrite(ctx, name, v);
  return v;
}
function clearCache(prov) {
  const dir = cacheDir();
  const files = FM.contentsOfDirectoryAtPathError(dir, $());
  if (files.isNil()) return;
  for (let i = 0; i < files.count; i++) {
    const f = files.objectAtIndex(i).js;
    if (f.startsWith(`${prov.key}-`)) removeFile(`${dir}/${f}`);
  }
}

const norm = (s) => String(s || "").toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/[\s_-]+/g, "");
const slug = (name) => String(name).trim().replace(/\s+/g, "_");

function trackSettings() {
  return readJSON(`${dataDir()}/track.json`, {});
}

function context(prov) {
  const token = getToken(prov.key);
  if (!token) throw new ApiError(`Set your ${prov.name} API token`, prov.tokenHelp, "token");
  const ctx = { prov, token };
  ctx.me = cached(ctx, "me", DAY, () => prov.me(ctx));
  const sel = trackSettings()[prov.key] || {};
  ctx.ws = sel.ws || ctx.me.defaultWs;
  ctx.wsName = sel.name || "";
  return ctx;
}

const entriesName = (ctx) => `entries-${ctx.ws}`;

function fetchEntries(ctx) {
  const v = ctx.prov.entries(ctx);
  cacheWrite(ctx, entriesName(ctx), v);
  removeFile(cachePath(ctx, "error"));
  return v;
}

// Entries from the cache; a stale cache is shown at once and refreshed in the background.
function loadEntries(ctx) {
  const c = cacheRead(ctx, entriesName(ctx));
  if (c && now() - c.t < ENTRY_TTL() && now() >= c.t) return { entries: c.v, age: now() - c.t };
  if (c && !flag("FT_NO_BACKGROUND")) {
    // After a failed refresh (offline, rate limit, quota) wait a while before trying again,
    // or every rerun of the Script Filter would spend an API call.
    const err = cacheRead(ctx, "error");
    if (err && now() - err.t < ERROR_BACKOFF && now() >= err.t) return { entries: c.v, age: now() - c.t };
    spawnRefresh();
    return { entries: c.v, age: now() - c.t, refreshing: true };
  }
  return { entries: fetchEntries(ctx), age: 0 };
}

function spawnRefresh() {
  const lock = `${dataDir()}/refresh.lock`;
  const m = mtime(lock);
  if (m !== null && Date.now() / 1000 - m < 30) return;
  spawnDetached(["/usr/bin/osascript", "-l", "JavaScript", `${FM.currentDirectoryPath.js}/focus.js`, "track-refresh"]);
}

function trackRefresh() {
  const prov = provider();
  if (!prov) return "";
  const lock = `${dataDir()}/refresh.lock`;
  if (!FM.createDirectoryAtPathWithIntermediateDirectoriesAttributesError(lock, false, $(), $())) {
    const m = mtime(lock);
    if (m !== null && Date.now() / 1000 - m < 30) return "";
    removeFile(lock);
    FM.createDirectoryAtPathWithIntermediateDirectoriesAttributesError(lock, false, $(), $());
  }
  let ctx = null;
  try {
    ctx = context(prov);
    fetchEntries(ctx);
  } catch (e) {
    if (ctx) cacheWrite(ctx, "error", { title: e.message, subtitle: e.subtitle || "", kind: e.kind || "api" });
  } finally {
    removeFile(lock);
  }
  return "";
}

// Update the cached entries after starting or stopping, so the list is right without a request.
function cacheAfterStart(ctx, entry) {
  const c = cacheRead(ctx, entriesName(ctx));
  const list = (c ? c.v : []).map((e) => (e.running ? Object.assign({}, e, { running: false, stop: now() }) : e));
  list.unshift(entry);
  writeJSON(cachePath(ctx, entriesName(ctx)), { t: c ? c.t : now(), v: list.slice(0, 200) });
}
function cacheAfterStop(ctx, id) {
  const c = cacheRead(ctx, entriesName(ctx));
  if (!c) return;
  const list = c.v.map((e) => (String(e.id) === String(id) ? Object.assign({}, e, { running: false, stop: now() }) : e));
  writeJSON(cachePath(ctx, entriesName(ctx)), { t: c.t, v: list });
}

// ----- query parsing -----

function parseTrack(q) {
  const tokens = oneLine(q, 500).split(" ").filter(Boolean);
  const desc = [], tags = [];
  let project = null;
  for (const t of tokens) {
    if (t.length > 1 && t[0] === "@") project = t.slice(1);
    else if (t.length > 1 && t[0] === "#") tags.push(t.slice(1));
    else desc.push(t);
  }
  return { description: desc.join(" "), project, tags, last: tokens[tokens.length - 1] || "" };
}

// Exact name, else a unique prefix, else a unique substring (ignoring case, accents, spaces, _ and -).
function matchProject(projects, token) {
  const n = norm(token);
  if (!n) return { list: projects };
  const exact = projects.filter((p) => norm(p.name) === n);
  if (exact.length) return { project: exact[0], list: exact };
  const pre = projects.filter((p) => norm(p.name).startsWith(n));
  if (pre.length === 1) return { project: pre[0], list: pre };
  const sub = pre.length ? pre : projects.filter((p) => norm(p.name).includes(n));
  return { project: sub.length === 1 ? sub[0] : null, list: sub };
}

function entryLine(e) {
  return [e.project ? `@ ${e.project}` : "", ...e.tags.map((t) => `#${t}`)].filter(Boolean).join(" · ");
}

function errorItems(prov, e) {
  const items = [];
  if (e.kind === "auth" || e.kind === "token") {
    items.push(item(e.message, e.subtitle, "key", { a: "token-set" }, {
      mods: { cmd: mod(`Open the ${prov.name} page with your token`, { a: "open", url: prov.tokenURL }) },
    }));
  } else {
    items.push(item(e.message, e.subtitle, "error"));
  }
  return items;
}

function tokenItems(prov) {
  const has = !!getToken(prov.key);
  const items = [item(has ? `Replace the ${prov.name} API token` : `Set your ${prov.name} API token`, `${prov.tokenHelp} · ⌘↩ Open that page`, "key", { a: "token-set" }, {
    mods: { cmd: mod(`Open the ${prov.name} page with your token`, { a: "open", url: prov.tokenURL }) },
  })];
  if (has) items.push(item("Remove the saved token", "Deletes it from the Keychain", "stop", { a: "token-remove" }));
  return items;
}

function trackItems(query) {
  const prov = provider();
  const q = oneLine(query, 500);
  if (!prov) {
    return output([item("Choose Toggl Track or Clockify", "Set the time tracking service in the Workflow’s Configuration", "info")]);
  }
  const lower = q.toLowerCase();
  if (lower === "token" || lower === "api token") return output(tokenItems(prov));

  let ctx;
  try {
    ctx = context(prov);
  } catch (e) {
    if (!(e instanceof ApiError)) throw e;
    return output(errorItems(prov, e));
  }

  if (/^workspaces?(\s|$)/.test(lower)) {
    try {
      const list = cached(ctx, "workspaces", DAY, () => prov.workspaces(ctx));
      const f = norm(q.replace(/^\S+\s*/, ""));
      const items = list.filter((w) => !f || norm(w.name).includes(f)).map((w) =>
        item(`${String(w.id) === String(ctx.ws) ? "✓ " : ""}${w.name}`, `Use this ${prov.name} workspace`, "workspace", { a: "track-ws", ws: w.id, name: w.name }));
      return output(items.length ? items : [item("No matching workspace", "", "info")]);
    } catch (e) {
      if (!(e instanceof ApiError)) throw e;
      return output(errorItems(prov, e));
    }
  }

  const items = [];
  let entries = [], age = 0, refreshing = false, projects = [], tags = [];
  try {
    const r = loadEntries(ctx);
    entries = r.entries;
    age = r.age;
    refreshing = !!r.refreshing;
    projects = cached(ctx, `projects-${ctx.ws}`, DAY, () => prov.projects(ctx));
  } catch (e) {
    if (!(e instanceof ApiError)) throw e;
    items.push(...errorItems(prov, e));
    const c = cacheRead(ctx, entriesName(ctx));
    if (!c) return output(items);
    entries = c.v;
    const pc = cacheRead(ctx, `projects-${ctx.ws}`);
    projects = pc ? pc.v : [];
  }
  if (/(^|\s)#\S/.test(q)) {
    try {
      tags = cached(ctx, `tags-${ctx.ws}`, DAY, () => prov.tags(ctx));
    } catch (e) {
      const tc = cacheRead(ctx, `tags-${ctx.ws}`);
      tags = tc ? tc.v : [];
    }
  }
  const bgError = cacheRead(ctx, "error");
  if (bgError && !items.length) items.push(item(bgError.v.title, `Showing saved entries · ${bgError.v.subtitle}`, bgError.v.kind === "auth" ? "key" : "error", bgError.v.kind === "auth" ? { a: "token-set" } : null));

  const running = entries.find((e) => e.running);
  const nowT = now();
  const runningItem = () => {
    const el = nowT - running.start;
    return item(`▶ ${running.description || "(no description)"} · ${fmtClock(el)}`, `${entryLine(running) || "No project"} · Started ${clockTime(running.start)} · ↩ Stop`, "track",
      { a: "track-stop", id: running.id, ws: running.ws, description: running.description });
  };
  const p = parseTrack(q);
  const rerun = running || refreshing ? 1 : undefined;

  // Picking a project: the last word starts with @
  if (p.last.startsWith("@") && !/\s$/.test(query)) {
    const m = matchProject(projects, p.last.slice(1));
    const head = q.slice(0, q.length - p.last.length);
    for (const pr of m.list.slice(0, 40)) {
      items.push(item(pr.name, `${pr.client ? `${pr.client} · ` : ""}↩ Use this project`, "project", null, { autocomplete: `${head}@${slug(pr.name)} `, valid: false }));
    }
    if (!m.list.length) items.push(item(projects.length ? "No matching project" : "No projects in this workspace", `Active projects in ${ctx.wsName || prov.name}`, "info"));
    return output(items, { rerun });
  }

  if (!q) {
    if (running) items.push(runningItem());
    else items.push(item("Nothing is being tracked", "Type what you're working on: writing docs @project #tag", "info"));
    const seen = new Set();
    for (const e of entries) {
      if (e.running || String(e.ws) !== String(ctx.ws)) continue;
      const k = JSON.stringify([e.description, e.projectId, e.tags.slice().sort()]);
      if (seen.has(k)) continue;
      seen.add(k);
      items.push(recentItem(e, ctx));
      if (seen.size >= 12) break;
    }
    items.push(item("Projects", `${projects.length} active in ${ctx.wsName || "this workspace"} · Type @ to pick one`, "project", null, { autocomplete: "@" }));
    items.push(item(`Workspace${ctx.wsName ? `: ${ctx.wsName}` : ""}`, `Choose the ${prov.name} workspace`, "workspace", null, { autocomplete: "workspace " }));
    const when = refreshing ? "Refreshing…" : age < 60 ? "Updated just now" : `Updated ${fmtDur(age)} ago`;
    items.push(item(`Refresh from ${prov.name}`, `${when} · Signed in as ${ctx.me.name}`, "refresh", { a: "track-refresh" }));
    items.push(...tokenItems(prov).slice(0, 1));
    return output(items, { rerun });
  }

  // Starting a new entry
  let project = null, warn = "";
  if (p.project !== null) {
    const m = matchProject(projects, p.project);
    project = m.project;
    if (!project) warn = m.list.length ? `“${p.project}” matches ${m.list.length} projects: type more` : `No project matches “${p.project}”`;
  }
  const tagNames = p.tags.map((t) => {
    const found = tags.find((x) => norm(x.name) === norm(t));
    return found ? found.name : t;
  });
  if (running && /^stop$/.test(lower)) items.push(runningItem());
  const what = p.description ? `“${p.description}”` : project ? `@ ${project.name}` : "";
  if (warn) items.push(item(warn, "Type @ to list the projects", "error"));
  else if (!what) items.push(item("Type a description, @project or #tag", "", "info"));
  else {
    const act = { a: "track-start", description: p.description, projectId: project ? project.id : null, project: project ? project.name : null, tags: tagNames };
    const line = [project ? `@ ${project.name}` : "", ...tagNames.map((t) => `#${t}`)].filter(Boolean).join(" · ");
    items.push(item(`Start ${what}`, `${line ? `${line} · ` : ""}${running ? `Stops “${oneLine(running.description, 40)}” · ` : ""}⌘↩ With a Pomodoro`, "play", act, {
      mods: { cmd: mod(`Start with a ${fmtDur(lengthOf("focus"))} Pomodoro`, Object.assign({}, act, { pomo: true })) },
    }));
  }
  const f = norm(p.description);
  if (f) {
    const seen = new Set();
    for (const e of entries) {
      if (e.running || String(e.ws) !== String(ctx.ws) || !norm(e.description).includes(f)) continue;
      const k = JSON.stringify([e.description, e.projectId, e.tags.slice().sort()]);
      if (seen.has(k)) continue;
      seen.add(k);
      items.push(recentItem(e, ctx));
      if (seen.size >= 8) break;
    }
  }
  if (running && !/^stop$/.test(lower)) items.push(runningItem());
  return output(items, { rerun });
}

function recentItem(e, ctx) {
  const dur = e.stop && e.start ? ` · ${fmtDur(e.stop - e.start)}` : "";
  const act = { a: "track-start", description: e.description, projectId: e.projectId, project: e.project, tags: e.tags };
  return item(e.description || "(no description)", `${entryLine(e) || "No project"}${dur} · ${relDay(e.start)} · ↩ Restart`, "recent", act, {
    mods: { cmd: mod(`Restart with a ${fmtDur(lengthOf("focus"))} Pomodoro`, Object.assign({}, act, { pomo: true })) },
  });
}

// ----- tracking actions -----

function startEntry(ctx, a) {
  let billable;
  if (a.projectId) {
    try {
      const projects = cached(ctx, `projects-${ctx.ws}`, DAY, () => ctx.prov.projects(ctx));
      const pr = projects.find((x) => String(x.id) === String(a.projectId));
      if (pr) billable = pr.billable;
    } catch (e) {
      billable = undefined; // start anyway; the service applies its default
    }
  }
  const entry = ctx.prov.start(ctx, {
    description: oneLine(a.description, 3000), projectId: a.projectId || null, project: a.project || null,
    tags: (a.tags || []).map((t) => oneLine(t, 100)).filter(Boolean), billable,
  });
  cacheAfterStart(ctx, entry);
  return entry;
}

function trackAction(a) {
  const prov = provider();
  if (!prov) return "Choose Toggl Track or Clockify in the Workflow’s Configuration.";
  try {
    switch (a.a) {
      case "token-set": {
        let token = env("FT_TOKEN_INPUT", null);
        if (token === null) token = askToken(prov);
        token = String(token || "").trim();
        if (!token) return "";
        setToken(prov.key, token);
        clearCache(prov);
        const ctx = context(prov);
        return `Connected to ${prov.name}${ctx.me.name ? ` as ${ctx.me.name}` : ""}.`;
      }
      case "token-remove":
        deleteToken(prov.key);
        clearCache(prov);
        return `Removed the ${prov.name} token.`;
      case "open":
        if (a.url === prov.tokenURL) exec("/usr/bin/open", [a.url]);
        return "";
      case "track-ws": {
        const all = trackSettings();
        all[prov.key] = { ws: a.ws, name: oneLine(a.name, 200) };
        writeJSON(`${dataDir()}/track.json`, all);
        return `Using the ${oneLine(a.name, 80)} workspace.`;
      }
      case "track-refresh": {
        const ctx = context(prov);
        clearCache(prov);
        ctx.me = cached(ctx, "me", DAY, () => prov.me(ctx));
        fetchEntries(ctx);
        cacheWrite(ctx, `projects-${ctx.ws}`, prov.projects(ctx));
        alfredTrigger("track", "");
        return "";
      }
      case "track-stop": {
        const ctx = context(prov);
        const cachedEntry = (cacheRead(ctx, entriesName(ctx)) || { v: [] }).v.find((e) => String(e.id) === String(a.id));
        const stopped = prov.stop(ctx, { id: a.id, ws: a.ws });
        cacheAfterStop(ctx, a.id);
        // A Pomodoro that started this entry no longer owns it.
        withLock(() => {
          const s = loadState();
          if (s.track && String(s.track.id) === String(a.id)) {
            s.track = null;
            saveState(s);
          }
        });
        if (!stopped) return `“${oneLine(a.description, 60) || "timer"}” was already stopped.`;
        const el = cachedEntry && cachedEntry.start ? ` after ${fmtClock(now() - cachedEntry.start)}` : "";
        return `Stopped “${oneLine(a.description, 60) || "timer"}”${el}.`;
      }
      case "track-start": {
        const ctx = context(prov);
        const entry = startEntry(ctx, a);
        let msg = `Started “${oneLine(entry.description || a.project || "timer", 60)}” in ${prov.short}.`;
        if (a.pomo) {
          const s = withLock(() => loadState());
          const r = pomoAction({ a: "start", kind: "focus", secs: lengthOf("focus"), label: oneLine(a.description || a.project, 200), expect: isActive(s) ? s.id : "", track: { prov: prov.key, ws: entry.ws || ctx.ws, id: entry.id, owned: true } });
          msg += ` ${r}`;
        }
        return msg;
      }
    }
  } catch (e) {
    if (!(e instanceof ApiError)) throw e;
    return `${e.message}. ${e.subtitle}`.trim();
  }
  return "";
}

function askToken(prov) {
  const app = Application.currentApplication();
  app.includeStandardAdditions = true;
  try {
    app.activate();
    const r = app.displayDialog(`Paste your ${prov.name} API token.\n${prov.tokenHelp}.\nIt is stored in your macOS Keychain.`, {
      defaultAnswer: "", hiddenAnswer: true, buttons: ["Cancel", "Save"], defaultButton: "Save", cancelButton: "Cancel", withTitle: "Focus Timer",
    });
    return r.textReturned;
  } catch (e) {
    return ""; // cancelled
  }
}

// ----- Pomodoro ↔ tracker link -----

// Start an entry for a new focus session unless something is already being tracked.
function trackerStartForFocus(f) {
  const prov = provider();
  if (!prov) return "";
  try {
    const ctx = context(prov);
    const running = fetchEntries(ctx).find((e) => e.running);
    if (running) return `${prov.short} is already tracking “${oneLine(running.description, 40)}”.`;
    const entry = startEntry(ctx, { description: f.label || "Focus", tags: [] });
    let owned = false;
    withLock(() => {
      const s = loadState();
      if (s.id === f.id && isActive(s)) {
        s.track = { prov: prov.key, ws: entry.ws || ctx.ws, id: entry.id, owned: true };
        saveState(s);
        owned = true;
      }
    });
    if (!owned) prov.stop(ctx, entry); // the session ended while we were starting the entry
    return owned ? `Tracking in ${prov.short}.` : "";
  } catch (e) {
    if (!(e instanceof ApiError)) throw e;
    return `${prov.short}: ${e.message}.`;
  }
}

// Stop the entry a focus session started, if it is still the one running.
function trackerStopOwned(track) {
  const prov = PROVIDERS[track.prov];
  if (!prov) return "";
  try {
    const ctx = context(prov);
    const stopped = prov.stop(ctx, { id: track.id, ws: track.ws });
    cacheAfterStop(ctx, track.id);
    return stopped ? `Stopped the ${prov.short} timer.` : "";
  } catch (e) {
    if (!(e instanceof ApiError)) throw e;
    return `${prov.short}: ${e.message}.`;
  }
}

// =====================================================================

function action(arg) {
  let a;
  try {
    a = JSON.parse(arg);
  } catch (e) {
    return "";
  }
  if (!a || typeof a !== "object") return "";
  if (["start", "pause", "resume", "stop", "skip", "extend"].includes(a.a)) return pomoAction(a);
  if (a.a === "reveal") {
    if (exists(logPath())) exec("/usr/bin/open", ["-R", logPath()]);
    return "";
  }
  return trackAction(a);
}

function run(argv) {
  const [cmd, ...rest] = argv;
  const arg = rest.join(" ");
  try {
    switch (cmd) {
      case "pomo": return pomoItems(arg);
      case "track": return trackItems(arg);
      case "action": return action(arg);
      case "complete": return complete(arg);
      case "track-refresh": return trackRefresh();
    }
  } catch (e) {
    if (cmd === "pomo" || cmd === "track") return output([item("Something went wrong", oneLine(e.message, 150), "error")]);
    return `Focus Timer: ${oneLine(e.message, 150)}`;
  }
  return "";
}
