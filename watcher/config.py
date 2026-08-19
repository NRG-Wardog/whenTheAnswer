from __future__ import annotations

import os
import platform
from pathlib import Path

TARGET_URL = "https://inbar.biu.ac.il/Live/StudentAssignmentTermList.aspx?edpr=3"
DIRECT_LOGIN_URL = (
    "https://inbar.biu.ac.il/Live/Login.aspx"
    "?ReturnUrl=/Live/StudentAssignmentTermList.aspx?edpr=3"
)
MY_BIU_URL = "https://my.biu.ac.il/"
TARGET_PATH = "StudentAssignmentTermList.aspx"
LOGIN_PATH = "Login.aspx"
TABLE_HINT = "gvStudentAssignmentTermList"

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

GRADE_HEADER_NAMES = {"ציון", "ציון סופי"}
COURSE_HEADER_NAMES = {"שם קבוצת קורס", "שם הקורס", "קורס"}
COURSE_CODE_HEADER_NAMES = {"קוד קבוצת קורס", "קוד קורס", "מספר קורס"}
DATE_HEADER_NAMES = {"תאריך", "תאריך בחינה"}
TERM_HEADER_NAMES = {"מועד", "תקופה"}
NOTEBOOK_HEADER_NAMES = {"מספר מחברת", "מחברת"}

BLOCK_STATUSES = {403, 406, 418, 423}
RATE_LIMIT_STATUSES = {429}
AUTH_STATUSES = {401}
TRANSIENT_STATUSES = {408, 425, 500, 502, 503, 504}

BLOCK_TEXT_PATTERNS = (
    "access denied", "request blocked", "temporarily blocked",
    "your request has been blocked", "unusual traffic", "automated requests",
    "verify you are human", "security challenge", "bot detection", "captcha",
    "forbidden", "הגישה נדחתה", "הבקשה נחסמה", "נחסמת", "אימות אנושי",
    "אימות שאתה אנושי", "אינך מורשה", "קפצ'ה",
)
RATE_LIMIT_TEXT_PATTERNS = (
    "too many requests", "rate limit", "retry later", "request limit",
    "יותר מדי בקשות", "חרגת ממספר הבקשות", "נסה שוב מאוחר יותר",
)

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
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / APP_NAME_WINDOWS
    elif PLATFORM == "linux":
        xdg_data_home = os.environ.get("XDG_DATA_HOME")
        path = (
            Path(xdg_data_home) / APP_NAME_LINUX
            if xdg_data_home
            else Path.home() / ".local" / "share" / APP_NAME_LINUX
        )
    else:
        raise RuntimeError(f"Unsupported operating system: {platform.system()}")
    path.mkdir(parents=True, exist_ok=True)
    return path


APP_DIR = application_directory()
PROFILE_DIR = APP_DIR / "browser_profile"
SNAPSHOT_FILE = APP_DIR / "grade_snapshot.json"
PROTECTION_FILE = APP_DIR / "protection_state.json"
LOG_FILE = APP_DIR / "watcher.log"
LOCK_FILE = APP_DIR / "watcher.lock"
