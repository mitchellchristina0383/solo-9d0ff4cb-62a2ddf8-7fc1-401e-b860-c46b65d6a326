# 供水账户用量明细服务

把换表前后的**累计抄表读数**转换为可装载的**账户用量明细**区间结果。
技术栈：FastAPI + SQLite + 原生 HTML/JS 前端，可用 Docker Compose 一键启动。

## 1. 启动

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

SQLite 数据库保存在命名卷 `water-data`（容器内 `/data`），容器重建后数据保留。

### 本地直接运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 默认数据目录 /data，可用环境变量覆盖：
#   WATER_DATA_DIR=./data  WATER_DB_PATH=./data/water.db
```

健康检查：`GET /healthz`；交互式 API 文档：`GET /docs`。

## 2. 输入文件格式（均为 CSV，UTF-8 编码，兼容带 BOM 与 GB18030）

### 读数 CSV（增量上传，可多次）

| 列名 | 含义 |
| --- | --- |
| `account` | 账户号 |
| `meter` | 表号 |
| `read_at` | 抄表时刻，ISO-8601；无时区后缀按 UTC 处理，统一以 UTC 存储 |
| `reading_cum` | 累计立方米读数，非负数 |

### 换表 CSV（整版上传，**每户一条**）

| 列名 | 含义 |
| --- | --- |
| `account` | 账户号 |
| `old_meter` | 旧表表号 |
| `new_meter` | 新表表号 |
| `switched_at` | 切换时刻（UTC） |

换表文件为**整版快照**：每次成功上传都会整体替换旧换表数据（换表更正场景）。

样例见 `samples/` 目录。

## 3. 业务规则

1. **仅同户、同表、相邻的两笔读数可相减**，且区间不得跨越该户的换表切换时刻；
   用量 = 后笔累计读数 − 前笔累计读数，**归属到后笔所在的 UTC 日**。
2. 以下读数标 `REVIEW`（待核），不产生用量：
   - `unpaired_reading`：无法配对（每只表的第一笔、或前序读数已被待核拦截）；
   - `reversed_reading`：累计读数倒退（后笔 < 前笔）；
   - `crosses_switchover`：相邻区间跨越切换时刻；
   - `outside_meter_service_window`：旧表读数晚于切换时刻，或新表读数早于切换时刻；
   - `no_replacement_record`：账户（或该表）在换表整版中无记录，表归属无法确认。
3. **重传去重**：同户同表同时刻、同值的读数只计一次（批内与跨批均生效）；
   同表同时刻**异值**则**整批拒收**（HTTP 422，不写入任何数据，拒收批次留审计记录）。
4. **迟到读数 / 换表更正**：任何新提交的输入都会使当前现役结果自动转为 `expired`（过期），
   需重新生成才会有新的现役结果；过期结果及其明细行仍保留、仍可下载。
5. **生成与上传交错**：生成在单个 `BEGIN IMMEDIATE` 事务内完成，只基于生成时
   **已提交**的输入产出完整快照；生成期间提交的输入不会被半包纳入。
6. **生成失败**（如尚无任何读数）：不发布新结果；旧结果保留可下载，并标记为
   `expired`（`status_detail` 记录失败原因），系统中不会存在声称"现役"的错误结果。

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/uploads/readings` | 增量上传读数 CSV（multipart 字段 `file`） |
| POST | `/api/uploads/replacements` | 整版上传换表 CSV |
| POST | `/api/generations` | 基于当前全部已提交输入生成新一代结果 |
| GET | `/api/generations` | 列出全部结果代际（active/expired/failed、行数、待核数、输入批次） |
| GET | `/api/generations/{id}/rows` | 查看某代明细（JSON，分页参数 `limit`） |
| GET | `/api/generations/{id}/download` | 下载某代结果 CSV（响应头 `X-Generation-Status` 标明状态） |
| GET | `/api/generations/latest/download` | 下载最新一代结果 |
| GET | `/api/batches` | 上传批次审计（含 rejected 批次及拒收原因） |
| GET | `/healthz` | 健康检查 |

### 下载 CSV 列

`generation_id, account, meter, interval_start_utc, interval_end_utc,
start_reading_m3, end_reading_m3, consumption_m3, attributed_utc_day,
status, review_reason, review_reason_detail, source_reading_ids`

其中 `source_reading_ids` 为产生该行的来源读数主键（待核行为单笔，正常区间为前后两笔）。

## 5. 数据模型（SQLite）

- `upload_batches`：每次上传的批次与审计（committed/rejected、拒收原因）；
- `readings`：去重后的累计读数，`(account, meter, read_at)` 唯一；
- `replacements`：当前生效的换表整版快照；
- `generations`：结果代际及生成时所用的输入批次清单；
- `result_rows`：每代的区间明细与待核原因。

## 6. 测试

```bash
pip install -r requirements.txt
python3 -m pytest tests/ -q
```

覆盖：正常配对与 UTC 归属日、首笔无法配对、倒退待核、跨切换与服务窗口、
同值重传去重、异值整批拒收（批内/跨批）、迟到读数过期、换表更正过期、
生成失败保留旧结果、CSV 下载内容、无换表记录待核、朴素时间按 UTC 处理，
以及多线程上传/生成交错的串行化一致性。
