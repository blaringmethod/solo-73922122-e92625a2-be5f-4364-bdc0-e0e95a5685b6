"""GTFS ZIP 解析、校验与站牌发车清单查询核心逻辑。

只处理运营方班表中的四张表：
    - calendar.txt        （必需）
    - calendar_dates.txt  （可选）
    - trips.txt           （必需）
    - stop_times.txt      （必需）

任何数据问题都抛出 :class:`GTFSValidationError`，由 API 层整体拒绝，
绝不返回部分清单。
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ---------------------------------------------------------------------------
# 约束常量
# ---------------------------------------------------------------------------

REQUIRED_FILES = ("calendar.txt", "trips.txt", "stop_times.txt")
OPTIONAL_FILES = ("calendar_dates.txt",)
ALL_TABLES = REQUIRED_FILES + OPTIONAL_FILES

MAX_ROWS_PER_FILE = 300
MAX_FILE_BYTES = 10 * 1024 * 1024  # 单张表 10 MiB 上限，防止解压炸弹
MAX_GTFS_TIME = timedelta(hours=35, minutes=59, seconds=59)

_TIME_RE = re.compile(r"^(\d{1,3}):([0-5]\d):([0-5]\d)$")
_DATE_RE = re.compile(r"^\d{8}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NON_NEG_INT_RE = re.compile(r"^\d+$")
# Python weekday(): Monday=0 ... Sunday=6；GTFS: Monday=1 ... Sunday=7
_GTFS_WEEKDAY_COLUMNS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_CALENDAR_REQUIRED = (
    "service_id",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "start_date",
    "end_date",
)
_CALENDAR_DATES_REQUIRED = ("service_id", "date", "exception_type")
_TRIPS_REQUIRED = ("trip_id", "service_id")
_STOP_TIMES_REQUIRED = ("trip_id", "stop_sequence", "stop_id", "departure_time")


class GTFSValidationError(ValueError):
    """整包拒绝级别的数据错误。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalendarEntry:
    service_id: str
    weekdays: frozenset[int]
    start_date: date
    end_date: date


@dataclass(frozen=True)
class CalendarException:
    service_id: str
    day: date
    exception_type: int  # 1 = 增加服务，2 = 移除服务


@dataclass(frozen=True)
class Trip:
    trip_id: str
    service_id: str


@dataclass(frozen=True)
class StopTime:
    trip_id: str
    stop_sequence: int
    stop_id: str
    departure_time: str  # 原始 GTFS 时刻字符串，原样返回
    time_offset: timedelta


@dataclass
class GTFSPackage:
    calendar: dict[str, CalendarEntry]
    exceptions: dict[tuple[str, date], int]
    trips: dict[str, Trip]
    stop_times: list[StopTime]
    stop_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Departure:
    """查询结果中的一条发车记录。"""

    trip_id: str
    stop_sequence: int
    service_date: date  # GTFS 服务日（本地日历日）
    departure_time: str  # 原始 GTFS 时刻，如 "25:30:00"
    departure_utc: datetime


# ---------------------------------------------------------------------------
# 基础解析辅助
# ---------------------------------------------------------------------------


def parse_gtfs_time(value: str) -> timedelta:
    """解析 GTFS 时刻（HH:MM:SS，小时可超过 24，上限 35:59:59）。"""
    match = _TIME_RE.match(value)
    if not match:
        raise GTFSValidationError(
            f"无效 GTFS 时刻 {value!r}，应为 HH:MM:SS（小时可超过 24）"
        )
    hours, minutes, seconds = (int(part) for part in match.groups())
    offset = timedelta(hours=hours, minutes=minutes, seconds=seconds)
    if offset > MAX_GTFS_TIME:
        raise GTFSValidationError(f"GTFS 时刻 {value!r} 超过上限 35:59:59")
    return offset


def parse_gtfs_date(value: str) -> date:
    """解析 GTFS 的 YYYYMMDD 日期。"""
    if not _DATE_RE.match(value):
        raise GTFSValidationError(f"无效 GTFS 日期 {value!r}，应为 YYYYMMDD")
    try:
        return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    except ValueError as exc:
        raise GTFSValidationError(f"无效日历日期 {value!r}: {exc}") from exc


def parse_iso_date(value: str) -> date:
    """解析查询参数中的 YYYY-MM-DD 本地自然日。"""
    if not _ISO_DATE_RE.match(value):
        raise GTFSValidationError(f"查询日期 {value!r} 应为 YYYY-MM-DD 格式")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise GTFSValidationError(f"无效查询日期 {value!r}: {exc}") from exc


def get_timezone(name: str) -> ZoneInfo:
    """校验 agency_timezone 是否为合法 IANA 时区。"""
    if not name or not name.strip():
        raise GTFSValidationError("agency_timezone 不能为空")
    name = name.strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise GTFSValidationError(f"无效 IANA 时区 {name!r}: {exc}") from exc


