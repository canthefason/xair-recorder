#!/bin/bash
# Adds a NetworkManager WiFi profile that turns a WiFi device into a
# standalone access point, WITHOUT touching or auto-connecting over
# whatever network it normally uses. Run once; afterwards use
# venue-mode.sh / home-mode.sh to switch between the two.
set -euo pipefail

AP_CONN_NAME="xair-ap"
AP_PASSPHRASE="${1:?usage: sudo ./setup-ap-profile.sh <wifi-passphrase> [ssid] [wifi-device]}"
AP_SSID="${2:-XAir-Recorder}"
WIFI_DEVICE="${3:-wlan0}"

if nmcli -t -f NAME connection show | grep -qx "$AP_CONN_NAME"; then
  echo "Profile '$AP_CONN_NAME' already exists, skipping creation."
else
  nmcli connection add type wifi ifname "$WIFI_DEVICE" con-name "$AP_CONN_NAME" autoconnect no ssid "$AP_SSID"
fi

nmcli connection modify "$AP_CONN_NAME" \
  802-11-wireless.mode ap \
  802-11-wireless.band bg \
  ipv4.method shared \
  ipv4.addresses 192.168.4.1/24 \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.psk "$AP_PASSPHRASE" \
  connection.autoconnect no

echo "AP profile '$AP_CONN_NAME' configured (SSID: $AP_SSID, device: $WIFI_DEVICE)."
echo "It will NOT auto-activate. Use venue-mode.sh to switch into AP mode,"
echo "and home-mode.sh to switch back to the regular network."
