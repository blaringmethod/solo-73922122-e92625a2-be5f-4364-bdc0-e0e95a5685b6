"""核心逻辑单元测试：服务日判定、跨日凌晨班次、例外、DST、校验拒绝。"""

from __future__ import annotations

import io
import zipfile
from datetime import date
from zoneinfo import ZoneInfo

import pytest

from app.gtfs import (
    MAX_ROWS_PER_FILE,
    Departure,
    GTFSValidationError,
    get_timezone,
    load_gtfs_zip,
    parse_gtfs_date,
    parse_gtfs_time,
    parse_iso_date,
    query_departures,
)
from tests.gtfs_zip_builder import (
    CALENDAR_DATES_HEADER,
    CALENDAR_HEADER,
    STOP_TIMES_HEADER,
    TRIPS_HEADER,
    build_zip,
    calendar_row,
    csv_text,
)

SH = ZoneInfo("Asia/Shanghai")  # UTC+8，全年无 DST
NY = ZoneInfo("America/New_York")  # 2026-03-08 春令，2026-11-01 秋令


# ---------------------------------------------------------------------------
# 构造常用数据集
# ---------------------------------------------------------------------------


def base_tables() -> dict[str, str]:
    """服务 S1 覆盖 2026-01-05(周一)~2026-01-11(周日)，全周运营。"""
    return {
        "calendar.txt": csv_text(
            CALENDAR_HEADER,
            [calendar_row("S1", "1111111", "20260105", "20260111")],
        ),
        "trips.txt": csv_text(
            TRIPS_HEADER, [["T1", "S1"], ["T2", "S1"]]
        ),
        "stop_times.txt": csv_text(
            STOP_TIMES_HEADER,
            [
                ["T1", "1", "STOP_A", "07:30:00"],
                ["T1", "2", "STOP_B", "08:00:00"],
                ["T2", "1", "STOP_A", "25:30:00"],  # 次日凌晨 01:30
            ],
        ),
    }


def load(tables: dict[str, str]):
    return load_gtfs_zip(build_zip(tables))


# ---------------------------------------------------------------------------
# 基础解析
# ---------------------------------------------------------------------------


def test_parse_gtfs_time_allows_over_24_hours():
    assert parse_gtfs_time("25:30:00").total_seconds() == 25 * 3600 + 1800
    assert parse_gtfs_time("35:59:59").total_seconds() == 35 * 3600 + 3599


@pytest.mark.parametrize("bad", ["24:00:00:00", "12:60:00", "ab:cd:ef", "36:00:00"])
def test_parse_gtfs_time_rejects_bad_values(bad):
    with pytest.raises(GTFSValidationError):
        parse_gtfs_time(bad)


def test_gtfs_time_upper_bound_enforced():
    tables = base_tables()
    tables["stop_times.txt"] = csv_text(
        STOP_TIMES_HEADER, [["T1", "1", "STOP_A", "36:00:00"]]
    )
    with pytest.raises(GTFSValidationError, match="35:59:59"):
        load(tables)


def test_parse_dates():
    assert parse_gtfs_date("20260105") == date(2026, 1, 5)
    assert parse_iso_date("2026-01-05") == date(2026, 1, 5)
    with pytest.raises(GTFSValidationError):
        parse_gtfs_date("2026-13-01")
    with pytest.raises(GTFSValidationError):
        parse_iso_date("2026/01/05")


def test_invalid_timezone_rejected():
    with pytest.raises(GTFSValidationError):
        get_timezone("Asia/NoSuchCity")
    with pytest.raises(GTFSValidationError):
        get_timezone("")


# ---------------------------------------------------------------------------
# 查询：自然日筛选、跨日凌晨班次、排序
# ---------------------------------------------------------------------------


