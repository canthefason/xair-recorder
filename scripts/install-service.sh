#!/bin/bash
# Generates systemd/xair-recorder.service, xair-network-restore.service, and
# xair-network-watchdog.service from their templates using the current user
# and install location, then installs and enables all three.
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

# Both of the following are only needed if the venue/home AP switching
# feature (network/*.sh) is in use - harmless to install unconditionally,
# since both no-op (or just log a retryable failure) if the xair-ap profile
# was never set up via setup-ap-profile.sh.
RESTORE_TEMPLATE="$INSTALL_DIR/systemd/xair-network-restore.service.template"
RESTORE_OUT="/etc/systemd/system/xair-network-restore.service"

sed \
  -e "s#__INSTALL_DIR__#${INSTALL_DIR}#g" \
  -e "s#__WIFI_DEVICE__#${WIFI_DEVICE}#g" \
  "$RESTORE_TEMPLATE" | sudo tee "$RESTORE_OUT" > /dev/null

WATCHDOG_TEMPLATE="$INSTALL_DIR/systemd/xair-network-watchdog.service.template"
WATCHDOG_OUT="/etc/systemd/system/xair-network-watchdog.service"

sed \
  -e "s#__INSTALL_DIR__#${INSTALL_DIR}#g" \
  -e "s#__WIFI_DEVICE__#${WIFI_DEVICE}#g" \
  "$WATCHDOG_TEMPLATE" | sudo tee "$WATCHDOG_OUT" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable xair-recorder xair-network-restore
sudo systemctl enable --now xair-network-watchdog

echo "Installed $OUT for user '$SERVICE_USER', app dir '$INSTALL_DIR', recordings '$RECORDINGS_DIR'."
echo "Installed $RESTORE_OUT - restores venue AP mode on boot if it was last selected."
echo "Installed and started $WATCHDOG_OUT - auto-switches to venue AP after the WiFi device ($WIFI_DEVICE) has had no network for ~60s."
echo "Start the recorder with: sudo systemctl start xair-recorder"
