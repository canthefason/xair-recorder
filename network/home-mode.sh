#!/bin/bash
# Switches the WiFi device back from the access point to whatever network
# was active before venue-mode.sh ran. Falls back to NetworkManager's own
# autoconnect behavior if no saved network is found (e.g. it was never
# switched to venue mode, or the state file was cleared).
set -euo pipefail

AP_CONN_NAME="xair-ap"
STATE_FILE="${XAIR_NETWORK_STATE_FILE:-/var/lib/xair-recorder/previous-connection}"

nmcli connection down "$AP_CONN_NAME" 2>/dev/null || true

if [ -f "$STATE_FILE" ]; then
  PREVIOUS=$(cat "$STATE_FILE")
  nmcli connection up "$PREVIOUS"
  echo "WiFi has rejoined $PREVIOUS."
else
  echo "No saved previous network found; relying on NetworkManager autoconnect."
fi