# ---------------------------------------------------------------------------
# ZIP 与 CSV 读取
# ---------------------------------------------------------------------------


def _safe_zip_member_name(name: str) -> bool:
    """ZIP 条目名不得用于路径穿越。"""
    if name.startswith(("/", "\\")):
        return False
    if re.match(r"^[A-Za-z]:[\\/]", name):  # Windows 盘符
        return False
    # 任何路径分量都不允许是 ".."
    parts = re.split(r"[\\/]+", name)
    return ".." not in parts and "" not in parts[:-1]


def _read_table(zf: zipfile.ZipFile, table_name: str) -> list[dict[str, str]]:
    """读取 ZIP 根目录下的一张 CSV 表，返回去空白后的字典行。"""
    if table_name not in zf.namelist():
        return []
    info = zf.getinfo(table_name)
    if info.is_dir():
        raise GTFSValidationError(f"{table_name} 是目录而不是文件")
    if info.file_size > MAX_FILE_BYTES:
        raise GTFSValidationError(f"{table_name} 超过 {MAX_FILE_BYTES} 字节上限")

    raw = zf.read(table_name)
    if len(raw) > MAX_FILE_BYTES:
        raise GTFSValidationError(f"{table_name} 超过 {MAX_FILE_BYTES} 字节上限")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise GTFSValidationError(f"{table_name} 不是合法 UTF-8 文本: {exc}") from exc

    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        raise GTFSValidationError(f"{table_name} 缺少表头")
    if len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise GTFSValidationError(f"{table_name} 表头存在重复列名")

    rows: list[dict[str, str]] = []
    for raw_row in reader:
        if None in raw_row:  # 列数多于表头
            raise GTFSValidationError(f"{table_name} 存在表头之外的多余字段")
        row = {(k.strip() if k else k): (v.strip() if isinstance(v, str) else v)
               for k, v in raw_row.items()}
        rows.append(row)
        if len(rows) > MAX_ROWS_PER_FILE:
            raise GTFSValidationError(
                f"{table_name} 数据行超过 {MAX_ROWS_PER_FILE} 行上限"
            )
    return rows


def _require_columns(table_name: str, rows: list[dict[str, str]],
                     required: tuple[str, ...]) -> None:
    if not rows:
        raise GTFSValidationError(f"{table_name} 为空，缺少任何数据行")
    present = set(rows[0].keys())
    missing = [column for column in required if column not in present]
    if missing:
        raise GTFSValidationError(
            f"{table_name} 缺少必需列: {', '.join(missing)}"
        )


def _require_value(table_name: str, row: dict[str, str], column: str,
                   row_number: int) -> str:
    value = row.get(column)
    if value is None or value == "":
        raise GTFSValidationError(
            f"{table_name} 第 {row_number} 行的 {column} 为空"
        )
    return value


# ---------------------------------------------------------------------------
# 各表解析与引用完整性校验
# ---------------------------------------------------------------------------


def _parse_calendar(rows: list[dict[str, str]]) -> dict[str, CalendarEntry]:
    _require_columns("calendar.txt", rows, _CALENDAR_REQUIRED)
    calendar: dict[str, CalendarEntry] = {}
    for index, row in enumerate(rows, start=2):  # 含表头，数据从第 2 行起
        service_id = _require_value("calendar.txt", row, "service_id", index)
        if service_id in calendar:
            raise GTFSValidationError(
                f"calendar.txt 第 {index} 行 service_id {service_id!r} 重复定义"
            )
        weekdays: set[int] = set()
        for column, weekday in _GTFS_WEEKDAY_COLUMNS.items():
            flag = _require_value("calendar.txt", row, column, index)
            if flag not in ("0", "1"):
                raise GTFSValidationError(
                    f"calendar.txt 第 {index} 行 {column} 只能是 0 或 1，"
                    f"实际为 {flag!r}"
                )
            if flag == "1":
                weekdays.add(weekday)
        start = parse_gtfs_date(
            _require_value("calendar.txt", row, "start_date", index))
        end = parse_gtfs_date(
            _require_value("calendar.txt", row, "end_date", index))
        if end < start:
            raise GTFSValidationError(
                f"calendar.txt 第 {index} 行 end_date 早于 start_date"
            )
        calendar[service_id] = CalendarEntry(
            service_id=service_id,
            weekdays=frozenset(weekdays),
            start_date=start,
            end_date=end,
        )
    return calendar


