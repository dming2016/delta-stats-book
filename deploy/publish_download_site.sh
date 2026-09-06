#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="${1:?publish source directory is required}"
RELEASE_SOURCE="$SOURCE_DIR/release"
APP_DIR=/opt/delta-stats
DATA_DIR=/var/lib/delta-stats
BACKUP_DIR="$APP_DIR/backups"
NGINX_AVAILABLE=/etc/nginx/sites-available/zhou.conf
NGINX_ENABLED=/etc/nginx/sites-enabled/zhou.conf
SERVICE=delta-stats.service
PUBLISH_LOCK_FILE=/run/lock/delta-stats-publish.lock

fail() {
  echo "$1" >&2
  return 1
}

acquire_publish_lock() {
  if ! exec {PUBLISH_LOCK_FD}>"$PUBLISH_LOCK_FILE"; then
    fail "unable to open production publish lock: $PUBLISH_LOCK_FILE"
  fi
  if ! flock -n "$PUBLISH_LOCK_FD"; then
    exec {PUBLISH_LOCK_FD}>&-
    fail "another delta-stats production publish is already running"
  fi
}

transaction_rollback_started=0

rollback_on_failure() {
  local status="$1"
  if (( transaction_rollback_started )); then
    return
  fi
  transaction_rollback_started=1
  trap - ERR
  trap '' INT TERM HUP
  set +e
  rollback
  exit "$status"
}

arm_transaction_traps() {
  trap 'rollback_on_failure "$?"' ERR
  trap 'rollback_on_failure 130' INT
  trap 'rollback_on_failure 143' TERM
  trap 'rollback_on_failure 129' HUP
}

clear_transaction_traps() {
  trap - ERR INT TERM HUP
}

ensure_release_permissions() {
  install -d -o delta-stats -g delta-stats -m 751 "$DATA_DIR"
  install -d -o delta-stats -g delta-stats -m 755 "$DATA_DIR/releases"
  find "$DATA_DIR/releases" -mindepth 1 -maxdepth 1 -type f \
    -exec chown delta-stats:delta-stats {} +
  find "$DATA_DIR/releases" -mindepth 1 -maxdepth 1 -type f \
    -exec chmod 644 {} +
}

validate_release() {
  python3 - "$1" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
try:
    manifest = json.loads((root / "version.json").read_text(encoding="utf-8"))
except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"invalid release manifest: {exc}") from exc

if not isinstance(manifest, dict):
    raise SystemExit("release manifest must be an object")
version_pattern = re.compile(r"[0-9]+(?:\.[0-9]+)+(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?")
version = manifest.get("version")
if not isinstance(version, str) or not version_pattern.fullmatch(version):
    raise SystemExit("invalid release version")
launcher_version = manifest.get("launcher_version")
if not isinstance(launcher_version, str) or not version_pattern.fullmatch(launcher_version):
    raise SystemExit("invalid launcher version")
if manifest.get("launcher_protocol") != 2:
    raise SystemExit("invalid launcher protocol")
if manifest.get("package_format") != "pyinstaller-onedir-zip-v1":
    raise SystemExit("invalid package format")

entries = (
    ("file", "file_size", "file_sha256", f"DeltaStatsApp-{version}.exe"),
    (
        "launcher_file",
        "launcher_size",
        "launcher_sha256",
        f"DeltaStatsLauncher-{launcher_version}.exe",
    ),
    (
        "package_file",
        "package_size",
        "package_sha256",
        f"DeltaStatsAppDir-{version}.zip",
    ),
    (
        "download_file",
        "download_size",
        "download_sha256",
        f"DeltaStatsAssistant-{version}.zip",
    ),
    (
        "installer_file",
        "installer_size",
        "installer_sha256",
        f"DeltaStatsAssistant-{version}-Setup.exe",
    ),
)
filenames = []
for field, size_field, sha_field, expected_name in entries:
    filename = manifest.get(field)
    if filename != expected_name or Path(filename).name != filename:
        raise SystemExit(f"unexpected {field}")
    if any(character in filename for character in ("/", "\\", "\r", "\n", "\0", "\t")):
        raise SystemExit(f"unsafe {field}")
    expected_size = manifest.get(size_field)
    if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size <= 0:
        raise SystemExit(f"invalid {size_field}")
    expected_sha256 = manifest.get(sha_field)
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise SystemExit(f"invalid {sha_field}")

    path = root / filename
    if not path.is_file() or path.stat().st_size != expected_size:
        raise SystemExit(f"{field} size mismatch")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise SystemExit(f"{field} sha256 mismatch")
    filenames.append(filename)