def test_basic_query_filters_by_stop_and_day():
    package = load(base_tables())
    result = query_departures(package, "STOP_A", date(2026, 1, 6), SH)
    # 窗口内两条：
    #   T2 25:30 来自 1/5 服务日的溢出（UTC 17:30）
    #   T1 07:30 来自 1/6 服务日本身（UTC 23:30）
    # 按 UTC 升序，溢出的凌晨班在前
    assert [(d.trip_id, d.departure_time) for d in result] == [
        ("T2", "25:30:00"),
        ("T1", "07:30:00"),
    ]
    # 基准 = 服务日中午(上海 12:00 = 04:00 UTC)减 12h = 前一日 16:00 UTC
    assert result[0].service_date == date(2026, 1, 5)
    assert result[0].departure_utc.isoformat() == "2026-01-05T17:30:00+00:00"
    assert result[1].service_date == date(2026, 1, 6)
    assert result[1].departure_utc.isoformat() == "2026-01-05T23:30:00+00:00"
    # 1/6 服务日自己的 T2(25:30) 物理时刻在 1/7 自然日，应出现在 1/7 的结果里
    seventh = query_departures(package, "STOP_A", date(2026, 1, 7), SH)
    assert any(
        d.trip_id == "T2" and d.service_date == date(2026, 1, 6)
        for d in seventh
    )


def test_early_morning_departure_comes_from_previous_service_day():
    tables = base_tables()
    # 只保留跨日班次 T2（25:30:00 = 服务日次日凌晨 01:30）
    tables["stop_times.txt"] = csv_text(
        STOP_TIMES_HEADER, [["T2", "1", "STOP_A", "25:30:00"]]
    )
    package = load(tables)
    # 查覆盖首日 1 月 5 日：跨日班次只能来自 1 月 4 日服务日，但 4 日不在 S1 范围
    assert query_departures(package, "STOP_A", date(2026, 1, 5), SH) == []
    # 查 1 月 6 日：来自 1 月 5 日服务日的跨日班次（25:30 -> 1 月 6 日凌晨本地）
    result = query_departures(package, "STOP_A", date(2026, 1, 6), SH)
    assert len(result) == 1
    dep = result[0]
    assert dep.trip_id == "T2"
    assert dep.service_date == date(2026, 1, 5)
    assert dep.departure_time == "25:30:00"
    # 1 月 5 日服务日基准(上海午夜) = 1 月 4 日 16:00 UTC，+25.5h = 1 月 5 日 17:30 UTC
    assert dep.departure_utc.isoformat() == "2026-01-05T17:30:00+00:00"


def test_stopped_query_day_keeps_overnight_departures():
    """查询日当天停运，前一服务日跨日凌晨班次不能被删除。"""
    tables = base_tables()
    # 1 月 7 日移除服务；T2 在 1 月 6 日服务日 25:30 发车，即 1 月 7 日凌晨
    tables["calendar_dates.txt"] = csv_text(
        CALENDAR_DATES_HEADER, [["S1", "20260107", "2"]]
    )
    package = load(tables)
    result = query_departures(package, "STOP_A", date(2026, 1, 7), SH)
    # T1 的 07:30 属于 7 日服务日 -> 被例外移除；T2 的 25:30 来自 6 日服务日 -> 保留
    assert [(d.trip_id, d.departure_time) for d in result] == [
        ("T2", "25:30:00")
    ]
    assert result[0].service_date == date(2026, 1, 6)


def test_exception_type_2_removes_whole_service_day():
    tables = base_tables()
    tables["calendar_dates.txt"] = csv_text(
        CALENDAR_DATES_HEADER, [["S1", "20260106", "2"]]
    )
    package = load(tables)
    # 1 月 6 日服务日整个移除：当天白天班次消失；1 月 5 日服务日仍正常，
    # 其 25:30 跨日班次落在 1 月 6 日自然日内，必须保留
    result_6th = query_departures(package, "STOP_A", date(2026, 1, 6), SH)
    assert [(d.trip_id, d.service_date) for d in result_6th] == [
        ("T2", date(2026, 1, 5))
    ]
    # 对照：没有例外时 1 月 6 日应有两条（T2 跨日 + T1 白天）
    baseline = query_departures(load(base_tables()), "STOP_A",
                                date(2026, 1, 6), SH)
    assert len(baseline) == 2
    # 查 1 月 7 日：来自 6 日服务日的跨日班次随例外一并消失，只剩 7 日自身的 T1
    result_7th = query_departures(package, "STOP_A", date(2026, 1, 7), SH)
    assert all(d.service_date != date(2026, 1, 6) for d in result_7th)
    assert [(d.trip_id, d.departure_time) for d in result_7th] == [
        ("T1", "07:30:00")
    ]


