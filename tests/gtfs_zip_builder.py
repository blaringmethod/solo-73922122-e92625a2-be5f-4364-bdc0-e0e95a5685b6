"""测试辅助：在内存中构造 GTFS ZIP。"""

from __future__ import annotations

import io
import zipfile


def csv_text(header: list[str], rows: list[list[str]]) -> str:
    lines = [",".join(header)]
    lines.extend(",".join(row) for row in rows)
    return "\n".join(lines) + "\n"


def build_zip(tables: dict[str, str]) -> bytes:
    """tables: 表名 -> CSV 文本。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in tables.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def calendar_row(service_id: str, weekdays: str, start: str, end: str) -> list[str]:
    # weekdays: 7 个 0/1，周一到周日
    flags = list(weekdays)
    return [service_id, *flags, start, end]


CALENDAR_HEADER = [
    "service_id", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday", "start_date", "end_date",
]
CALENDAR_DATES_HEADER = ["service_id", "date", "exception_type"]
TRIPS_HEADER = ["trip_id", "service_id"]
STOP_TIMES_HEADER = [
    "trip_id", "stop_sequence", "stop_id", "departure_time",
]
