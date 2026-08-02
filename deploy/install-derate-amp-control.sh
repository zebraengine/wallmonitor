#!/usr/bin/env bash
# Install (or remove) the derate amp-control daemon as a systemd timer, so
# wallmonitor's thermal forecast can automatically cap (and later restore)
# the Tesla's charge current through an esphome-tesla-ble device, without
# any cloud API involved.
#
# Usage (run with sudo, from anywhere):
#   sudo ./install-derate-amp-control.sh --tesla-ble http://<esp32-host>
#   sudo ./install-derate-amp-control.sh --tesla-ble http://<esp32-host> --dry-run
#   sudo ./install-derate-amp-control.sh --uninstall
#
# The ESP32 host/IP lands only in the local systemd unit — never commit it.
# The paired device should be configured with the least-privilege
# CHARGING_MANAGER role; this script has no opinion on that, it only talks
# to the device's own web_server REST API.

set -euo pipefail

SERVICE_NAME="derate-amp-control"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
TIMER_PATH="/etc/systemd/system/${SERVICE_NAME}.timer"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON="$(dirname "$SCRIPT_DIR")/contrib/derate_amp_control.py"

TESLA_BLE=""
WALLMONITOR="http://127.0.0.1:8480"
NORMAL_AMPS=""
LEAD_TIME_MIN=""
CONFIRM_TICKS=""
MIN_CAP_DELTA_A=""
STATE_FILE=""
INTERVAL="30"
DRY_RUN="0"
EXTRA_ARGS=""
RUN_USER="${SUDO_USER:-$(id -un)}"
UNINSTALL="0"

usage() { grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tesla-ble) TESLA_BLE="$2"; shift 2 ;;
    --wallmonitor) WALLMONITOR="$2"; shift 2 ;;
    --normal-amps) NORMAL_AMPS="$2"; shift 2 ;;
    --lead-time-min) LEAD_TIME_MIN="$2"; shift 2 ;;
    --confirm-ticks) CONFIRM_TICKS="$2"; shift 2 ;;
    --min-cap-delta-a) MIN_CAP_DELTA_A="$2"; shift 2 ;;
    --state-file) STATE_FILE="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --dry-run) DRY_RUN="1"; shift ;;
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

if [[ -z "$TESLA_BLE" ]]; then
  echo "error: --tesla-ble <http://esp32-host> is required" >&2
  exit 1
fi
if ! id -u "$RUN_USER" >/dev/null 2>&1 || [[ "$RUN_USER" == "root" ]]; then
  echo "error: --user must name an existing non-root user (got: ${RUN_USER})" >&2
  exit 1
fi

DAEMON_ARGS="--tesla-ble ${TESLA_BLE} --wallmonitor ${WALLMONITOR}"
[[ -n "$NORMAL_AMPS" ]] && DAEMON_ARGS+=" --normal-amps ${NORMAL_AMPS}"
[[ -n "$LEAD_TIME_MIN" ]] && DAEMON_ARGS+=" --lead-time-min ${LEAD_TIME_MIN}"
[[ -n "$CONFIRM_TICKS" ]] && DAEMON_ARGS+=" --confirm-ticks ${CONFIRM_TICKS}"
[[ -n "$MIN_CAP_DELTA_A" ]] && DAEMON_ARGS+=" --min-cap-delta-a ${MIN_CAP_DELTA_A}"
[[ -n "$STATE_FILE" ]] && DAEMON_ARGS+=" --state-file ${STATE_FILE}"
[[ "$DRY_RUN" == "1" ]] && DAEMON_ARGS+=" --dry-run"
[[ -n "$EXTRA_ARGS" ]] && DAEMON_ARGS+=" ${EXTRA_ARGS}"

{
  echo "[Unit]"
  echo "Description=Derate amp-control (wallmonitor forecast -> Tesla charge current cap)"
  echo ""
  echo "[Service]"
  echo "Type=oneshot"
  echo "User=${RUN_USER}"
  echo "ExecStart=/usr/bin/env python3 ${DAEMON} ${DAEMON_ARGS}"
} > "$UNIT_PATH"

{
  echo "[Unit]"
  echo "Description=Run derate amp-control every ${INTERVAL}s"
  echo ""
  echo "[Timer]"
  echo "OnBootSec=30"
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
