#!/bin/bash
# Run at boot (via the xair-network-restore systemd unit) to re-activate
# venue AP mode if that's what was last selected.
#
# NetworkManager's own autoconnect only ever brings up profiles with
# autoconnect=yes, and xair-ap deliberately has autoconnect=no (so a normal
# boot at home doesn't make the device broadcast its own AP instead of
# joining the saved network). That means without this script, rebooting
# while in venue mode - away from the saved network - leaves wlan0 with
# nothing active at all: the saved network isn't in range to autoconnect to,
# and the AP profile never self-activates either.
#
# Deliberately does NOT touch the "previous connection" state file that
# venue-mode.sh wrote when the user first switched - only recreates the AP,
# which is already the current nmcli state we want to restore.
set -euo pipefail

WIFI_DEVICE="${1:-wlan0}"
AP_CONN_NAME="xair-ap"
MODE_FILE="${XAIR_NETWORK_MODE_FILE:-/var/lib/xair-recorder/network-mode}"

if [ ! -f "$MODE_FILE" ] || [ "$(cat "$MODE_FILE")" != "venue" ]; then
  echo "Mode is not 'venue' (or not set) - leaving NetworkManager autoconnect to handle $WIFI_DEVICE."
  exit 0
fi

# NetworkManager may not have finished bringing up wlan0 immediately after
# boot; retry briefly rather than failing the whole systemd unit on a race.
ERR_FILE=$(mktemp)
trap 'rm -f "$ERR_FILE"' EXIT

for attempt in 1 2 3 4 5; do
  if nmcli connection up "$AP_CONN_NAME" 2>"$ERR_FILE"; then
    echo "Restored venue AP mode on boot."
    exit 0
  fi
  sleep 2
done

echo "Failed to restore venue AP mode after boot:" >&2
cat "$ERR_FILE" >&2
exit 1
