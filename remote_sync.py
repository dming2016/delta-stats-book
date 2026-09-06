#!/usr/bin/env python3
"""Upload the local match cache to the private shared server."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import requests

from account_storage import account_id
from delta_api import load_auth
from delta_data import read_cache
from secure_store import exists, read_json, write_json


REMOTE_FILE = "remote_sync.dat"
REMOTE_ENTROPY = "DeltaForceIntelAssistant:remote:v1"


def player_identity(auth: dict[str, Any]) -> str:
    return account_id(auth)


def configure(url: str, token: str, display_name: str) -> None:
    url = url.strip().rstrip("/")
    token = token.strip()
    display_name = display_name.strip()
    if not url.startswith("https://"):
        raise ValueError("远程地址必须使用 HTTPS")
    if not token:
        raise ValueError("上传令牌不能为空")
    if not display_name or len(display_name) > 32:
        raise ValueError("显示名称长度必须为 1-32 个字符")
    write_json(
        REMOTE_FILE,
        {"url": url, "token": token, "display_name": display_name},
        REMOTE_ENTROPY,
    )


def remote_status() -> dict[str, Any]:
    if not exists(REMOTE_FILE):
        return {"enabled": False}
    config = read_json(REMOTE_FILE, REMOTE_ENTROPY)
    return {
        "enabled": True,
        "display_name": config.get("display_name"),
        "url": config.get("url"),
    }


def upload_cache(cache: dict[str, Any] | None = None) -> dict[str, Any]:
    if not exists(REMOTE_FILE):
        return {"enabled": False}
    config = read_json(REMOTE_FILE, REMOTE_ENTROPY)
    auth = load_auth()
    payload = cache if cache is not None else read_cache(auth)
    expected_account_id = player_identity(auth)
    if payload.get("account_id") != expected_account_id:
        raise ValueError("本地战绩缓存属于其他账号，已停止上传")
    body = {
        "player_id": expected_account_id,
        "display_name": config["display_name"],
        "updated_at": payload.get("updated_at"),
        "matches": payload.get("matches", []),
    }
    response = requests.post(
        f"{config['url']}/api/upload",
        headers={"Authorization": f"Bearer {config['token']}"},
        json=body,
        timeout=90,
    )
    response.raise_for_status()
    result = response.json()
    return {"enabled": True, **result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("configure", "status", "upload"))
    parser.add_argument("--name")
    args = parser.parse_args()
    if args.action == "configure":
        url = os.environ.get("DELTA_REMOTE_URL", "")
        token = os.environ.get("DELTA_REMOTE_TOKEN", "")
        configure(url, token, args.name or "")
        result = remote_status()
    elif args.action == "upload":
        result = upload_cache()
    else:
        result = remote_status()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
