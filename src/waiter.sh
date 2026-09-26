#!/bin/bash
# Background timer for one Focus Timer session, started detached by focus.js.
# Usage: waiter.sh <session-id> <data-dir>
#
# It sleeps in short steps and compares the wall clock with the end time each time, so it fires
# on time after the Mac wakes from sleep. It exits quietly as soon as the session is paused,
# stopped or replaced, because <data-dir>/running then no longer names this session. After asking
# focus.js to complete the session it keeps watching, in case time was added at the last moment.
id="$1"
data="$2"
cd "$(dirname "$0")" || exit 1
case "$id" in '' | *[!0-9a-f]*) exit 1 ;; esac
run="$data/running"
step="${FT_WAITER_STEP:-15}"
sp=""
trap '[ -n "$sp" ] && kill "$sp" 2>/dev/null; exit 0' TERM INT

tries=0
while :; do
  [ -f "$run" ] || exit 0
  read -r rid rend <"$run" || exit 0
  [ "$rid" = "$id" ] || exit 0
  case "$rend" in '' | *[!0-9]*) exit 0 ;; esac
  left=$((rend - $(date +%s)))
  if [ "$left" -le 0 ]; then
    # Keep watching afterwards: if time was added just as the session ended, "complete" does nothing
    # and the running file still names this session with a later end time.
    tries=$((tries + 1))
    [ "$tries" -gt 5 ] && exit 0
    /usr/bin/osascript -l JavaScript ./focus.js complete "$id" >/dev/null 2>&1
    left=1
  fi
  [ "$left" -gt "$step" ] && left="$step"
  sleep "$left" &
  sp=$!
  wait "$sp"
  sp=""
done