print("\t".join(filenames))
PY
}

validate_release_transition() {
  python3 - "$1" "$2" "$3" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

incoming_path = Path(sys.argv[1])
current_path = Path(sys.argv[2])
releases_dir = Path(sys.argv[3])

try:
    incoming = json.loads(incoming_path.read_text(encoding="utf-8"))
except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"unable to compare release versions: {exc}") from exc

current = None
if current_path.is_file():
    try:
        current = json.loads(current_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"unable to compare release versions: {exc}") from exc
elif current_path.exists() or current_path.is_symlink():
    raise SystemExit("current release manifest is not a regular file")

def numeric_version(value, label):
    if not isinstance(value, str):
        raise SystemExit(f"invalid {label} version")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)+)(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?", value)
    if not match:
        raise SystemExit(f"invalid {label} version")
    return tuple(int(part) for part in match.group(1).split("."))

def compare_numeric(left, right):
    width = max(len(left), len(right))
    padded_left = left + (0,) * (width - len(left))
    padded_right = right + (0,) * (width - len(right))
    return (padded_left > padded_right) - (padded_left < padded_right)

artifact_fields = (
    ("file", "file_size", "file_sha256"),
    ("launcher_file", "launcher_size", "launcher_sha256"),
    ("package_file", "package_size", "package_sha256"),
    ("download_file", "download_size", "download_sha256"),
    ("installer_file", "installer_size", "installer_sha256"),
)

incoming_version = incoming.get("version")
if current is not None:
    current_version = current.get("version")
    comparison = compare_numeric(
        numeric_version(incoming_version, "incoming"),
        numeric_version(current_version, "current"),
    )
    if comparison < 0:
        raise SystemExit(
            f"refusing older release {incoming_version}; production is {current_version}"
        )
    if comparison == 0:
        if incoming_version != current_version:
            raise SystemExit(
                f"refusing ambiguous release transition {current_version} -> {incoming_version}"
            )
        for fields in artifact_fields:
            if any(incoming.get(field) != current.get(field) for field in fields):
                raise SystemExit(
                    f"refusing to replace immutable artifacts for published version {incoming_version}"
                )

if releases_dir.exists() and not releases_dir.is_dir():
    raise SystemExit("release artifact destination is not a directory")
if releases_dir.is_dir():
    for name_field, size_field, sha_field in artifact_fields:
        filename = incoming.get(name_field)
        existing = releases_dir / filename
        if not existing.exists() and not existing.is_symlink():
            continue
        if existing.is_symlink() or not existing.is_file():
            raise SystemExit(f"immutable artifact destination is not a regular file: {filename}")
        digest = hashlib.sha256()
        with existing.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        if (
            existing.stat().st_size != incoming.get(size_field)
            or digest.hexdigest() != incoming.get(sha_field)
        ):
            raise SystemExit(
                f"refusing to replace immutable artifact with different content: {filename}"
            )
PY
}

validate_download_page_script() {
  python3 - "$1" "$2" <<'PY'
import json
import sys
from html.parser import HTMLParser
from pathlib import Path

source = Path(sys.argv[1])
manifest_path = Path(sys.argv[2])
try:
    version = json.loads(manifest_path.read_text(encoding="utf-8"))["version"]
except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
    raise SystemExit(f"unable to read release version for download page: {exc}") from exc

script_name = f"download-{version}.js"
script = source / "web" / "assets" / script_name
page = source / "web" / "download.html"
if not script.is_file() or script.stat().st_size == 0:
    raise SystemExit(f"missing versioned download script: {script_name}")

class DownloadPageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.stylesheets = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and attributes.get("src"):
            self.scripts.append(attributes["src"])
        if tag == "link" and "stylesheet" in attributes.get("rel", "").split():
            href = attributes.get("href")
            if href:
                self.stylesheets.append(href)

parser = DownloadPageParser()
parser.feed(page.read_text(encoding="utf-8"))
if f"assets/{script_name}" not in parser.scripts:
    raise SystemExit(f"download page does not reference {script_name}")
local_stylesheets = [
    href for href in parser.stylesheets
    if href.startswith("assets/")
]
if len(local_stylesheets) != 1:
    raise SystemExit("download page must reference exactly one local stylesheet")
stylesheet_href = local_stylesheets[0]
if "?" in stylesheet_href or "#" in stylesheet_href:
    raise SystemExit("download page stylesheet must use a cache-stable path without query or fragment")
stylesheet_relative = Path(stylesheet_href)
if (
    stylesheet_relative.is_absolute()
    or ".." in stylesheet_relative.parts
    or len(stylesheet_relative.parts) != 2
    or stylesheet_relative.parts[0] != "assets"
    or stylesheet_relative.suffix != ".css"
):
    raise SystemExit("download page stylesheet path is unsafe")
stylesheet = source / "web" / stylesheet_relative
if not stylesheet.is_file() or stylesheet.stat().st_size == 0:
    raise SystemExit(f"missing download page stylesheet: {stylesheet_href}")
PY
}

