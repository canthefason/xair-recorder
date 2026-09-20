#!/bin/bash
# Generates systemd/xair-recorder.service from the template using the
# current user and install location, then installs and enables it.
#
# Run this ON the target device, from inside the repo checkout, e.g.:
#   ./scripts/install-service.sh
#   ./scripts/install-service.sh /mnt/recordings   # custom recordings dir
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RECORDINGS_DIR="${1:-$HOME/recordings}"
SERVICE_USER="$(id -un)"
TEMPLATE="$INSTALL_DIR/systemd/xair-recorder.service.template"
OUT="/etc/systemd/system/xair-recorder.service"

sed \
  -e "s#__USER__#${SERVICE_USER}#g" \
  -e "s#__INSTALL_DIR__#${INSTALL_DIR}#g" \
  -e "s#__RECORDINGS_DIR__#${RECORDINGS_DIR}#g" \
  "$TEMPLATE" | sudo tee "$OUT" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable xair-recorder

echo "Installed $OUT for user '$SERVICE_USER', app dir '$INSTALL_DIR', recordings '$RECORDINGS_DIR'."
echo "Start it with: sudo systemctl start xair-recorder"
