#!/usr/bin/env python3
"""Build the local Windows app, launcher, and basic online update release."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

from app_version import (
    APP_DISPLAY_NAME,
    APP_VERSION,
    LAUNCHER_PROTOCOL,
    LAUNCHER_VERSION,
    RELEASE_HISTORY,
    RELEASE_NOTES,
)
ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "artifacts" / "local-package"
APP_BINARY = "DeltaStatsApp.exe"
APP_RELEASE_BINARY = f"DeltaStatsApp-{APP_VERSION}.exe"
APP_PACKAGE_FORMAT = "pyinstaller-onedir-zip-v1"
APP_PACKAGE_ARCHIVE = f"DeltaStatsAppDir-{APP_VERSION}.zip"
LAUNCHER_RELEASE_BINARY = f"DeltaStatsLauncher-{LAUNCHER_VERSION}.exe"
DOWNLOAD_ARCHIVE = f"DeltaStatsAssistant-{APP_VERSION}.zip"
INSTALLER_BINARY = f"DeltaStatsAssistant-{APP_VERSION}-Setup.exe"
APP_ICON = ROOT / "web" / "assets" / "app-icon.ico"
INSTALLER_SCRIPT = ROOT / "installer" / "DeltaStatsAssistant.iss"
DEFAULT_UPDATE_BASE_URL = "https://zhou.opendeep.top"


def pyinstaller(*arguments: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", *arguments],
        cwd=ROOT,
        check=True,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def update_base_urls() -> list[str]:
    primary = os.environ.get("DELTA_UPDATE_BASE_URL", "").strip() or DEFAULT_UPDATE_BASE_URL
    alternate_value = os.environ.get("DELTA_UPDATE_ALTERNATE_BASE_URLS", "")
    candidates = [primary, *alternate_value.replace("\r", "\n").replace("\n", ",").split(",")]
    urls: list[str] = []
    for candidate in candidates:
        normalized = candidate.strip().rstrip("/")
        if not normalized:
            continue
        parsed = urllib.parse.urlparse(normalized)
        if (
            parsed.scheme.casefold() != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError("update base URLs must be absolute HTTPS URLs without credentials, query, or fragment")
        if normalized not in urls:
            urls.append(normalized)
    if not urls:
        raise RuntimeError("at least one update base URL is required")
    return urls


def artifact_download_urls(base_urls: list[str], filename: str) -> list[str]:
    return [f"{base_url}/updates/{filename}" for base_url in base_urls]


def inno_setup_compiler() -> Path:
    configured = os.environ.get("INNO_SETUP_COMPILER", "").strip()
    candidates = [Path(configured)] if configured else []
    executable = shutil.which("ISCC.exe") or shutil.which("iscc")
    if executable:
        candidates.append(Path(executable))
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        candidates.append(Path(local_app_data) / "Programs" / "Inno Setup 6" / "ISCC.exe")
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(variable, "").strip()
        if root:
            candidates.append(Path(root) / "Inno Setup 6" / "ISCC.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        "未找到 Inno Setup 6 编译器。请安装 Inno Setup，或设置 INNO_SETUP_COMPILER。"
    )


def build_installer(source_dir: Path, output_dir: Path) -> Path:
    if not INSTALLER_SCRIPT.is_file():
        raise RuntimeError(f"安装器脚本不存在：{INSTALLER_SCRIPT}")
    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(inno_setup_compiler()),
            f"/DAppVersion={APP_VERSION}",
            f"/DSourceDir={source_dir}",
            f"/DOutputDir={output_dir}",
            f"/DIconFile={APP_ICON}",
            str(INSTALLER_SCRIPT),
        ],
        cwd=ROOT,
        check=True,
    )
    installer = output_dir / INSTALLER_BINARY
    if not installer.is_file() or installer.stat().st_size == 0:
        raise RuntimeError(f"安装器构建未生成：{installer}")
    return installer


def release_history_payload() -> list[dict[str, object]]:
    history: list[dict[str, object]] = []
    seen_versions: set[str] = set()
    for record in RELEASE_HISTORY:
        if not isinstance(record, dict):
            raise RuntimeError("RELEASE_HISTORY entries must be objects")
        version = record.get("version")
        notes = record.get("release_notes")
        if (
            not isinstance(version, str)
            or not version.strip()
            or version in seen_versions
            or not isinstance(notes, (tuple, list))
            or not notes
            or any(not isinstance(note, str) or not note.strip() for note in notes)
        ):
            raise RuntimeError("RELEASE_HISTORY must contain unique versions with non-empty notes")
        seen_versions.add(version)
        history.append({"version": version, "release_notes": list(notes)})
    if not history or history[-1] != {
        "version": APP_VERSION,
        "release_notes": list(RELEASE_NOTES),
    }:
        raise RuntimeError("RELEASE_HISTORY must end with the current version and RELEASE_NOTES")
    return history


def main() -> int:
    release_notes = list(RELEASE_NOTES)
    if not release_notes or any(
        not isinstance(note, str) or not note.strip() for note in release_notes
    ):
        raise RuntimeError("RELEASE_NOTES must contain at least one non-empty release note")
    release_history = release_history_payload()
    legacy_release_notes = [
        f"v{record['version']}：{note}"
        for record in release_history
        for note in record["release_notes"]
    ]
    if len(legacy_release_notes) > 32 or any(
        len(note) > 500 for note in legacy_release_notes
    ):
        raise RuntimeError(
            "RELEASE_HISTORY is too large for clients that only support release_notes"
        )

    update_urls = update_base_urls()
    version_url = f"{update_urls[0]}/updates/version.json"
    launcher_urls = artifact_download_urls(update_urls, LAUNCHER_RELEASE_BINARY)
    package_urls = artifact_download_urls(update_urls, APP_PACKAGE_ARCHIVE)

    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    dist = OUTPUT / "dist"
    app_dir = dist / "app"
    app_version_dir = app_dir / "versions" / APP_VERSION
    onedir_output = OUTPUT / "onedir-app"
    legacy_output = OUTPUT / "legacy-app"
    installer_output = OUTPUT / "installer"
    release = OUTPUT / "release"
    app_version_dir.parent.mkdir(parents=True)
    release.mkdir(parents=True)

    with tempfile.TemporaryDirectory(prefix="delta-local-build-") as temporary:
        temporary_path = Path(temporary)
        bootstrap = temporary_path / "update-bootstrap.json"
        bootstrap.write_text(
            json.dumps({"version_url": version_url}, separators=(",", ":")),
            encoding="utf-8",
        )

        pyinstaller(
            "--onedir",
            "--contents-directory",
            "_internal",
            "--windowed",
            "--icon",
            str(APP_ICON),
            "--name",
            Path(APP_BINARY).stem,
            "--distpath",
            str(onedir_output),
            "--workpath",
            str(OUTPUT / "build-app-onedir"),
            "--specpath",
            str(OUTPUT),
            "--collect-all",
            "tzdata",
            "--collect-all",
            "webview",
            "--add-data",
            f"{ROOT / 'web'}{os.pathsep}web",
            "--add-data",
            f"{bootstrap}{os.pathsep}.",
            str(ROOT / "friend_client.py"),
        )
        pyinstaller(
            "--onefile",
            "--windowed",
            "--icon",
            str(APP_ICON),
            "--name",
            Path(APP_BINARY).stem,
            "--distpath",
            str(legacy_output),
            "--workpath",
            str(OUTPUT / "build-app-legacy"),
            "--specpath",
            str(OUTPUT),
            "--collect-all",
            "tzdata",
            "--collect-all",
            "webview",
            "--add-data",
            f"{ROOT / 'web'}{os.pathsep}web",
            "--add-data",
            f"{bootstrap}{os.pathsep}.",
            str(ROOT / "friend_client.py"),
        )
        pyinstaller(
            "--onefile",
            "--windowed",
            "--icon",
            str(APP_ICON),
            "--name",
            "三角洲情报助手",
            "--distpath",
            str(dist),
            "--workpath",
            str(OUTPUT / "build-launcher"),
            "--specpath",
            str(OUTPUT),
            str(ROOT / "launcher.py"),
        )

    built_onedir = onedir_output / Path(APP_BINARY).stem
    if not (built_onedir / APP_BINARY).is_file():
        raise RuntimeError(f"onedir build did not produce {built_onedir / APP_BINARY}")
    shutil.move(str(built_onedir), str(app_version_dir))
    for destination in (dist, app_version_dir):
        for notice in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
            shutil.copy2(ROOT / notice, destination / notice)
        shutil.copytree(ROOT / "licenses", destination / "licenses")
    (app_dir / "current.json").write_text(
        json.dumps(
            {"schema": 1, "version": APP_VERSION},
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    instructions = dist / "使用说明.txt"
    instructions.write_text(
        "1. 先解压整个 ZIP，不要单独移动 EXE 或 app 文件夹。\n"
        "2. 在电脑版微信打开三角洲官方小程序，并切换到要读取的微信区或 QQ 区账号。\n"
        "3. 双击解压目录外层的启动程序。\n"
        "4. 首次使用在桌面窗口点击“读取当前小程序账号”；如果提示缺少登录态，点击“刷新登录态”，程序会拉起小程序并自动等待、添加账号。\n"
        "5. 点击右上角同步按钮，直接在同一窗口查看和筛选。\n"
        "6. 已保存账号可点击顶栏账号名称，在“账号管理”中直接切换。\n"
        "7. 添加新账号时先在官方小程序切换，再读取当前账号；已保存账号登录过期时，按弹窗点击“刷新登录态”，确认原账号后程序会等待并自动刷新。\n"
        "8. 以后打开时如果有新版本，按提示更新即可。\n",
        encoding="utf-8",
    )
    built_installer = build_installer(dist, installer_output)

    release_app = release / APP_RELEASE_BINARY
    release_package = release / APP_PACKAGE_ARCHIVE
    release_launcher = release / LAUNCHER_RELEASE_BINARY
    shutil.copy2(legacy_output / APP_BINARY, release_app)
    shutil.copy2(dist / "三角洲情报助手.exe", release_launcher)
    shutil.make_archive(
        str(release_package.with_suffix("")),
        "zip",
        root_dir=app_version_dir,
    )
    archive = shutil.make_archive(str(OUTPUT / APP_DISPLAY_NAME), "zip", dist)
    release_archive = release / DOWNLOAD_ARCHIVE
    release_installer = release / INSTALLER_BINARY
    shutil.copy2(archive, release_archive)
    shutil.copy2(built_installer, release_installer)
    (release / "version.json").write_text(
        json.dumps(
            {
                "version": APP_VERSION,
                "release_notes": legacy_release_notes,
                "release_history": release_history,
                "file": APP_RELEASE_BINARY,
                "file_size": release_app.stat().st_size,
                "file_sha256": sha256_file(release_app),
                "launcher_version": LAUNCHER_VERSION,
                "launcher_file": LAUNCHER_RELEASE_BINARY,
                "launcher_url": launcher_urls[0],
                "launcher_urls": launcher_urls,
                "launcher_size": release_launcher.stat().st_size,
                "launcher_sha256": sha256_file(release_launcher),
                "launcher_protocol": LAUNCHER_PROTOCOL,
                "package_format": APP_PACKAGE_FORMAT,
                "package_file": APP_PACKAGE_ARCHIVE,
                "package_url": package_urls[0],
                "package_urls": package_urls,
                "package_size": release_package.stat().st_size,
                "package_sha256": sha256_file(release_package),
                "update_mode": "prompt",
                "download_file": DOWNLOAD_ARCHIVE,
                "download_size": release_archive.stat().st_size,
                "download_sha256": sha256_file(release_archive),
                "installer_file": INSTALLER_BINARY,
                "installer_size": release_installer.stat().st_size,
                "installer_sha256": sha256_file(release_installer),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    print(json.dumps({"ok": True, "version": APP_VERSION, "archive": archive, "release": str(release)}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
