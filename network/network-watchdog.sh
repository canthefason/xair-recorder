#!/bin/bash
# Watches the WiFi device and automatically switches into venue AP mode if
# it's been disconnected (no reachable network, e.g. away from home) for
# several consecutive checks - so there's no need to remember to click
# "Switch to Venue AP" before leaving.
#
# Deliberately does NOT auto-switch back to home when a network becomes
# reachable again: that would silently disconnect anyone currently using the
# venue AP mid-show with no warning. Switching back stays a deliberate
# action (web UI "Switch to Home WiFi" / home-mode.sh).
#
# Runs as a long-lived loop (systemd Restart=always keeps it alive if it
# ever exits), not a one-shot timer job, so the consecutive-disconnect count
# just lives in a local variable - no state file needed for that part.
set -euo pipefail

WIFI_DEVICE="${1:-wlan0}"
CHECK_INTERVAL="${XAIR_WATCHDOG_INTERVAL:-15}"       # seconds between checks
DISCONNECT_THRESHOLD="${XAIR_WATCHDOG_THRESHOLD:-4}" # consecutive failed checks before switching
MODE_FILE="${XAIR_NETWORK_MODE_FILE:-/var/lib/xair-recorder/network-mode}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

is_connected() {
  local state
  state=$(nmcli -t -f DEVICE,STATE device status | awk -F: -v dev="$WIFI_DEVICE" '$1==dev {print $2}')
  [ "$state" = "connected" ]
}

current_mode() {
  if [ -f "$MODE_FILE" ]; then cat "$MODE_FILE"; else echo "home"; fi
}

echo "xair-network-watchdog: watching $WIFI_DEVICE every ${CHECK_INTERVAL}s" \
     "(auto-switches to venue AP after $DISCONNECT_THRESHOLD consecutive disconnected checks," \
     "i.e. ~$((CHECK_INTERVAL * DISCONNECT_THRESHOLD))s with no network)"

consecutive_disconnected=0

while true; do
  sleep "$CHECK_INTERVAL"

  if [ "$(current_mode)" = "venue" ]; then
    consecutive_disconnected=0
    continue
  fi

  if is_connected; then
    consecutive_disconnected=0
    continue
  fi

  consecutive_disconnected=$((consecutive_disconnected + 1))
  echo "xair-network-watchdog: $WIFI_DEVICE disconnected ($consecutive_disconnected/$DISCONNECT_THRESHOLD)"

  if [ "$consecutive_disconnected" -ge "$DISCONNECT_THRESHOLD" ]; then
    echo "xair-network-watchdog: no network for ~$((CHECK_INTERVAL * DISCONNECT_THRESHOLD))s - switching to venue AP"
    "$SCRIPT_DIR/venue-mode.sh" "$WIFI_DEVICE" || echo "xair-network-watchdog: venue-mode.sh failed" >&2
    consecutive_disconnected=0
  fi
done
