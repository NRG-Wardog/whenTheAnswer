from __future__ import annotations

import asyncio
import ctypes
import json
import os
import random
import re
import shutil
import subprocess
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional, Tuple

from .config import LOG_FILE, PLATFORM


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: Optional[datetime] = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds")


def parse_utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def jittered_seconds(base_seconds: float, ratio: float) -> float:
    ratio = clamp(ratio, 0.0, 0.5)
    offset = base_seconds * ratio
    return max(1.0, random.uniform(base_seconds - offset, base_seconds + offset))


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u200f", "").replace("\u200e", "")
    return re.sub(r"\s+", " ", text).strip()


def log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def desktop_notification(title: str, message: str) -> None:
    try:
        if PLATFORM == "windows":
            ctypes.windll.user32.MessageBoxW(
                None,
                message,
                title,
                0x00000000 | 0x00000040 | 0x00010000 | 0x00040000,
            )
            return
        if PLATFORM == "linux":
            notify_send = shutil.which("notify-send")
            if notify_send:
                subprocess.run(
                    [notify_send, title, message],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
    except Exception as exc:
        log(f"Desktop notification failed: {exc}")
    log(f"NOTIFICATION: {title} - {message}")


async def notify_async(title: str, message: str) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, desktop_notification, title, message)


def load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception as exc:
        raise RuntimeError(f"Could not read environment file '{path}': {exc}") from exc


def atomic_write_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"Could not read '{path}': {exc}")
        return default


def parse_retry_after(
    raw_value: Optional[str], now: Optional[datetime] = None
) -> Optional[int]:
    if not raw_value:
        return None
    value = raw_value.strip()
    current = now or utc_now()
    if value.isdigit():
        return max(0, int(value))
    try:
        retry_time = parsedate_to_datetime(value)
        if retry_time.tzinfo is None:
            retry_time = retry_time.replace(tzinfo=timezone.utc)
        seconds = int((retry_time.astimezone(timezone.utc) - current).total_seconds())
        return max(0, seconds)
    except (TypeError, ValueError, OverflowError):
        return None


def text_contains_any(text: str, patterns: Tuple[str, ...]) -> bool:
    lower = normalize_text(text).lower()
    return any(pattern.lower() in lower for pattern in patterns)


async def sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.0, seconds))
        return True
    except asyncio.TimeoutError:
        return False


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