def test_exception_type_1_adds_service_outside_calendar():
    tables = base_tables()
    # S1 常规只覆盖 1/05~1/11；1 月 4 日（周日）通过例外加开
    tables["calendar_dates.txt"] = csv_text(
        CALENDAR_DATES_HEADER, [["S1", "20260104", "1"]]
    )
    package = load(tables)
    result = query_departures(package, "STOP_A", date(2026, 1, 4), SH)
    # 加开的 1/4 服务日：T1 的 07:30 在 4 日自然日内；
    # T2 的 25:30 属于 4 日服务日但物理时刻落在 5 日，故不在 4 日窗口
    trip_ids = {d.trip_id for d in result}
    assert trip_ids == {"T1"}
    # 验证 T2 确实因加开而在 5 日凌晨出现
    next_day = query_departures(package, "STOP_A", date(2026, 1, 5), SH)
    assert any(d.trip_id == "T2" and d.service_date == date(2026, 1, 4)
               for d in next_day)


def test_weekday_filter_in_calendar():
    tables = base_tables()
    # S1 只在周一运营（2026-01-05 是周一）
    tables["calendar.txt"] = csv_text(
        CALENDAR_HEADER,
        [calendar_row("S1", "1000000", "20260105", "20260111")],
    )
    package = load(tables)
    monday = query_departures(package, "STOP_A", date(2026, 1, 5), SH)
    tuesday = query_departures(package, "STOP_A", date(2026, 1, 6), SH)
    # 周一服务日：T1 白天班次在周一窗口；T2 的 25:30 物理时刻在周二凌晨
    assert {d.trip_id for d in monday} == {"T1"}
    # 周二自身不运营，窗口内只有周一服务日溢出的 T2
    assert [(d.trip_id, d.service_date) for d in tuesday] == [
        ("T2", date(2026, 1, 5))
    ]


def test_result_sorted_by_utc():
    tables = base_tables()
    tables["stop_times.txt"] = csv_text(
        STOP_TIMES_HEADER,
        [
            ["T2", "1", "STOP_A", "24:10:00"],  # 次日 00:10 本地
            ["T1", "1", "STOP_A", "06:00:00"],
            ["T1", "2", "STOP_A", "06:00:00"],  # 同时刻，靠 stop_sequence 决胜
        ],
    )
    package = load(tables)
    result = query_departures(package, "STOP_A", date(2026, 1, 6), SH)
    # 上海 UTC+8：T2 的 24:10(=本地次日 00:10) 对应 UTC 16:10 最早；
    # 两条 06:00 同为 UTC 22:00，靠 stop_sequence 决胜
    assert [d.trip_id for d in result] == ["T2", "T1", "T1"]
    assert [d.stop_sequence for d in result[1:]] == [1, 2]
    assert all(isinstance(d, Departure) for d in result)


def test_unknown_stop_returns_empty_not_error():
    package = load(base_tables())
    assert query_departures(package, "GHOST", date(2026, 1, 6), SH) == []


def test_time_35_59_59_can_spill_into_query_window():
    tables = base_tables()
    tables["stop_times.txt"] = csv_text(
        STOP_TIMES_HEADER, [["T1", "1", "STOP_A", "35:59:59"]]
    )
    package = load(tables)
    # 服务日 1/6 的 35:59:59 ≈ 1/7 上午 11:59:59 本地，落在 1/7 自然日
    result = query_departures(package, "STOP_A", date(2026, 1, 7), SH)
    assert len(result) == 1
    assert result[0].service_date == date(2026, 1, 6)


# ---------------------------------------------------------------------------
# DST：以“服务日中午减 12 小时”为基准
# ---------------------------------------------------------------------------


