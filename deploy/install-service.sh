#!/usr/bin/env bash
# Install (or remove) wallmonitor as a systemd service on Ubuntu/Debian-style
# Linux, so monitoring survives reboots and unattended OS updates.
#
# Usage (from anywhere; run with sudo):
#   sudo ./install-service.sh --host 192.168.1.50 --split-phase
#   sudo ./install-service.sh --host 192.168.1.50 --bind 0.0.0.0 --port 8480
#   sudo ./install-service.sh --uninstall
#
# Optional daily backup (nothing is scheduled unless you ask): a verified,
# compressed snapshot into a directory of your choosing — local, a NAS
# mount, or a folder a sync client watches — every night at 03:30:
#   sudo ./install-service.sh --host 192.168.1.50 --backup-dir /srv/backups/wallmonitor
#
# More than one Wall Connector: give each its own --name. That yields a
# separate unit (wallmonitor-<name>), a separate database (<name>.db) and
# the label shown in that instance's UI and notifications; link the
# instances to each other with --peer so the header can switch between them:
#   sudo ./install-service.sh --name left  --host 192.168.1.50 --port 8480 --peer "right=http://192.168.1.10:8481"
#   sudo ./install-service.sh --name right --host 192.168.1.51 --port 8481 --peer "left=http://192.168.1.10:8480"
#   sudo ./install-service.sh --name right --uninstall
#
# The service runs as the invoking (non-root) user, restarts automatically on
# failure, and starts on boot once the network is up. The database lands in
# the monitor directory (wallmonitor.db) unless --db points elsewhere.

set -euo pipefail

SERVICE_NAME="wallmonitor"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MONITOR_DIR="$(dirname "$SCRIPT_DIR")"

HOST=""
PORT="8480"
BIND="0.0.0.0"
DB_PATH=""
SPLIT_PHASE="0"
NOTIFY_URL=""
NOTIFY_FORMAT=""
DEMO="0"
RUN_USER="${SUDO_USER:-$(id -un)}"
UNINSTALL="0"
NAME=""
LABEL=""
PEERS=()
BACKUP_DIR=""

usage() { grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --bind) BIND="$2"; shift 2 ;;
    --db) DB_PATH="$2"; shift 2 ;;
    --split-phase) SPLIT_PHASE="1"; shift ;;
    --notify-url) NOTIFY_URL="$2"; shift 2 ;;
    --notify-format) NOTIFY_FORMAT="$2"; shift 2 ;;
    --demo) DEMO="1"; shift ;;
    --user) RUN_USER="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --peer) PEERS+=("$2"); shift 2 ;;
    --backup-dir) BACKUP_DIR="$2"; shift 2 ;;
    --uninstall) UNINSTALL="1"; shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage 1 ;;
  esac
done

if [[ -n "$NAME" ]]; then
  if [[ ! "$NAME" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "error: --name may contain only letters, digits, '-' and '_' (got: ${NAME})" >&2
    exit 1
  fi
  SERVICE_NAME="wallmonitor-${NAME}"
  [[ -z "$LABEL" ]] && LABEL="$NAME"
  [[ -z "$DB_PATH" ]] && DB_PATH="${MONITOR_DIR}/${NAME}.db"
fi
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ "$(uname -s)" != "Linux" ]] || ! command -v systemctl >/dev/null 2>&1; then
  echo "error: this installer targets Linux with systemd (for macOS, ask for a launchd plist)" >&2
  exit 1
fi
if [[ "$(id -u)" -ne 0 ]]; then
  echo "error: run with sudo (writes ${UNIT_PATH})" >&2
  exit 1
fi

if [[ "$UNINSTALL" == "1" ]]; then
  systemctl disable --now "${SERVICE_NAME}-backup.timer" 2>/dev/null || true
  rm -f "/etc/systemd/system/${SERVICE_NAME}-backup.timer" "/etc/systemd/system/${SERVICE_NAME}-backup.service"
  systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
  rm -f "$UNIT_PATH"
  systemctl daemon-reload
  echo "removed ${SERVICE_NAME} service (database and code left untouched)"
  exit 0
fi

if [[ -z "$HOST" && "$DEMO" != "1" ]]; then
  echo "error: --host <wall-connector-ip> is required (or --demo)" >&2
  exit 1
fi
if ! id -u "$RUN_USER" >/dev/null 2>&1 || [[ "$RUN_USER" == "root" ]]; then
  echo "error: --user must name an existing non-root user (got: ${RUN_USER})" >&2
  exit 1
fi

