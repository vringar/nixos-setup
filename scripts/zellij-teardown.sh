#!/usr/bin/env bash
# Tear down the running Zellij session without destroying its saved layout.
#
# Order is the whole point. While the server is alive it watches panes exit,
# closes the emptied tabs, and serialises that collapsed state over the saved
# layout -- so killing the pane shells first silently destroys the layout. The
# server must die before the shells do.
#
# Pane shells live in zjpanes.slice, and so does this script when it is run
# from inside a pane. Stopping that slice would therefore kill the teardown
# halfway through, so the destructive half re-execs into a detached unit under
# app.slice first.
#
# The session itself is left intact so it can be resurrected on next attach.
# Use `zellij delete-session` for that, and expect to lose the layout with it.

SLICE=zjpanes.slice
LAYOUT_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/zellij/contract_version_1/session_info"
BACKUP_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/zellij-layouts"

if [ -z "${ZELLIJ_TEARDOWN_DETACHED:-}" ]; then
  # Back up in the foreground so the paths land where the user can see them.
  mkdir -p "$BACKUP_DIR"
  stamp=$(date +%Y%m%d-%H%M%S)
  for layout in "$LAYOUT_DIR"/*/session-layout.kdl; do
    [ -e "$layout" ] || continue
    session=$(basename "$(dirname "$layout")")
    cp -- "$layout" "$BACKUP_DIR/$session-$stamp.kdl"
    echo "saved layout: $BACKUP_DIR/$session-$stamp.kdl"
  done

  echo "tearing down (server first, then panes)..."
  exec systemd-run --user --quiet --collect \
    --description="zellij teardown" \
    --setenv=ZELLIJ_TEARDOWN_DETACHED=1 \
    -- "$0" "$@"
fi

# Server first: nothing may re-serialise a session whose panes are dying.
pkill -f 'zellij --server' || true
for _ in $(seq 1 50); do
  pgrep -f 'zellij --server' >/dev/null || break
  sleep 0.1
done
if pgrep -f 'zellij --server' >/dev/null; then
  pkill -9 -f 'zellij --server' || true
fi

# Only now the pane shells, which pkill cannot reach by name.
systemctl --user stop "$SLICE" || true
