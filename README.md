# GTFS 站牌发车清单服务

把运营方 GTFS 班表 ZIP 转成站牌系统可读取的「某站点 × 某本地自然日」发车清单。
技术栈：Python 3.12 + FastAPI + 标准库 `zipfile` / `csv` / `zoneinfo`，无第三方 GTFS 依赖。

## 快速启动（Docker Compose）

```bash
docker compose up --build
# 服务地址
#   API 文档（上传查询入口）: http://localhost:8000/docs
#   OpenAPI Schema         : http://localhost:8000/openapi.json
#   健康检查               : http://localhost:8000/health
```

## 本地直接运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口

### `POST /departures`（multipart/form-data）

| 字段 | 说明 |
| --- | --- |
| `file` | GTFS ZIP 压缩包 |
| `stop_id` | 要查询的站牌 ID |
| `query_date` | 查询的本地自然日，`YYYY-MM-DD` |
| `agency_timezone` | 运营方 IANA 时区，如 `Asia/Shanghai`、`America/New_York` |

响应（按 `departure_utc` 升序）：

```json
{
  "stop_id": "STOP_A",
  "query_date": "2026-01-06",
  "agency_timezone": "Asia/Shanghai",
  "count": 3,
  "departures": [
    {
      "trip_id": "T200",
      "stop_sequence": 1,
      "service_date": "2026-01-05",
      "departure_time": "25:30:00",
      "departure_utc": "2026-01-05T17:30:00Z"
    }
  ]
}
```

curl 示例：

```bash
curl -X POST http://localhost:8000/departures \
  -F "file=@sample.zip;type=application/zip" \
  -F "stop_id=STOP_A" \
  -F "query_date=2026-01-06" \
  -F "agency_timezone=Asia/Shanghai"
```

## 核心业务规则

### 1. 只读取四张表

- 必需：`calendar.txt`、`trips.txt`、`stop_times.txt`
- 可选：`calendar_dates.txt`
- 每张表数据行至多 300 行；GTFS 时刻上限 `35:59:59`（小时允许超过 24）
- ZIP 仅按根目录精确文件名读取；拒绝路径穿越、符号链接条目与非 UTF-8 内容

### 2. 服务日判定（calendar + calendar_dates）

1. 先查 `calendar_dates.txt`：例外优先于常规班表
   - `exception_type=1`：当日加开
   - `exception_type=2`：当日停运
   - 同一 `(service_id, date)` 出现两条例外即冲突/重复，整包拒绝
2. 无例外时按 `calendar.txt`：日期落在 `[start_date, end_date]` 且对应 weekday 标志为 1

### 3. UTC 换算（DST 安全）

基准 = **GTFS 服务日本地中午 12:00 减去 12 小时**（由该时区中午的 UTC 时刻反推，
避免在夏令时切换日直接构造本地午夜带来的歧义）：

```
departure_utc = 服务日12:00(本地)的UTC时刻 - 12h + HH:MM:SS 时间偏移
```

GTFS 时刻可以超过 24:00，表示同一服务日跨到次日凌晨的班次。

### 4. 自然日筛选与凌晨班次

- 对查询自然日 D，只需要检查 **D 与 D−1** 两个 GTFS 服务日
  （最大时刻 35:59:59 决定更早的服务日不可能落入 D 的窗口）
- 查询窗口 `[D 基准, D+1 基准)` 同样按「中午减 12 小时」计算，DST 切换日也正确
- **凌晨发车可能来自前一服务日；即使查询日当天停运，前一服务日跨日的凌晨班次仍保留**
- 查询日窗口内无任何班次时返回空列表（200），站点不存在不是数据错误

### 5. 整包拒绝（HTTP 400，不返回部分清单）

以下情况一律拒绝整个 ZIP：

- 缺少任一必需表 / 表头缺列 / 必需字段为空 / 表为空
- 悬空引用：`trips.service_id`、`calendar_dates.service_id` 不在 `calendar.txt`；
  `stop_times.trip_id` 不在 `trips.txt`
- `calendar_dates` 同一服务日冲突（同时 1 和 2）或重复定义
- 非法时刻、超过 35:59:59、非法日期、weekday/exception_type 取值非法、
  `end_date < start_date`、`trip_id` 或 `(trip_id, stop_sequence)` 重复
- 单表超过 300 行、文件不是合法 ZIP、ZIP 内含不安全路径
- `agency_timezone` 不是合法 IANA 时区、`query_date` 不是 `YYYY-MM-DD`

错误响应统一为：

```json
{"error": "GTFS 数据包被拒绝", "detail": "具体原因（含表名与行号）"}
```

## 项目结构

```
app/
  gtfs.py      # ZIP 解析、校验、服务日判定、UTC 换算与查询核心
  schemas.py   # Pydantic 请求/响应模型（UTC 输出带 Z 后缀）
  main.py      # FastAPI 路由、异常处理、上传大小限制
tests/         # pytest 单元与接口集成测试（43 个用例）
examples/sample_gtfs/  # 可直接打包上传的示例四张表
Dockerfile
docker-compose.yml
```

## 运行测试

```bash
pip install pytest httpx
python3 -m pytest tests/ -q
```

测试覆盖：跨日凌晨班次归属、查询日停运保留凌晨班、节假日加开/移除、
纽约时区 2026 春令/秋令 DST 换算、UTC 排序、悬空引用、冲突例外、
300 行上限、路径穿越、非 ZIP 上传、非法时区/日期以及 API 400 语义。

## 安全说明

- 上传 ZIP 限制 20 MiB，单表解压后限制 10 MiB / 300 行
- 容器内以非 root 用户（uid 10001）运行
- 不信任 ZIP 内路径，拒绝绝对路径、盘符、`..` 与符号链接条目
