#!/usr/bin/env bash
# Sets up the launcher's file server on Linux Mint. Run with sudo after /srv/wow-cdn is mounted.
set -euo pipefail

ROOT=/srv/wow-cdn
OWNER=${SUDO_USER:-$USER}
HERE=$(cd "$(dirname "$0")" && pwd)

if ! mountpoint -q "$ROOT"; then
    echo "$ROOT is not a mount point. Mount the data partition there first (see docs/whe-setup.md)." >&2
    exit 1
fi

apt-get install -y nginx
mkdir -p "$ROOT/client/live/.bundles" "$ROOT/addons" "$ROOT/launcher-updates"
chown -R "$OWNER:$OWNER" "$ROOT"

install -m 644 "$HERE/whe-cdn.nginx.conf" /etc/nginx/sites-available/whe-cdn
ln -sf /etc/nginx/sites-available/whe-cdn /etc/nginx/sites-enabled/whe-cdn

# nginx can start before tailscaled has brought up the address; this lets it bind anyway.
echo 'net.ipv4.ip_nonlocal_bind=1' > /etc/sysctl.d/90-whe-cdn.conf
sysctl -q -p /etc/sysctl.d/90-whe-cdn.conf

if command -v ufw >/dev/null && ufw status | grep -q 'Status: active'; then
    ufw allow in on tailscale0 to any port 7384 proto tcp
fi

nginx -t
systemctl enable --now nginx
systemctl reload nginx
echo "Done. Test from another tailnet machine: curl -I http://100.110.63.66:7384/client/live/manifest.json"
