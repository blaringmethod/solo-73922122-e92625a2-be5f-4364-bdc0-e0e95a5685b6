"""FastAPI 应用：上传 GTFS ZIP 并查询某站某日的发车清单。"""

from __future__ import annotations

from datetime import timezone

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

from .gtfs import GTFSValidationError, process_zip
from .schemas import DepartureItem, DeparturesResponse

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 整个 ZIP 上传上限 20 MiB

app = FastAPI(
    title="GTFS 站牌发车清单服务",
    description=(
        "上传运营方 GTFS ZIP，指定站点、本地自然日与 agency_timezone，"
        "返回该站点在查询自然日内按 UTC 排序的发车清单。"
        "数据不合规时整包拒绝，不返回部分结果。"
    ),
    version="1.0.0",
)


@app.exception_handler(GTFSValidationError)
async def gtfs_validation_exception_handler(_request, exc: GTFSValidationError):
    return JSONResponse(
        status_code=400,
        content={"error": "GTFS 数据包被拒绝", "detail": str(exc)},
    )


@app.get("/health", tags=["运维"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/departures",
    response_model=DeparturesResponse,
    tags=["发车查询"],
    summary="上传 GTFS ZIP 并查询某站某日发车清单",
    responses={400: {"description": "数据校验失败，整包拒绝"}},
)
async def departures(
    file: UploadFile = File(..., description="GTFS ZIP 压缩包"),
    stop_id: str = Form(..., description="要查询的站牌 stop_id"),
    query_date: str = Form(
        ..., description="查询的本地自然日，格式 YYYY-MM-DD"
    ),
    agency_timezone: str = Form(
        ..., description="运营方 IANA 时区，例如 Asia/Shanghai"
    ),
) -> DeparturesResponse:
    zip_bytes = await file.read()
    if len(zip_bytes) > MAX_UPLOAD_BYTES:
        raise GTFSValidationError(
            f"上传 ZIP 超过 {MAX_UPLOAD_BYTES} 字节上限"
        )
    if not zip_bytes:
        raise GTFSValidationError("上传文件为空")

    results = process_zip(zip_bytes, stop_id, query_date, agency_timezone)
    return DeparturesResponse(
        stop_id=stop_id.strip(),
        query_date=query_date,
        agency_timezone=agency_timezone.strip(),
        count=len(results),
        departures=[
            DepartureItem(
                trip_id=item.trip_id,
                stop_sequence=item.stop_sequence,
                service_date=item.service_date,
                departure_time=item.departure_time,
                departure_utc=item.departure_utc.astimezone(timezone.utc),
            )
            for item in results
        ],
    )
