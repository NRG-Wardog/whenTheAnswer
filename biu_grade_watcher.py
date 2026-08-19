from __future__ import annotations

import argparse
import asyncio
import json
import platform
import traceback

from watcher.config import (
    DEFAULT_INTERVAL_MINUTES,
    DEFAULT_KEEPALIVE_MINUTES,
    DEFAULT_MAX_TOP_LEVEL_REQUESTS_PER_HOUR,
    DEFAULT_REQUEST_MIN_GAP_SECONDS,
    LOCK_FILE,
    MIN_GRADE_INTERVAL_MINUTES,
    MIN_KEEPALIVE_INTERVAL_MINUTES,
    PLATFORM,
    PROTECTION_FILE,
)
from watcher.errors import CircuitOpen, ProtectionEvent, SessionExpired, TransientFailure
from watcher.reliability import (
    AuthenticationBudget,
    InstanceLock,
    ProtectionController,
    ProtectionState,
    RequestGate,
)
from watcher.runtime import run
from watcher.snapshot import (
    canonical_header,
    compare_snapshots,
    describe_row,
    grade_values,
    has_meaningful_grade,
    read_snapshot,
    reset_snapshot,
    row_identity,
    write_snapshot,
)
from watcher.utils import (
    desktop_notification,
    jittered_seconds,
    log,
    normalize_text,
    parse_retry_after,
)

# This module remains the stable command-line entry point and compatibility
# import surface. Runtime implementation lives in the focused watcher package.


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
            .format(MIN_GRADE_INTERVAL_MINUTES, DEFAULT_INTERVAL_MINUTES)
        ),
    )
    parser.add_argument(
        "--keepalive",
        type=int,
        default=DEFAULT_KEEPALIVE_MINUTES,
        help=(
            "Session keepalive interval in minutes. Minimum: {}. Default: {}."
            .format(MIN_KEEPALIVE_INTERVAL_MINUTES, DEFAULT_KEEPALIVE_MINUTES)
        ),
    )
    parser.add_argument(
        "--auth-method",
        choices=("direct", "my-biu"),
        help=(
            "Authentication method. If omitted, an interactive menu is shown."
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
            "Minimum seconds between top-level BIU operations. Default: {}."
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
    if args.keepalive >= args.interval and not args.once:
        parser.error(
            "--keepalive must be shorter than --interval for continuous monitoring"
        )
    if args.request_min_gap < 5:
        parser.error("--request-min-gap must be at least 5 seconds")
    if args.max_requests_per_hour < 10:
        parser.error("--max-requests-per-hour must be at least 10")
    if args.max_requests_per_hour > 60:
        parser.error("--max-requests-per-hour cannot exceed 60")

    return args


def main() -> int:
    if PLATFORM == "unsupported":
        print("Unsupported operating system: {}".format(platform.system()))
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
        log("Fatal error: {}\n{}".format(exc, traceback.format_exc()))
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