def _parse_calendar_dates(
    rows: list[dict[str, str]], calendar: dict[str, CalendarEntry]
) -> dict[tuple[str, date], int]:
    if not rows:
        return {}
    _require_columns("calendar_dates.txt", rows, _CALENDAR_DATES_REQUIRED)
    exceptions: dict[tuple[str, date], int] = {}
    for index, row in enumerate(rows, start=2):
        service_id = _require_value(
            "calendar_dates.txt", row, "service_id", index)
        if service_id not in calendar:
            raise GTFSValidationError(
                f"calendar_dates.txt 第 {index} 行的 service_id "
                f"{service_id!r} 在 calendar.txt 中不存在（悬空引用）"
            )
        day = parse_gtfs_date(
            _require_value("calendar_dates.txt", row, "date", index))
        exception_type_raw = _require_value(
            "calendar_dates.txt", row, "exception_type", index)
        if exception_type_raw not in ("1", "2"):
            raise GTFSValidationError(
                f"calendar_dates.txt 第 {index} 行 exception_type "
                f"只能是 1 或 2，实际为 {exception_type_raw!r}"
            )
        exception_type = int(exception_type_raw)
        key = (service_id, day)
        if key in exceptions:
            if exceptions[key] == exception_type:
                raise GTFSValidationError(
                    f"calendar_dates.txt 第 {index} 行对 {service_id!r} "
                    f"{day.isoformat()} 的例外重复定义"
                )
            raise GTFSValidationError(
                f"calendar_dates.txt 第 {index} 行对 {service_id!r} "
                f"{day.isoformat()} 同时存在增加(1)与移除(2)的冲突例外"
            )
        exceptions[key] = exception_type
    return exceptions


def _parse_trips(rows: list[dict[str, str]],
                 calendar: dict[str, CalendarEntry]) -> dict[str, Trip]:
    _require_columns("trips.txt", rows, _TRIPS_REQUIRED)
    trips: dict[str, Trip] = {}
    for index, row in enumerate(rows, start=2):
        trip_id = _require_value("trips.txt", row, "trip_id", index)
        if trip_id in trips:
            raise GTFSValidationError(
                f"trips.txt 第 {index} 行 trip_id {trip_id!r} 重复定义"
            )
        service_id = _require_value("trips.txt", row, "service_id", index)
        if service_id not in calendar:
            raise GTFSValidationError(
                f"trips.txt 第 {index} 行 trip {trip_id!r} 引用了 "
                f"calendar.txt 中不存在的 service_id {service_id!r}（悬空引用）"
            )
        trips[trip_id] = Trip(trip_id=trip_id, service_id=service_id)
    return trips


def _parse_stop_times(rows: list[dict[str, str]],
                      trips: dict[str, Trip]) -> list[StopTime]:
    _require_columns("stop_times.txt", rows, _STOP_TIMES_REQUIRED)
    stop_times: list[StopTime] = []
    seen_pairs: set[tuple[str, int]] = set()
    for index, row in enumerate(rows, start=2):
        trip_id = _require_value("stop_times.txt", row, "trip_id", index)
        if trip_id not in trips:
            raise GTFSValidationError(
                f"stop_times.txt 第 {index} 行引用了 trips.txt 中不存在的 "
                f"trip_id {trip_id!r}（悬空引用）"
            )
        sequence_raw = _require_value(
            "stop_times.txt", row, "stop_sequence", index)
        if not _NON_NEG_INT_RE.match(sequence_raw):
            raise GTFSValidationError(
                f"stop_times.txt 第 {index} 行 stop_sequence "
                f"{sequence_raw!r} 不是非负整数"
            )
        stop_sequence = int(sequence_raw)
        pair = (trip_id, stop_sequence)
        if pair in seen_pairs:
            raise GTFSValidationError(
                f"stop_times.txt 第 {index} 行 trip {trip_id!r} 的 "
                f"stop_sequence {stop_sequence} 重复"
            )
        seen_pairs.add(pair)
        stop_id = _require_value("stop_times.txt", row, "stop_id", index)
        departure_time = _require_value(
            "stop_times.txt", row, "departure_time", index)
        offset = parse_gtfs_time(departure_time)
        stop_times.append(
            StopTime(
                trip_id=trip_id,
                stop_sequence=stop_sequence,
                stop_id=stop_id,
                departure_time=departure_time,
                time_offset=offset,
            )
        )
    return stop_times


