"""FastAPI 接口集成测试：/docs 可用、成功查询、整包拒绝返回 400。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.gtfs_zip_builder import (
    CALENDAR_HEADER,
    STOP_TIMES_HEADER,
    TRIPS_HEADER,
    build_zip,
    calendar_row,
    csv_text,
)

client = TestClient(app)


def good_zip() -> bytes:
    return build_zip(
        {
            "calendar.txt": csv_text(
                CALENDAR_HEADER,
                [calendar_row("S1", "1111111", "20260105", "20260111")],
            ),
            "trips.txt": csv_text(TRIPS_HEADER, [["T1", "S1"]]),
            "stop_times.txt": csv_text(
                STOP_TIMES_HEADER,
                [
                    ["T1", "1", "STOP_A", "07:30:00"],
                    ["T1", "2", "STOP_A", "25:30:00"],
                ],
            ),
        }
    )


def test_health_and_docs_available():
    assert client.get("/health").json() == {"status": "ok"}
    docs = client.get("/docs")
    assert docs.status_code == 200
    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    assert "/departures" in openapi.json()["paths"]


def test_departures_success_envelope_and_utc_z_suffix():
    response = client.post(
        "/departures",
        files={"file": ("gtfs.zip", good_zip(), "application/zip")},
        data={
            "stop_id": "STOP_A",
            "query_date": "2026-01-06",
            "agency_timezone": "Asia/Shanghai",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["stop_id"] == "STOP_A"
    assert body["query_date"] == "2026-01-06"
    assert body["agency_timezone"] == "Asia/Shanghai"
    assert body["count"] == 2
    first, second = body["departures"]
    assert set(first.keys()) == {
        "trip_id",
        "stop_sequence",
        "service_date",
        "departure_time",
        "departure_utc",
    }
    # 上海 UTC+8：25:30 跨日班(1/5 服务日溢出)对应 UTC 17:30，排在前；
    # 07:30 班属于 1/6 服务日，对应 UTC 23:30
    assert first["trip_id"] == "T1" and first["departure_time"] == "25:30:00"
    assert first["stop_sequence"] == 2
    assert first["service_date"] == "2026-01-05"
    assert first["departure_utc"].endswith("Z")
    assert first["departure_utc"] == "2026-01-05T17:30:00Z"
    assert second["departure_time"] == "07:30:00"
    assert second["service_date"] == "2026-01-06"
    assert second["departure_utc"] == "2026-01-05T23:30:00Z"
    # UTC 升序
    assert first["departure_utc"] < second["departure_utc"]


def test_rejected_package_returns_400_with_no_partial_list():
    # trips 引用不存在的 service_id -> 整包拒绝
    bad_zip = build_zip(
        {
            "calendar.txt": csv_text(
                CALENDAR_HEADER,
                [calendar_row("S1", "1111111", "20260105", "20260111")],
            ),
            "trips.txt": csv_text(TRIPS_HEADER, [["T1", "GHOST"]]),
            "stop_times.txt": csv_text(
                STOP_TIMES_HEADER, [["T1", "1", "STOP_A", "07:30:00"]]
            ),
        }
    )
    response = client.post(
        "/departures",
        files={"file": ("bad.zip", bad_zip, "application/zip")},
        data={
            "stop_id": "STOP_A",
            "query_date": "2026-01-06",
            "agency_timezone": "Asia/Shanghai",
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "GTFS 数据包被拒绝"
    assert "悬空引用" in body["detail"]
    assert "departures" not in body


def test_invalid_timezone_returns_400():
    response = client.post(
        "/departures",
        files={"file": ("gtfs.zip", good_zip(), "application/zip")},
        data={
            "stop_id": "STOP_A",
            "query_date": "2026-01-06",
            "agency_timezone": "Mars/Olympus",
        },
    )
    assert response.status_code == 400
    assert "时区" in response.json()["detail"]


def test_invalid_query_date_returns_400():
    response = client.post(
        "/departures",
        files={"file": ("gtfs.zip", good_zip(), "application/zip")},
        data={
            "stop_id": "STOP_A",
            "query_date": "2026/01/06",
            "agency_timezone": "Asia/Shanghai",
        },
    )
    assert response.status_code == 400


def test_empty_upload_returns_400():
    response = client.post(
        "/departures",
        files={"file": ("empty.zip", b"", "application/zip")},
        data={
            "stop_id": "STOP_A",
            "query_date": "2026-01-06",
            "agency_timezone": "Asia/Shanghai",
        },
    )
    assert response.status_code == 400


def test_unknown_stop_returns_empty_list_with_200():
    response = client.post(
        "/departures",
        files={"file": ("gtfs.zip", good_zip(), "application/zip")},
        data={
            "stop_id": "NO_SUCH_STOP",
            "query_date": "2026-01-06",
            "agency_timezone": "Asia/Shanghai",
        },
    )
    assert response.status_code == 200
    assert response.json()["departures"] == []
    assert response.json()["count"] == 0
