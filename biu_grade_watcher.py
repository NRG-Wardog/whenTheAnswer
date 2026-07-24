#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
BIU In-Bar Grade Watcher v9
Cross-platform: Windows and Linux
Python 3.8+

Design goals
------------
- Conservative request pacing.
- Persistent browser profile and authenticated session.
- No CAPTCHA bypass, fingerprint spoofing, or anti-bot circumvention.
- Immediate detection of explicit blocking and rate limiting.
- Persistent circuit breaker with one controlled recovery probe.
- Exponential backoff for transient failures.
- Retry-After support.
- Randomized scheduling jitter to spread load.
- A single running instance per user.
- No storage of OTP values.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import os
import platform
import random
import re
import shutil
import subprocess
import traceback
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, AsyncIterator, Deque, Dict, List, Optional, Set, Tuple

from playwright.async_api import (
    BrowserContext,
    Locator,
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


# ---------------------------------------------------------------------------
# BIU endpoints and page identifiers
# ---------------------------------------------------------------------------

TARGET_URL = (
    "https://inbar.biu.ac.il/Live/"
    "StudentAssignmentTermList.aspx?edpr=3"
)

DIRECT_LOGIN_URL = (
    "https://inbar.biu.ac.il/Live/Login.aspx"
    "?ReturnUrl=/Live/StudentAssignmentTermList.aspx?edpr=3"
)

MY_BIU_URL = "https://my.biu.ac.il/"

TARGET_PATH = "StudentAssignmentTermList.aspx"
LOGIN_PATH = "Login.aspx"
TABLE_HINT = "gvStudentAssignmentTermList"


# ---------------------------------------------------------------------------
# Conservative defaults
# ---------------------------------------------------------------------------

DEFAULT_INTERVAL_MINUTES = 10
DEFAULT_KEEPALIVE_MINUTES = 2

MIN_GRADE_INTERVAL_MINUTES = 5
MIN_KEEPALIVE_INTERVAL_MINUTES = 2

DEFAULT_REQUEST_MIN_GAP_SECONDS = 12
DEFAULT_MAX_TOP_LEVEL_REQUESTS_PER_HOUR = 45

GRADE_JITTER_RATIO = 0.10
KEEPALIVE_JITTER_RATIO = 0.15

TRANSIENT_FAILURE_THRESHOLD = 3
TRANSIENT_BACKOFF_BASE_SECONDS = 60
TRANSIENT_BACKOFF_MAX_SECONDS = 60 * 60

RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS = 30 * 60
EXPLICIT_BLOCK_DEFAULT_COOLDOWN_SECONDS = 6 * 60 * 60

MAX_REAUTH_ATTEMPTS_PER_HOUR = 2
MIN_REAUTH_GAP_SECONDS = 10 * 60

LOGIN_TIMEOUT_SECONDS = 15 * 60
LOGIN_POLL_SECONDS = 2
ROW_STABILITY_ROUNDS = 3

PROTECTION_NOTIFICATION_COOLDOWN_SECONDS = 60 * 60


# ---------------------------------------------------------------------------
# Table headers
# ---------------------------------------------------------------------------

GRADE_HEADER_NAMES = {"ציון", "ציון סופי"}
COURSE_HEADER_NAMES = {"שם קבוצת קורס", "שם הקורס", "קורס"}
COURSE_CODE_HEADER_NAMES = {"קוד קבוצת קורס", "קוד קורס", "מספר קורס"}
DATE_HEADER_NAMES = {"תאריך", "תאריך בחינה"}
TERM_HEADER_NAMES = {"מועד", "תקופה"}
NOTEBOOK_HEADER_NAMES = {"מספר מחברת", "מחברת"}


# ---------------------------------------------------------------------------
# Block/challenge detection
# ---------------------------------------------------------------------------

BLOCK_STATUSES = {403, 406, 418, 423}
RATE_LIMIT_STATUSES = {429}
AUTH_STATUSES = {401}
TRANSIENT_STATUSES = {408, 425, 500, 502, 503, 504}

BLOCK_TEXT_PATTERNS = (
    "access denied",
    "request blocked",
    "temporarily blocked",
    "your request has been blocked",
    "unusual traffic",
    "automated requests",
    "verify you are human",
    "security challenge",
    "bot detection",
    "captcha",
    "forbidden",
    "הגישה נדחתה",
    "הבקשה נחסמה",
    "נחסמת",
    "אימות אנושי",
    "אימות שאתה אנושי",
    "אינך מורשה",
    "קפצ'ה",
)

RATE_LIMIT_TEXT_PATTERNS = (
    "too many requests",
    "rate limit",
    "retry later",
    "request limit",
    "יותר מדי בקשות",
    "חרגת ממספר הבקשות",
    "נסה שוב מאוחר יותר",
)


# ---------------------------------------------------------------------------
# Platform paths
# ---------------------------------------------------------------------------

APP_NAME_WINDOWS = "BIUGradeWatcher"
APP_NAME_LINUX = "biu-grade-watcher"


def current_platform() -> str:
    system = platform.system().lower()

    if system == "windows":
        return "windows"

    if system == "linux":
        return "linux"

    return "unsupported"


PLATFORM = current_platform()


def application_directory() -> Path:
    if PLATFORM == "windows":
        base = os.environ.get("LOCALAPPDATA")

        if not base:
            base = str(Path.home() / "AppData" / "Local")

        path = Path(base) / APP_NAME_WINDOWS

    elif PLATFORM == "linux":
        xdg_data_home = os.environ.get("XDG_DATA_HOME")

        if xdg_data_home:
            path = Path(xdg_data_home) / APP_NAME_LINUX
        else:
            path = Path.home() / ".local" / "share" / APP_NAME_LINUX

    else:
        raise RuntimeError(
            "Unsupported operating system: {}".format(platform.system())
        )

    path.mkdir(parents=True, exist_ok=True)
    return path


APP_DIR = application_directory()
PROFILE_DIR = APP_DIR / "browser_profile"
SNAPSHOT_FILE = APP_DIR / "grade_snapshot.json"
PROTECTION_FILE = APP_DIR / "protection_state.json"
LOG_FILE = APP_DIR / "watcher.log"
LOCK_FILE = APP_DIR / "watcher.lock"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class WatcherError(RuntimeError):
    pass


class SessionExpired(WatcherError):
    pass


class TransientFailure(WatcherError):
    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class ProtectionEvent(WatcherError):
    def __init__(
        self,
        message: str,
        kind: str,
        status: Optional[int] = None,
        retry_after_seconds: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after_seconds = retry_after_seconds


class CircuitOpen(WatcherError):
    def __init__(self, wait_seconds: int, reason: str) -> None:
        super().__init__(
            "Protection circuit is open for approximately {} second(s): {}"
            .format(wait_seconds, reason)
        )
        self.wait_seconds = wait_seconds
        self.reason = reason


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: Optional[datetime] = None) -> str:
    current = value or utc_now()
    return current.isoformat(timespec="seconds")


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


def canonical_header(value: str) -> str:
    return normalize_text(value).replace(":", "")


def is_grade_header(header: str) -> bool:
    header = canonical_header(header)
    return header in GRADE_HEADER_NAMES or "ציון" in header


def has_meaningful_grade(row: Dict[str, str]) -> bool:
    return any(
        is_grade_header(header) and normalize_text(value)
        for header, value in row.items()
    )


def get_first_value(row: Dict[str, str], names: Set[str]) -> str:
    for name in names:
        value = normalize_text(row.get(name, ""))

        if value:
            return value

    return ""


def log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[{}] {}".format(stamp, message)
    print(line, flush=True)

    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def desktop_notification(title: str, message: str) -> None:
    try:
        if PLATFORM == "windows":
            MB_OK = 0x00000000
            MB_ICONINFORMATION = 0x00000040
            MB_SETFOREGROUND = 0x00010000
            MB_TOPMOST = 0x00040000

            ctypes.windll.user32.MessageBoxW(
                None,
                message,
                title,
                (
                    MB_OK
                    | MB_ICONINFORMATION
                    | MB_SETFOREGROUND
                    | MB_TOPMOST
                ),
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
        log("Desktop notification failed: {}".format(exc))

    log("NOTIFICATION: {} - {}".format(title, message))


async def notify_async(title: str, message: str) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, desktop_notification, title, message)


def load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return

    try:
        for raw_line in path.read_text(
            encoding="utf-8-sig"
        ).splitlines():
            line = raw_line.strip()

            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()

            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {"'", '"'}
            ):
                value = value[1:-1]

            if key and key not in os.environ:
                os.environ[key] = value

    except Exception as exc:
        raise RuntimeError(
            "Could not read environment file '{}': {}".format(path, exc)
        )