def load_gtfs_zip(zip_bytes: bytes) -> GTFSPackage:
    """解析并整体校验一个 GTFS ZIP 包。"""
    if not zipfile.is_zipfile(io.BytesIO(zip_bytes)):
        raise GTFSValidationError("上传文件不是合法的 ZIP 压缩包")
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        bad = zf.testzip()
        if bad is not None:
            raise GTFSValidationError(f"ZIP 压缩包条目 {bad!r} 校验失败")
        for info in zf.infolist():
            # 符号链接等非常规条目一律拒绝
            if info.external_attr >> 16 == 0o120777:
                raise GTFSValidationError(f"ZIP 包含符号链接条目 {info.filename!r}")
            if not _safe_zip_member_name(info.filename):
                raise GTFSValidationError(
                    f"ZIP 包含不安全的条目路径 {info.filename!r}"
                )
        names = set(zf.namelist())
        missing = [name for name in REQUIRED_FILES if name not in names]
        if missing:
            raise GTFSValidationError(
                "缺少必需表: " + ", ".join(missing)
            )
        table_rows = {name: _read_table(zf, name) for name in ALL_TABLES}
    except zipfile.BadZipFile as exc:
        raise GTFSValidationError(f"ZIP 压缩包损坏: {exc}") from exc

    calendar = _parse_calendar(table_rows["calendar.txt"])
    exceptions = _parse_calendar_dates(
        table_rows["calendar_dates.txt"], calendar
    )
    trips = _parse_trips(table_rows["trips.txt"], calendar)
    stop_times = _parse_stop_times(table_rows["stop_times.txt"], trips)
    return GTFSPackage(
        calendar=calendar,
        exceptions=exceptions,
        trips=trips,
        stop_times=stop_times,
        stop_ids={item.stop_id for item in stop_times},
    )


# ---------------------------------------------------------------------------
# 服务日判定与发车查询
# ---------------------------------------------------------------------------


def _is_active(package: GTFSPackage, service_id: str, day: date) -> bool:
    """按 calendar + calendar_dates 判定某服务日是否运营。"""
    exception = package.exceptions.get((service_id, day))
    if exception is not None:
        return exception == 1  # 例外优先于常规班表
    entry = package.calendar[service_id]
    if not (entry.start_date <= day <= entry.end_date):
        return False
    return day.weekday() in entry.weekdays


def _service_noon_utc(day: date, tz: ZoneInfo) -> datetime:
    """GTFS 服务日中午（本地 12:00）对应的 UTC 时刻。"""
    noon_local = datetime(
        day.year, day.month, day.day, 12, 0, 0, tzinfo=tz
    )
    return noon_local.astimezone(timezone.utc)


def service_midnight_utc(day: date, tz: ZoneInfo) -> datetime:
    """服务日基准 = 本地服务日中午减 12 小时（DST 安全的“本地午夜”）。"""
    return _service_noon_utc(day, tz) - timedelta(hours=12)


def departure_to_utc(service_day: date, offset: timedelta,
                     tz: ZoneInfo) -> datetime:
    """把 (服务日, GTFS 时刻偏移) 换算成 UTC 发车时刻。"""
    base_utc = service_midnight_utc(service_day, tz)
    return base_utc + offset


def query_departures(package: GTFSPackage, stop_id: str, query_day: date,
                     tz: ZoneInfo) -> list[Departure]:
    """返回某站点在某本地自然日内的全部发车，按 UTC 升序。

    凌晨班次可能来自前一个 GTFS 服务日（GTFS 时刻可超过 24:00），
    因此即使查询日当天停运，前一服务日跨日的凌晨班次也必须保留。
    """
    candidate_service_days = (query_day - timedelta(days=1), query_day)

    # 查询自然日的 UTC 窗口同样以“中午减 12 小时”为界，保证 DST 切换日正确
    window_start = service_midnight_utc(query_day, tz)
    window_end = service_midnight_utc(
        query_day + timedelta(days=1), tz
    )

    active_cache: dict[tuple[str, date], bool] = {}

    def active(service_id: str, day: date) -> bool:
        key = (service_id, day)
        if key not in active_cache:
            active_cache[key] = _is_active(package, service_id, day)
        return active_cache[key]

    results: list[Departure] = []
    for stop_time in package.stop_times:
        if stop_time.stop_id != stop_id:
            continue
        trip = package.trips[stop_time.trip_id]
        for service_day in candidate_service_days:
            if not active(trip.service_id, service_day):
                continue
            departure_utc = departure_to_utc(
                service_day, stop_time.time_offset, tz
            )
            if window_start <= departure_utc < window_end:
                results.append(
                    Departure(
                        trip_id=trip.trip_id,
                        stop_sequence=stop_time.stop_sequence,
                        service_date=service_day,
                        departure_time=stop_time.departure_time,
                        departure_utc=departure_utc,
                    )
                )
    results.sort(
        key=lambda item: (
            item.departure_utc,
            item.stop_sequence,
            item.trip_id,
        )
    )
    return results


def process_zip(zip_bytes: bytes, stop_id: str, query_day_raw: str,
                timezone_name: str) -> list[Departure]:
    """API 入口：校验时区与日期，解析 ZIP，执行查询。"""
    tz = get_timezone(timezone_name)
    query_day = parse_iso_date(query_day_raw)
    if not stop_id or not stop_id.strip():
        raise GTFSValidationError("stop_id 不能为空")
    package = load_gtfs_zip(zip_bytes)
    return query_departures(package, stop_id.strip(), query_day, tz)