python3 -m py_compile "$SOURCE_DIR/shared_server.py"
test -s "$SOURCE_DIR/web/index.html"
test -s "$SOURCE_DIR/web/download.html"
test -s "$SOURCE_DIR/deploy/zhou.nginx.conf"
release_files="$(validate_release "$RELEASE_SOURCE")"
validate_download_page_script "$SOURCE_DIR" "$RELEASE_SOURCE/version.json"
acquire_publish_lock
validate_release_transition "$RELEASE_SOURCE/version.json" "$DATA_DIR/releases/version.json" "$DATA_DIR/releases"
IFS=$'\t' read -r APP_FILE LAUNCHER_FILE PACKAGE_FILE DOWNLOAD_FILE INSTALLER_FILE <<< "$release_files"
if [[ -z "$APP_FILE" || -z "$LAUNCHER_FILE" || -z "$PACKAGE_FILE" || -z "$DOWNLOAD_FILE" || -z "$INSTALLER_FILE" ]]; then
  fail "release manifest did not provide all filenames"
fi

asset_names=()
while IFS= read -r -d '' source_asset; do
  asset_names+=("$(basename "$source_asset")")
done < <(find "$SOURCE_DIR/web/assets" -mindepth 1 -maxdepth 1 -type f -print0)

install -d -o root -g root -m 700 "$BACKUP_DIR"
install -d -o root -g root -m 755 "$APP_DIR/web" "$APP_DIR/web/assets"
ensure_release_permissions
install -d -o root -g root -m 700 "$DATA_DIR/.release-staging"

stamp="$(date +%Y%m%d%H%M%S)"
backup="$BACKUP_DIR/download-site-$stamp-$$"
staging="$DATA_DIR/.release-staging/publish-$stamp-$$"
install -d -o root -g root -m 700 \
  "$backup" \
  "$backup/releases" \
  "$backup/web/assets" \
  "$backup/nginx" \
  "$staging"

backup_if_present() {
  local current="$1"
  local saved="$2"
  if [[ -e "$current" || -L "$current" ]]; then
    cp -a -- "$current" "$saved"
  fi
}

backup_if_present "$APP_DIR/shared_server.py" "$backup/shared_server.py"
backup_if_present "$APP_DIR/web/index.html" "$backup/web/index.html"
backup_if_present "$APP_DIR/web/download.html" "$backup/web/download.html"
for asset_name in "${asset_names[@]}"; do
  backup_if_present "$APP_DIR/web/assets/$asset_name" "$backup/web/assets/$asset_name"
done
backup_if_present "$NGINX_AVAILABLE" "$backup/nginx/available"
backup_if_present "$NGINX_ENABLED" "$backup/nginx/enabled"
for filename in "$APP_FILE" "$LAUNCHER_FILE" "$PACKAGE_FILE" "$DOWNLOAD_FILE" "$INSTALLER_FILE" version.json; do
  backup_if_present "$DATA_DIR/releases/$filename" "$backup/releases/$filename"
done

restore_or_remove() {
  local saved="$1"
  local destination="$2"
  rm -f -- "$destination"
  if [[ -e "$saved" || -L "$saved" ]]; then
    cp -a -- "$saved" "$destination"
  fi
}

