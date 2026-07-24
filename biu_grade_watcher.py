#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
BIU In-Bar Grade Watcher v7
Windows / Python 3.8

Authentication modes
--------------------
1. direct
   - Runs fully headless from start to finish.
   - Reads ID and phone from environment variables or .env.
   - Fills the direct In-Bar login form automatically.
   - Prompts for the one-time verification code in PowerShell.
   - Submits the OTP inside the same headless browser context.

2. my-biu
   - Opens the "My Bar-Ilan" portal in a visible browser.
   - The user completes authentication manually.
   - The same browser is minimized after authentication.

Session TTL
-----------
BIU sessions may expire quickly when idle. A lightweight authenticated
keepalive request runs independently from the grade-check interval.

Example:
- grade check every 10 minutes
- keepalive every 2 minutes
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import os
import re
import traceback
import winreg
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import (
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

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
TABLE_HINT = "gvStudentAssignmentTermList"

APP_NAME = "BIUGradeWatcher"
REGISTRY_PATH = r"Software\BIUGradeWatcher"
REGISTRY_SNAPSHOT_VALUE = "GradeSnapshotV1"
REGISTRY_LAST_CHECK_VALUE = "LastSuccessfulCheck"

DEFAULT_INTERVAL_MINUTES = 10
DEFAULT_KEEPALIVE_MINUTES = 2
LOGIN_TIMEOUT_SECONDS = 15 * 60
LOGIN_POLL_SECONDS = 2
ROW_STABILITY_ROUNDS = 3

GRADE_HEADER_NAMES = {"ציון", "ציון סופי"}
COURSE_HEADER_NAMES = {"שם קבוצת קורס", "שם הקורס", "קורס"}
COURSE_CODE_HEADER_NAMES = {"קוד קבוצת קורס", "קוד קורס", "מספר קורס"}
DATE_HEADER_NAMES = {"תאריך", "תאריך בחינה"}
TERM_HEADER_NAMES = {"מועד", "תקופה"}
NOTEBOOK_HEADER_NAMES = {"מספר מחברת", "מחברת"}


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Local")

    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


APP_DIR = app_data_dir()
PROFILE_DIR = APP_DIR / "browser_profile"
LOG_FILE = APP_DIR / "watcher.log"


def log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[{}] {}".format(stamp, message)
    print(line, flush=True)

    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
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


def get_first_value(row: Dict[str, str], names: set) -> str:
    for name in names:
        value = normalize_text(row.get(name, ""))
        if value:
            return value
    return ""


def windows_popup(title: str, message: str) -> None:
    MB_OK = 0x00000000
    MB_ICONINFORMATION = 0x00000040
    MB_SETFOREGROUND = 0x00010000
    MB_TOPMOST = 0x00040000

    ctypes.windll.user32.MessageBoxW(
        None,
        message,
        title,
        MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST,
    )


def registry_read_snapshot() -> Optional[List[Dict[str, str]]]:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            REGISTRY_PATH,
            0,
            winreg.KEY_READ,
        ) as key:
            raw, _ = winreg.QueryValueEx(key, REGISTRY_SNAPSHOT_VALUE)

        data = json.loads(raw)

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

    except FileNotFoundError:
        return None

    except Exception as exc:
        log("Could not read the saved snapshot: {}".format(exc))
        return None


def registry_write_snapshot(rows: List[Dict[str, str]]) -> None:
    raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        REGISTRY_PATH,
        0,
        winreg.KEY_WRITE,
    ) as key:
        winreg.SetValueEx(
            key,
            REGISTRY_SNAPSHOT_VALUE,
            0,
            winreg.REG_SZ,
            raw,
        )
        winreg.SetValueEx(
            key,
            REGISTRY_LAST_CHECK_VALUE,
            0,
            winreg.REG_SZ,
            datetime.now().isoformat(timespec="seconds"),
        )


