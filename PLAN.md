# Focus Timer — Plan

**Priority tier:** 1 · **Bundle ID:** `io.github.x-o-r-r-o.focus-timer` · **Keywords:** `pomo`, `track`

## Why build it
Raycast demand this workflow replaces (downloads, 2026-09-26):

| Raycast extension | Downloads |
|---|---|
| Pomodoro | 110,926 |
| Toggl Track | 14,032 |
| Session | 8,757 |
| Flow Timer | 8,666 |
| Clockify | 4,630 |
| **Total** | **147,011** |

**Alfred today:** SandwichTimer, Timer (gallery), Toggl workflow from 2013, no Clockify.

## Features (v1.0)
- [x] `pomo` start 25/5/15 (configurable) cycles, long break every N, custom lengths (`pomo 50`, `pomo 1h30`), labels, pause/resume/stop/skip/+time, notifications + sound, optional Focus mode on/off via Shortcuts, auto-start options
- [x] Background waiter (wall-clock, sleep/wake safe, PID-checked, recovers after reboot); live countdown with `rerun`
- [x] `pomo stats` today/week/month, daily goal, streak and best streak (DST-safe, locale week start); JSON-lines session log
- [x] `track <desc> @project #tag` start/stop Toggl Track (v9) or Clockify (v1, regions) timer, pick project/workspace, restart recent entries, Universal Action
- [x] Token in the Keychain via a hidden-input dialog (`security -i`, never in argv); 401/402/403/429/offline handled; cached with background refresh and back-off
- [x] Link a pomodoro to a time-tracking entry automatically (config) or per entry (⌘↩)

## Features (v1.1, round-4 audit)
- [x] Recent custom focus sessions (label or non-default length) in the `pomo` menu; typing part of a label finds them (Raycast Pomodoro requests for several interval lengths, raycast/extensions#28699, #17949)
- [x] `track report`: time tracked today and this week, in total and by project (Raycast Toggl Track request raycast/extensions#25258)
- [x] Default project for new timers and Pomodoro tracking (Raycast Toggl Track request raycast/extensions#25076)

## Tech
- **Stack:** bash + JXA; background timer via a `launchd`-free detached waiter (`src/waiter.sh`) + Alfred External Triggers for notifications.
- **Dependencies:** None (API tokens for Toggl/Clockify in Keychain).
- Output via Alfred Script Filter JSON; settings via Workflow Configuration (`userconfigurationconfig`).
- Secrets (API keys/tokens) in the macOS Keychain, never in `prefs.plist`.
- Target: macOS 13+ on Apple Silicon and Intel.

## Milestones
1. Script filter prototype for the main keyword
2. Actions + modifiers, Universal Actions / File Actions where relevant
3. Workflow Configuration, icons, error states (no network / missing dependency)
4. README with screenshots, `python3 tools/build.py --package` release, forum post, then Gallery submission when invited

## Known limitations
- Toggl Track's free plan allows 30 API requests per hour. Entries are cached for 5 minutes, projects and tags for a day, and the workflow stops sending requests until the quota resets; a quota reached elsewhere (another app) shows up the same way.
- Clockify lists the 50 most recent entries; Toggl Track the last 14 days.
- The session log (`sessions.jsonl` in the workflow's data folder) is kept forever for the stats and the best streak; it grows by about 150 bytes per session.
- Cached entries are kept per workspace; switching between many workspaces leaves a small file per workspace in the cache folder.
- A session that ended while the Mac was asleep or off is reported quietly and doesn't auto-start the next one.
- The background timer keeps the Workflow Configuration it was started with: a setting changed during a session (sound, auto-start, Shortcuts, Pomodoro tracking) applies from the next session.
- `track report` counts the cached entries: Toggl Track's last 14 days (the whole week) but only Clockify's 50 most recent entries, which the report says when it hits that cap.
- A failed request in the Script Filter (offline, bad token, server error) is not retried for 15 seconds while typing; ↩ on the error tries again at once.

## Verify in real Alfred
- [ ] Notifications from the background timer (External Trigger → Post Notification) appear, including when Alfred is closed, and the sound plays.
- [ ] `osascript` → Alfred `run trigger` doesn't raise an Automation permission prompt (or the prompt is understandable).
- [ ] The token dialog (hidden input) comes to the front when started from Alfred, and Cancel does nothing.
- [ ] A running session survives quitting Alfred and a sleep/wake cycle; after a reboot, it catches up the next time `pomo` opens.
- [ ] The Shortcuts start/end hooks run a real Shortcut, and a wrong name produces the notification.
- [ ] The live countdown (`rerun`) updates every second without flicker; typing while it runs is not slowed down.
- [ ] Universal Action on selected text opens `track` with the text; ⌘↩ and ⌥↩ in `pomo` do what the subtitles say on every row.
- [ ] Updating the workflow while a session runs: the session still ends with a notification (the waiter re-enters the replaced folder).
- [ ] Clockify regional endpoints (EU/UK/AU/USA) with a real account; Toggl Track 402 quota headers on a free plan.

## Release checklist (Alfred forum + Gallery)
Sources: alfred.app/submit, alfred.app/submit/styleguide, alfred.app/submit/screenshots, alfredforum.com topics 23976 and 23388.

- [x] README starts with `## Usage`; each paragraph ends "via the `kw` keyword" / "via the Universal Action"
- [ ] A clean screenshot (window only, transparent background, real-looking data, no other workflows) after each paragraph, stored in `images/`
- [x] Modifiers listed as `* <kbd>⌘</kbd><kbd>↩</kbd> Action.`; Quick Look written as <kbd>⌘</kbd><kbd>Y</kbd>
- [x] `## Setup` only for genuine manual steps (no app installs or API keys; the Gallery lists those)
- [x] Every keyword is ≥ 3 characters and configurable via `{var:keyword_*}`
- [x] Settings in Workflow Configuration; the info.plist `readme` (About This Workflow) matches README.md
- [x] Main icon ≥ 256×256 px
- [x] No self-updater; never download or install software (no pip/brew/curl of binaries); dependencies declared for Alfred to handle
- [x] No compiled binaries (nothing to sign or notarise); never strip quarantine
- [x] No hard-coded paths; `prefs.plist` is git-ignored; secrets stay in Keychain
- [x] AI assistance disclosed in the README and the forum post
- [ ] Version bumped in `src/info.plist`; `python3 tools/build.py --package`; GitHub release with the `.alfredworkflow` attached
- [ ] Forum post in "Share your Workflows" with a screenshot, keywords, and the GitHub link

## Ideas for v1.1
Ranked by value for effort; not implemented in the round-4 audit.
1. Pick up Workflow Configuration changes in a running session (read the current values from the workflow's `prefs.plist` when the waiter completes a session, falling back to the environment).
2. `track report` for last week and per-day totals (Toggl Reports API or a wider time-entries window; Clockify paging beyond 50 entries).
3. Choose the next session from the notification (Alfred 5.5 Text View or an "only show if populated" follow-up Script Filter) instead of `show_alfred`.
4. Clockify tasks and Toggl tasks (`@project/task`), requested for the Raycast Clockify extension.
5. Add a manual entry for past time (`track 1h meeting yesterday`), requested for Raycast Clockify (raycast/extensions#23844).
6. Export the Pomodoro log as CSV from `pomo stats` (⌘↩ on the reveal row).
7. A "pause tracking with the Pomodoro" option (stop the linked entry on pause, start a new one on resume).