def test_dst_spring_forward_new_york():
    """2026-03-08 美国东部春令（02:00 -> 03:00）。"""
    tables = {
        "calendar.txt": csv_text(
            CALENDAR_HEADER,
            [calendar_row("S1", "1111111", "20260301", "20260331")],
        ),
        "trips.txt": csv_text(TRIPS_HEADER, [["T1", "S1"]]),
        "stop_times.txt": csv_text(
            STOP_TIMES_HEADER,
            [
                ["T1", "1", "STOP_A", "01:30:00"],   # 本地 01:30 EST = 06:30 UTC
                ["T1", "2", "STOP_A", "25:30:00"],   # 3/9 01:30 EDT = 05:30 UTC
            ],
        ),
    }
    package = load(tables)
    result = query_departures(package, "STOP_A", date(2026, 3, 8), NY)
    utc_values = [d.departure_utc.isoformat() for d in result]
    # 题目规定基准 = 服务日中午减 12h：
    # 3/7(EST, UTC-5) 中午 = 17:00 UTC -> 基准 05:00 UTC；
    #   其 25:30 溢出班 -> 05:00 + 25.5h = 3/8 06:30 UTC，落在 3/8 窗口
    # 3/8 春令当天中午 12:00 EDT(UTC-4) = 16:00 UTC -> 基准 04:00 UTC；
    #   当天 01:30 GTFS 时刻 -> 04:00 + 1.5h = 05:30 UTC
    # 注意按基准换算，01:30 不对应墙钟推导的 06:30
    assert [(d.service_date.isoformat(), d.departure_time) for d in result] == [
        ("2026-03-08", "01:30:00"),
        ("2026-03-07", "25:30:00"),
    ]
    assert utc_values == [
        "2026-03-08T05:30:00+00:00",
        "2026-03-08T06:30:00+00:00",
    ]
    # 3/8 服务日自己的 25:30 落在 3/9 自然日：基准 3/8 04:00 UTC + 25.5h
    ninth = query_departures(package, "STOP_A", date(2026, 3, 9), NY)
    spill = [d for d in ninth if d.service_date == date(2026, 3, 8)]
    assert spill[0].departure_utc.isoformat() == "2026-03-09T05:30:00+00:00"
    # 非 DST 日对照：3/7 当天的 01:30（EST）= 基准 05:00 + 1.5h = 06:30 UTC
    sunday = query_departures(package, "STOP_A", date(2026, 3, 7), NY)
    own = [d for d in sunday if d.service_date == date(2026, 3, 7)]
    assert own[0].departure_utc.isoformat() == "2026-03-07T06:30:00+00:00"


def test_dst_fall_back_new_york():
    """2026-11-01 秋令（02:00 退回 01:00），本地 01:30 会出现两次。"""
    tables = {
        "calendar.txt": csv_text(
            CALENDAR_HEADER,
            [calendar_row("S1", "1111111", "20261025", "20261107")],
        ),
        "trips.txt": csv_text(TRIPS_HEADER, [["T1", "S1"], ["T2", "S1"]]),
        "stop_times.txt": csv_text(
            STOP_TIMES_HEADER,
            [
                # 10/31 服务日跨日：11/1 凌晨 01:30 EDT(UTC-4) = 05:30 UTC
                ["T1", "1", "STOP_A", "25:30:00"],
                # 11/1 服务日凌晨 01:30 EST(UTC-5) = 06:30 UTC
                ["T2", "1", "STOP_A", "01:30:00"],
            ],
        ),
    }
    package = load(tables)
    result = query_departures(package, "STOP_A", date(2026, 11, 1), NY)
    assert [(d.trip_id, d.service_date) for d in result] == [
        ("T1", date(2026, 10, 31)),
        ("T2", date(2026, 11, 1)),
    ]
    assert [d.departure_utc.hour for d in result] == [5, 6]


# ---------------------------------------------------------------------------
# 整包拒绝：缺表、悬空引用、冲突/重复例外、行数上限等
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["calendar.txt", "trips.txt", "stop_times.txt"])
def test_missing_required_table_rejected(missing):
    tables = base_tables()
    del tables[missing]
    with pytest.raises(GTFSValidationError, match="缺少必需表"):
        load(tables)


