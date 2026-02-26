from __future__ import annotations

import ctypes
import json
import os
import re
import string
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.request import Request, urlopen


def now_ts() -> float:
    return time.time()


def iso_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def day_key_local() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def parse_iso_or_epoch_to_ms(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        x = float(value)
        if x <= 0:
            return None
        if x > 10_000_000_000:
            return int(x)
        return int(x * 1000)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return parse_iso_or_epoch_to_ms(float(s))
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            return None
    return None


def env_str(name: str) -> str:
    return str(os.environ.get(name, "") or "").strip()


def user_env_from_registry(name: str) -> str:
    if not sys.platform.startswith("win"):
        return ""
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
            v, _t = winreg.QueryValueEx(k, name)
            return str(v or "").strip()
    except Exception:
        return ""


def env_int(name: str) -> Optional[int]:
    v = env_str(name)
    if not v:
        return None
    try:
        return int(float(v))
    except Exception:
        return None


def env_float(name: str) -> Optional[float]:
    v = env_str(name)
    if not v:
        return None
    try:
        return float(v)
    except Exception:
        return None


def env_bool(name: str) -> Optional[bool]:
    v = env_str(name).lower()
    if not v:
        return None
    if v in {"1", "true", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _post_json(url: str, payload: dict, timeout_sec: float, user_agent: str) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": user_agent,
        },
        method="POST",
    )
    with urlopen(req, timeout=timeout_sec) as _:
        return


def maybe_notify_discord(
    logger: Any,
    message: str,
    timeout_sec: float = 5.0,
    user_agent: str = "clob-bot/1.0",
) -> None:
    # Webhook URLs are secrets. Never print them (including indirectly via exception strings).
    url = (
        env_str("CLOBBOT_DISCORD_WEBHOOK_URL")
        or user_env_from_registry("CLOBBOT_DISCORD_WEBHOOK_URL")
        or env_str("DISCORD_WEBHOOK_URL")
        or user_env_from_registry("DISCORD_WEBHOOK_URL")
    )
    if not url:
        return

    mention = env_str("CLOBBOT_DISCORD_MENTION")
    content = f"{mention} {message}".strip() if mention else message

    def _send() -> None:
        try:
            _post_json(url, {"content": content}, timeout_sec=timeout_sec, user_agent=user_agent)
        except Exception as e:
            code = getattr(e, "code", None)
            if isinstance(code, int):
                logger.info(f"[{iso_now()}] notify(discord) failed: HTTP {code}")
            else:
                logger.info(f"[{iso_now()}] notify(discord) failed: {type(e).__name__}")

    threading.Thread(target=_send, daemon=True).start()


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi_unprotect(ciphertext: bytes) -> bytes:
    if not ciphertext:
        return b""
    if not sys.platform.startswith("win"):
        raise RuntimeError("DPAPI decrypt is only supported on Windows.")

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    CryptUnprotectData = crypt32.CryptUnprotectData
    CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(_DATA_BLOB),
    ]
    CryptUnprotectData.restype = ctypes.c_int

    LocalFree = kernel32.LocalFree
    LocalFree.argtypes = [ctypes.c_void_p]
    LocalFree.restype = ctypes.c_void_p

    buf = ctypes.create_string_buffer(ciphertext, len(ciphertext))
    in_blob = _DATA_BLOB(len(ciphertext), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DATA_BLOB()

    ok = CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            LocalFree(out_blob.pbData)


def load_powershell_dpapi_securestring_file(path: str) -> str:
    p = (path or "").strip()
    if not p:
        return ""
    try:
        raw = Path(p).read_text(encoding="ascii", errors="ignore").strip()
    except Exception:
        return ""

    hex_only = "".join(ch for ch in raw if ch in string.hexdigits)
    if len(hex_only) < 8 or (len(hex_only) % 2) != 0:
        return ""

    try:
        clear = _dpapi_unprotect(bytes.fromhex(hex_only))
    except Exception:
        return ""

    try:
        return clear.decode("utf-16-le").rstrip("\x00").strip()
    except Exception:
        try:
            return clear.decode("utf-8", errors="ignore").strip()
        except Exception:
            return ""


def load_plaintext_secret_file(path: str, mode: str = "raw") -> str:
    p = (path or "").strip()
    if not p:
        return ""
    try:
        raw = Path(p).read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return ""
    if not raw:
        return ""

    s = raw.splitlines()[0].strip()
    if mode == "hex_private_key":
        if re.fullmatch(r"[0-9a-fA-F]{64}", s or ""):
            s = "0x" + s
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", s or ""):
            return ""
    return s

