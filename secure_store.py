#!/usr/bin/env python3
"""Small Windows DPAPI JSON store used by the local stats service."""

from __future__ import annotations

import json
import msvcrt
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO

import win32crypt


APP_DIR = Path.home() / "AppData" / "Local" / "DeltaForceIntelAssistant"


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(path: Path) -> threading.Lock:
    key = str(path.resolve()).casefold()
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


class InterProcessFileLock:
    """Small Windows file lock that can be released from another thread."""

    def __init__(
        self,
        name: str,
        *,
        timeout: float | None = 10.0,
        poll_interval: float = 0.05,
        app_dir: Path = APP_DIR,
    ) -> None:
        self.path = Path(app_dir) / name
        self.timeout = timeout
        self.poll_interval = max(0.01, poll_interval)
        self._handle: BinaryIO | None = None
        self._local_lock: threading.Lock | None = None

    def acquire(self, timeout: float | None = None) -> bool:
        if self._handle is not None:
            return True
        effective_timeout = self.timeout if timeout is None else timeout
        deadline = (
            None
            if effective_timeout is None or effective_timeout < 0
            else time.monotonic() + effective_timeout
        )
        local_lock = _thread_lock(self.path)
        if deadline is None:
            acquired = local_lock.acquire()
        else:
            acquired = local_lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
        if not acquired:
            raise TimeoutError(f"timed out waiting for lock {self.path.name}")

        handle: BinaryIO | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    self._handle = handle
                    self._local_lock = local_lock
                    return True
                except OSError:
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(
                                f"timed out waiting for lock {self.path.name}"
                            )
                        time.sleep(min(self.poll_interval, remaining))
                    else:
                        time.sleep(self.poll_interval)
        except Exception:
            if handle is not None:
                handle.close()
            local_lock.release()
            raise

    def release(self) -> None:
        handle = self._handle
        local_lock = self._local_lock
        if handle is None or local_lock is None:
            return
        self._handle = None
        self._local_lock = None
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()
            local_lock.release()

    def is_locked(self) -> bool:
        return self._handle is not None

    def __enter__(self) -> "InterProcessFileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


def write_json(
    name: str,
    payload: dict[str, Any],
    entropy: str,
    *,
    app_dir: Path = APP_DIR,
) -> Path:
    path = Path(app_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encrypted = win32crypt.CryptProtectData(
        plain,
        "DeltaForceIntelAssistant",
        entropy.encode("utf-8"),
        None,
        None,
        0,
    )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        with InterProcessFileLock(
            f".{path.name}.write.lock",
            app_dir=path.parent,
        ):
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def read_json(
    name: str, entropy: str, *, app_dir: Path = APP_DIR
) -> dict[str, Any]:
    path = Path(app_dir) / name
    encrypted = path.read_bytes()
    _, plain = win32crypt.CryptUnprotectData(
        encrypted,
        entropy.encode("utf-8"),
        None,
        None,
        0,
    )
    value = json.loads(plain.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"DPAPI payload {name!r} is not a JSON object")
    return value


def exists(name: str, *, app_dir: Path = APP_DIR) -> bool:
    return (Path(app_dir) / name).is_file()