def test_optional_calendar_dates_missing_is_fine():
    package = load(base_tables())
    assert package.exceptions == {}


def test_dangling_service_id_in_trips_rejected():
    tables = base_tables()
    tables["trips.txt"] = csv_text(TRIPS_HEADER, [["T1", "GHOST_SVC"]])
    with pytest.raises(GTFSValidationError, match="悬空引用"):
        load(tables)


def test_dangling_trip_id_in_stop_times_rejected():
    tables = base_tables()
    tables["stop_times.txt"] = csv_text(
        STOP_TIMES_HEADER, [["GHOST_TRIP", "1", "STOP_A", "07:00:00"]]
    )
    with pytest.raises(GTFSValidationError, match="悬空引用"):
        load(tables)


def test_dangling_service_id_in_calendar_dates_rejected():
    tables = base_tables()
    tables["calendar_dates.txt"] = csv_text(
        CALENDAR_DATES_HEADER, [["GHOST_SVC", "20260106", "1"]]
    )
    with pytest.raises(GTFSValidationError, match="悬空引用"):
        load(tables)


def test_conflicting_exceptions_rejected():
    tables = base_tables()
    tables["calendar_dates.txt"] = csv_text(
        CALENDAR_DATES_HEADER,
        [["S1", "20260106", "1"], ["S1", "20260106", "2"]],
    )
    with pytest.raises(GTFSValidationError, match="冲突例外"):
        load(tables)


def test_duplicate_exception_rejected():
    tables = base_tables()
    tables["calendar_dates.txt"] = csv_text(
        CALENDAR_DATES_HEADER,
        [["S1", "20260106", "1"], ["S1", "20260106", "1"]],
    )
    with pytest.raises(GTFSValidationError, match="重复定义"):
        load(tables)


def test_duplicate_trip_id_rejected():
    tables = base_tables()
    tables["trips.txt"] = csv_text(
        TRIPS_HEADER, [["T1", "S1"], ["T1", "S1"]]
    )
    with pytest.raises(GTFSValidationError, match="重复定义"):
        load(tables)


def test_bad_exception_type_rejected():
    tables = base_tables()
    tables["calendar_dates.txt"] = csv_text(
        CALENDAR_DATES_HEADER, [["S1", "20260106", "3"]]
    )
    with pytest.raises(GTFSValidationError, match="exception_type"):
        load(tables)


def test_empty_required_field_rejected():
    tables = base_tables()
    tables["stop_times.txt"] = csv_text(
        STOP_TIMES_HEADER, [["T1", "1", "STOP_A", ""]]
    )
    with pytest.raises(GTFSValidationError, match="departure_time 为空"):
        load(tables)


def test_missing_required_column_rejected():
    tables = base_tables()
    tables["stop_times.txt"] = "trip_id,stop_sequence,departure_time\nT1,1,07:00:00\n"
    with pytest.raises(GTFSValidationError, match="缺少必需列"):
        load(tables)


def test_row_limit_enforced():
    tables = base_tables()
    rows = [[f"T{i}", "S1"] for i in range(MAX_ROWS_PER_FILE + 1)]
    tables["trips.txt"] = csv_text(TRIPS_HEADER, rows)
    with pytest.raises(GTFSValidationError, match="300 行上限"):
        load(tables)


def test_not_a_zip_rejected():
    with pytest.raises(GTFSValidationError, match="不是合法的 ZIP"):
        load_gtfs_zip(b"this is definitely not a zip file")


def test_zip_path_traversal_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("../evil.txt", "pwned")
    with pytest.raises(GTFSValidationError, match="不安全"):
        load_gtfs_zip(buffer.getvalue())


def test_end_before_start_rejected():
    tables = base_tables()
    tables["calendar.txt"] = csv_text(
        CALENDAR_HEADER,
        [calendar_row("S1", "1111111", "20260111", "20260105")],
    )
    with pytest.raises(GTFSValidationError, match="end_date"):
        load(tables)
