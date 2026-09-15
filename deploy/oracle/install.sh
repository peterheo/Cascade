#!/usr/bin/env bash
set -euo pipefail

repo_url="https://github.com/peterheo/Cascade.git"
repo_dir="/opt/cascade"
uv_bin="/usr/local/bin/uv"
unit_file="/etc/systemd/system/cascade.service"
caddy_file="/etc/caddy/Caddyfile"
caddy_source="$repo_dir/deploy/oracle/Caddyfile.cascade"

if ! id cascade >/dev/null 2>&1; then
    sudo useradd --system --user-group --home-dir "$repo_dir" --shell /sbin/nologin cascade
fi

if [ -e "$repo_dir" ] && [ ! -d "$repo_dir/.git" ]; then
    echo "$repo_dir exists but is not a git checkout" >&2
    exit 1
fi
if [ ! -d "$repo_dir/.git" ]; then
    sudo git clone "$repo_url" "$repo_dir"
fi
sudo chown -R cascade:cascade "$repo_dir"
sudo install -d -o cascade -g cascade "$repo_dir/.uv-cache"

if [ ! -x "$uv_bin" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh
    sudo restorecon "$uv_bin" /usr/local/bin/uvx || true
fi

if [ ! -f /etc/cascade/cascade.env ]; then
    sudo install -d -m 0755 -o root -g root /etc/cascade
    echo "Create /etc/cascade/cascade.env as root:root with mode 0600 and the NEBIUS_* variables from .env.example, then run this script again." >&2
    exit 1
fi

sudo -u cascade env HOME="$repo_dir" UV_CACHE_DIR="$repo_dir/.uv-cache" UV_PYTHON_DOWNLOADS=never UV_PYTHON=/usr/bin/python3.12 "$uv_bin" --directory "$repo_dir" sync --locked --no-dev
sudo install -D -m 0644 "$repo_dir/deploy/oracle/cascade.service" "$unit_file"
sudo systemctl daemon-reload
sudo systemctl enable --now cascade.service

if ! sudo grep -Fq 'cascade.150.136.6.100.nip.io {' "$caddy_file"; then
    backup="$caddy_file.$(date +%Y%m%d%H%M%S).bak"
    sudo cp -a "$caddy_file" "$backup"
    sudo cat "$caddy_source" | sudo tee -a "$caddy_file" >/dev/null
    if sudo caddy validate --config "$caddy_file"; then
        sudo systemctl reload caddy
    else
        sudo cp -a "$backup" "$caddy_file"
        echo "caddy validation failed; restored $caddy_file from $backup" >&2
        exit 1
    fi
fi

for _ in $(seq 1 20); do
    if curl --fail --silent --show-error --max-time 1 http://127.0.0.1:8500/health >/dev/null; then
        exit 0
    fi
    sleep 1
done
sudo journalctl -u cascade.service -n 30 --no-pager >&2
exit 1
