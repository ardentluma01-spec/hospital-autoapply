#!/usr/bin/env bash
# One-line installer for a fresh Ubuntu 24.04 server (e.g. a Vultr "Startup Script"). Run as root:
#   curl -fsSL https://raw.githubusercontent.com/ardentluma01-spec/hospital-autoapply/main/install.sh | SETUP_CODE=pick-a-code bash
# Progress log: /var/log/autoapply-install.log
set -euo pipefail
exec > >(tee -a /var/log/autoapply-install.log) 2>&1
: "${SETUP_CODE:?Please set SETUP_CODE=something in the install command}"
export DEBIAN_FRONTEND=noninteractive HOME="${HOME:-/root}"
REPO="${REPO:-https://github.com/ardentluma01-spec/hospital-autoapply.git}"

echo "==> $(date) Installing Hospital Auto-Apply"
apt-get update -y && apt-get install -y git curl
rm -rf /tmp/autoapply-src
git clone --depth 1 "$REPO" /tmp/autoapply-src
cd /tmp/autoapply-src
bash deploy/setup.sh

# the one-time setup page asks for this code before accepting your keys
if grep -q '^SETUP_CODE=' /opt/autoapply/.env; then
  sed -i "s#^SETUP_CODE=.*#SETUP_CODE=${SETUP_CODE}#" /opt/autoapply/.env
else
  echo "SETUP_CODE=${SETUP_CODE}" >> /opt/autoapply/.env
fi
systemctl restart autoapply
echo "==> $(date) DONE. Open: $(grep '^PUBLIC_URL=' /opt/autoapply/.env | cut -d= -f2)"
