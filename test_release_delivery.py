import base64
import http.client
import hashlib
import io
import json
import os
import subprocess
import sys
import textwrap
import threading
import unittest
import zipfile
from contextlib import redirect_stdout
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import build_friend_package
from app_version import (
    APP_DISPLAY_NAME,
    APP_VERSION,
    LAUNCHER_PROTOCOL,
    LAUNCHER_VERSION,
    RELEASE_HISTORY,
    RELEASE_NOTES,
)
from build_friend_package import (
    APP_PACKAGE_ARCHIVE,
    APP_PACKAGE_FORMAT,
    APP_RELEASE_BINARY,
    DOWNLOAD_ARCHIVE,
    INSTALLER_BINARY,
    LAUNCHER_RELEASE_BINARY,
    artifact_download_urls,
    release_history_payload,
    sha256_file,
    update_base_urls,
)
from shared_server import SharedStore, build_handler, load_release_files
from http.server import ThreadingHTTPServer


ROOT = Path(__file__).resolve().parent


class ReleaseDeliveryTests(unittest.TestCase):
    def test_builder_uses_versioned_release_names_and_hashes_download(self):
        self.assertEqual(APP_RELEASE_BINARY, f"DeltaStatsApp-{APP_VERSION}.exe")
        self.assertEqual(
            APP_PACKAGE_ARCHIVE,
            f"DeltaStatsAppDir-{APP_VERSION}.zip",
        )
        self.assertEqual(
            LAUNCHER_RELEASE_BINARY,
            f"DeltaStatsLauncher-{LAUNCHER_VERSION}.exe",
        )
        self.assertEqual(
            DOWNLOAD_ARCHIVE,
            f"DeltaStatsAssistant-{APP_VERSION}.zip",
        )
        with TemporaryDirectory() as directory:
            output = Path(directory) / "package"

            def fake_pyinstaller(*arguments):
                name = arguments[arguments.index("--name") + 1]
                distpath = Path(arguments[arguments.index("--distpath") + 1])
                distpath.mkdir(parents=True, exist_ok=True)
                if "--onedir" in arguments:
                    bundle = distpath / name
                    (bundle / "_internal").mkdir(parents=True)
                    (bundle / f"{name}.exe").write_bytes(name.encode("utf-8"))
                    (bundle / "_internal" / "runtime.bin").write_bytes(b"runtime")
                else:
                    (distpath / f"{name}.exe").write_bytes(name.encode("utf-8"))

            def fake_build_installer(source_dir, output_dir):
                self.assertTrue((source_dir / "app" / "current.json").is_file())
                output_dir.mkdir(parents=True, exist_ok=True)
                installer = output_dir / INSTALLER_BINARY
                installer.write_bytes(b"installer")
                return installer

            with redirect_stdout(io.StringIO()), patch.dict(
                os.environ,
                {
                    "DELTA_UPDATE_BASE_URL": "https://zhou.opendeep.top",
                    "DELTA_UPDATE_ALTERNATE_BASE_URLS": "",
                },
            ), patch.object(
                build_friend_package, "OUTPUT", output
            ), patch.object(
                build_friend_package, "pyinstaller", fake_pyinstaller
            ), patch.object(
                build_friend_package, "build_installer", fake_build_installer
            ):
                self.assertEqual(build_friend_package.main(), 0)

            release = output / "release"
            manifest = json.loads((release / "version.json").read_text(encoding="utf-8"))
            history = release_history_payload()
            self.assertEqual(manifest["release_history"], history)
            self.assertEqual(
                manifest["release_notes"],
                [
                    f"v{record['version']}：{note}"
                    for record in history
                    for note in record["release_notes"]
                ],
            )
            self.assertEqual(history[-1], {
                "version": APP_VERSION,
                "release_notes": list(RELEASE_NOTES),
            })
            self.assertEqual(RELEASE_HISTORY[-1]["version"], APP_VERSION)
            self.assertEqual(manifest["file"], APP_RELEASE_BINARY)
            self.assertEqual(manifest["launcher_protocol"], LAUNCHER_PROTOCOL)
            self.assertEqual(manifest["launcher_version"], LAUNCHER_VERSION)
            self.assertEqual(manifest["package_format"], APP_PACKAGE_FORMAT)
            self.assertEqual(manifest["package_file"], APP_PACKAGE_ARCHIVE)
            self.assertEqual(manifest["launcher_file"], LAUNCHER_RELEASE_BINARY)
            self.assertEqual(
                manifest["launcher_url"],
                f"https://zhou.opendeep.top/updates/{LAUNCHER_RELEASE_BINARY}",
            )
            self.assertEqual(manifest["launcher_urls"], [manifest["launcher_url"]])
            self.assertEqual(
                manifest["package_url"],
                f"https://zhou.opendeep.top/updates/{APP_PACKAGE_ARCHIVE}",
            )
            self.assertEqual(manifest["package_urls"], [manifest["package_url"]])
            self.assertEqual(manifest["download_file"], DOWNLOAD_ARCHIVE)
            self.assertEqual(manifest["installer_file"], INSTALLER_BINARY)
            release_fields = (
                ("file", "file"),
                ("launcher", "launcher_file"),
                ("package", "package_file"),
                ("download", "download_file"),
                ("installer", "installer_file"),
            )
            for field, filename_field in release_fields:
                release_file = release / manifest[filename_field]
                self.assertEqual(manifest[f"{field}_size"], release_file.stat().st_size)
                self.assertEqual(manifest[f"{field}_sha256"], sha256_file(release_file))
            package = release / APP_PACKAGE_ARCHIVE
            self.assertEqual(manifest["package_size"], package.stat().st_size)
            self.assertEqual(manifest["package_sha256"], sha256_file(package))
            archive = release / DOWNLOAD_ARCHIVE
            self.assertEqual(
                manifest["download_sha256"],
                sha256_file(archive),
            )
            with zipfile.ZipFile(package) as package_zip:
                package_files = set(package_zip.namelist())
            self.assertIn("DeltaStatsApp.exe", package_files)
            self.assertIn("_internal/runtime.bin", package_files)
            self.assertIn("LICENSE", package_files)
            self.assertIn("THIRD_PARTY_NOTICES.md", package_files)
            self.assertIn("licenses/Inno-Setup.txt", package_files)

            portable = output / f"{APP_DISPLAY_NAME}.zip"
            with zipfile.ZipFile(portable) as portable_zip:
                portable_files = set(portable_zip.namelist())
                current = json.loads(portable_zip.read("app/current.json"))
            version_root = f"app/versions/{APP_VERSION}"
            self.assertEqual(current, {"schema": 1, "version": APP_VERSION})
            self.assertIn(f"{version_root}/DeltaStatsApp.exe", portable_files)
            self.assertIn(f"{version_root}/_internal/runtime.bin", portable_files)
            self.assertNotIn("app/DeltaStatsApp.exe", portable_files)
            self.assertIn("LICENSE", portable_files)
            self.assertIn("THIRD_PARTY_NOTICES.md", portable_files)

    def test_builder_preserves_ordered_alternate_update_bases(self):
        with patch.dict(
            os.environ,
            {
                "DELTA_UPDATE_BASE_URL": "https://primary.example/base/",
                "DELTA_UPDATE_ALTERNATE_BASE_URLS": (
                    "https://mirror-one.example/releases,\n"
                    "https://mirror-two.example"
                ),
            },
        ):
            bases = update_base_urls()

        self.assertEqual(
            bases,
            [
                "https://primary.example/base",
                "https://mirror-one.example/releases",
                "https://mirror-two.example",
            ],
        )
        self.assertEqual(
            artifact_download_urls(bases, "package.zip"),
            [
                "https://primary.example/base/updates/package.zip",
                "https://mirror-one.example/releases/updates/package.zip",
                "https://mirror-two.example/updates/package.zip",
            ],
        )

    def test_builder_requires_release_notes_for_every_published_version(self):
        with patch.object(build_friend_package, "RELEASE_NOTES", ()):
            with self.assertRaisesRegex(RuntimeError, "RELEASE_NOTES"):
                build_friend_package.main()

    def test_shared_server_uses_manifest_routes_and_redirects_legacy_zip(self):
        version = "9.8.7"
        filenames = {
            "file": f"DeltaStatsApp-{version}.exe",
            "launcher_file": f"DeltaStatsLauncher-{version}.exe",
            "package_file": f"DeltaStatsAppDir-{version}.zip",
            "download_file": f"DeltaStatsAssistant-{version}.zip",
            "installer_file": f"DeltaStatsAssistant-{version}-Setup.exe",
        }
        payloads = {
            filenames["file"]: b"desktop-app",
            filenames["launcher_file"]: b"desktop-launcher",
            filenames["package_file"]: b"onedir-package",
            filenames["download_file"]: b"complete-package",
            filenames["installer_file"]: b"installer-package",
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "releases"
            release.mkdir()
            manifest = {
                "version": version,
                "package_format": APP_PACKAGE_FORMAT,
                **filenames,
            }
            (release / "version.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            for filename, payload in payloads.items():
                (release / filename).write_bytes(payload)

            store = SharedStore(root / "shared.sqlite3")
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                build_handler(store, "token", release),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                server.server_address[1],
                timeout=5,
            )
            try:
                connection.request("GET", "/updates/version.json")
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Cache-Control"), "no-store")

                connection.request("GET", "/downloads/DeltaStatsAssistant.zip")
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 302)
                self.assertEqual(
                    response.getheader("Location"),
                    f'/downloads/{filenames["download_file"]}',
                )
                self.assertEqual(response.getheader("Cache-Control"), "no-store")

                connection.request("GET", f'/downloads/{filenames["download_file"]}')
                response = connection.getresponse()
                package = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(package, payloads[filenames["download_file"]])
                self.assertEqual(
                    response.getheader("Cache-Control"),
                    "public, max-age=31536000, immutable",
                )
                self.assertEqual(
                    response.getheader("Content-Disposition"),
                    f'attachment; filename="{filenames["download_file"]}"',
                )

                connection.request("GET", "/downloads/DeltaStatsAssistant-Setup.exe")
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 302)
                self.assertEqual(
                    response.getheader("Location"),
                    f'/downloads/{filenames["installer_file"]}',
                )
                self.assertEqual(response.getheader("Cache-Control"), "no-store")

                connection.request("GET", f'/downloads/{filenames["installer_file"]}')
                response = connection.getresponse()
                installer = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(installer, payloads[filenames["installer_file"]])
                self.assertEqual(
                    response.getheader("Content-Disposition"),
                    f'attachment; filename="{filenames["installer_file"]}"',
                )

                connection.request("HEAD", f'/updates/{filenames["file"]}')
                response = connection.getresponse()
                body = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(body, b"")
                self.assertEqual(
                    int(response.getheader("Content-Length")),
                    len(payloads[filenames["file"]]),
                )
                self.assertIn("immutable", response.getheader("Cache-Control"))

                connection.request("HEAD", f'/updates/{filenames["package_file"]}')
                response = connection.getresponse()
                body = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(body, b"")
                self.assertEqual(response.getheader("Content-Type"), "application/zip")
                self.assertEqual(
                    int(response.getheader("Content-Length")),
                    len(payloads[filenames["package_file"]]),
                )
                self.assertIn("immutable", response.getheader("Cache-Control"))

                connection.request("GET", "/updates/DeltaStatsApp.exe")
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 404)
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_shared_server_rejects_manifest_paths(self):
        with TemporaryDirectory() as directory:
            release = Path(directory)
            (release / "version.json").write_text(
                json.dumps(
                    {
                        "file": "../DeltaStatsApp-1.0.0.exe",
                        "launcher_file": "DeltaStatsLauncher-1.0.0.exe",
                        "download_file": "DeltaStatsAssistant-1.0.0.zip",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "release filename"):
                load_release_files(release)

    def test_shared_server_rejects_unsafe_onedir_package_path(self):
        with TemporaryDirectory() as directory:
            release = Path(directory)
            (release / "version.json").write_text(
                json.dumps(
                    {
                        "file": "DeltaStatsApp-1.0.0.exe",
                        "launcher_file": "DeltaStatsLauncher-1.0.0.exe",
                        "package_format": APP_PACKAGE_FORMAT,
                        "package_file": "../DeltaStatsAppDir-1.0.0.zip",
                        "download_file": "DeltaStatsAssistant-1.0.0.zip",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "release filename"):
                load_release_files(release)

    def test_shared_server_rejects_unsafe_installer_path(self):
        with TemporaryDirectory() as directory:
            release = Path(directory)
            (release / "version.json").write_text(
                json.dumps(
                    {
                        "file": "DeltaStatsApp-1.0.0.exe",
                        "launcher_file": "DeltaStatsLauncher-1.0.0.exe",
                        "download_file": "DeltaStatsAssistant-1.0.0.zip",
                        "installer_file": "../DeltaStatsAssistant-1.0.0-Setup.exe",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "release filename"):
                load_release_files(release)

    def test_shared_server_accepts_published_1_5_8_manifest_without_onedir_package(self):
        with TemporaryDirectory() as directory:
            release = Path(directory)
            manifest = {
                "version": "1.5.8",
                "file": "DeltaStatsApp-1.5.8.exe",
                "launcher_file": "DeltaStatsLauncher-1.5.8.exe",
                "download_file": "DeltaStatsAssistant-1.5.8.zip",
            }
            (release / "version.json").write_text(json.dumps(manifest), encoding="utf-8")

            self.assertEqual(load_release_files(release), {
                "file": manifest["file"],
                "launcher_file": manifest["launcher_file"],
                "download_file": manifest["download_file"],
            })

    def test_nginx_serves_versioned_releases_without_python_buffering(self):
        config = (ROOT / "deploy" / "zhou.nginx.conf").read_text(encoding="utf-8")
        self.assertIn("sendfile on;", config)
        self.assertIn("tcp_nopush on;", config)
        self.assertIn("location = /updates/version.json", config)
        self.assertIn("location = /downloads/DeltaStatsAssistant.zip", config)
        self.assertIn("location = /downloads/DeltaStatsAssistant-Setup.exe", config)
        self.assertIn("location /updates/", config)
        self.assertIn("location /downloads/", config)
        self.assertEqual(config.count("alias /var/lib/delta-stats/releases/;"), 2)
        self.assertIn("public, max-age=31536000, immutable", config)
        self.assertIn('add_header Content-Disposition "attachment";', config)

    def test_publish_scripts_validate_then_switch_manifest_last(self):
        for relative_path, source_prefix in (
            ("deploy/publish_release.sh", "$SOURCE_DIR"),
            ("deploy/publish_download_site.sh", "$RELEASE_SOURCE"),
        ):
            with self.subTest(script=relative_path):
                script = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn("set -Eeuo pipefail", script)
                self.assertIn(
                    "PUBLISH_LOCK_FILE=/run/lock/delta-stats-publish.lock",
                    script,
                )
                self.assertIn("download_sha256", script)
                self.assertIn("installer_sha256", script)
                self.assertIn("package_sha256", script)
                self.assertIn('manifest.get("launcher_protocol") != 2', script)
                self.assertIn('launcher_version = manifest.get("launcher_version")', script)
                self.assertIn('f"DeltaStatsLauncher-{launcher_version}.exe"', script)
                self.assertIn('"pyinstaller-onedir-zip-v1"', script)
                self.assertIn('m 751 "$DATA_DIR"', script)
                self.assertIn('m 755 "$DATA_DIR/releases"', script)
                self.assertIn(".release-staging", script)
                self.assertIn("validate_release_transition", script)
                transition_check = script.index(
                    'validate_release_transition "'
                )
                lock_check = script.index("\nacquire_publish_lock\n")
                self.assertLess(lock_check, transition_check)
                self.assertLess(transition_check, script.index('install -d -o root -g root -m 700 "$BACKUP_DIR"'))
                rollback_definition = script.index("\nrollback() {\n")
                arm_traps = script.index("\narm_transaction_traps\n")
                first_production_replacement = script.index(
                    'install -o root -g root -m 644 "$SOURCE_DIR/shared_server.py"'
                )
                self.assertLess(rollback_definition, arm_traps)
                self.assertLess(arm_traps, first_production_replacement)
                self.assertGreater(
                    script.index("\nclear_transaction_traps\n"),
                    first_production_replacement,
                )
                if relative_path == "deploy/publish_download_site.sh":
                    self.assertIn("validate_download_page_script", script)
                    self.assertIn('f"download-{version}.js"', script)
                    self.assertIn("HTMLParser", script)
                    self.assertIn("stylesheet_href", script)
                    asset_install = script.index(
                        'for asset_name in "${asset_names[@]}"; do'
                    )
                    index_install = script.index(
                        'install -o root -g root -m 644 "$SOURCE_DIR/web/index.html"'
                    )
                    download_install = script.index(
                        'install -o root -g root -m 644 "$SOURCE_DIR/web/download.html"'
                    )
                    self.assertLess(asset_install, index_install)
                    self.assertLess(asset_install, download_install)
                self.assertIn(
                    'find "$DATA_DIR/releases" -mindepth 1 -maxdepth 1 -type f',
                    script,
                )
                self.assertIn("-exec chmod 644 {} +", script)
                self.assertNotIn("-delete", script)
                self.assertNotIn('rm -rf "$DATA_DIR/releases"', script)
                self.assertIn(
                    f'install -o delta-stats -g delta-stats -m 644 "{source_prefix}/$APP_FILE"',
                    script,
                )
                self.assertIn(
                    f'install -o delta-stats -g delta-stats -m 644 "{source_prefix}/$PACKAGE_FILE"',
                    script,
                )
                self.assertIn(
                    f'install -o delta-stats -g delta-stats -m 644 "{source_prefix}/$INSTALLER_FILE"',
                    script,
                )
                manifest_move = script.index(
                    'mv -f -- "$staging/version.json" "$DATA_DIR/releases/version.json"'
                )
                for filename in (
                    "$APP_FILE",
                    "$LAUNCHER_FILE",
                    "$PACKAGE_FILE",
                    "$DOWNLOAD_FILE",
                    "$INSTALLER_FILE",
                ):
                    self.assertLess(
                        script.index(
                            f'mv -f -- "$staging/{filename}" "$DATA_DIR/releases/{filename}"'
                        ),
                        manifest_move,
                    )

    @unittest.skipIf(os.name == "nt", "POSIX signal semantics are verified by the Linux CI job")
    def test_publish_transaction_traps_execute_rollback_once(self):
        def shell_function(script: str, name: str) -> str:
            start = script.index(f"{name}() {{\n")
            end = script.index("\n}\n", start) + len("\n}\n")
            return script[start:end]

        for relative_path in (
            "deploy/publish_release.sh",
            "deploy/publish_download_site.sh",
        ):
            with self.subTest(script=relative_path, failure="function"):
                script = (ROOT / relative_path).read_text(encoding="utf-8")
                helpers = "\n".join(
                    shell_function(script, name)
                    for name in (
                        "rollback_on_failure",
                        "arm_transaction_traps",
                        "clear_transaction_traps",
                    )
                )
                fixture = textwrap.dedent(
                    f"""
                    set -Eeuo pipefail
                    transaction_rollback_started=0
                    rollback() {{
                      printf '%s\\n' rollback
                      kill -TERM "$$"
                      printf '%s\\n' rollback-complete
                    }}
                    {helpers}
                    arm_transaction_traps
                    fail_inside_function() {{
                      return 37
                    }}
                    fail_inside_function
                    """
                )
                result = subprocess.run(
                    ["bash"],
                    input=fixture.encode("utf-8"),
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                stdout = result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace")
                self.assertEqual(result.returncode, 37, stderr)
                self.assertEqual(
                    stdout.splitlines(),
                    ["rollback", "rollback-complete"],
                )

            for signal_name, expected_status in (
                ("INT", 130),
                ("TERM", 143),
                ("HUP", 129),
            ):
                with self.subTest(script=relative_path, signal=signal_name):
                    fixture = textwrap.dedent(
                        f"""
                        set -Eeuo pipefail
                        transaction_rollback_started=0
                        rollback() {{
                          printf '%s\\n' rollback
                        }}
                        {helpers}
                        arm_transaction_traps
                        while :; do
                          :
                        done
                        """
                    )
                    encoded_fixture = base64.b64encode(
                        fixture.encode("utf-8")
                    ).decode("ascii")
                    harness = (
                        "set -o pipefail\n"
                        f"printf '%s' '{encoded_fixture}' | base64 -d | "
                        "timeout --foreground --preserve-status --kill-after=1s "
                        f"--signal={signal_name} 0.1s bash\n"
                    )
                    result = subprocess.run(
                        ["bash"],
                        input=harness.encode("utf-8"),
                        capture_output=True,
                        timeout=5,
                        check=False,
                    )
                    stdout = result.stdout.decode("utf-8", errors="replace")
                    stderr = result.stderr.decode("utf-8", errors="replace")
                    self.assertEqual(result.returncode, expected_status, stderr)
                    self.assertEqual(stdout.splitlines(), ["rollback"])

    @unittest.skipIf(os.name == "nt", "flock semantics are verified by the Linux CI job")
    def test_publish_scripts_share_a_nonblocking_process_lock(self):
        def shell_function(script: str, name: str) -> str:
            start = script.index(f"{name}() {{\n")
            end = script.index("\n}\n", start) + len("\n}\n")
            return script[start:end]

        release_script = (ROOT / "deploy" / "publish_release.sh").read_text(
            encoding="utf-8"
        )
        download_script = (
            ROOT / "deploy" / "publish_download_site.sh"
        ).read_text(encoding="utf-8")
        lock_assignment = "PUBLISH_LOCK_FILE=/run/lock/delta-stats-publish.lock"
        self.assertIn(lock_assignment, release_script)
        self.assertIn(lock_assignment, download_script)

        holder = textwrap.dedent(
            f"""
            set -Eeuo pipefail
            PUBLISH_LOCK_FILE="$1"
            READY_FILE="$2"
            fail() {{
              echo "$1" >&2
              return 1
            }}
            {shell_function(release_script, "acquire_publish_lock")}
            acquire_publish_lock
            : > "$READY_FILE"
            while :; do
              :
            done
            """
        )
        contender = textwrap.dedent(
            f"""
            set -Eeuo pipefail
            PUBLISH_LOCK_FILE="$1"
            fail() {{
              echo "$1" >&2
              return 1
            }}
            {shell_function(download_script, "acquire_publish_lock")}
            acquire_publish_lock
            """
        )
        encoded_holder = base64.b64encode(holder.encode("utf-8")).decode("ascii")
        encoded_contender = base64.b64encode(contender.encode("utf-8")).decode(
            "ascii"
        )
        harness = textwrap.dedent(
            """
            set -Eeuo pipefail
            work_dir="$(mktemp -d)"
            holder_pid=
            cleanup() {
              if [[ -n "$holder_pid" ]]; then
                kill -TERM "$holder_pid" 2>/dev/null || true
                wait "$holder_pid" 2>/dev/null || true
              fi
              rm -rf -- "$work_dir"
            }
            trap cleanup EXIT
            lock_file="$work_dir/publish.lock"
            ready_file="$work_dir/ready"
            printf '%s' '__HOLDER_PAYLOAD__' | base64 -d > "$work_dir/holder.sh"
            printf '%s' '__CONTENDER_PAYLOAD__' | base64 -d > "$work_dir/contender.sh"
            bash "$work_dir/holder.sh" "$lock_file" "$ready_file" &
            holder_pid=$!
            for attempt in {1..100}; do
              [[ -e "$ready_file" ]] && break
              sleep 0.01
            done
            [[ -e "$ready_file" ]]
            set +e
            output="$(bash "$work_dir/contender.sh" "$lock_file" 2>&1)"
            status=$?
            set -e
            printf 'status=%s\\n%s\\n' "$status" "$output"
            [[ "$status" -ne 0 ]]
            [[ "$output" == *"another delta-stats production publish is already running"* ]]
            """
        ).replace("__HOLDER_PAYLOAD__", encoded_holder).replace(
            "__CONTENDER_PAYLOAD__", encoded_contender
        )
        result = subprocess.run(
            ["bash"],
            input=harness.encode("utf-8"),
            capture_output=True,
            timeout=10,
            check=False,
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, stderr)
        self.assertRegex(stdout, r"status=[1-9][0-9]*")
        self.assertIn(
            "another delta-stats production publish is already running",
            stdout,
        )

    def test_publish_scripts_reject_downgrade_and_immutable_artifact_replacement(self):
        artifact_fields = (
            ("file", "file_size", "file_sha256"),
            ("launcher_file", "launcher_size", "launcher_sha256"),
            ("package_file", "package_size", "package_sha256"),
            ("download_file", "download_size", "download_sha256"),
            ("installer_file", "installer_size", "installer_sha256"),
        )

        def manifest(
            version: str,
            digest: str = "a",
            launcher_version: str | None = None,
        ) -> dict[str, object]:
            payload: dict[str, object] = {"version": version}
            filenames = (
                f"DeltaStatsApp-{version}.exe",
                f"DeltaStatsLauncher-{launcher_version or version}.exe",
                f"DeltaStatsAppDir-{version}.zip",
                f"DeltaStatsAssistant-{version}.zip",
                f"DeltaStatsAssistant-{version}-Setup.exe",
            )
            for index, ((name_field, size_field, sha_field), filename) in enumerate(
                zip(artifact_fields, filenames), 1
            ):
                payload[name_field] = filename
                payload[size_field] = index
                payload[sha_field] = digest * 64
            return payload

        def validator_source(script: str) -> str:
            function_start = script.index("validate_release_transition()")
            source_start = script.index("<<'PY'\n", function_start) + len("<<'PY'\n")
            source_end = script.index("\nPY\n}", source_start)
            return script[source_start:source_end]

        def validate(
            source: str,
            incoming: dict[str, object],
            current: dict[str, object],
            existing_files: dict[str, bytes] | None = None,
        ) -> None:
            with TemporaryDirectory() as temporary:
                root = Path(temporary)
                incoming_path = root / "incoming.json"
                current_path = root / "current.json"
                releases = root / "releases"
                releases.mkdir()
                incoming_path.write_text(json.dumps(incoming), encoding="utf-8")
                current_path.write_text(json.dumps(current), encoding="utf-8")
                for filename, content in (existing_files or {}).items():
                    (releases / filename).write_bytes(content)
                try:
                    with patch.object(
                        sys,
                        "argv",
                        ["validator", str(incoming_path), str(current_path), str(releases)],
                    ):
                        exec(compile(source, "release-transition-validator", "exec"), {})
                except SystemExit as exc:
                    if exc.code not in (None, 0):
                        raise

        for relative_path in (
            "deploy/publish_release.sh",
            "deploy/publish_download_site.sh",
        ):
            with self.subTest(script=relative_path):
                source = validator_source((ROOT / relative_path).read_text(encoding="utf-8"))
                current = manifest("1.8.5")
                validate(source, manifest("1.8.6"), current)
                validate(source, manifest("1.8.5"), current)
                with self.assertRaisesRegex(SystemExit, "refusing older release"):
                    validate(source, manifest("1.8.4"), current)
                with self.assertRaisesRegex(SystemExit, "immutable artifacts"):
                    validate(source, manifest("1.8.5", digest="b"), current)
                with self.assertRaisesRegex(SystemExit, "ambiguous release transition"):
                    validate(source, manifest("1.8.5+rebuilt"), current)

                published_launcher = b"published launcher"
                published_launcher_sha256 = hashlib.sha256(published_launcher).hexdigest()
                current = manifest("1.8.5")
                upgrade = manifest("1.8.6", launcher_version="1.8.5")
                for payload in (current, upgrade):
                    payload["launcher_size"] = len(published_launcher)
                    payload["launcher_sha256"] = published_launcher_sha256
                validate(
                    source,
                    upgrade,
                    current,
                    {str(upgrade["launcher_file"]): published_launcher},
                )

                rebuilt_launcher = b"rebuilt launcher"
                rebuilt_upgrade = dict(upgrade)
                rebuilt_upgrade["launcher_size"] = len(rebuilt_launcher)
                rebuilt_upgrade["launcher_sha256"] = hashlib.sha256(
                    rebuilt_launcher
                ).hexdigest()
                with self.assertRaisesRegex(SystemExit, "immutable artifact"):
                    validate(
                        source,
                        rebuilt_upgrade,
                        current,
                        {str(upgrade["launcher_file"]): published_launcher},
                    )

    def test_install_permissions_allow_nginx_to_read_only_releases(self):
        script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
        self.assertIn('m 751 "$DATA_DIR"', script)
        self.assertIn('m 755 "$DATA_DIR/releases"', script)
        self.assertIn('-m 644 -t "$DATA_DIR/releases"', script)
        self.assertNotIn('chmod -R', script)
        self.assertNotIn('chown -R', script)

    def test_installer_uses_a_writable_per_user_root_and_preserves_local_data(self):
        script = (ROOT / "installer" / "DeltaStatsAssistant.iss").read_text(encoding="utf-8")
        self.assertIn('#define AppName "三角洲战绩本"', script)
        self.assertIn('#define AppExeName "三角洲情报助手.exe"', script)
        self.assertIn("DefaultDirName={localappdata}\\Programs\\DeltaStatsAssistant", script)
        self.assertIn("PrivilegesRequired=lowest", script)
        self.assertIn("PrivilegesRequiredOverridesAllowed=commandline", script)
        self.assertIn("Name: \"{autoprograms}\\{#AppName}\"", script)
        self.assertIn("Name: \"{autodesktop}\\{#AppName}\"", script)
        self.assertIn('Type: files; Name: "{autoprograms}\\三角洲情报助手.lnk"', script)
        self.assertIn('Type: files; Name: "{autodesktop}\\三角洲情报助手.lnk"', script)
        self.assertIn("Type: filesandordirs; Name: \"{app}\\app\"", script)
        self.assertNotIn("DeltaForceIntelAssistant", script)
        self.assertIn("InstalledVersionIsNewer", script)

    def test_download_page_only_accepts_safe_versioned_release_names(self):
        script_name = f"download-{APP_VERSION}.js"
        script = (ROOT / "web" / "assets" / script_name).read_text(encoding="utf-8")
        self.assertIn("DeltaStatsAssistant-Setup.exe", script)
        self.assertIn("installerFilePattern.test(release.installer_file)", script)
        self.assertIn("portableFilePattern.test(release.download_file)", script)
        self.assertIn("[data-installer-download]", script)
        self.assertIn("[data-portable-download]", script)

    def test_download_page_uses_a_current_versioned_script_path(self):
        page = (ROOT / "web" / "download.html").read_text(encoding="utf-8")
        self.assertIn(f"assets/download-{APP_VERSION}.js", page)
        self.assertNotIn("assets/download.js", page)

    def test_download_page_does_not_advertise_firebreak_single_match_kd(self):
        page = (ROOT / "web" / "download.html").read_text(encoding="utf-8")
        detail_copy = page.split("<dt>逐局详情</dt>", 1)[1].split("</dd>", 1)[0]

        self.assertNotIn("KD", detail_copy)
        self.assertIn("击杀、死亡、AI 击杀", detail_copy)

    def test_web_pages_use_the_current_icon_design_assets(self):
        download_page = (ROOT / "web" / "download.html").read_text(encoding="utf-8")
        app_page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        for filename in ("app-icon-v2.png", "favicon-32-v2.png", "favicon-16-v2.png"):
            self.assertTrue((ROOT / "web" / "assets" / filename).is_file())
            self.assertIn(f"assets/{filename}", download_page)
        self.assertIn("assets/app-icon-v2.png", app_page)
        self.assertIn("assets/favicon-32-v2.png", app_page)
        self.assertIn("assets/favicon-16-v2.png", app_page)
        self.assertNotIn("app-icon.png?v=", download_page)
        self.assertNotIn("favicon-32.png?v=", download_page)
        self.assertNotIn("favicon-16.png?v=", download_page)
        self.assertTrue((ROOT / "tools" / "assets" / "app-icon-master.png").is_file())

    def test_download_page_uses_a_cache_stable_local_stylesheet(self):
        class DownloadPageParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stylesheets = []

            def handle_starttag(self, tag, attrs):
                attributes = dict(attrs)
                if tag == "link" and "stylesheet" in attributes.get("rel", "").split():
                    href = attributes.get("href")
                    if href:
                        self.stylesheets.append(href)

        page = (ROOT / "web" / "download.html").read_text(encoding="utf-8")
        parser = DownloadPageParser()
        parser.feed(page)
        local_stylesheets = [
            href for href in parser.stylesheets
            if href.startswith("assets/")
        ]
        self.assertEqual(len(local_stylesheets), 1)
        stylesheet_href = local_stylesheets[0]
        self.assertNotIn("?", stylesheet_href)
        self.assertNotIn("#", stylesheet_href)
        stylesheet_path = Path(stylesheet_href)
        self.assertEqual(stylesheet_path.parts[0], "assets")
        self.assertEqual(len(stylesheet_path.parts), 2)
        self.assertEqual(stylesheet_path.suffix, ".css")
        self.assertGreater(
            (ROOT / "web" / stylesheet_path).stat().st_size,
            0,
        )
        self.assertNotIn("v1.5.3", page)


if __name__ == "__main__":
    unittest.main()
