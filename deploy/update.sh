#!/usr/bin/env bash
# Update the app to the latest version (keeps your settings, logins and data).
# From the Vultr console, as root:
#   curl -fsSL https://raw.githubusercontent.com/ardentluma01-spec/hospital-autoapply/main/deploy/update.sh | bash
set -euo pipefail
T=$(mktemp -d)
curl -fsSL https://codeload.github.com/ardentluma01-spec/hospital-autoapply/tar.gz/refs/heads/main | tar xz -C "$T"
chown -R autoapply:autoapply "$T"
sudo -u autoapply /opt/autoapply/.venv/bin/python "$T"/*/app/updater.py "$T" /opt/autoapply
systemctl restart autoapply
rm -rf "$T"
echo "DONE - refresh your dashboard"
