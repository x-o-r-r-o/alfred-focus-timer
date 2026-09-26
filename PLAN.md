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
- [ ] `pomo` start 25/5 (configurable) cycles, notifications + sound, optional Focus mode on/off via Shortcuts
- [ ] `pomo stats` today/week sessions log
- [ ] `track <desc>` start/stop Toggl or Clockify timer, pick project, list recent entries
- [ ] Link a pomodoro to a time-tracking entry automatically

## Tech
- **Stack:** zsh + JXA; background timer via `launchd`-free detached process + Alfred External Triggers.
- **Dependencies:** None (API tokens for Toggl/Clockify in Keychain).
- Output via Alfred Script Filter JSON; settings via Workflow Configuration (`userconfigurationconfig`).
- Secrets (API keys/tokens) in the macOS Keychain, never in `prefs.plist`.
- Target: macOS 13+ on Apple Silicon and Intel.

## Milestones
1. Script filter prototype for the main keyword
2. Actions + modifiers, Universal Actions / File Actions where relevant
3. Workflow Configuration, icons, error states (no network / missing dependency)
4. README with screenshots, `build.sh` release, forum post, then Gallery submission when invited

## Release checklist (Alfred forum + Gallery)
Sources: alfred.app/submit, alfred.app/submit/styleguide, alfred.app/submit/screenshots, alfredforum.com topics 23976 and 23388.

- [ ] README starts with `## Usage`; each paragraph ends "via the `kw` keyword" / "via the Universal Action"
- [ ] A clean screenshot (window only, transparent background, real-looking data, no other workflows) after each paragraph, stored in `images/`
- [ ] Modifiers listed as `* <kbd>⌘</kbd><kbd>↩</kbd> Action.`; Quick Look written as <kbd>⌘</kbd><kbd>Y</kbd>
- [ ] `## Setup` only for genuine manual steps (no app installs or API keys; the Gallery lists those)
- [ ] Every keyword is ≥ 3 characters and configurable via `{var:keyword_*}`
- [ ] Settings in Workflow Configuration; the info.plist `readme` (About This Workflow) matches README.md
- [ ] Main icon ≥ 256×256 px
- [ ] No self-updater; never download or install software (no pip/brew/curl of binaries); dependencies declared for Alfred to handle
- [ ] Any compiled binary is Developer ID signed + notarised; never strip quarantine
- [ ] No hard-coded paths; `prefs.plist` is git-ignored; secrets stay in Keychain
- [ ] AI assistance disclosed in the README and the forum post
- [ ] Version bumped in `src/info.plist`; `./build.sh`; GitHub release with the `.alfredworkflow` attached
- [ ] Forum post in "Share your Workflows" with a screenshot, keywords, and the GitHub link
