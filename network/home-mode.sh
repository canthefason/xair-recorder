#!/bin/bash
# Switches the WiFi device back from the access point to whatever network
# was active before venue-mode.sh ran. Falls back to NetworkManager's own
# autoconnect behavior if no saved network is found (e.g. it was never
# switched to venue mode, or the state file was cleared).
set -euo pipefail

AP_CONN_NAME="xair-ap"
STATE_FILE="${XAIR_NETWORK_STATE_FILE:-/var/lib/xair-recorder/previous-connection}"
MODE_FILE="${XAIR_NETWORK_MODE_FILE:-/var/lib/xair-recorder/network-mode}"

nmcli connection down "$AP_CONN_NAME" 2>/dev/null || true

# Record that we're deliberately back in "home" mode, so a reboot's
# restore-mode.sh (see venue-mode.sh for why that exists) doesn't re-activate
# the AP - NetworkManager's own autoconnect handles getting back onto the
# saved network from here, which is what mode=home means.
echo "home" > "$MODE_FILE"

if [ -f "$STATE_FILE" ]; then
  PREVIOUS=$(cat "$STATE_FILE")
  nmcli connection up "$PREVIOUS"
  echo "WiFi has rejoined $PREVIOUS."
else
  echo "No saved previous network found; relying on NetworkManager autoconnect."
fi