def registry_reset() -> None:
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH)
        log("The saved grade snapshot was reset.")

    except FileNotFoundError:
        log("No saved grade snapshot existed.")

    except OSError:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            REGISTRY_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            for value_name in (
                REGISTRY_SNAPSHOT_VALUE,
                REGISTRY_LAST_CHECK_VALUE,
            ):
                try:
                    winreg.DeleteValue(key, value_name)
                except FileNotFoundError:
                    pass

        log("The saved grade snapshot was reset.")


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
    previous_by_id = {row_identity(row): row for row in previous}
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
                differences.append("{} added: {}".format(header, after))
            elif before and after:
                differences.append(
                    "{} changed from {} to {}".format(header, before, after)
                )
            else:
                differences.append(
                    "{} removed; previous value was {}".format(header, before)
                )

        if differences:
            changes.append(
                "{}\n{}".format(
                    describe_row(row),
                    "\n".join(differences),
                )
            )

    return changes


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


async def navigate(page: Page, url: str) -> None:
    try:
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )

    except PlaywrightTimeoutError:
        log("Navigation timed out. Inspecting the current page.")

    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        pass


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
            if await candidate.is_visible() and await candidate.is_enabled():
                visible.append(candidate)
        except Exception:
            pass

    if fallback_index is not None and 0 <= fallback_index < len(visible):
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
    patterns = [
        r"קוד",
        r"אימות",
        r"חד.?פעמי",
        r"verification",
        r"otp",
        r"code",
    ]

    candidate = await find_text_input(page, patterns)

    if candidate is not None:
        return candidate

    inputs = page.locator(
        "input[type='text'], input[type='tel'], "
        "input[type='number'], input[type='password']"
    )

    visible: List[Locator] = []

    for index in range(await inputs.count()):
        item = inputs.nth(index)

        try:
            if await item.is_visible() and await item.is_enabled():
                visible.append(item)
        except Exception:
            pass

    if len(visible) == 1:
        return visible[0]

    return None


async def wait_for_otp_or_grades(page: Page, timeout_seconds: int = 90) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds

    while loop.time() < deadline:
        if await table_exists(page):
            return

        otp_input = await find_otp_input(page)

        if otp_input is not None:
            return

        await asyncio.sleep(1)

    raise RuntimeError(
        "The OTP field or grades page was not detected after login submission."
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


async def direct_login_headless(page: Page) -> Page:
    student_id, phone = get_direct_credentials()

    log("Starting direct BIU login in headless mode.")
    await navigate(page, DIRECT_LOGIN_URL)

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
        raise RuntimeError(
            "Could not identify the ID and phone fields on the BIU login page."
        )

    await id_input.fill(student_id)
    await phone_input.fill(phone)

    submit = await find_submit_button(page)

    if submit is None:
        raise RuntimeError(
            "Could not identify the submit button on the BIU login page."
        )

    log("ID and phone number were filled automatically.")
    await submit.click()
    await wait_for_otp_or_grades(page)

    if await table_exists(page):
        log("Direct BIU authentication completed without a new OTP.")
        return page

    otp_input = await find_otp_input(page)

    if otp_input is None:
        raise RuntimeError(
            "Could not identify the one-time verification code field."
        )

    otp = input(
    "Enter the one-time verification code sent by BIU: "
    ).strip()

    if not otp:
        raise RuntimeError("No one-time verification code was entered.")

    await otp_input.fill(otp)

    otp_submit = await find_submit_button(page)

    if otp_submit is None:
        raise RuntimeError(
            "Could not identify the OTP submit button."
        )

    await otp_submit.click()
    log("The one-time verification code was submitted.")

    try:
        await page.wait_for_selector(
            "table[id*='{}']".format(TABLE_HINT),
            timeout=90_000,
        )
    except PlaywrightTimeoutError as exc:
        # Try the target URL in the same authenticated headless context.
        await navigate(page, TARGET_URL)

        if not await table_exists(page):
            raise RuntimeError(
                "Authentication completed, but the grades page was not available."
            ) from exc

    log("Direct headless authentication completed successfully.")
    return page


async def wait_for_manual_login(context: BrowserContext) -> Page:
    log("Waiting for manual BIU authentication.")
    log("No Enter key is required.")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + LOGIN_TIMEOUT_SECONDS
    last_target_attempt = 0.0

    while loop.time() < deadline:
        for candidate in reversed(context.pages):
            if await table_exists(candidate):
                log("Manual authentication was detected automatically.")
                return candidate

        if loop.time() - last_target_attempt >= 5:
            current = await choose_page(context)

            try:
                await current.goto(
                    TARGET_URL,
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )

                if await table_exists(current):
                    log("Manual authentication was detected automatically.")
                    return current

            except Exception:
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
            "Could not minimize the browser automatically: {}.".format(exc)
        )


