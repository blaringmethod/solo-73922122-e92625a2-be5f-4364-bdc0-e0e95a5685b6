"""FastAPI 请求/响应模型。"""

from __future__ import annotations

from datetime import date, datetime, timezone

from pydantic import BaseModel, Field, field_serializer


class DepartureItem(BaseModel):
    trip_id: str = Field(..., description="GTFS trip_id")
    stop_sequence: int = Field(..., description="GTFS stop_sequence")
    service_date: date = Field(
        ..., description="GTFS 服务日（本地日历日 YYYY-MM-DD），可能早于查询日"
    )
    departure_time: str = Field(
        ..., description="原始 GTFS 发车时刻，例如 25:30:00"
    )
    departure_utc: datetime = Field(
        ..., description="换算后的 UTC 发车时刻（ISO 8601，带 Z 后缀）"
    )

    @field_serializer("departure_utc")
    def _serialize_utc(self, value: datetime) -> str:
        # 统一输出 UTC，秒级精度，Z 后缀，例如 2026-01-01T16:30:00Z
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DeparturesResponse(BaseModel):
    stop_id: str
    query_date: date
    agency_timezone: str
    count: int
    departures: list[DepartureItem]


class ErrorResponse(BaseModel):
    error: str
    detail: str
