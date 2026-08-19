from __future__ import annotations

import argparse
import asyncio
import json
import platform
import traceback
from pathlib import Path
from typing import List, Optional

from playwright.async_api import BrowserContext, Page, async_playwright

from .browser import (
    choose_page,
    direct_login_headless,
    inspect_fetch_result,
    minimize_browser,
    paced_navigate,
    perform_check,
    resolve_auth_method,
    table_exists,
    wait_for_manual_login,
)
from .config import (
    GRADE_JITTER_RATIO,
    KEEPALIVE_JITTER_RATIO,
    MY_BIU_URL,
    PROFILE_DIR,
    TARGET_URL,
    TRANSIENT_BACKOFF_BASE_SECONDS,
)
from .errors import CircuitOpen, ProtectionEvent, SessionExpired, TransientFailure
from .reliability import AuthenticationBudget, ProtectionController, RequestGate
from .utils import (
    jittered_seconds,
    load_dotenv_file,
    log,
    notify_async,
    sleep_or_stop,
)


async def browser_keepalive(
    page: Page,
    gate: RequestGate,
    protection: ProtectionController,
) -> None:
    """Refresh the authenticated session without creating a separate client identity."""
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
                        headers: {"Accept": "text/html,application/xhtml+xml"}
                    });
                    const text = await response.text();
                    return {
                        status: response.status,
                        url: response.url,
                        retry_after: response.headers.get("retry-after") || "",
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
        raise TransientFailure(f"Browser keepalive failed: {result['error']}")
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
        wait_seconds = jittered_seconds(base_seconds, KEEPALIVE_JITTER_RATIO)
        if await sleep_or_stop(stop_event, wait_seconds):
            break
        try:
            await browser_keepalive(page, gate, protection)
            log("BIU session keepalive completed successfully.")
        except CircuitOpen:
            continue
        except SessionExpired:
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
            cooldown = await protection.record_transient_failure(str(exc), exc.status)
            if cooldown:
                log("Keepalive transient failures opened the circuit.")
        except Exception as exc:
            cooldown = await protection.record_transient_failure(
                f"Unexpected keepalive failure: {exc}"
            )
            if cooldown:
                log("Unexpected keepalive failures opened the circuit.")


def make_notification_message(changes: List[str]) -> str:
    message = "\n\n".join(changes[:6])
    if len(changes) > 6:
        message += f"\n\nAnd {len(changes) - 6} more change(s)."
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
    page = await wait_for_manual_login(context, gate, protection)
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
        "Protection circuit is OPEN. Next controlled probe in approximately "
        f"{max(1, remaining // 60)} minute(s)."
    )
    return await sleep_or_stop(stop_event, min(remaining, 15 * 60))


async def run(args: argparse.Namespace) -> int:
    dotenv_path = Path(args.env_file).expanduser().resolve()
    load_dotenv_file(dotenv_path)

    auth_method = resolve_auth_method(args.auth_method)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    headless = auth_method == "direct"

    protection = ProtectionController(Path(args.protection_file)) if getattr(args, "protection_file", None) else None
    if protection is None:
        from .config import PROTECTION_FILE
        protection = ProtectionController(PROTECTION_FILE)

    gate = RequestGate(
        minimum_gap_seconds=args.request_min_gap,
        maximum_requests_per_hour=args.max_requests_per_hour,
    )
    auth_budget = AuthenticationBudget()
    stop_event = asyncio.Event()

    async with async_playwright() as playwright:
        log("Starting the BIU browser session.")
        log(f"Operating system: {platform.system()}.")
        log(f"Authentication method: {auth_method}.")
        log(f"Browser mode: {'headless' if headless else 'visible'}.")
        log(
            "Safety pacing: minimum {} seconds between top-level operations, "
            "maximum {} per hour.".format(
                args.request_min_gap, args.max_requests_per_hour
            )
        )

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
                if not args.force_login and await table_exists(page):
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
                    page, gate, protection, stop_event, args.keepalive
                )
            )

            while not stop_event.is_set():
                if protection.state.circuit_state == "OPEN":
                    if await wait_for_circuit_recovery(protection, stop_event):
                        break
                    continue

                log("Checking BIU In-Bar for new grades now.")
                try:
                    rows, changes, initialized = await perform_check(
                        page, gate, protection
                    )
                    if initialized:
                        log(
                            "Initial snapshot saved successfully: {} rows. "
                            "No notification was generated.".format(len(rows))
                        )
                    elif changes:
                        log(f"{len(changes)} grade change(s) detected.")
                        await notify_async(
                            "BIU In-Bar grade update",
                            make_notification_message(changes),
                        )
                    else:
                        log(f"Check completed: {len(rows)} rows, no new grades.")

                    if args.print_rows:
                        print(json.dumps(rows, ensure_ascii=False, indent=2))

                except SessionExpired as exc:
                    log(f"The BIU session expired: {exc}")
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
                        await handle_protection_event(protection, event)
                    except Exception as auth_exc:
                        cooldown = await protection.record_transient_failure(
                            f"Authentication failed: {auth_exc}"
                        )
                        log(
                            "Authentication failed without automatic retry: "
                            f"{auth_exc}"
                        )
                        if cooldown == 0:
                            await sleep_or_stop(stop_event, 5 * 60)
                    continue

                except ProtectionEvent as event:
                    await handle_protection_event(protection, event)
                    continue
                except CircuitOpen:
                    continue
                except TransientFailure as exc:
                    cooldown = await protection.record_transient_failure(
                        str(exc), exc.status
                    )
                    log(f"Transient BIU failure: {exc}")
                    if cooldown == 0:
                        delay = int(
                            jittered_seconds(
                                TRANSIENT_BACKOFF_BASE_SECONDS
                                * max(1, protection.state.consecutive_failures),
                                0.20,
                            )
                        )
                        log(f"Waiting {delay} second(s) before continuing.")
                        await sleep_or_stop(stop_event, delay)
                    continue
                except Exception as exc:
                    cooldown = await protection.record_transient_failure(
                        f"Unexpected check failure: {exc}"
                    )
                    log(f"Unexpected check failure: {exc}\n{traceback.format_exc()}")
                    if cooldown == 0:
                        await sleep_or_stop(stop_event, 5 * 60)
                    continue

                if args.once:
                    return 0

                wait_seconds = jittered_seconds(
                    args.interval * 60, GRADE_JITTER_RATIO
                )
                log(
                    "Waiting approximately {:.1f} minute(s) before the next grade check.".format(
                        wait_seconds / 60
                    )
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
