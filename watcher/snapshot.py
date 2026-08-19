from __future__ import annotations

import hashlib
from typing import Dict, List, Optional, Set

from .config import (
    COURSE_CODE_HEADER_NAMES,
    COURSE_HEADER_NAMES,
    DATE_HEADER_NAMES,
    GRADE_HEADER_NAMES,
    NOTEBOOK_HEADER_NAMES,
    SNAPSHOT_FILE,
    TERM_HEADER_NAMES,
)
from .utils import atomic_write_json, log, normalize_text, read_json


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


def read_snapshot() -> Optional[List[Dict[str, str]]]:
    data = read_json(SNAPSHOT_FILE, None)
    if not isinstance(data, list):
        return None
    return [
        {normalize_text(key): normalize_text(value) for key, value in item.items()}
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
            f"{header}={value}"
            for header, value in sorted(row.items())
            if value and not is_grade_header(header)
        ]
    return hashlib.sha256("\x1f".join(meaningful).encode("utf-8")).hexdigest()


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
        parts.append(f"Code {code}")
    if date:
        parts.append(date)
    if term:
        parts.append(term)
    return " | ".join(parts)


def compare_snapshots(
    previous: List[Dict[str, str]], current: List[Dict[str, str]]
) -> List[str]:
    previous_by_id = {row_identity(row): row for row in previous}
    changes: List[str] = []
    for row in current:
        old_row = previous_by_id.get(row_identity(row))
        current_grades = grade_values(row)
        if old_row is None:
            if has_meaningful_grade(row):
                values = [
                    f"{header}: {value}"
                    for header, value in current_grades.items()
                    if value
                ]
                changes.append(
                    "New grade\n{}\n{}".format(describe_row(row), " | ".join(values))
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
                differences.append(f"{header} added: {after}")
            elif before and after:
                differences.append(f"{header} changed from {before} to {after}")
            else:
                differences.append(f"{header} removed; previous value was {before}")
        if differences:
            changes.append("{}\n{}".format(describe_row(row), "\n".join(differences)))
    return changes
