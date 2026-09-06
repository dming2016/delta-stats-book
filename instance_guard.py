"""Windows named-mutex helpers for the single desktop application instance."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


APP_MUTEX_NAME = r"Local\DeltaStatsAssistant.App"
ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.OpenMutexW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    _kernel32.OpenMutexW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL
else:
    _kernel32 = None


def acquire_app_mutex() -> int | None:
    """Return the owned mutex handle, or None when another instance owns it."""
    if _kernel32 is None:
        return 1
    ctypes.set_last_error(0)
    handle = _kernel32.CreateMutexW(None, False, APP_MUTEX_NAME)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return None
    return int(handle)


def app_instance_running() -> bool:
    if _kernel32 is None:
        return False
    handle = _kernel32.OpenMutexW(SYNCHRONIZE, False, APP_MUTEX_NAME)
    if not handle:
        return False
    _kernel32.CloseHandle(handle)
    return True


def release_app_mutex(handle: int | None) -> None:
    if _kernel32 is not None and handle:
        _kernel32.CloseHandle(handle)
