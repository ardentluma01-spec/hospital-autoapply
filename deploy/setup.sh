#!/usr/bin/env bash
# One-time server setup for Ubuntu 24.04 (Vultr / DigitalOcean / any VPS).
# Run as root from inside the project folder:   sudo bash deploy/setup.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_USER="autoapply"

echo "==> Installing system packages"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip debian-keyring debian-archive-keyring apt-transport-https curl gnupg ufw

echo "==> Creating app user and moving app to /opt/autoapply"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
mkdir -p /opt/autoapply
cp -r "$APP_DIR"/. /opt/autoapply/
[ -f /opt/autoapply/.env ] || cp /opt/autoapply/.env.example /opt/autoapply/.env
chown -R "$APP_USER":"$APP_USER" /opt/autoapply
chmod 600 /opt/autoapply/.env

echo "==> Python environment + browser"
sudo -u "$APP_USER" python3 -m venv /opt/autoapply/.venv
sudo -u "$APP_USER" /opt/autoapply/.venv/bin/pip install -q -r /opt/autoapply/requirements.txt
/opt/autoapply/.venv/bin/playwright install-deps chromium
sudo -u "$APP_USER" /opt/autoapply/.venv/bin/playwright install chromium

echo "==> Background service"
cp /opt/autoapply/deploy/autoapply.service /etc/systemd/system/autoapply.service
systemctl daemon-reload
systemctl enable --now autoapply

echo "==> HTTPS (Caddy) so your dashboard password is encrypted"
IP="$(curl -4 -s https://api.ipify.org)"
HOST="$(echo "$IP" | tr . -).sslip.io"
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y && apt-get install -y caddy
fi
cat > /etc/caddy/Caddyfile <<EOF
$HOST {
    reverse_proxy 127.0.0.1:8000
}
EOF
systemctl restart caddy

echo "==> Firewall"
ufw allow OpenSSH
ufw allow 80
ufw allow 443
ufw --force enable

sed -i "s#^PUBLIC_URL=.*#PUBLIC_URL=https://$HOST#" /opt/autoapply/.env

echo
echo "All done. Next:"
echo "  1. Open your dashboard and complete the one-time setup page"
echo "     (or edit /opt/autoapply/.env by hand and: systemctl restart autoapply)"
echo "  3. Open your dashboard:    https://$HOST"
echo "  Logs:                      sudo journalctl -u autoapply -f"
