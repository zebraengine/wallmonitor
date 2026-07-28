#!/usr/bin/env bash
# Install (or remove) the TeslaMate ambient bridge as a systemd timer, so a
# vehicle parked in the garage feeds wallmonitor's /api/ambient (source
# "car") every few minutes without touching the TeslaMate stack.
#
# Usage (run with sudo, from anywhere):
#   sudo ./install-teslamate-bridge.sh --car-id 1 --geofence Home
#   sudo ./install-teslamate-bridge.sh --car-id 1 --home-lat 39.2 --home-lon -77.3
#   sudo ./install-teslamate-bridge.sh --car-id 1              # plugged-in gate only
#   sudo ./install-teslamate-bridge.sh --uninstall
#
# Home coordinates land only in the local systemd unit — never commit them.
# The run user must be able to run `docker exec` (i.e. be in the docker
# group). Without --geofence/--home-lat the bridge only posts while a
# vehicle is plugged into the Wall Connector.

set -euo pipefail

SERVICE_NAME="teslamate-ambient-bridge"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
TIMER_PATH="/etc/systemd/system/${SERVICE_NAME}.timer"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE="$(dirname "$SCRIPT_DIR")/contrib/teslamate_ambient_bridge.py"

CAR_ID=""
GEOFENCE=""
HOME_LAT=""
HOME_LON=""
RADIUS=""
WALLMONITOR="http://127.0.0.1:8480"
INTERVAL="300"
EXTRA_ARGS=""
RUN_USER="${SUDO_USER:-$(id -un)}"
UNINSTALL="0"

usage() { grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --car-id) CAR_ID="$2"; shift 2 ;;
    --geofence) GEOFENCE="$2"; shift 2 ;;
    --home-lat) HOME_LAT="$2"; shift 2 ;;
    --home-lon) HOME_LON="$2"; shift 2 ;;
    --home-radius-m) RADIUS="$2"; shift 2 ;;
    --wallmonitor) WALLMONITOR="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --extra-args) EXTRA_ARGS="$2"; shift 2 ;;
    --user) RUN_USER="$2"; shift 2 ;;
    --uninstall) UNINSTALL="1"; shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage 1 ;;
  esac
done

if [[ "$(uname -s)" != "Linux" ]] || ! command -v systemctl >/dev/null 2>&1; then
  echo "error: this installer targets Linux with systemd" >&2
  exit 1
fi
if [[ "$(id -u)" -ne 0 ]]; then
  echo "error: run with sudo (writes ${UNIT_PATH})" >&2
  exit 1
fi

if [[ "$UNINSTALL" == "1" ]]; then
  systemctl disable --now "${SERVICE_NAME}.timer" 2>/dev/null || true
  rm -f "$UNIT_PATH" "$TIMER_PATH"
  systemctl daemon-reload
  echo "removed ${SERVICE_NAME} timer and service"
  exit 0
fi

if [[ -z "$CAR_ID" ]]; then
  echo "error: --car-id <TeslaMate car id> is required" >&2
  exit 1
fi
if ! id -u "$RUN_USER" >/dev/null 2>&1 || [[ "$RUN_USER" == "root" ]]; then
  echo "error: --user must name an existing non-root user (got: ${RUN_USER})" >&2
  exit 1
fi

BRIDGE_ARGS="--car-id ${CAR_ID} --wallmonitor ${WALLMONITOR}"
[[ -n "$GEOFENCE" ]] && BRIDGE_ARGS+=" --geofence \"${GEOFENCE}\""
[[ -n "$HOME_LAT" ]] && BRIDGE_ARGS+=" --home-lat ${HOME_LAT} --home-lon ${HOME_LON}"
[[ -n "$RADIUS" ]] && BRIDGE_ARGS+=" --home-radius-m ${RADIUS}"
[[ -n "$EXTRA_ARGS" ]] && BRIDGE_ARGS+=" ${EXTRA_ARGS}"

{
  echo "[Unit]"
  echo "Description=TeslaMate ambient bridge (car -> wallmonitor /api/ambient)"
  echo "After=docker.service"
  echo ""
  echo "[Service]"
  echo "Type=oneshot"
  echo "User=${RUN_USER}"
  echo "ExecStart=/usr/bin/env python3 ${BRIDGE} ${BRIDGE_ARGS}"
} > "$UNIT_PATH"

{
  echo "[Unit]"
  echo "Description=Run the TeslaMate ambient bridge every ${INTERVAL}s"
  echo ""
  echo "[Timer]"
  echo "OnBootSec=90"
  echo "OnUnitActiveSec=${INTERVAL}"
  echo ""
  echo "[Install]"
  echo "WantedBy=timers.target"
} > "$TIMER_PATH"

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.timer"

echo ""
echo "installed. Useful commands:"
echo "  systemctl list-timers ${SERVICE_NAME}.timer   # next run"
echo "  journalctl -u ${SERVICE_NAME} -n 20           # recent decisions"
echo "  sudo $0 --uninstall                           # remove"
