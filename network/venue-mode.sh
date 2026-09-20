#!/bin/bash
# Switches the WiFi device from its current network into the standalone
# access point defined by setup-ap-profile.sh. Run this before heading
# somewhere with no network available.
#
# Doesn't hardcode a "home network" name: whatever connection is active on
# the WiFi device right now gets saved to a state file so home-mode.sh can
# restore it later, regardless of what that network is called.
set -euo pipefail

WIFI_DEVICE="${1:-wlan0}"
AP_CONN_NAME="xair-ap"
STATE_FILE="${XAIR_NETWORK_STATE_FILE:-/var/lib/xair-recorder/previous-connection}"

mkdir -p "$(dirname "$STATE_FILE")"

CURRENT=$(nmcli -t -f NAME,DEVICE connection show --active | awk -F: -v dev="$WIFI_DEVICE" '$2==dev {print $1}')
if [ -n "$CURRENT" ] && [ "$CURRENT" != "$AP_CONN_NAME" ]; then
  echo "$CURRENT" > "$STATE_FILE"
  nmcli connection down "$CURRENT" 2>/dev/null || true
fi

nmcli connection up "$AP_CONN_NAME"
echo "WiFi is now broadcasting the access point (was: ${CURRENT:-nothing active})."
