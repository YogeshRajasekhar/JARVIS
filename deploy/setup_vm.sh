#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 22.04/24.04 VM (Oracle Cloud Always Free or Google Cloud e2-micro).
# Usage (on the VM):  git clone https://github.com/YogeshRajasekhar/JARVIS.git && cd JARVIS/deploy && sudo ./setup_vm.sh
set -euo pipefail

echo "== Security updates on autopilot"
apt-get update -y && apt-get install -y unattended-upgrades ufw fail2ban ca-certificates curl
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "== Firewall: deny everything inbound except SSH (JARVIS is reached through Tailscale, not open ports)"
ufw default deny incoming && ufw default allow outgoing && ufw allow OpenSSH && ufw --force enable

echo "== SSH: keys only"
sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh || systemctl restart sshd

echo "== Docker"
if ! command -v docker >/dev/null; then curl -fsSL https://get.docker.com | sh; fi

if [ ! -f .env ]; then
  cp ../.env.example .env
  python3 - <<'PY'
import secrets, base64, os, re
p = ".env"; s = open(p).read()
s = re.sub(r"^JARVIS_DEVICE_TOKEN=.*$", "JARVIS_DEVICE_TOKEN=" + secrets.token_urlsafe(24), s, flags=re.M)
s = re.sub(r"^JARVIS_SEED_DEMO=.*$", "JARVIS_SEED_DEMO=0", s, flags=re.M)
s = re.sub(r"^JARVIS_DB_PATH=.*$", "JARVIS_DB_PATH=/data/jarvis.db", s, flags=re.M)  # the Docker volume
s = re.sub(r"^JARVIS_DATA_KEY=.*$", "JARVIS_DATA_KEY=" + base64.urlsafe_b64encode(os.urandom(32)).decode(), s, flags=re.M)
open(p, "w").write(s)
PY
  chmod 600 .env
  chown "${SUDO_USER:-ubuntu}": .env   # editable by you without sudo, unreadable by others
  echo "Created deploy/.env with a fresh device token and data key. Add your GROQ_API_KEY / GEMINI_API_KEY now."
fi
echo "Next: TS_AUTHKEY=tskey-auth-... docker compose up -d --build"
