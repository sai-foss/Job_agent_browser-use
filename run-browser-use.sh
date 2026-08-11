#!/usr/bin/env bash

cd "$(dirname "$(readlink -f "$0")")"

# One agent at a time. This is not optional given the pkill below: without the lock a second
# launch would tear the browser out from under a run already in progress. Concurrent runs are
# a bad idea here anyway - they share user_data_dir, and progress.csv is read-modify-written
# with no locking, so they lose each other's rows.
exec 9>/tmp/browser-use.lock
flock -n 9 || { echo "another run-browser-use.sh already holds the lock - exiting"; exit 1; }

source .env
source .venv/bin/activate

# A crashed run leaves chromium holding user_data_dir, and the next launch then contends with
# the corpse instead of starting clean. TERM first so the profile gets flushed properly; only
# escalate to KILL for whatever ignored it.
# NOTE: this is deliberately every chrome/chromium on the box, not just the agent's - so it
# will also close a personal browsing session. Narrow the pattern to
# 'user-data-dir=.*/.config/browser-use' if that ever matters.
kill_browsers() {
  pgrep -f '[c]hrome|[c]hromium' >/dev/null || return 0
  echo "killing leftover chrome/chromium..."
  pkill -f  '[c]hrome|[c]hromium'
  sleep 3
  pkill -9 -f '[c]hrome|[c]hromium' 2>/dev/null
  sleep 1
}

# main.py parks itself and re-probes when Ollama credits run out, so it is expected to
# run forever. This loop only covers hard crashes (browser death, OOM, network drop).
while true; do
  kill_browsers
  python main.py && break
  echo "main.py exited $? - restarting in 60s"
  sleep 60
done
