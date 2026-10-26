#!/usr/bin/env bash
# Keeps the JARVIS server up to date by itself: every 6 hours it pulls the v2 branch and, only if
# something changed, rebuilds and restarts the containers. Your data volume and .env are untouched.
#
#   Turn on (once):  sudo ./autoupdate.sh install
#   Update now:      sudo ./autoupdate.sh run
#   Turn off:        sudo ./autoupdate.sh remove
set -euo pipefail
DEPLOY="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$DEPLOY")"

run() {
  cd "$REPO"
  OWNER="$(stat -c %U "$REPO")"
  before="$(sudo -u "$OWNER" git rev-parse HEAD)"
  sudo -u "$OWNER" git fetch --quiet origin v2
  if [ "$before" = "$(sudo -u "$OWNER" git rev-parse origin/v2)" ] && [ "${1:-}" != "force" ]; then
    echo "JARVIS is up to date ($before)"; return 0
  fi
  sudo -u "$OWNER" git merge --ff-only --quiet origin/v2
  cd "$DEPLOY"
  docker compose up -d --build
  docker image prune -f >/dev/null
  echo "JARVIS updated to $(git -C "$REPO" rev-parse --short HEAD)"
}

case "${1:-run}" in
  install)
    cat > /etc/systemd/system/jarvis-update.service <<UNIT
[Unit]
Description=Update JARVIS from GitHub
After=network-online.target docker.service
[Service]
Type=oneshot
ExecStart=$DEPLOY/autoupdate.sh run
UNIT
    cat > /etc/systemd/system/jarvis-update.timer <<UNIT
[Unit]
Description=Update JARVIS every 6 hours
[Timer]
OnBootSec=10min
OnUnitActiveSec=6h
RandomizedDelaySec=15min
Persistent=true
[Install]
WantedBy=timers.target
UNIT
    systemctl daemon-reload && systemctl enable --now jarvis-update.timer
    echo "Auto-update is on. Updating now..."; run force ;;
  remove) systemctl disable --now jarvis-update.timer || true; rm -f /etc/systemd/system/jarvis-update.*; systemctl daemon-reload ;;
  run) run "${2:-}" ;;
  *) echo "usage: $0 install|run|remove"; exit 2 ;;
esac