# uv is typically installed per-user (~/.local/bin), so resolve it as the run
# user rather than as root.
UV_BIN="$(sudo -u "$RUN_USER" bash -lc 'command -v uv' || true)"
if [[ -z "$UV_BIN" ]]; then
  echo "error: uv not found for user ${RUN_USER}." >&2
  echo "  install it first:  curl -LsSf https://astral.sh/uv/install.sh | sudo -u ${RUN_USER} sh" >&2
  exit 1
fi

echo "syncing dependencies in ${MONITOR_DIR} as ${RUN_USER}..."
sudo -u "$RUN_USER" "$UV_BIN" sync --project "$MONITOR_DIR"

# Ports below 1024 need the bind capability; grant it only when required.
CAP_LINE=""
if [[ "$PORT" -lt 1024 ]]; then
  CAP_LINE="AmbientCapabilities=CAP_NET_BIND_SERVICE"
fi

{
  echo "[Unit]"
  echo "Description=Tesla Wall Connector monitor (${SERVICE_NAME})"
  echo "Wants=network-online.target"
  echo "After=network-online.target"
  echo ""
  echo "[Service]"
  echo "Type=simple"
  echo "User=${RUN_USER}"
  echo "WorkingDirectory=${MONITOR_DIR}"
  [[ -n "$HOST" ]] && echo "Environment=WM_WC_HOST=${HOST}"
  echo "Environment=WM_PORT=${PORT}"
  echo "Environment=WM_BIND=${BIND}"
  [[ -n "$DB_PATH" ]] && echo "Environment=WM_DB=${DB_PATH}"
  [[ "$SPLIT_PHASE" == "1" ]] && echo "Environment=WM_SPLIT_PHASE=1"
  [[ -n "$NOTIFY_URL" ]] && echo "Environment=WM_NOTIFY_URL=${NOTIFY_URL}"
  [[ -n "$NOTIFY_FORMAT" ]] && echo "Environment=WM_NOTIFY_FORMAT=${NOTIFY_FORMAT}"
  [[ "$DEMO" == "1" ]] && echo "Environment=WM_DEMO=1"
  [[ -n "$LABEL" ]] && echo "Environment=WM_LABEL=${LABEL}"
  if [[ ${#PEERS[@]} -gt 0 ]]; then
    echo "Environment=WM_PEERS=$(IFS=,; echo "${PEERS[*]}")"
  fi
  [[ -n "$CAP_LINE" ]] && echo "$CAP_LINE"
  echo "ExecStart=${UV_BIN} run --project ${MONITOR_DIR} python -m wallmonitor"
  echo "Restart=always"
  echo "RestartSec=5"
  echo ""
  echo "[Install]"
  echo "WantedBy=multi-user.target"
} > "$UNIT_PATH"

# Opt-in daily backup: nothing is scheduled unless --backup-dir is given.
BACKUP_UNIT="${SERVICE_NAME}-backup"
if [[ -n "$BACKUP_DIR" ]]; then
  ENV_LINES=""
  [[ -n "$DB_PATH" ]] && ENV_LINES="Environment=WM_DB=${DB_PATH}"
  cat > "/etc/systemd/system/${BACKUP_UNIT}.service" <<EOF_UNIT
[Unit]
Description=Daily database backup (${SERVICE_NAME})

[Service]
Type=oneshot
User=${RUN_USER}
WorkingDirectory=${MONITOR_DIR}
${ENV_LINES}
ExecStart=${UV_BIN} run --project ${MONITOR_DIR} python -m wallmonitor --backup ${BACKUP_DIR}
EOF_UNIT
  cat > "/etc/systemd/system/${BACKUP_UNIT}.timer" <<EOF_TIMER
[Unit]
Description=Run the ${SERVICE_NAME} backup daily

[Timer]
OnCalendar=*-*-* 03:30:00
RandomizedDelaySec=15m
Persistent=true

[Install]
WantedBy=timers.target
EOF_TIMER
fi

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
if [[ -n "$BACKUP_DIR" ]]; then
  systemctl enable --now "${BACKUP_UNIT}.timer"
  echo "daily backup timer enabled -> ${BACKUP_DIR} (systemctl list-timers ${BACKUP_UNIT}.timer)"
fi

echo ""
echo "installed and started. Useful commands:"
echo "  systemctl status ${SERVICE_NAME}      # is it running"
echo "  journalctl -u ${SERVICE_NAME} -f      # follow logs"
echo "  sudo $0 ${NAME:+--name ${NAME} }--uninstall   # remove the service"
if [[ "$DEMO" == "1" ]]; then
  echo "UI: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${PORT} (demo mode)"
else
  echo "UI: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${PORT} (watching ${HOST})"
fi
