from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Deque, Dict, Optional

from .config import (
    EXPLICIT_BLOCK_DEFAULT_COOLDOWN_SECONDS,
    MAX_REAUTH_ATTEMPTS_PER_HOUR,
    MIN_REAUTH_GAP_SECONDS,
    PROTECTION_NOTIFICATION_COOLDOWN_SECONDS,
    RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS,
    TRANSIENT_BACKOFF_BASE_SECONDS,
    TRANSIENT_BACKOFF_MAX_SECONDS,
    TRANSIENT_FAILURE_THRESHOLD,
)
from .errors import CircuitOpen, ProtectionEvent
from .utils import (
    atomic_write_json,
    jittered_seconds,
    log,
    parse_utc,
    process_exists,
    read_json,
    utc_iso,
    utc_now,
)


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def acquire(self) -> None:
        for _ in range(2):
            try:
                descriptor = os.open(
                    str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps({"pid": os.getpid(), "created_at": utc_iso()}))
                self.acquired = True
                return
            except FileExistsError:
                data = read_json(self.path, {})
                existing_pid = int(data.get("pid", 0) or 0)
                if process_exists(existing_pid):
                    raise RuntimeError(
                        "Another BIU Grade Watcher instance is already running with PID {}.".format(
                            existing_pid
                        )
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
        filtered = {key: value for key, value in raw.items() if key in allowed}
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
        self, reason: str, status: Optional[int] = None
    ) -> int:
        async with self._lock:
            self.state.consecutive_failures += 1
            self.state.last_reason = reason
            self.state.last_status = status
            self.state.last_event_at = utc_iso()
            should_open = (
                self.state.circuit_state == "HALF_OPEN"
                or self.state.consecutive_failures >= TRANSIENT_FAILURE_THRESHOLD
            )
            if not should_open:
                self.save()
                return 0
            exponent = max(
                0, self.state.consecutive_failures - TRANSIENT_FAILURE_THRESHOLD
            )
            cooldown = min(
                TRANSIENT_BACKOFF_MAX_SECONDS,
                TRANSIENT_BACKOFF_BASE_SECONDS * (2**exponent),
            )
            cooldown = int(jittered_seconds(cooldown, 0.20))
            self._open_unlocked(reason, status, cooldown)
            return cooldown

    async def record_protection_event(self, event: ProtectionEvent) -> int:
        async with self._lock:
            if event.retry_after_seconds is not None:
                cooldown = event.retry_after_seconds
            elif event.kind == "rate_limit":
                cooldown = RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS
            else:
                cooldown = EXPLICIT_BLOCK_DEFAULT_COOLDOWN_SECONDS
            multiplier = min(4, max(1, self.state.opened_count + 1))
            cooldown = min(24 * 60 * 60, cooldown * multiplier)
            cooldown = int(jittered_seconds(cooldown, 0.10))
            self._open_unlocked(str(event), event.status, cooldown)
            return cooldown

    def _open_unlocked(
        self, reason: str, status: Optional[int], cooldown_seconds: int
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
            "Protection circuit opened for approximately {} minute(s): {}".format(
                max(1, cooldown_seconds // 60), reason
            )
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


class RequestGate:
    """Serialize top-level BIU operations and enforce pacing/budgets."""

    def __init__(self, minimum_gap_seconds: int, maximum_requests_per_hour: int) -> None:
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
                wait_seconds = max(1.0, 3600 - (now - self._timestamps[0]))
                log(f"Hourly request budget reached. Waiting {wait_seconds:.0f} second(s).")
                await asyncio.sleep(wait_seconds)
                now = loop.time()
                while self._timestamps and now - self._timestamps[0] >= 3600:
                    self._timestamps.popleft()
            gap_remaining = self.minimum_gap_seconds - (now - self._last_started_at)
            if gap_remaining > 0:
                await asyncio.sleep(jittered_seconds(gap_remaining, 0.10))
            started = loop.time()
            self._last_started_at = started
            self._timestamps.append(started)
            log(f"Starting paced BIU operation: {label}.")
            yield


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
            remaining = int(MIN_REAUTH_GAP_SECONDS - (now - self._last_attempt_at))
            raise ProtectionEvent(
                "Authentication was requested again too soon.",
                kind="rate_limit",
                retry_after_seconds=remaining,
            )
        self._attempts.append(now)
        self._last_attempt_at = now
