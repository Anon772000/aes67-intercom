#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME=${SERVICE_NAME:-aes67-intercom}
FRONTEND_DEV=${FRONTEND_DEV:-no}   # yes|no
FRONTEND_SERVICE_NAME=${FRONTEND_SERVICE_NAME:-aes67-frontend}
REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
BACKEND_DIR="$REPO_DIR/backend"
FRONTEND_DIR="$REPO_DIR/frontend"

SERVICE_USER=${SERVICE_USER:-${SUDO_USER:-$(logname 2>/dev/null || echo pi)}}
BUILD_FRONTEND=${BUILD_FRONTEND:-auto} # auto|yes|no

usage() {
  cat <<EOF
Install AES67 Intercom backend as a systemd service (Gunicorn) on Debian/Raspberry Pi.

Environment overrides:
  SERVICE_NAME     Service name (default: aes67-intercom)
  SERVICE_USER     User to run service (default: current sudo user or 'pi')
  BUILD_FRONTEND   auto|yes|no (default: auto — builds if npm present)

CLI options:
  -n, --name NAME  Set service name (same as SERVICE_NAME)
  -h, --help             Show this help
  --frontend-dev         Run CRA dev server (npm start) under systemd
  --frontend-name NAME   Set frontend service name (default: aes67-frontend)

Examples:
  sudo SERVICE_USER=harrison BUILD_FRONTEND=no deploy/install.sh
  sudo ./deploy/install.sh
  sudo ./deploy/install.sh --name intercom-prod
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage; exit 0;;
    -n|--name)
      SERVICE_NAME="${2:-}"
      if [[ -z "$SERVICE_NAME" ]]; then echo "Missing value for --name" >&2; exit 1; fi
      shift 2;;
    --frontend-dev)
      FRONTEND_DEV=yes; shift 1;;
    --frontend-name)
      FRONTEND_SERVICE_NAME="${2:-}"
      if [[ -z "$FRONTEND_SERVICE_NAME" ]]; then echo "Missing value for --frontend-name" >&2; exit 1; fi
      shift 2;;
    *)
      echo "Unknown argument: $1" >&2; usage; exit 1;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root (via sudo)." >&2
  exit 1
fi

echo "==> Repo dir:        $REPO_DIR"
echo "==> Backend dir:     $BACKEND_DIR"
echo "==> Service user:    $SERVICE_USER"
echo "==> Service name:    $SERVICE_NAME"

echo "==> Installing OS packages (GStreamer, ALSA, Python GI)"
apt-get update -y
apt-get install -y \
  python3 python3-venv python3-pip \
  python3-gi gir1.2-gstreamer-1.0 \
  gstreamer1.0-tools gstreamer1.0-alsa \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
  alsa-utils

if [[ "$FRONTEND_DEV" == "yes" || "$SHOULD_BUILD" == "yes" ]]; then
  echo "==> Ensuring Node.js + npm present"
  if ! command -v npm >/dev/null 2>&1; then
    apt-get install -y nodejs npm || true
  fi
fi

echo "==> Ensuring user '$SERVICE_USER' is in 'audio' group"
usermod -aG audio "$SERVICE_USER" || true

echo "==> Creating Python venv with system site packages (for GI)"
python3 -m venv --system-site-packages "$BACKEND_DIR/venv"
"$BACKEND_DIR/venv/bin/pip" install -U pip wheel
"$BACKEND_DIR/venv/bin/pip" install -r "$BACKEND_DIR/requirements.txt"

# Optional: build frontend (if desired and npm present)
SHOULD_BUILD=no
if [[ "$BUILD_FRONTEND" == "yes" ]]; then
  SHOULD_BUILD=yes
elif [[ "$BUILD_FRONTEND" == "auto" ]]; then
  if command -v npm >/dev/null 2>&1; then SHOULD_BUILD=yes; fi
fi

if [[ "$SHOULD_BUILD" == "yes" ]]; then
  echo "==> Building frontend (this can take a while on a Pi)"
  pushd "$FRONTEND_DIR" >/dev/null
  # Prefer clean, reproducible installs if lockfile exists
  if [[ -f package-lock.json ]]; then npm ci; else npm install; fi
  npm run build
  popd >/dev/null
else
  echo "==> Skipping frontend build (BUILD_FRONTEND=$BUILD_FRONTEND)"
fi

echo "==> Installing systemd service"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
cat > "$UNIT_PATH" <<UNIT
[Unit]
Description=AES67 Intercom backend (Gunicorn + Flask + GStreamer)
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=audio
WorkingDirectory=${BACKEND_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=/usr/lib/python3/dist-packages
Environment=PATH=${BACKEND_DIR}/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=${BACKEND_DIR}/venv/bin/gunicorn -b 0.0.0.0:8080 --workers 1 --threads 2 --timeout 120 server:app
Restart=always
RestartSec=2
KillMode=control-group
TimeoutStopSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

if [[ "$FRONTEND_DEV" == "yes" ]]; then
  echo "==> Installing frontend dev service ($FRONTEND_SERVICE_NAME)"
  FE_UNIT_PATH="/etc/systemd/system/${FRONTEND_SERVICE_NAME}.service"
  cat > "$FE_UNIT_PATH" <<FEUNIT
[Unit]
Description=AES67 Intercom frontend (CRA dev server)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${FRONTEND_DIR}
Environment=HOST=0.0.0.0
Environment=PORT=3000
Environment=CHOKIDAR_USEPOLLING=1
Environment=BROWSER=none
ExecStart=/usr/bin/env npm start
Restart=always
RestartSec=2
KillMode=control-group
TimeoutStopSec=5

[Install]
WantedBy=multi-user.target
FEUNIT
  systemctl daemon-reload
  systemctl enable --now "$FRONTEND_SERVICE_NAME"
  echo "==> Frontend dev server listening on :3000"
fi

echo "==> Done. Logs: journalctl -u ${SERVICE_NAME} -f"
echo "==> If you just added user to 'audio', a reboot or re-login may be required."
echo "==> If using the IQaudIO CODEC, run 'alsamixer -c 0' to select input and enable Mic Bias, then 'sudo alsactl store'."
