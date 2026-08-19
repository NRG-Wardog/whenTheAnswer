from __future__ import annotations

from typing import Optional


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
            "Protection circuit is open for approximately {} second(s): {}".format(
                wait_seconds, reason
            )
        )
        self.wait_seconds = wait_seconds
        self.reason = reason