async def load_all_rows(page: Page) -> int:
    selector = "table[id*='{}']".format(TABLE_HINT)
    last_count = -1
    stable_rounds = 0

    while stable_rounds < ROW_STABILITY_ROUNDS:
        total = await page.locator("{} tr".format(selector)).count()
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
    log("Grades table stabilized at {} data row(s).".format(last_count))
    return last_count


async def extract_grade_rows(page: Page) -> List[Dict[str, str]]:
    selector = "table[id*='{}']".format(TABLE_HINT)
    table = page.locator(selector).first

    if await table.count() == 0:
        raise RuntimeError("The BIU grades table was not found.")

    await load_all_rows(page)

    result = await table.evaluate(
        """
        table => {
            const clean = value =>
                (value || "")
                    .replace(/[\\u200e\\u200f]/g, "")
                    .replace(/\\s+/g, " ")
                    .trim();

            const allRows = Array.from(table.querySelectorAll("tr"));

            if (allRows.length === 0) {
                return { headers: [], rows: [] };
            }

            const headerRow = allRows.find(
                row => row.querySelectorAll("th").length > 0
            ) || allRows[0];

            let headers = Array.from(
                headerRow.querySelectorAll("th, td")
            ).map(cell => clean(cell.innerText || cell.textContent));

            const dataRows = allRows.filter(row => {
                if (row === headerRow) return false;

                if (row.querySelectorAll(":scope > td").length === 0) {
                    return false;
                }

                const cls = row.className || "";

                return /GridRow|AlternatingRow|GridAlternatingRow/i.test(cls)
                    || row.querySelector(
                        "span[id*='gvStudentAssignmentTermList_']"
                    )
                    || row.querySelector(
                        "input[id*='gvStudentAssignmentTermList_']"
                    );
            });

            const rows = dataRows.map(row => {
                const cells = Array.from(row.querySelectorAll(":scope > td"));

                const values = cells.map(
                    cell => clean(cell.innerText || cell.textContent)
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
                        headers[index] || ("Column " + (index + 1));

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
        raise RuntimeError(
            "The grades table was found, but no rows were extracted."
        )

    if not any(is_grade_header(header) for header in headers):
        raise RuntimeError(
            "The grades table was found, but no grade column was identified."
        )

    return rows


async def perform_check(
    page: Page,
) -> Tuple[List[Dict[str, str]], List[str], bool]:
    try:
        await page.reload(
            wait_until="domcontentloaded",
            timeout=60_000,
        )

    except PlaywrightTimeoutError:
        log("Page reload timed out. Inspecting the current page.")

    if not await table_exists(page):
        raise RuntimeError(
            "The BIU session expired or the grades table is unavailable."
        )

    rows = await extract_grade_rows(page)
    previous = registry_read_snapshot()

    if previous is None:
        registry_write_snapshot(rows)
        return rows, [], True

    changes = compare_snapshots(previous, rows)
    registry_write_snapshot(rows)

    return rows, changes, False


async def keepalive_loop(
    context: BrowserContext,
    stop_event: asyncio.Event,
    interval_minutes: int,
) -> None:
    """
    Keep the BIU server session active independently from grade checks.

    context.request shares cookies with the browser context.
    """
    interval_seconds = interval_minutes * 60

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=interval_seconds,
            )
            break

        except asyncio.TimeoutError:
            pass

        try:
            response = await context.request.get(
                TARGET_URL,
                timeout=45_000,
                fail_on_status_code=False,
            )

            if response.ok:
                log(
                    "BIU session keepalive completed successfully."
                )
            else:
                log(
                    "BIU session keepalive returned HTTP {}.".format(
                        response.status
                    )
                )

        except Exception as exc:
            log("BIU session keepalive failed: {}".format(exc))


def make_popup_message(changes: List[str]) -> str:
    message = "\n\n".join(changes[:6])

    if len(changes) > 6:
        message += "\n\nAnd {} more change(s).".format(len(changes) - 6)

    if len(message) > 3500:
        message = message[:3500] + "\n\n[Message truncated]"

    return message


async def run(args: argparse.Namespace) -> int:
    dotenv_path = Path(args.env_file).expanduser().resolve()
    load_dotenv_file(dotenv_path)

    auth_method = resolve_auth_method(args.auth_method)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    # Direct login is fully headless.
    # My-BIU remains visible because it is a manual interactive flow.
    headless = auth_method == "direct"

    async with async_playwright() as playwright:
        log("Starting the BIU browser session.")
        log("Authentication method: {}.".format(auth_method))
        log("Browser mode: {}.".format(
            "headless" if headless else "visible"
        ))

        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            viewport={"width": 1440, "height": 1000},
            locale="he-IL",
            timezone_id="Asia/Jerusalem",
            args=["--disable-blink-features=AutomationControlled"],
        )

        stop_event = asyncio.Event()
        keepalive_task: Optional[asyncio.Task] = None

        try:
            page = await choose_page(context)
            await navigate(page, TARGET_URL)

            if not args.force_login and await table_exists(page):
                log("The existing BIU session is valid.")

            elif auth_method == "direct":
                page = await direct_login_headless(page)

            else:
                log('Opening the "My Bar-Ilan" portal.')
                await navigate(page, MY_BIU_URL)
                page = await wait_for_manual_login(context)
                await minimize_browser(page)

            keepalive_task = asyncio.create_task(
                keepalive_loop(
                    context,
                    stop_event,
                    args.keepalive,
                )
            )

            while True:
                log("Checking BIU In-Bar for new grades now.")

                try:
                    rows, changes, initialized = await perform_check(page)

                    if initialized:
                        log(
                            "Initial snapshot saved successfully: {} rows. "
                            "No popup was generated.".format(len(rows))
                        )

                    elif changes:
                        log(
                            "{} grade change(s) detected.".format(len(changes))
                        )

                        windows_popup(
                            "BIU In-Bar grade update",
                            make_popup_message(changes),
                        )

                    else:
                        log(
                            "Check completed: {} rows, no new grades.".format(
                                len(rows)
                            )
                        )

                    if args.print_rows:
                        print(
                            json.dumps(
                                rows,
                                ensure_ascii=False,
                                indent=2,
                            )
                        )

                except Exception as exc:
                    log("The current BIU session failed: {}".format(exc))

                    if auth_method == "direct":
                        log("Re-authenticating in headless direct-login mode.")
                        page = await direct_login_headless(page)
                    else:
                        log("Manual authentication is required again.")
                        await navigate(page, MY_BIU_URL)
                        page = await wait_for_manual_login(context)
                        await minimize_browser(page)

                    continue

                if args.once:
                    return 0

                log(
                    "Waiting {} minute(s) before the next grade check.".format(
                        args.interval
                    )
                )

                await asyncio.sleep(args.interval * 60)

        finally:
            stop_event.set()

            if keepalive_task is not None:
                keepalive_task.cancel()

                try:
                    await keepalive_task
                except asyncio.CancelledError:
                    pass

            await context.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor BIU In-Bar for new or changed grades."
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
            "Grade-check interval in minutes. Default: {}.".format(
                DEFAULT_INTERVAL_MINUTES
            )
        ),
    )

    parser.add_argument(
        "--keepalive",
        type=int,
        default=DEFAULT_KEEPALIVE_MINUTES,
        help=(
            "BIU session keepalive interval in minutes. Default: {}.".format(
                DEFAULT_KEEPALIVE_MINUTES
            )
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
        "--print-rows",
        action="store_true",
        help="Print all extracted grade rows as JSON.",
    )

    args = parser.parse_args()

    if args.interval < 1:
        parser.error("--interval must be at least 1 minute")

    if args.keepalive < 1:
        parser.error("--keepalive must be at least 1 minute")

    if args.keepalive >= args.interval and not args.once:
        parser.error(
            "--keepalive should be shorter than --interval "
            "for continuous monitoring"
        )

    return args


def main() -> int:
    args = parse_args()

    if args.reset:
        registry_reset()
        return 0

    try:
        return asyncio.run(run(args))

    except KeyboardInterrupt:
        log("Stopped by user.")
        return 130

    except Exception as exc:
        log("Fatal error: {}\n{}".format(exc, traceback.format_exc()))

        try:
            windows_popup(
                "BIU Grade Watcher error",
                "The watcher stopped:\n{}".format(exc),
            )
        except Exception:
            pass

        return 1


if __name__ == "__main__":
    raise SystemExit(main())
