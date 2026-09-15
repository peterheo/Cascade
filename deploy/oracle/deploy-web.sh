#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cascade_host="${CASCADE_HOST:-opc@150.136.6.100}"
ssh_opts=()
if [ -n "${SSH_OPTS:-}" ]; then
    read -r -a ssh_opts <<<"$SSH_OPTS"
fi

remote() {
    ssh "${ssh_opts[@]}" "$cascade_host" "$@"
}

(
    cd "$repo_dir/apps/web"
    npm ci
    npm run build:export
)

client_dir="$repo_dir/apps/web/dist/client"
test -f "$client_dir/index.html"
if grep -rIl '/Users/' "$client_dir" >/dev/null; then
    echo "export contains an absolute /Users/ path" >&2
    exit 1
fi

tar -C "$client_dir" -czf - . | remote 'sudo rm -rf /opt/cascade-web.new && sudo install -d -m 0755 -o root -g root /opt/cascade-web.new && sudo tar -xzf - -C /opt/cascade-web.new && sudo chown -R root:root /opt/cascade-web.new && sudo find /opt/cascade-web.new -type d -exec chmod 0755 {} + && sudo find /opt/cascade-web.new -type f -exec chmod 0644 {} + && sudo rm -rf /opt/cascade-web.old && if [ -d /opt/cascade-web ]; then sudo mv /opt/cascade-web /opt/cascade-web.old; fi && sudo mv /opt/cascade-web.new /opt/cascade-web'

caddy_file="/etc/caddy/Caddyfile"
if ! remote "sudo grep -Fq 'app.cascade.150.136.6.100.nip.io {' '$caddy_file'"; then
    backup="$caddy_file.$(date +%Y%m%d%H%M%S).bak"
    remote "sudo cp -a '$caddy_file' '$backup'"
    if ! cat "$repo_dir/deploy/oracle/Caddyfile.web" | remote "sudo tee -a '$caddy_file' >/dev/null"; then
        remote "sudo cp -a '$backup' '$caddy_file'"
        exit 1
    fi
    if remote "sudo caddy validate --config '$caddy_file'"; then
        remote "sudo systemctl reload caddy"
    else
        remote "sudo cp -a '$backup' '$caddy_file'"
        echo "caddy validation failed; restored $caddy_file from $backup" >&2
        exit 1
    fi
fi

workspace_url="https://app.cascade.150.136.6.100.nip.io"
curl --fail --silent --show-error --max-time 15 "$workspace_url/" >/dev/null
curl --fail --silent --show-error --max-time 15 "$workspace_url/cascade-api/health" >/dev/null