def atomic_write_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))

    except Exception as exc:
        log("Could not read '{}': {}".format(path, exc))
        return default


def parse_retry_after(
    raw_value: Optional[str],
    now: Optional[datetime] = None,
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

        seconds = int(
            (retry_time.astimezone(timezone.utc) - current).total_seconds()
        )
        return max(0, seconds)

    except (TypeError, ValueError, OverflowError):
        return None


def text_contains_any(text: str, patterns: Tuple[str, ...]) -> bool:
    lower = normalize_text(text).lower()
    return any(pattern.lower() in lower for pattern in patterns)


async def sleep_or_stop(
    stop_event: asyncio.Event,
    seconds: float,
) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.0, seconds))
        return True

    except asyncio.TimeoutError:
        return False


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------

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


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def acquire(self) -> None:
        for _ in range(2):
            try:
                descriptor = os.open(
                    str(self.path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )

                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "pid": os.getpid(),
                                "created_at": utc_iso(),
                            }
                        )
                    )

                self.acquired = True
                return

            except FileExistsError:
                data = read_json(self.path, {})
                existing_pid = int(data.get("pid", 0) or 0)

                if process_exists(existing_pid):
                    raise RuntimeError(
                        "Another BIU Grade Watcher instance is already "
                        "running with PID {}.".format(existing_pid)
                    )

                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass

        raise RuntimeError("Could not acquire the watcher instance lock.")

    def release(self) -> None:
        if not self.acquired:
            return

        try:
            data = read_json(self.path, {})

            if int(data.get("pid", 0) or 0) == os.getpid():
                self.path.unlink()

        except FileNotFoundError:
            pass

        finally:
            self.acquired = False

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Persistent circuit breaker
# ---------------------------------------------------------------------------

@dataclass
class ProtectionState:
    circuit_state: str = "CLOSED"
    consecutive_failures: int = 0
    opened_count: int = 0
    cooldown_until: Optional[str] = None
    last_reason: str = ""
    last_status: Optional[int] = None
    last_event_at: Optional[str] = None
    last_notification_at: Optional[str] = None
    half_open_probe_used: bool = False


