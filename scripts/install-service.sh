#!/bin/bash
# Generates systemd/xair-recorder.service (and xair-network-restore.service)
# from their templates using the current user and install location, then
# installs and enables both.
#
# Run this ON the target device, from inside the repo checkout, e.g.:
#   ./scripts/install-service.sh
#   ./scripts/install-service.sh /mnt/recordings           # custom recordings dir
#   ./scripts/install-service.sh /mnt/recordings wlan1      # custom WiFi device too
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RECORDINGS_DIR="${1:-$HOME/recordings}"
WIFI_DEVICE="${2:-wlan0}"
SERVICE_USER="$(id -un)"

TEMPLATE="$INSTALL_DIR/systemd/xair-recorder.service.template"
OUT="/etc/systemd/system/xair-recorder.service"

sed \
  -e "s#__USER__#${SERVICE_USER}#g" \
  -e "s#__INSTALL_DIR__#${INSTALL_DIR}#g" \
  -e "s#__RECORDINGS_DIR__#${RECORDINGS_DIR}#g" \
  "$TEMPLATE" | sudo tee "$OUT" > /dev/null

# Only needed if the venue/home AP switching feature (network/*.sh) is in
# use - harmless to install unconditionally, since restore-mode.sh no-ops
# unless venue mode was actually last selected.
NET_TEMPLATE="$INSTALL_DIR/systemd/xair-network-restore.service.template"
NET_OUT="/etc/systemd/system/xair-network-restore.service"

sed \
  -e "s#__INSTALL_DIR__#${INSTALL_DIR}#g" \
  -e "s#__WIFI_DEVICE__#${WIFI_DEVICE}#g" \
  "$NET_TEMPLATE" | sudo tee "$NET_OUT" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable xair-recorder xair-network-restore

echo "Installed $OUT for user '$SERVICE_USER', app dir '$INSTALL_DIR', recordings '$RECORDINGS_DIR'."
echo "Installed $NET_OUT for WiFi device '$WIFI_DEVICE' - restores venue AP mode on boot if it was last selected."
echo "Start the recorder with: sudo systemctl start xair-recorder"
