from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import (
    BrowserContext,
    Locator,
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
)

from .config import (
    AUTH_STATUSES,
    BLOCK_STATUSES,
    BLOCK_TEXT_PATTERNS,
    DIRECT_LOGIN_URL,
    LOGIN_PATH,
    LOGIN_POLL_SECONDS,
    LOGIN_TIMEOUT_SECONDS,
    MY_BIU_URL,
    RATE_LIMIT_STATUSES,
    RATE_LIMIT_TEXT_PATTERNS,
    ROW_STABILITY_ROUNDS,
    TABLE_HINT,
    TARGET_PATH,
    TARGET_URL,
    TRANSIENT_STATUSES,
)
from .errors import ProtectionEvent, SessionExpired, TransientFailure
from .reliability import AuthenticationBudget, ProtectionController, RequestGate
from .snapshot import (
    canonical_header,
    compare_snapshots,
    is_grade_header,
    read_snapshot,
    write_snapshot,
)
from .utils import log, normalize_text, parse_retry_after


async def page_text_sample(page: Page, limit: int = 10000) -> str:
    try:
        return await page.evaluate(
            """
            limit => {
                const body = document.body;
                if (!body) return "";
                return (body.innerText || body.textContent || "").slice(0, limit);
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
            f"{operation} returned HTTP {status}.",
            kind="rate_limit",
            status=status,
            retry_after_seconds=retry_after,
        )
    if status in BLOCK_STATUSES:
        raise ProtectionEvent(
            f"{operation} returned HTTP {status}.",
            kind="blocked",
            status=status,
            retry_after_seconds=retry_after,
        )
    if status in AUTH_STATUSES:
        raise SessionExpired(f"{operation} returned HTTP {status}.")
    if status in TRANSIENT_STATUSES:
        if status == 503 and retry_after is not None:
            raise ProtectionEvent(
                f"{operation} returned HTTP 503 with Retry-After.",
                kind="rate_limit",
                status=status,
                retry_after_seconds=retry_after,
            )
        raise TransientFailure(f"{operation} returned HTTP {status}.", status=status)

    sample = await page_text_sample(page)
    try:
        title = await page.title()
    except Exception:
        title = ""
    combined = f"{page.url}\n{title}\n{sample}"

    if any(pattern.lower() in normalize_text(combined).lower() for pattern in RATE_LIMIT_TEXT_PATTERNS):
        raise ProtectionEvent(
            f"{operation} displayed a rate-limit response.",
            kind="rate_limit",
            status=status,
            retry_after_seconds=retry_after,
        )
    if any(pattern.lower() in normalize_text(combined).lower() for pattern in BLOCK_TEXT_PATTERNS):
        raise ProtectionEvent(
            f"{operation} displayed a blocking or human-verification challenge.",
            kind="blocked",
            status=status,
            retry_after_seconds=retry_after,
        )
    if not allow_login_page and LOGIN_PATH.lower() in page.url.lower():
        raise SessionExpired(f"{operation} redirected to the BIU login page.")


def inspect_fetch_result(result: Dict[str, Any], operation: str) -> None:
    status = int(result.get("status", 0) or 0)
    url = normalize_text(result.get("url", ""))
    text = normalize_text(result.get("text", ""))
    retry_after = parse_retry_after(normalize_text(result.get("retry_after", "")))
    combined = f"{url}\n{text}"

    if status in RATE_LIMIT_STATUSES:
        raise ProtectionEvent(
            f"{operation} returned HTTP {status}.",
            kind="rate_limit",
            status=status,
            retry_after_seconds=retry_after,
        )
    if status in BLOCK_STATUSES:
        raise ProtectionEvent(
            f"{operation} returned HTTP {status}.",
            kind="blocked",
            status=status,
            retry_after_seconds=retry_after,
        )
    if status in AUTH_STATUSES:
        raise SessionExpired(f"{operation} returned HTTP {status}.")
    if status in TRANSIENT_STATUSES:
        if status == 503 and retry_after is not None:
            raise ProtectionEvent(
                f"{operation} returned HTTP 503 with Retry-After.",
                kind="rate_limit",
                status=status,
                retry_after_seconds=retry_after,
            )
        raise TransientFailure(f"{operation} returned HTTP {status}.", status=status)

    lower = normalize_text(combined).lower()
    if any(pattern.lower() in lower for pattern in RATE_LIMIT_TEXT_PATTERNS):
        raise ProtectionEvent(
            f"{operation} displayed a rate-limit response.",
            kind="rate_limit",
            status=status or None,
            retry_after_seconds=retry_after,
        )
    if any(pattern.lower() in lower for pattern in BLOCK_TEXT_PATTERNS):
        raise ProtectionEvent(
            f"{operation} displayed a blocking or human-verification challenge.",
            kind="blocked",
            status=status or None,
            retry_after_seconds=retry_after,
        )
    if LOGIN_PATH.lower() in url.lower():
        raise SessionExpired(f"{operation} redirected to the BIU login page.")
    if TABLE_HINT.lower() not in text.lower():
        raise SessionExpired(f"{operation} did not return the authenticated grades page.")


def resolve_auth_method(argument: Optional[str]) -> str:
    if argument:
        return argument
    print("\nChoose a BIU authentication method:")
    print("  1. Direct In-Bar login (fully headless)")
    print('  2. Manual login through the "My Bar-Ilan" portal\n')
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
        return await page.locator(f"table[id*='{TABLE_HINT}']").count() > 0
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
            response = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightTimeoutError as exc:
            raise TransientFailure(f"{operation} timed out.") from exc
    await inspect_loaded_page(
        page, response, allow_login_page=allow_login_page, operation=operation
    )
    return response


async def first_visible(locator: Locator) -> Optional[Locator]:
    for index in range(await locator.count()):
        candidate = locator.nth(index)
        try:
            if await candidate.is_visible() and await candidate.is_enabled():
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
                page.get_by_label(re.compile(pattern, re.IGNORECASE))
            )
            if candidate is not None:
                return candidate
        except Exception:
            pass

    candidates = page.locator(
        "input[type='text'],input[type='tel'],input[type='number'],"
        "input:not([type]),input[type='password']"
    )
    visible: List[Locator] = []
    for index in range(await candidates.count()):
        candidate = candidates.nth(index)
        try:
            if await candidate.is_visible() and await candidate.is_enabled():
                visible.append(candidate)
        except Exception:
            pass
    if fallback_index is not None and 0 <= fallback_index < len(visible):
        return visible[fallback_index]
    return None


async def find_submit_button(page: Page) -> Optional[Locator]:
    patterns = [
        r"המשך", r"כניסה", r"התחבר", r"אישור", r"שלח",
        r"continue", r"login", r"sign in", r"submit", r"next",
    ]
    for pattern in patterns:
        try:
            candidate = await first_visible(
                page.get_by_role("button", name=re.compile(pattern, re.IGNORECASE))
            )
            if candidate is not None:
                return candidate
        except Exception:
            pass
    return await first_visible(page.locator("button, input[type='submit'], input[type='button']"))


async def find_otp_input(page: Page) -> Optional[Locator]:
    candidate = await find_text_input(
        page,
        [r"קוד", r"אימות", r"חד.?פעמי", r"verification", r"otp", r"code"],
    )
    if candidate is not None:
        return candidate
    inputs = page.locator(
        "input[type='text'], input[type='tel'], input[type='number'], input[type='password']"
    )
    visible: List[Locator] = []
    for index in range(await inputs.count()):
        item = inputs.nth(index)
        try:
            if await item.is_visible() and await item.is_enabled():
                visible.append(item)
        except Exception:
            pass
    return visible[0] if len(visible) == 1 else None


async def wait_for_otp_or_grades(page: Page, timeout_seconds: int = 90) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        await inspect_loaded_page(
            page, None, allow_login_page=True, operation="BIU login"
        )
        if await table_exists(page) or await find_otp_input(page) is not None:
            return
        await asyncio.sleep(1)
    raise TransientFailure("The OTP field or grades page was not detected after login submission.")


def get_direct_credentials() -> Tuple[str, str]:
    student_id = (
        os.environ.get("BIU_ID") or os.environ.get("id") or os.environ.get("ID") or ""
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
        raise RuntimeError(f"Missing required login value(s): {', '.join(missing)}.")
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
        [r"ת.?ז", r"תעודת זהות", r"דרכון", r"passport", r"identity"],
        fallback_index=0,
    )
    phone_input = await find_text_input(
        page, [r"טלפון", r"נייד", r"phone", r"mobile"], fallback_index=1
    )
    if id_input is None or phone_input is None:
        raise TransientFailure("Could not identify the ID and phone fields on the BIU login page.")
    await id_input.fill(student_id)
    await phone_input.fill(phone)
    submit = await find_submit_button(page)
    if submit is None:
        raise TransientFailure("Could not identify the submit button on the BIU login page.")
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
        raise TransientFailure("Could not identify the one-time verification code field.")
    otp = input("Enter the one-time verification code sent by BIU: ").strip()
    if not otp:
        raise RuntimeError("No one-time verification code was entered.")
    await otp_input.fill(otp)
    otp_submit = await find_submit_button(page)
    if otp_submit is None:
        raise TransientFailure("Could not identify the OTP submit button.")
    async with gate.slot("submit OTP"):
        await otp_submit.click()
    log("The one-time verification code was submitted.")
    try:
        await page.wait_for_selector(f"table[id*='{TABLE_HINT}']", timeout=90_000)
    except PlaywrightTimeoutError:
        await inspect_loaded_page(
            page, None, allow_login_page=True, operation="OTP submission"
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
        raise SessionExpired("Authentication completed, but the grades table was not available.")
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
                candidate, None, allow_login_page=True, operation="manual login"
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
                    log("Manual authentication was detected automatically.")
                    return current
            except SessionExpired:
                pass
            last_target_attempt = loop.time()
        await asyncio.sleep(LOGIN_POLL_SECONDS)
    raise RuntimeError(
        f"Manual login was not detected within {LOGIN_TIMEOUT_SECONDS // 60} minutes."
    )


async def minimize_browser(page: Page) -> None:
    try:
        cdp = await page.context.new_cdp_session(page)
        window = await cdp.send("Browser.getWindowForTarget")
        await cdp.send(
            "Browser.setWindowBounds",
            {"windowId": window["windowId"], "bounds": {"windowState": "minimized"}},
        )
        await cdp.detach()
        log("The authenticated browser was minimized.")
    except Exception as exc:
        log(f"Could not minimize the browser automatically: {exc}. The watcher will continue.")


async def load_all_rows(page: Page) -> int:
    selector = f"table[id*='{TABLE_HINT}']"
    last_count = -1
    stable_rounds = 0
    while stable_rounds < ROW_STABILITY_ROUNDS:
        total = await page.locator(f"{selector} tr").count()
        count = max(0, total - 1)
        if count == last_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
            last_count = count
        await page.evaluate(
            """
            () => {
                window.scrollTo(0, document.documentElement.scrollHeight);
                for (const element of document.querySelectorAll("*")) {
                    const style = getComputedStyle(element);
                    const canScroll = /(auto|scroll)/.test(style.overflowY) &&
                        element.scrollHeight > element.clientHeight;
                    if (canScroll) element.scrollTop = element.scrollHeight;
                }
            }
            """
        )
        await page.wait_for_timeout(800)
    await page.evaluate("window.scrollTo(0, 0)")
    log(f"Grades table stabilized at {last_count} data row(s).")
    return last_count


async def extract_grade_rows(page: Page) -> List[Dict[str, str]]:
    selector = f"table[id*='{TABLE_HINT}']"
    table = page.locator(selector).first
    if await table.count() == 0:
        raise SessionExpired("The BIU grades table was not found.")
    await load_all_rows(page)
    result = await table.evaluate(
        """
        table => {
            const clean = value => (value || "")
                .replace(/[\u200e\u200f]/g, "").replace(/\s+/g, " ").trim();
            const allRows = Array.from(table.querySelectorAll("tr"));
            if (allRows.length === 0) return { headers: [], rows: [] };
            const headerRow = allRows.find(row => row.querySelectorAll("th").length > 0) || allRows[0];
            let headers = Array.from(headerRow.querySelectorAll("th, td"))
                .map(cell => clean(cell.innerText || cell.textContent));
            const dataRows = allRows.filter(row => {
                if (row === headerRow || row.querySelectorAll(":scope > td").length === 0) return false;
                const cls = row.className || "";
                return /GridRow|AlternatingRow|GridAlternatingRow/i.test(cls)
                    || row.querySelector("span[id*='gvStudentAssignmentTermList_']")
                    || row.querySelector("input[id*='gvStudentAssignmentTermList_']");
            });
            const rows = dataRows.map(row => {
                const cells = Array.from(row.querySelectorAll(":scope > td"));
                const values = cells.map(cell => clean(cell.innerText || cell.textContent));
                if (headers.length < values.length) {
                    const expanded = headers.slice();
                    for (let index = expanded.length; index < values.length; index++) {
                        expanded.push("Column " + (index + 1));
                    }
                    headers = expanded;
                }
                const record = {};
                values.forEach((value, index) => {
                    let header = headers[index] || ("Column " + (index + 1));
                    if (Object.prototype.hasOwnProperty.call(record, header)) {
                        header = header + " #" + (index + 1);
                    }
                    record[header] = value;
                });
                return record;
            });
            return { headers, rows };
        }
        """
    )
    headers = [canonical_header(header) for header in result.get("headers", [])]
    rows: List[Dict[str, str]] = []
    for raw_row in result.get("rows", []):
        row = {
            canonical_header(header): normalize_text(value)
            for header, value in raw_row.items()
        }
        if any(row.values()):
            rows.append(row)
    if not rows:
        raise TransientFailure("The grades table was found, but no rows were extracted.")
    if not any(is_grade_header(header) for header in headers):
        raise TransientFailure("The grades table was found, but no grade column was identified.")
    return rows


async def perform_check(
    page: Page,
    gate: RequestGate,
    protection: ProtectionController,
) -> Tuple[List[Dict[str, str]], List[str], bool]:
    await protection.prepare_activity(allow_recovery_probe=True)
    async with gate.slot("grade check"):
        try:
            response = await page.reload(wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightTimeoutError as exc:
            raise TransientFailure("The grades page reload timed out.") from exc
    await inspect_loaded_page(
        page, response, allow_login_page=False, operation="grade check"
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
