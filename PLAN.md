# Focus Timer — Plan

**Priority tier:** 1 · **Bundle ID:** `com.xorro.focus-timer`

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
- [ ] `pomo` start 25/5 (configurable) cycles, notifications + sound, optional Focus mode on/off via Shortcuts
- [ ] `pomo stats` today/week sessions log
- [ ] `track <desc>` start/stop Toggl or Clockify timer, pick project, list recent entries
- [ ] Link a pomodoro to a time-tracking entry automatically

## Tech
- **Stack:** zsh + JXA; background timer via `launchd`-free detached process + Alfred External Triggers.
- **Dependencies:** None (API tokens for Toggl/Clockify in Keychain).
- Output via Alfred Script Filter JSON; settings via Workflow Configuration (`userconfigurationconfig`).
- Secrets (API keys/tokens) in the macOS Keychain, never in `prefs.plist`.
- Target: macOS 13+ on Apple Silicon and Intel (universal binaries for any Swift helpers).

## Milestones
1. Script filter prototype for the main keyword
2. Actions + modifiers, Universal Actions / File Actions where relevant
3. Workflow Configuration, icons, error states (no network / missing dependency)
4. README with screenshots, `build.sh` release, submit to Alfred Gallery + forum post