rollback() {
  restore_or_remove "$backup/shared_server.py" "$APP_DIR/shared_server.py"
  restore_or_remove "$backup/web/index.html" "$APP_DIR/web/index.html"
  for asset_name in "${asset_names[@]}"; do
    restore_or_remove "$backup/web/assets/$asset_name" "$APP_DIR/web/assets/$asset_name"
  done
  restore_or_remove "$backup/web/download.html" "$APP_DIR/web/download.html"
  restore_or_remove "$backup/nginx/available" "$NGINX_AVAILABLE"
  restore_or_remove "$backup/nginx/enabled" "$NGINX_ENABLED"
  for filename in "$APP_FILE" "$LAUNCHER_FILE" "$PACKAGE_FILE" "$DOWNLOAD_FILE" "$INSTALLER_FILE" version.json; do
    restore_or_remove "$backup/releases/$filename" "$DATA_DIR/releases/$filename"
  done
  rm -f -- \
    "$staging/$APP_FILE" \
    "$staging/$LAUNCHER_FILE" \
    "$staging/$PACKAGE_FILE" \
    "$staging/$DOWNLOAD_FILE" \
    "$staging/$INSTALLER_FILE" \
    "$staging/version.json"
  rmdir "$staging" 2>/dev/null || true
  ensure_release_permissions
  systemctl restart "$SERVICE" || true
  nginx -t >/dev/null 2>&1 && systemctl reload nginx || true
}
arm_transaction_traps

install -o root -g root -m 644 "$SOURCE_DIR/shared_server.py" "$APP_DIR/shared_server.py"
for asset_name in "${asset_names[@]}"; do
  install -o root -g root -m 644 \
    "$SOURCE_DIR/web/assets/$asset_name" \
    "$APP_DIR/web/assets/$asset_name"
done
install -o root -g root -m 644 "$SOURCE_DIR/web/index.html" "$APP_DIR/web/index.html"
install -o root -g root -m 644 "$SOURCE_DIR/web/download.html" "$APP_DIR/web/download.html"
install -o delta-stats -g delta-stats -m 644 "$RELEASE_SOURCE/$APP_FILE" "$staging/$APP_FILE"
install -o delta-stats -g delta-stats -m 644 "$RELEASE_SOURCE/$LAUNCHER_FILE" "$staging/$LAUNCHER_FILE"
install -o delta-stats -g delta-stats -m 644 "$RELEASE_SOURCE/$PACKAGE_FILE" "$staging/$PACKAGE_FILE"
install -o delta-stats -g delta-stats -m 644 "$RELEASE_SOURCE/$DOWNLOAD_FILE" "$staging/$DOWNLOAD_FILE"
install -o delta-stats -g delta-stats -m 644 "$RELEASE_SOURCE/$INSTALLER_FILE" "$staging/$INSTALLER_FILE"
install -o delta-stats -g delta-stats -m 644 "$RELEASE_SOURCE/version.json" "$staging/version.json"

if [[ "$(validate_release "$staging")" != "$release_files" ]]; then
  fail "staged release does not match source manifest"
fi

mv -f -- "$staging/$APP_FILE" "$DATA_DIR/releases/$APP_FILE"
mv -f -- "$staging/$LAUNCHER_FILE" "$DATA_DIR/releases/$LAUNCHER_FILE"
mv -f -- "$staging/$PACKAGE_FILE" "$DATA_DIR/releases/$PACKAGE_FILE"
mv -f -- "$staging/$DOWNLOAD_FILE" "$DATA_DIR/releases/$DOWNLOAD_FILE"
mv -f -- "$staging/$INSTALLER_FILE" "$DATA_DIR/releases/$INSTALLER_FILE"
mv -f -- "$staging/version.json" "$DATA_DIR/releases/version.json"
rmdir "$staging"
ensure_release_permissions

install -o root -g root -m 644 "$SOURCE_DIR/deploy/zhou.nginx.conf" "$NGINX_AVAILABLE"
ln -sfnT "$NGINX_AVAILABLE" "$NGINX_ENABLED"

systemctl restart "$SERVICE"
for attempt in {1..20}; do
  if curl --fail --silent http://127.0.0.1:3012/ >/dev/null; then
    break
  fi
  if [[ "$attempt" == 20 ]]; then
    fail "delta-stats health check timed out"
  fi
  sleep 0.25
done

nginx -t
systemctl reload nginx
systemctl is-active --quiet "$SERVICE"
curl --fail --silent --header 'Host: zhou.opendeep.top' http://127.0.0.1/ >/dev/null
curl --fail --silent --insecure \
  --resolve zhou.opendeep.top:443:127.0.0.1 \
  https://zhou.opendeep.top/ >/dev/null
clear_transaction_traps
echo "download site published"
