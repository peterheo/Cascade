#!/usr/bin/env bash
set -euo pipefail

repo_dir="/opt/cascade"
uv_bin="/usr/local/bin/uv"

sudo -u cascade env HOME="$repo_dir" git -C "$repo_dir" pull --ff-only
sudo -u cascade env HOME="$repo_dir" UV_CACHE_DIR="$repo_dir/.uv-cache" UV_PYTHON_DOWNLOADS=never UV_PYTHON=/usr/bin/python3.12 "$uv_bin" --directory "$repo_dir" sync --locked --no-dev
sudo systemctl restart cascade.service

for _ in $(seq 1 20); do
    if curl --fail --silent --show-error --max-time 2 http://127.0.0.1:8500/health >/dev/null; then
        exit 0
    fi
    sleep 1
done

sudo journalctl -u cascade.service -n 30 --no-pager >&2
exit 1