class ProtectionController:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.state = self._load()
        self._lock = asyncio.Lock()

    def _load(self) -> ProtectionState:
        raw = read_json(self.path, {})

        if not isinstance(raw, dict):
            return ProtectionState()

        allowed = set(ProtectionState.__dataclass_fields__.keys())
        filtered = {
            key: value
            for key, value in raw.items()
            if key in allowed
        }

        try:
            state = ProtectionState(**filtered)
        except TypeError:
            state = ProtectionState()

        if state.circuit_state not in {"CLOSED", "OPEN", "HALF_OPEN"}:
            state.circuit_state = "CLOSED"

        return state

    def save(self) -> None:
        atomic_write_json(self.path, asdict(self.state))

    def status_dict(self) -> Dict[str, Any]:
        payload = asdict(self.state)
        payload["seconds_until_probe"] = self.seconds_until_probe()
        return payload

    def seconds_until_probe(self) -> int:
        cooldown = parse_utc(self.state.cooldown_until)

        if cooldown is None:
            return 0

        return max(0, int((cooldown - utc_now()).total_seconds()))

    async def prepare_activity(self, allow_recovery_probe: bool) -> None:
        async with self._lock:
            if self.state.circuit_state == "CLOSED":
                return

            remaining = self.seconds_until_probe()

            if self.state.circuit_state == "OPEN":
                if remaining > 0:
                    raise CircuitOpen(remaining, self.state.last_reason)

                if not allow_recovery_probe:
                    raise CircuitOpen(60, self.state.last_reason)

                self.state.circuit_state = "HALF_OPEN"
                self.state.half_open_probe_used = False
                self.save()

            if self.state.circuit_state == "HALF_OPEN":
                if not allow_recovery_probe:
                    raise CircuitOpen(60, self.state.last_reason)

                # A HALF_OPEN probe represents one serialized recovery
                # workflow, not one individual HTTP navigation. The request
                # gate prevents another workflow from running concurrently.
                return

    async def record_success(self) -> None:
        async with self._lock:
            was_protected = self.state.circuit_state != "CLOSED"

            self.state.circuit_state = "CLOSED"
            self.state.consecutive_failures = 0
            self.state.opened_count = 0
            self.state.cooldown_until = None
            self.state.last_reason = ""
            self.state.last_status = None
            self.state.half_open_probe_used = False
            self.save()

            if was_protected:
                log("Protection circuit closed after a successful recovery.")

    async def record_transient_failure(
        self,
        reason: str,
        status: Optional[int] = None,
    ) -> int:
        async with self._lock:
            self.state.consecutive_failures += 1
            self.state.last_reason = reason
            self.state.last_status = status
            self.state.last_event_at = utc_iso()

            should_open = (
                self.state.circuit_state == "HALF_OPEN"
                or self.state.consecutive_failures
                >= TRANSIENT_FAILURE_THRESHOLD
            )

            if not should_open:
                self.save()
                return 0

            exponent = max(
                0,
                self.state.consecutive_failures
                - TRANSIENT_FAILURE_THRESHOLD,
            )

            cooldown = min(
                TRANSIENT_BACKOFF_MAX_SECONDS,
                TRANSIENT_BACKOFF_BASE_SECONDS * (2 ** exponent),
            )

            cooldown = int(jittered_seconds(cooldown, 0.20))
            self._open_unlocked(reason, status, cooldown)
            return cooldown

    async def record_protection_event(
        self,
        event: ProtectionEvent,
    ) -> int:
        async with self._lock:
            if event.retry_after_seconds is not None:
                cooldown = event.retry_after_seconds

            elif event.kind == "rate_limit":
                cooldown = RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS

            else:
                cooldown = EXPLICIT_BLOCK_DEFAULT_COOLDOWN_SECONDS

            # Repeated explicit events increase the cooldown, but remain capped.
            multiplier = min(4, max(1, self.state.opened_count + 1))
            cooldown = min(24 * 60 * 60, cooldown * multiplier)
            cooldown = int(jittered_seconds(cooldown, 0.10))

            self._open_unlocked(
                str(event),
                event.status,
                cooldown,
            )
            return cooldown

    def _open_unlocked(
        self,
        reason: str,
        status: Optional[int],
        cooldown_seconds: int,
    ) -> None:
        self.state.circuit_state = "OPEN"
        self.state.opened_count += 1
        self.state.cooldown_until = utc_iso(
            utc_now() + timedelta(seconds=max(60, cooldown_seconds))
        )
        self.state.last_reason = reason
        self.state.last_status = status
        self.state.last_event_at = utc_iso()
        self.state.half_open_probe_used = False
        self.save()

        log(
            "Protection circuit opened for approximately {} minute(s): {}"
            .format(max(1, cooldown_seconds // 60), reason)
        )

    async def should_notify(self) -> bool:
        async with self._lock:
            previous = parse_utc(self.state.last_notification_at)

            if previous is not None:
                elapsed = (utc_now() - previous).total_seconds()

                if elapsed < PROTECTION_NOTIFICATION_COOLDOWN_SECONDS:
                    return False

            self.state.last_notification_at = utc_iso()
            self.save()
            return True

    def clear(self) -> None:
        self.state = ProtectionState()
        self.save()


# ---------------------------------------------------------------------------
# Request pacing and hourly request budget
# ---------------------------------------------------------------------------

class RequestGate:
    """
    Serializes top-level BIU operations.

    This does not count every browser subresource. It prevents the watcher's
    explicit reload, navigation, and keepalive operations from overlapping or
    occurring too aggressively.
    """

    def __init__(
        self,
        minimum_gap_seconds: int,
        maximum_requests_per_hour: int,
    ) -> None:
        self.minimum_gap_seconds = minimum_gap_seconds
        self.maximum_requests_per_hour = maximum_requests_per_hour
        self._lock = asyncio.Lock()
        self._timestamps: Deque[float] = deque()
        self._last_started_at = 0.0

    @asynccontextmanager
    async def slot(self, label: str) -> AsyncIterator[None]:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()

            while self._timestamps and now - self._timestamps[0] >= 3600:
                self._timestamps.popleft()

            if len(self._timestamps) >= self.maximum_requests_per_hour:
                wait_seconds = 3600 - (now - self._timestamps[0])
                wait_seconds = max(1.0, wait_seconds)

                log(
                    "Hourly request budget reached. Waiting {:.0f} second(s)."
                    .format(wait_seconds)
                )
                await asyncio.sleep(wait_seconds)
                now = loop.time()

                while self._timestamps and now - self._timestamps[0] >= 3600:
                    self._timestamps.popleft()

            gap_remaining = (
                self.minimum_gap_seconds
                - (now - self._last_started_at)
            )

            if gap_remaining > 0:
                await asyncio.sleep(
                    jittered_seconds(gap_remaining, 0.10)
                )

            started = loop.time()
            self._last_started_at = started
            self._timestamps.append(started)

            log("Starting paced BIU operation: {}.".format(label))
            yield


# ---------------------------------------------------------------------------
# Authentication attempt budget
# ---------------------------------------------------------------------------

class AuthenticationBudget:
    def __init__(self) -> None:
        self._attempts: Deque[float] = deque()
        self._last_attempt_at = 0.0

    def record_or_raise(self, initial_login: bool = False) -> None:
        now = asyncio.get_running_loop().time()

        while self._attempts and now - self._attempts[0] >= 3600:
            self._attempts.popleft()

        if len(self._attempts) >= MAX_REAUTH_ATTEMPTS_PER_HOUR:
            raise ProtectionEvent(
                "Automatic authentication attempt budget exhausted.",
                kind="rate_limit",
                retry_after_seconds=60 * 60,
            )

        if (
            not initial_login
            and self._last_attempt_at > 0
            and now - self._last_attempt_at < MIN_REAUTH_GAP_SECONDS
        ):
            remaining = int(
                MIN_REAUTH_GAP_SECONDS - (now - self._last_attempt_at)
            )
            raise ProtectionEvent(
                "Authentication was requested again too soon.",
                kind="rate_limit",
                retry_after_seconds=remaining,
            )

        self._attempts.append(now)
        self._last_attempt_at = now


# ---------------------------------------------------------------------------
# Snapshot handling
# ---------------------------------------------------------------------------

def read_snapshot() -> Optional[List[Dict[str, str]]]:
    data = read_json(SNAPSHOT_FILE, None)

    if not isinstance(data, list):
        return None

    return [
        {
            normalize_text(key): normalize_text(value)
            for key, value in item.items()
        }
        for item in data
        if isinstance(item, dict)
    ]


def write_snapshot(rows: List[Dict[str, str]]) -> None:
    atomic_write_json(SNAPSHOT_FILE, rows)


def reset_snapshot() -> None:
    if SNAPSHOT_FILE.exists():
        SNAPSHOT_FILE.unlink()
        log("The saved grade snapshot was reset.")
    else:
        log("No saved grade snapshot existed.")


def row_identity(row: Dict[str, str]) -> str:
    preferred = [
        get_first_value(row, DATE_HEADER_NAMES),
        get_first_value(row, COURSE_CODE_HEADER_NAMES),
        get_first_value(row, COURSE_HEADER_NAMES),
        get_first_value(row, TERM_HEADER_NAMES),
        get_first_value(row, NOTEBOOK_HEADER_NAMES),
        normalize_text(row.get("שעה", "")),
        normalize_text(row.get("שם המרצה", "")),
    ]

    meaningful = [part for part in preferred if part]

    if len(meaningful) < 2:
        meaningful = [
            "{}={}".format(header, value)
            for header, value in sorted(row.items())
            if value and not is_grade_header(header)
        ]

    return hashlib.sha256(
        "\x1f".join(meaningful).encode("utf-8")
    ).hexdigest()


def grade_values(row: Dict[str, str]) -> Dict[str, str]:
    return {
        canonical_header(header): normalize_text(value)
        for header, value in row.items()
        if is_grade_header(header)
    }


def describe_row(row: Dict[str, str]) -> str:
    course = get_first_value(row, COURSE_HEADER_NAMES) or "Unknown course"
    code = get_first_value(row, COURSE_CODE_HEADER_NAMES)
    date = get_first_value(row, DATE_HEADER_NAMES)
    term = get_first_value(row, TERM_HEADER_NAMES)

    parts = [course]

    if code:
        parts.append("Code {}".format(code))

    if date:
        parts.append(date)

    if term:
        parts.append(term)

    return " | ".join(parts)


def compare_snapshots(
    previous: List[Dict[str, str]],
    current: List[Dict[str, str]],
) -> List[str]:
    previous_by_id = {
        row_identity(row): row
        for row in previous
    }

    changes: List[str] = []

    for row in current:
        old_row = previous_by_id.get(row_identity(row))
        current_grades = grade_values(row)

        if old_row is None:
            if has_meaningful_grade(row):
                values = [
                    "{}: {}".format(header, value)
                    for header, value in current_grades.items()
                    if value
                ]

                changes.append(
                    "New grade\n{}\n{}".format(
                        describe_row(row),
                        " | ".join(values),
                    )
                )

            continue

        old_grades = grade_values(old_row)
        differences: List[str] = []

        for header in sorted(set(old_grades) | set(current_grades)):
            before = normalize_text(old_grades.get(header, ""))
            after = normalize_text(current_grades.get(header, ""))

            if before == after:
                continue

            if not before and after:
                differences.append(
                    "{} added: {}".format(header, after)
                )

            elif before and after:
                differences.append(
                    "{} changed from {} to {}".format(
                        header,
                        before,
                        after,
                    )
                )

            else:
                differences.append(
                    "{} removed; previous value was {}".format(
                        header,
                        before,
                    )
                )

        if differences:
            changes.append(
                "{}\n{}".format(
                    describe_row(row),
                    "\n".join(differences),
                )
            )

    return changes


# ---------------------------------------------------------------------------
# Page and response inspection
# ---------------------------------------------------------------------------

async def page_text_sample(page: Page, limit: int = 10000) -> str:
    try:
        return await page.evaluate(
            """
            limit => {
                const body = document.body;
                if (!body) return "";
                return (body.innerText || body.textContent || "")
                    .slice(0, limit);
            }
            """,
            limit,
        )

    except Exception:
        return ""


async def response_retry_after(response: Optional[Response]) -> Optional[int]:
    if response is None:
        return None

    try:
        headers = await response.all_headers()
        return parse_retry_after(headers.get("retry-after"))

    except Exception:
        return None


async def inspect_loaded_page(
    page: Page,
    response: Optional[Response],
    *,
    allow_login_page: bool,
    operation: str,
) -> None:
    status = response.status if response is not None else None
    retry_after = await response_retry_after(response)

    if status in RATE_LIMIT_STATUSES:
        raise ProtectionEvent(
            "{} returned HTTP {}.".format(operation, status),
            kind="rate_limit",
            status=status,
            retry_after_seconds=retry_after,
        )

    if status in BLOCK_STATUSES:
        raise ProtectionEvent(
            "{} returned HTTP {}.".format(operation, status),
            kind="blocked",
            status=status,
            retry_after_seconds=retry_after,
        )

    if status in AUTH_STATUSES:
        raise SessionExpired(
            "{} returned HTTP {}.".format(operation, status)
        )

    if status in TRANSIENT_STATUSES:
        if status == 503 and retry_after is not None:
            raise ProtectionEvent(
                "{} returned HTTP 503 with Retry-After."
                .format(operation),
                kind="rate_limit",
                status=status,
                retry_after_seconds=retry_after,
            )

        raise TransientFailure(
            "{} returned HTTP {}.".format(operation, status),
            status=status,
        )

    sample = await page_text_sample(page)
    title = ""

    try:
        title = await page.title()
    except Exception:
        pass

    combined = "{}\n{}\n{}".format(page.url, title, sample)

    if text_contains_any(combined, RATE_LIMIT_TEXT_PATTERNS):
        raise ProtectionEvent(
            "{} displayed a rate-limit response.".format(operation),
            kind="rate_limit",
            status=status,
            retry_after_seconds=retry_after,
        )

    if text_contains_any(combined, BLOCK_TEXT_PATTERNS):
        raise ProtectionEvent(
            "{} displayed a blocking or human-verification challenge."
            .format(operation),
            kind="blocked",
            status=status,
            retry_after_seconds=retry_after,
        )

    if not allow_login_page and LOGIN_PATH.lower() in page.url.lower():
        raise SessionExpired(
            "{} redirected to the BIU login page.".format(operation)
        )


def inspect_fetch_result(
    result: Dict[str, Any],
    operation: str,
) -> None:
    status = int(result.get("status", 0) or 0)
    url = normalize_text(result.get("url", ""))
    text = normalize_text(result.get("text", ""))
    retry_after = parse_retry_after(
        normalize_text(result.get("retry_after", ""))
    )

    combined = "{}\n{}".format(url, text)

    if status in RATE_LIMIT_STATUSES:
        raise ProtectionEvent(
            "{} returned HTTP {}.".format(operation, status),
            kind="rate_limit",
            status=status,
            retry_after_seconds=retry_after,
        )

    if status in BLOCK_STATUSES:
        raise ProtectionEvent(
            "{} returned HTTP {}.".format(operation, status),
            kind="blocked",
            status=status,
            retry_after_seconds=retry_after,
        )

    if status in AUTH_STATUSES:
        raise SessionExpired(
            "{} returned HTTP {}.".format(operation, status)
        )

    if status in TRANSIENT_STATUSES:
        if status == 503 and retry_after is not None:
            raise ProtectionEvent(
                "{} returned HTTP 503 with Retry-After."
                .format(operation),
                kind="rate_limit",
                status=status,
                retry_after_seconds=retry_after,
            )

        raise TransientFailure(
            "{} returned HTTP {}.".format(operation, status),
            status=status,
        )

    if text_contains_any(combined, RATE_LIMIT_TEXT_PATTERNS):
        raise ProtectionEvent(
            "{} displayed a rate-limit response.".format(operation),
            kind="rate_limit",
            status=status or None,
            retry_after_seconds=retry_after,
        )

    if text_contains_any(combined, BLOCK_TEXT_PATTERNS):
        raise ProtectionEvent(
            "{} displayed a blocking or human-verification challenge."
            .format(operation),
            kind="blocked",
            status=status or None,
            retry_after_seconds=retry_after,
        )

    if LOGIN_PATH.lower() in url.lower():
        raise SessionExpired(
            "{} redirected to the BIU login page.".format(operation)
        )

    if TABLE_HINT.lower() not in text.lower():
        raise SessionExpired(
            "{} did not return the authenticated grades page."
            .format(operation)
        )


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

def resolve_auth_method(argument: Optional[str]) -> str:
    if argument:
        return argument

    print("")
    print("Choose a BIU authentication method:")
    print("  1. Direct In-Bar login (fully headless)")
    print('  2. Manual login through the "My Bar-Ilan" portal')
    print("")

    while True:
        choice = input("Enter 1 or 2: ").strip()

        if choice == "1":
            return "direct"

        if choice == "2":
            return "my-biu"

        print("Invalid selection. Enter 1 or 2.")


async def choose_page(context: BrowserContext) -> Page:
    if not context.pages:
        return await context.new_page()

    for page in reversed(context.pages):
        if TARGET_PATH.lower() in page.url.lower():
            return page

    return context.pages[-1]


async def table_exists(page: Page) -> bool:
    try:
        return (
            await page.locator(
                "table[id*='{}']".format(TABLE_HINT)
            ).count()
            > 0
        )

    except Exception:
        return False


async def paced_navigate(
    page: Page,
    url: str,
    gate: RequestGate,
    protection: ProtectionController,
    *,
    operation: str,
    allow_login_page: bool,
    allow_recovery_probe: bool,
) -> Optional[Response]:
    await protection.prepare_activity(allow_recovery_probe)

    async with gate.slot(operation):
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )

        except PlaywrightTimeoutError as exc:
            raise TransientFailure(
                "{} timed out.".format(operation)
            ) from exc

    await inspect_loaded_page(
        page,
        response,
        allow_login_page=allow_login_page,
        operation=operation,
    )
    return response


async def first_visible(locator: Locator) -> Optional[Locator]:
    for index in range(await locator.count()):
        candidate = locator.nth(index)

        try:
            if (
                await candidate.is_visible()
                and await candidate.is_enabled()
            ):
                return candidate

        except Exception:
            continue

    return None


async def find_text_input(
    page: Page,
    label_patterns: List[str],
    fallback_index: Optional[int] = None,
) -> Optional[Locator]:
    for pattern in label_patterns:
        try:
            candidate = await first_visible(
                page.get_by_label(
                    re.compile(pattern, re.IGNORECASE)
                )
            )

            if candidate is not None:
                return candidate

        except Exception:
            pass

    candidates = page.locator(
        "input[type='text'],"
        "input[type='tel'],"
        "input[type='number'],"
        "input:not([type]),"
        "input[type='password']"
    )

    visible: List[Locator] = []

    for index in range(await candidates.count()):
        candidate = candidates.nth(index)

        try:
            if (
                await candidate.is_visible()
                and await candidate.is_enabled()
            ):
                visible.append(candidate)

        except Exception:
            pass

    if (
        fallback_index is not None
        and 0 <= fallback_index < len(visible)
    ):
        return visible[fallback_index]

    return None


async def find_submit_button(page: Page) -> Optional[Locator]:
    patterns = [
        r"המשך",
        r"כניסה",
        r"התחבר",
        r"אישור",
        r"שלח",
        r"continue",
        r"login",
        r"sign in",
        r"submit",
        r"next",
    ]

    for pattern in patterns:
        try:
            candidate = await first_visible(
                page.get_by_role(
                    "button",
                    name=re.compile(pattern, re.IGNORECASE),
                )
            )

            if candidate is not None:
                return candidate

        except Exception:
            pass

    return await first_visible(
        page.locator(
            "button, input[type='submit'], input[type='button']"
        )
    )


async def find_otp_input(page: Page) -> Optional[Locator]:
    candidate = await find_text_input(
        page,
        [
            r"קוד",
            r"אימות",
            r"חד.?פעמי",
            r"verification",
            r"otp",
            r"code",
        ],
    )

    if candidate is not None:
        return candidate

    inputs = page.locator(
        "input[type='text'], "
        "input[type='tel'], "
        "input[type='number'], "
        "input[type='password']"
    )

    visible: List[Locator] = []

    for index in range(await inputs.count()):
        item = inputs.nth(index)

        try:
            if (
                await item.is_visible()
                and await item.is_enabled()
            ):
                visible.append(item)

        except Exception:
            pass

    if len(visible) == 1:
        return visible[0]

    return None


async def wait_for_otp_or_grades(
    page: Page,
    timeout_seconds: int = 90,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds

    while loop.time() < deadline:
        await inspect_loaded_page(
            page,
            None,
            allow_login_page=True,
            operation="BIU login",
        )

        if await table_exists(page):
            return

        otp_input = await find_otp_input(page)

        if otp_input is not None:
            return

        await asyncio.sleep(1)

    raise TransientFailure(
        "The OTP field or grades page was not detected "
        "after login submission."
    )


def get_direct_credentials() -> Tuple[str, str]:
    student_id = (
        os.environ.get("BIU_ID")
        or os.environ.get("id")
        or os.environ.get("ID")
        or ""
    ).strip()

    phone = (
        os.environ.get("BIU_PHONE")
        or os.environ.get("phonenumber")
        or os.environ.get("phone")
        or os.environ.get("PHONE")
        or ""
    ).strip()

    missing = []

    if not student_id:
        missing.append("BIU_ID or id")

    if not phone:
        missing.append("BIU_PHONE or phonenumber")

    if missing:
        raise RuntimeError(
            "Missing required login value(s): {}.".format(
                ", ".join(missing)
            )
        )

    return student_id, phone


async def direct_login_headless(
    page: Page,
    gate: RequestGate,
    protection: ProtectionController,
    auth_budget: AuthenticationBudget,
    *,
    initial_login: bool,
) -> Page:
    auth_budget.record_or_raise(initial_login=initial_login)
    student_id, phone = get_direct_credentials()

    log("Starting direct BIU login in headless mode.")

    await paced_navigate(
        page,
        DIRECT_LOGIN_URL,
        gate,
        protection,
        operation="direct login page",
        allow_login_page=True,
        allow_recovery_probe=True,
    )

    id_input = await find_text_input(
        page,
        [
            r"ת.?ז",
            r"תעודת זהות",
            r"דרכון",
            r"passport",
            r"identity",
        ],
        fallback_index=0,
    )

    phone_input = await find_text_input(
        page,
        [
            r"טלפון",
            r"נייד",
            r"phone",
            r"mobile",
        ],
        fallback_index=1,
    )

    if id_input is None or phone_input is None:
        raise TransientFailure(
            "Could not identify the ID and phone fields "
            "on the BIU login page."
        )

    await id_input.fill(student_id)
    await phone_input.fill(phone)

    submit = await find_submit_button(page)

    if submit is None:
        raise TransientFailure(
            "Could not identify the submit button "
            "on the BIU login page."
        )

    log("ID and phone number were filled automatically.")

    async with gate.slot("submit direct login"):
        await submit.click()

    await wait_for_otp_or_grades(page)

    if await table_exists(page):
        await protection.record_success()
        log("Direct BIU authentication completed without a new OTP.")
        return page

    otp_input = await find_otp_input(page)

    if otp_input is None:
        raise TransientFailure(
            "Could not identify the one-time verification code field."
        )

    otp = input(
        "Enter the one-time verification code sent by BIU: "
    ).strip()

    if not otp:
        raise RuntimeError(
            "No one-time verification code was entered."
        )

    await otp_input.fill(otp)

    otp_submit = await find_submit_button(page)

    if otp_submit is None:
        raise TransientFailure(
            "Could not identify the OTP submit button."
        )

    async with gate.slot("submit OTP"):
        await otp_submit.click()

    log("The one-time verification code was submitted.")

    try:
        await page.wait_for_selector(
            "table[id*='{}']".format(TABLE_HINT),
            timeout=90_000,
        )

    except PlaywrightTimeoutError:
        await inspect_loaded_page(
            page,
            None,
            allow_login_page=True,
            operation="OTP submission",
        )

        await paced_navigate(
            page,
            TARGET_URL,
            gate,
            protection,
            operation="open grades after OTP",
            allow_login_page=False,
            allow_recovery_probe=True,
        )

    if not await table_exists(page):
        raise SessionExpired(
            "Authentication completed, but the grades table "
            "was not available."
        )

    await protection.record_success()
    log("Direct headless authentication completed successfully.")
    return page


async def wait_for_manual_login(
    context: BrowserContext,
    gate: RequestGate,
    protection: ProtectionController,
) -> Page:
    log("Waiting for manual BIU authentication.")
    log("No Enter key is required.")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + LOGIN_TIMEOUT_SECONDS
    last_target_attempt = 0.0

    while loop.time() < deadline:
        for candidate in reversed(context.pages):
            await inspect_loaded_page(
                candidate,
                None,
                allow_login_page=True,
                operation="manual login",
            )

            if await table_exists(candidate):
                await protection.record_success()
                log("Manual authentication was detected automatically.")
                return candidate

        if loop.time() - last_target_attempt >= 90:
            current = await choose_page(context)

            try:
                await paced_navigate(
                    current,
                    TARGET_URL,
                    gate,
                    protection,
                    operation="manual login verification",
                    allow_login_page=True,
                    allow_recovery_probe=True,
                )

                if await table_exists(current):
                    await protection.record_success()
                    log(
                        "Manual authentication was detected automatically."
                    )
                    return current

            except SessionExpired:
                pass

            last_target_attempt = loop.time()

        await asyncio.sleep(LOGIN_POLL_SECONDS)

    raise RuntimeError(
        "Manual login was not detected within {} minutes.".format(
            LOGIN_TIMEOUT_SECONDS // 60
        )
    )


async def minimize_browser(page: Page) -> None:
    try:
        cdp = await page.context.new_cdp_session(page)
        window = await cdp.send("Browser.getWindowForTarget")

        await cdp.send(
            "Browser.setWindowBounds",
            {
                "windowId": window["windowId"],
                "bounds": {"windowState": "minimized"},
            },
        )

        await cdp.detach()
        log("The authenticated browser was minimized.")

    except Exception as exc:
        log(
            "Could not minimize the browser automatically: {}. "
            "The watcher will continue.".format(exc)
        )


# ---------------------------------------------------------------------------
# Grade extraction
# ---------------------------------------------------------------------------

async def load_all_rows(page: Page) -> int:
    selector = "table[id*='{}']".format(TABLE_HINT)
    last_count = -1
    stable_rounds = 0

    while stable_rounds < ROW_STABILITY_ROUNDS:
        total = await page.locator(
            "{} tr".format(selector)
        ).count()

        count = max(0, total - 1)

        if count == last_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
            last_count = count

        await page.evaluate(
            """
            () => {
                window.scrollTo(
                    0,
                    document.documentElement.scrollHeight
                );

                for (const element of document.querySelectorAll("*")) {
                    const style = getComputedStyle(element);

                    const canScroll =
                        /(auto|scroll)/.test(style.overflowY) &&
                        element.scrollHeight > element.clientHeight;

                    if (canScroll) {
                        element.scrollTop = element.scrollHeight;
                    }
                }
            }
            """
        )

        await page.wait_for_timeout(800)

    await page.evaluate("window.scrollTo(0, 0)")
    log(
        "Grades table stabilized at {} data row(s).".format(
            last_count
        )
    )
    return last_count


async def extract_grade_rows(page: Page) -> List[Dict[str, str]]:
    selector = "table[id*='{}']".format(TABLE_HINT)
    table = page.locator(selector).first

    if await table.count() == 0:
        raise SessionExpired("The BIU grades table was not found.")

    await load_all_rows(page)

    result = await table.evaluate(
        """
        table => {
            const clean = value =>
                (value || "")
                    .replace(/[\\u200e\\u200f]/g, "")
                    .replace(/\\s+/g, " ")
                    .trim();

            const allRows = Array.from(
                table.querySelectorAll("tr")
            );

            if (allRows.length === 0) {
                return { headers: [], rows: [] };
            }

            const headerRow = allRows.find(
                row => row.querySelectorAll("th").length > 0
            ) || allRows[0];

            let headers = Array.from(
                headerRow.querySelectorAll("th, td")
            ).map(
                cell => clean(
                    cell.innerText || cell.textContent
                )
            );

            const dataRows = allRows.filter(row => {
                if (row === headerRow) {
                    return false;
                }

                if (
                    row.querySelectorAll(":scope > td").length === 0
                ) {
                    return false;
                }

                const cls = row.className || "";

                return (
                    /GridRow|AlternatingRow|GridAlternatingRow/i
                        .test(cls)
                    || row.querySelector(
                        "span[id*='gvStudentAssignmentTermList_']"
                    )
                    || row.querySelector(
                        "input[id*='gvStudentAssignmentTermList_']"
                    )
                );
            });

            const rows = dataRows.map(row => {
                const cells = Array.from(
                    row.querySelectorAll(":scope > td")
                );

                const values = cells.map(
                    cell => clean(
                        cell.innerText || cell.textContent
                    )
                );

                if (headers.length < values.length) {
                    const expanded = headers.slice();

                    for (
                        let index = expanded.length;
                        index < values.length;
                        index++
                    ) {
                        expanded.push("Column " + (index + 1));
                    }

                    headers = expanded;
                }

                const record = {};

                values.forEach((value, index) => {
                    let header =
                        headers[index]
                        || ("Column " + (index + 1));

                    if (
                        Object.prototype.hasOwnProperty.call(
                            record,
                            header
                        )
                    ) {
                        header =
                            header + " #" + (index + 1);
                    }

                    record[header] = value;
                });

                return record;
            });

            return { headers, rows };
        }
        """
    )

    headers = [
        canonical_header(header)
        for header in result.get("headers", [])
    ]

    rows: List[Dict[str, str]] = []

    for raw_row in result.get("rows", []):
        row = {
            canonical_header(header): normalize_text(value)
            for header, value in raw_row.items()
        }

        if any(row.values()):
            rows.append(row)

    if not rows:
        raise TransientFailure(
            "The grades table was found, but no rows were extracted."
        )

    if not any(is_grade_header(header) for header in headers):
        raise TransientFailure(
            "The grades table was found, but no grade column "
            "was identified."
        )

    return rows


async def perform_check(
    page: Page,
    gate: RequestGate,
    protection: ProtectionController,
) -> Tuple[List[Dict[str, str]], List[str], bool]:
    await protection.prepare_activity(allow_recovery_probe=True)

    async with gate.slot("grade check"):
        try:
            response = await page.reload(
                wait_until="domcontentloaded",
                timeout=60_000,
            )

        except PlaywrightTimeoutError as exc:
            raise TransientFailure(
                "The grades page reload timed out."
            ) from exc

    await inspect_loaded_page(
        page,
        response,
        allow_login_page=False,
        operation="grade check",
    )

    rows = await extract_grade_rows(page)
    previous = read_snapshot()

    if previous is None:
        write_snapshot(rows)
        await protection.record_success()
        return rows, [], True

    changes = compare_snapshots(previous, rows)
    write_snapshot(rows)
    await protection.record_success()

    return rows, changes, False


# ---------------------------------------------------------------------------
# Keepalive
# ---------------------------------------------------------------------------

async def browser_keepalive(
    page: Page,
    gate: RequestGate,
    protection: ProtectionController,
) -> None:
    """
    Use fetch() inside the authenticated browser page.

    This keeps the request within the same browser context and avoids creating
    a separate API client identity.
    """
    await protection.prepare_activity(allow_recovery_probe=False)

    async with gate.slot("session keepalive"):
        result = await page.evaluate(
            """
            async url => {
                try {
                    const response = await fetch(url, {
                        method: "GET",
                        credentials: "include",
                        cache: "no-store",
                        redirect: "follow",
                        headers: {
                            "Accept": "text/html,application/xhtml+xml"
                        }
                    });

                    const text = await response.text();

                    return {
                        status: response.status,
                        url: response.url,
                        retry_after:
                            response.headers.get("retry-after") || "",
                        text: text.slice(0, 12000)
                    };
                } catch (error) {
                    return {
                        status: 0,
                        url: "",
                        retry_after: "",
                        text: "",
                        error: String(error)
                    };
                }
            }
            """,
            TARGET_URL,
        )

    if result.get("error"):
        raise TransientFailure(
            "Browser keepalive failed: {}".format(result["error"])
        )

    inspect_fetch_result(result, "session keepalive")


async def keepalive_loop(
    page: Page,
    gate: RequestGate,
    protection: ProtectionController,
    stop_event: asyncio.Event,
    interval_minutes: int,
) -> None:
    base_seconds = interval_minutes * 60

    while not stop_event.is_set():
        wait_seconds = jittered_seconds(
            base_seconds,
            KEEPALIVE_JITTER_RATIO,
        )

        if await sleep_or_stop(stop_event, wait_seconds):
            break

        try:
            await browser_keepalive(page, gate, protection)
            log("BIU session keepalive completed successfully.")

        except CircuitOpen:
            # Keepalive never consumes the single HALF_OPEN recovery probe.
            continue

        except SessionExpired:
            # The main grade loop owns reauthentication.
            log("Keepalive detected an expired BIU session.")
            continue

        except ProtectionEvent as event:
            cooldown = await protection.record_protection_event(event)

            if await protection.should_notify():
                await notify_async(
                    "BIU Grade Watcher paused",
                    (
                        "BIU returned a blocking or rate-limit response.\n"
                        "The watcher paused automatically for approximately "
                        "{} minute(s).\n\n"
                        "It will not attempt to bypass the restriction."
                    ).format(max(1, cooldown // 60)),
                )

        except TransientFailure as exc:
            cooldown = await protection.record_transient_failure(
                str(exc),
                exc.status,
            )

            if cooldown:
                log(
                    "Keepalive transient failures opened the circuit."
                )

        except Exception as exc:
            cooldown = await protection.record_transient_failure(
                "Unexpected keepalive failure: {}".format(exc)
            )

            if cooldown:
                log(
                    "Unexpected keepalive failures opened the circuit."
                )


# ---------------------------------------------------------------------------
# Main watcher
# ---------------------------------------------------------------------------

def make_notification_message(changes: List[str]) -> str:
    message = "\n\n".join(changes[:6])

    if len(changes) > 6:
        message += "\n\nAnd {} more change(s).".format(
            len(changes) - 6
        )

    if len(message) > 3500:
        message = message[:3500] + "\n\n[Message truncated]"

    return message


async def authenticate(
    auth_method: str,
    context: BrowserContext,
    page: Page,
    gate: RequestGate,
    protection: ProtectionController,
    auth_budget: AuthenticationBudget,
    *,
    initial_login: bool,
) -> Page:
    if auth_method == "direct":
        return await direct_login_headless(
            page,
            gate,
            protection,
            auth_budget,
            initial_login=initial_login,
        )

    auth_budget.record_or_raise(initial_login=initial_login)

    await paced_navigate(
        page,
        MY_BIU_URL,
        gate,
        protection,
        operation="open My Bar-Ilan",
        allow_login_page=True,
        allow_recovery_probe=True,
    )

    page = await wait_for_manual_login(
        context,
        gate,
        protection,
    )
    await minimize_browser(page)
    return page


async def handle_protection_event(
    protection: ProtectionController,
    event: ProtectionEvent,
) -> int:
    cooldown = await protection.record_protection_event(event)

    if await protection.should_notify():
        await notify_async(
            "BIU Grade Watcher paused",
            (
                "BIU returned a blocking or rate-limit response.\n"
                "The watcher paused automatically for approximately "
                "{} minute(s).\n\n"
                "Reason: {}\n"
                "No bypass attempt will be made."
            ).format(max(1, cooldown // 60), event),
        )

    return cooldown


async def wait_for_circuit_recovery(
    protection: ProtectionController,
    stop_event: asyncio.Event,
) -> bool:
    remaining = protection.seconds_until_probe()

    if remaining <= 0:
        return False

    log(
        "Protection circuit is OPEN. Next controlled probe in "
        "approximately {} minute(s).".format(max(1, remaining // 60))
    )

    return await sleep_or_stop(
        stop_event,
        min(remaining, 15 * 60),
    )


async def run(args: argparse.Namespace) -> int:
    dotenv_path = Path(args.env_file).expanduser().resolve()
    load_dotenv_file(dotenv_path)

    auth_method = resolve_auth_method(args.auth_method)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    headless = auth_method == "direct"

    protection = ProtectionController(PROTECTION_FILE)
    gate = RequestGate(
        minimum_gap_seconds=args.request_min_gap,
        maximum_requests_per_hour=args.max_requests_per_hour,
    )
    auth_budget = AuthenticationBudget()
    stop_event = asyncio.Event()

    async with async_playwright() as playwright:
        log("Starting the BIU browser session.")
        log("Operating system: {}.".format(platform.system()))
        log("Authentication method: {}.".format(auth_method))
        log(
            "Browser mode: {}.".format(
                "headless" if headless else "visible"
            )
        )
        log(
            "Safety pacing: minimum {} seconds between top-level "
            "operations, maximum {} per hour.".format(
                args.request_min_gap,
                args.max_requests_per_hour,
            )
        )

        # No stealth/fingerprint-spoofing flags are used.
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            viewport={"width": 1440, "height": 1000},
            locale="he-IL",
            timezone_id="Asia/Jerusalem",
        )

        keepalive_task: Optional[asyncio.Task] = None

        try:
            page = await choose_page(context)

            try:
                await paced_navigate(
                    page,
                    TARGET_URL,
                    gate,
                    protection,
                    operation="initial grades page",
                    allow_login_page=True,
                    allow_recovery_probe=True,
                )

                if (
                    not args.force_login
                    and await table_exists(page)
                ):
                    await protection.record_success()
                    log("The existing BIU session is valid.")

                else:
                    page = await authenticate(
                        auth_method,
                        context,
                        page,
                        gate,
                        protection,
                        auth_budget,
                        initial_login=True,
                    )

            except SessionExpired:
                page = await authenticate(
                    auth_method,
                    context,
                    page,
                    gate,
                    protection,
                    auth_budget,
                    initial_login=True,
                )

            keepalive_task = asyncio.create_task(
                keepalive_loop(
                    page,
                    gate,
                    protection,
                    stop_event,
                    args.keepalive,
                )
            )

            while not stop_event.is_set():
                if protection.state.circuit_state == "OPEN":
                    stopped = await wait_for_circuit_recovery(
                        protection,
                        stop_event,
                    )

                    if stopped:
                        break

                    continue

                log("Checking BIU In-Bar for new grades now.")

                try:
                    rows, changes, initialized = await perform_check(
                        page,
                        gate,
                        protection,
                    )

                    if initialized:
                        log(
                            "Initial snapshot saved successfully: {} rows. "
                            "No notification was generated."
                            .format(len(rows))
                        )

                    elif changes:
                        log(
                            "{} grade change(s) detected."
                            .format(len(changes))
                        )

                        await notify_async(
                            "BIU In-Bar grade update",
                            make_notification_message(changes),
                        )

                    else:
                        log(
                            "Check completed: {} rows, no new grades."
                            .format(len(rows))
                        )

                    if args.print_rows:
                        print(
                            json.dumps(
                                rows,
                                ensure_ascii=False,
                                indent=2,
                            )
                        )

                except SessionExpired as exc:
                    log("The BIU session expired: {}".format(exc))

                    try:
                        page = await authenticate(
                            auth_method,
                            context,
                            page,
                            gate,
                            protection,
                            auth_budget,
                            initial_login=False,
                        )

                    except ProtectionEvent as event:
                        await handle_protection_event(
                            protection,
                            event,
                        )

                    except Exception as auth_exc:
                        cooldown = await protection.record_transient_failure(
                            "Authentication failed: {}".format(auth_exc)
                        )

                        log(
                            "Authentication failed without automatic retry: {}"
                            .format(auth_exc)
                        )

                        if cooldown == 0:
                            # Prevent an immediate tight retry even before the
                            # transient threshold opens the circuit.
                            await sleep_or_stop(stop_event, 5 * 60)

                    continue

                except ProtectionEvent as event:
                    await handle_protection_event(
                        protection,
                        event,
                    )
                    continue

                except CircuitOpen:
                    continue

                except TransientFailure as exc:
                    cooldown = await protection.record_transient_failure(
                        str(exc),
                        exc.status,
                    )

                    log("Transient BIU failure: {}".format(exc))

                    if cooldown == 0:
                        delay = int(
                            jittered_seconds(
                                TRANSIENT_BACKOFF_BASE_SECONDS
                                * max(
                                    1,
                                    protection.state.consecutive_failures,
                                ),
                                0.20,
                            )
                        )

                        log(
                            "Waiting {} second(s) before continuing."
                            .format(delay)
                        )
                        await sleep_or_stop(stop_event, delay)

                    continue

                except Exception as exc:
                    cooldown = await protection.record_transient_failure(
                        "Unexpected check failure: {}".format(exc)
                    )

                    log(
                        "Unexpected check failure: {}\n{}"
                        .format(exc, traceback.format_exc())
                    )

                    if cooldown == 0:
                        await sleep_or_stop(stop_event, 5 * 60)

                    continue

                if args.once:
                    return 0

                wait_seconds = jittered_seconds(
                    args.interval * 60,
                    GRADE_JITTER_RATIO,
                )

                log(
                    "Waiting approximately {:.1f} minute(s) "
                    "before the next grade check."
                    .format(wait_seconds / 60)
                )

                if await sleep_or_stop(stop_event, wait_seconds):
                    break

            return 0

        finally:
            stop_event.set()

            if keepalive_task is not None:
                keepalive_task.cancel()

                try:
                    await keepalive_task
                except asyncio.CancelledError:
                    pass

            await context.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Monitor BIU In-Bar for new or changed grades "
            "with conservative pacing and automatic block protection."
        )
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Check once and exit.",
    )

    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_MINUTES,
        help=(
            "Grade-check interval in minutes. Minimum: {}. Default: {}."
            .format(
                MIN_GRADE_INTERVAL_MINUTES,
                DEFAULT_INTERVAL_MINUTES,
            )
        ),
    )

    parser.add_argument(
        "--keepalive",
        type=int,
        default=DEFAULT_KEEPALIVE_MINUTES,
        help=(
            "Session keepalive interval in minutes. Minimum: {}. "
            "Default: {}."
            .format(
                MIN_KEEPALIVE_INTERVAL_MINUTES,
                DEFAULT_KEEPALIVE_MINUTES,
            )
        ),
    )

    parser.add_argument(
        "--auth-method",
        choices=("direct", "my-biu"),
        help=(
            "Authentication method. "
            "If omitted, an interactive menu is shown."
        ),
    )

    parser.add_argument(
        "--force-login",
        action="store_true",
        help="Force the selected authentication flow.",
    )

    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to the optional .env file. Default: .env",
    )

    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset the saved grade snapshot and exit.",
    )

    parser.add_argument(
        "--clear-protection-state",
        action="store_true",
        help=(
            "Clear the persistent circuit-breaker state and exit. "
            "Use only after confirming BIU is accessible normally."
        ),
    )

    parser.add_argument(
        "--protection-status",
        action="store_true",
        help="Print the persistent protection state and exit.",
    )

    parser.add_argument(
        "--print-rows",
        action="store_true",
        help="Print all extracted grade rows as JSON.",
    )

    parser.add_argument(
        "--request-min-gap",
        type=int,
        default=DEFAULT_REQUEST_MIN_GAP_SECONDS,
        help=(
            "Minimum seconds between top-level BIU operations. "
            "Default: {}."
            .format(DEFAULT_REQUEST_MIN_GAP_SECONDS)
        ),
    )

    parser.add_argument(
        "--max-requests-per-hour",
        type=int,
        default=DEFAULT_MAX_TOP_LEVEL_REQUESTS_PER_HOUR,
        help=(
            "Maximum top-level BIU operations per hour. Default: {}."
            .format(DEFAULT_MAX_TOP_LEVEL_REQUESTS_PER_HOUR)
        ),
    )

    args = parser.parse_args()

    if args.interval < MIN_GRADE_INTERVAL_MINUTES:
        parser.error(
            "--interval must be at least {} minutes"
            .format(MIN_GRADE_INTERVAL_MINUTES)
        )

    if args.keepalive < MIN_KEEPALIVE_INTERVAL_MINUTES:
        parser.error(
            "--keepalive must be at least {} minutes"
            .format(MIN_KEEPALIVE_INTERVAL_MINUTES)
        )

    if (
        args.keepalive >= args.interval
        and not args.once
    ):
        parser.error(
            "--keepalive must be shorter than --interval "
            "for continuous monitoring"
        )

    if args.request_min_gap < 5:
        parser.error(
            "--request-min-gap must be at least 5 seconds"
        )

    if args.max_requests_per_hour < 10:
        parser.error(
            "--max-requests-per-hour must be at least 10"
        )

    if args.max_requests_per_hour > 60:
        parser.error(
            "--max-requests-per-hour cannot exceed 60"
        )

    return args


def main() -> int:
    if PLATFORM == "unsupported":
        print(
            "Unsupported operating system: {}"
            .format(platform.system())
        )
        return 1

    args = parse_args()

    if args.reset:
        reset_snapshot()
        return 0

    if args.clear_protection_state:
        protection = ProtectionController(PROTECTION_FILE)
        protection.clear()
        log("The persistent protection state was cleared.")
        return 0

    if args.protection_status:
        protection = ProtectionController(PROTECTION_FILE)
        print(
            json.dumps(
                protection.status_dict(),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    try:
        with InstanceLock(LOCK_FILE):
            return asyncio.run(run(args))

    except KeyboardInterrupt:
        log("Stopped by user.")
        return 130

    except ProtectionEvent as event:
        async def persist_event() -> int:
            controller = ProtectionController(PROTECTION_FILE)
            return await controller.record_protection_event(event)

        cooldown = asyncio.run(persist_event())

        log(
            "The watcher stopped safely after a protection event. "
            "Cooldown: approximately {} minute(s)."
            .format(max(1, cooldown // 60))
        )
        return 2

    except CircuitOpen as exc:
        log(str(exc))
        return 2

    except Exception as exc:
        log(
            "Fatal error: {}\n{}"
            .format(exc, traceback.format_exc())
        )

        try:
            desktop_notification(
                "BIU Grade Watcher error",
                "The watcher stopped:\n{}".format(exc),
            )
        except Exception:
            pass

        return 1


if __name__ == "__main__":
    raise SystemExit(main())
