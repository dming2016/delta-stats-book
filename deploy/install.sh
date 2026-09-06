#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR=/opt/delta-stats
DATA_DIR=/var/lib/delta-stats
ENV_FILE=/etc/delta-stats.env
INFO_FILE="$DATA_DIR/share-info.json"
NGINX_SITE=/etc/nginx/sites-enabled/random.conf
NGINX_SNIPPET=/etc/nginx/snippets/delta-stats.conf
BACKUP_DIR=/etc/nginx/backups

if ! id delta-stats >/dev/null 2>&1; then
  useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin delta-stats
fi

install -d -o root -g root -m 755 "$APP_DIR" "$APP_DIR/web" "$APP_DIR/web/assets"
install -d -o delta-stats -g delta-stats -m 751 "$DATA_DIR"
install -d -o delta-stats -g delta-stats -m 755 "$DATA_DIR/releases"
install -d -o root -g root -m 755 "$BACKUP_DIR"
install -m 644 "$SOURCE_DIR/shared_server.py" "$APP_DIR/shared_server.py"
install -m 644 "$SOURCE_DIR/web/index.html" "$APP_DIR/web/index.html"
install -m 644 "$SOURCE_DIR/web/download.html" "$APP_DIR/web/download.html"
find "$APP_DIR/web/assets" -mindepth 1 -maxdepth 1 -type f -delete
find "$SOURCE_DIR/web/assets" -mindepth 1 -maxdepth 1 -type f -exec install -m 644 -t "$APP_DIR/web/assets" {} +
if compgen -G "$SOURCE_DIR/artifacts/local-package/release/*" >/dev/null; then
  find "$SOURCE_DIR/artifacts/local-package/release" -mindepth 1 -maxdepth 1 -type f \
    -exec install -o delta-stats -g delta-stats -m 644 -t "$DATA_DIR/releases" {} +
fi
find "$DATA_DIR/releases" -mindepth 1 -maxdepth 1 -type f \
  -exec chown delta-stats:delta-stats {} +
find "$DATA_DIR/releases" -mindepth 1 -maxdepth 1 -type f \
  -exec chmod 644 {} +

if [[ ! -f "$INFO_FILE" ]]; then
  share_id="$(openssl rand -hex 12)"
  upload_token="$(openssl rand -hex 32)"
  printf 'DELTA_UPLOAD_TOKEN=%s\nDELTA_DATA_DIR=%s\n' "$upload_token" "$DATA_DIR" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  python3 - "$INFO_FILE" "$share_id" "$upload_token" <<'PY'
import json
import sys
from pathlib import Path

path, share_id, upload_token = sys.argv[1:]
Path(path).write_text(
    json.dumps(
        {
            "url": f"https://random.opendeep.top/delta/{share_id}",
            "upload_token": upload_token,
        },
        separators=(",", ":"),
    ),
    encoding="utf-8",
)
PY
  chown root:root "$INFO_FILE"
  chmod 600 "$INFO_FILE"
else
  share_id="$(python3 - "$INFO_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle)["url"].rstrip("/").rsplit("/", 1)[-1])
PY
)"
fi

install -m 644 "$SOURCE_DIR/deploy/delta-stats.service" /etc/systemd/system/delta-stats.service

cat > "$NGINX_SNIPPET" <<EOF
location = /delta/$share_id {
    return 302 /delta/$share_id/;
}

location /delta/$share_id/ {
    proxy_pass http://127.0.0.1:3012/;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_no_cache 1;
    proxy_cache_bypass 1;
    add_header Cache-Control "no-store" always;
}
EOF

backup="$BACKUP_DIR/random.conf.$(date +%Y%m%d%H%M%S)"
cp "$NGINX_SITE" "$backup"
rollback_nginx() {
  cp "$backup" "$NGINX_SITE"
  nginx -t >/dev/null 2>&1 && systemctl reload nginx || true
}
trap rollback_nginx ERR

if ! grep -q 'include /etc/nginx/snippets/delta-stats.conf;' "$NGINX_SITE"; then
  python3 - "$NGINX_SITE" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
marker = "    client_max_body_size 100m;\n"
if text.count(marker) != 1:
    raise SystemExit("nginx insertion marker is missing or ambiguous")
path.write_text(
    text.replace(marker, marker + "\n    include /etc/nginx/snippets/delta-stats.conf;\n"),
    encoding="utf-8",
)
PY
fi

systemctl daemon-reload
systemctl enable --now delta-stats.service
nginx -t
systemctl reload nginx
for attempt in {1..20}; do
  if curl --fail --silent http://127.0.0.1:3012/api/matches >/dev/null; then
    break
  fi
  if [[ "$attempt" == 20 ]]; then
    echo "delta-stats health check timed out" >&2
    exit 1
  fi
  sleep 0.25
done
systemctl is-active --quiet delta-stats.service
trap - ERR
echo "delta-stats deployed"
