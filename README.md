# 共用产线食品换线过敏原放行服务

面向共用产线的食品生产换线场景：按产线顺序登记生产批次、所用原料批号及其过敏原、
成品声明过敏原，以及批次开始前清洁核验确认清除的过敏原；服务自动计算每批**实际可能
含有的过敏原、未声明项**，给出**放行（RELEASE）/扣留（WITHHELD）结论及中文原因**。

技术栈：Python 3.11+ · FastAPI · SQLite（标准库 `sqlite3`，WAL 模式）· Uvicorn · Docker / Compose。

---

## 1. 核心裁决规则

按每条产线（`line_code`）独立、按登记顺序（`seq`）逐批计算：

```
上一批留存在产线的过敏原 incoming
  └─ 本次清洁核验中命中的项目 covered = incoming ∩ cleaning     # 仅这些被清除
     残留 remaining = incoming − covered
本批原料过敏原 material_allergens = ⋃(各原料批号过敏原)
实际可能含有 actual = remaining ∪ material_allergens
未声明项 undeclared = actual − declared
undeclared 非空 ⇒ WITHHELD（扣留）；否则 RELEASE（放行）
```

要点：

- **清洁只清除被本次核验确认覆盖的项目**，其余残留继续留在产线上。
- **上一批即使被扣留，其实际过敏原仍照常向下传递**（扣留不等于已清洁）。
- 残留过敏原的来源（上批残留 / 具体原料批号）会在响应 `sources` 中逐项标注。
- 原料批号或清洁/声明**更正**后，从被更正批次开始**按产线顺序重算该批及全部后续批次**；
  若更正的是**原料批号主数据的过敏原**（登记有误），则所有直接使用该批号的批次，
  按各自产线从最早受影响批次起**跨产线重算**（更正点之前的批次不受影响）。
  每次重算都会**追加新的裁决版本**，历史版本永久保留，可通过历史接口查询。
- 以下请求整体拒绝，**事务保证不留任何部分改动**：
  - 同一产线重复批次号 → `409 DUPLICATE_BATCH`（原料批号重复 → `409 DUPLICATE_MATERIAL`）
  - 未知原料批号 → `400 UNKNOWN_MATERIAL`
  - 无效过敏原标识 → `400 INVALID_ALLERGEN`
  - 更正目标批次不存在 → `404 BATCH_NOT_FOUND`
  - 更正目标原料批号不存在 → `404 MATERIAL_NOT_FOUND`

---

## 2. 一键启动

### Docker Compose（推荐）

```bash
docker compose up --build -d
docker compose ps        # 查看状态（含 /health 健康检查）
docker compose logs -f
```

启动即自动完成初始化：创建 SQLite 数据库文件、建表（幂等），随后提供 HTTP 服务。
数据保存在命名卷 `allergen_data`（容器内 `/data/allergen.db`），容器重建不丢数据。

停止与清理：

```bash
docker compose down          # 停止并移除容器（保留数据卷）
docker compose down -v       # 同时删除数据卷（清空全部数据）
```

### 本地直接运行（无 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
# 或：python -m app.main（监听地址/端口取环境变量，见下）
```

首次启动会在 `ALLERGEN_DB_PATH` 所在目录自动建库建表。

### 地址与端口

| 用途 | 地址 |
| --- | --- |
| 服务根路径 | <http://localhost:8000/> |
| 健康检查 | <http://localhost:8000/health> |
| Swagger 交互文档 | <http://localhost:8000/docs> |
| OpenAPI Schema | <http://localhost:8000/openapi.json> |
| ReDoc 文档 | <http://localhost:8000/redoc> |

宿主机端口默认映射 `8000:8000`，可用环境变量改宿主端口：
`ALLERGEN_PUBLIC_PORT=18000 docker compose up --build -d` → <http://localhost:18000/>。

---

## 3. 配置（全部为环境变量，均有默认值）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ALLERGEN_DB_PATH` | 本地运行默认 `data/allergen.db`；Docker 中为 `/data/allergen.db` | SQLite 数据库文件路径，启动自动建表 |
| `ALLERGEN_HOST` | `0.0.0.0` | 服务监听地址 |
| `ALLERGEN_PORT` | `8000` | 容器内监听端口 |
| `ALLERGEN_DEFAULT_LINE` | `L1` | 登记批次未指定 `line_code` 时使用的默认产线 |
| `ALLERGEN_PUBLIC_PORT` | `8000` | 仅 compose 使用：宿主机映射端口 |

---

## 4. HTTP API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/allergens` | 过敏原标识目录（规范代码、中文名、可接受别名） |
| POST | `/materials` | 登记原料批号及其过敏原 |
| GET | `/materials` / `/materials/{lot_no}` | 原料列表 / 详情 |
| PUT | `/materials/{lot_no}` | 更正原料批号主数据（名称/过敏原），跨产线重算 |
| GET | `/materials/{lot_no}/corrections` | 原料主数据更正审计 |
| POST | `/batches` | 按产线顺序登记生产批次并立即裁决 |
| GET | `/batches?line_code=L1` | 批次列表（可按产线过滤） |
| GET | `/batches/{id}` | 批次详情（含当前最新裁决） |
| PUT | `/batches/{id}` | 更正批次的原料批号引用/声明/清洁，级联重算 |
| GET | `/batches/{id}/history` | 该批全部历史裁决版本 |
| GET | `/batches/{id}/corrections` | 该批的批次级更正审计 |
| GET | `/lines` / `/lines/{code}` | 产线列表 / 产线状态（末批结论、留存过敏原） |

过敏原标识可传**规范代码**（如 `MILK`、`EGG`、`PEANUT`）或**中文名**
（如 `乳`、`蛋`、`花生`），服务统一规范化。完整目录见 `GET /allergens`。

### 4.1 登记原料批号

```bash
curl -s -X POST http://localhost:8000/materials \
  -H 'Content-Type: application/json' \
  -d '{"lot_no":"RM-MILK-01","material_name":"蛋奶液","allergens":["乳","蛋"]}'
```

### 4.2 登记生产批次

```bash
curl -s -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_no": "B2026-001",
    "line_code": "L1",
    "material_lots": ["RM-MILK-01"],
    "declared_allergens": ["MILK","EGG"],
    "cleaning": []
  }'
```

响应中的 `verdict` 关键字段：

| 字段 | 含义 |
| --- | --- |
| `incoming_residual` | 本批开始前上一批留在产线的过敏原 |
| `cleaning_covered` | 本次清洁核验实际清除（命中残留）的项目 |
| `remaining_carryover` | 清洁后仍残留、继续向后传递的过敏原 |
| `material_allergens` | 本批原料过敏原并集 |
| `actual_allergens` | **实际可能含有的过敏原**（残留 ∪ 原料） |
| `undeclared_allergens` | **未声明项**（实际含有但未在成品声明） |
| `decision` | `RELEASE` 放行 / `WITHHELD` 扣留 |
| `reason` | 中文结论原因，逐项说明未声明项来源 |
| `sources` | 每个过敏原来自上批残留和/或哪些原料批号 |

### 4.3 更正并级联重算

更正有两类，均只追加历史、不覆盖旧裁决：

**批次级更正** `PUT /batches/{id}`：改本批的原料批号引用 / 声明 / 清洁核验，
只传需要修改的字段（`material_lots` / `declared_allergens` / `cleaning`）。
从被更正批次起按产线顺序重算该批及后续批次：

```bash
curl -s -X PUT http://localhost:8000/batches/2 \
  -H 'Content-Type: application/json' \
  -d '{"cleaning": ["MILK","EGG"]}'
```

**原料主数据更正** `PUT /materials/{lot_no}`：原料批号登记的名称 / 过敏原有误时
更正主数据；服务自动找出所有使用该批号的批次，按各自产线从最早受影响批次起
跨产线重算（直接使用批号的批次新版本标记为 `MATERIAL_CORRECTION`，
其后级联批次标记为 `RECASCADE`）：

```bash
curl -s -X PUT http://localhost:8000/materials/RM-WATER-01 \
  -H 'Content-Type: application/json' \
  -d '{"allergens": ["SOY"]}'
```

两类更正响应均含 `recalculated_batch_ids`；批次级更正另有 `correction_event_id`，
原料更正另有 `material_correction_event_id`。更正点之前的批次不受影响。
审计可查 `GET /batches/{id}/corrections` 与 `GET /materials/{lot_no}/corrections`。

### 4.4 查询历史裁决版本

```bash
curl -s http://localhost:8000/batches/2/history
```

### 4.5 典型换线场景（清洁只清除部分残留）

| 批次 | 原料过敏原 | 声明 | 清洁核验 | 结果 |
| --- | --- | --- | --- | --- |
| B1 | MILK, EGG | MILK, EGG | — | 放行；产线残留 MILK、EGG |
| B2 | 无 | 无 | 仅 MILK | **扣留**：EGG 未被清洁覆盖而残留，且未声明；继续向 B3 传递 EGG |
| B2 更正 | 无 | 无 | MILK, EGG | 重算后放行；B3 同步重算，残留清空 |

---

## 5. 调用示例

- `examples/call_api.py`：仅依赖 Python 标准库，端到端演示
  登记 → 裁决 → 扣留 → 更正重算 → 历史查询 → 四类拒绝场景：

  ```bash
  python examples/call_api.py http://localhost:8000
  ```

- `examples/requests.http`：VS Code REST Client / JetBrains HTTP Client 可直接发送。

---

## 6. 测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

覆盖：残留传递、部分清洁覆盖、扣留批仍传递、更正级联重算、历史版本追加、
多产线隔离、重复批次号/未知原料/无效标识/更正目标不存在的拒绝，以及失败无部分改动。

---

## 7. 数据模型（SQLite）

- `material_lots`：原料批号主数据（过敏原 JSON 数组）
- `batches`：生产批次（产线、批次号、产线内顺序 `seq`、声明、清洁核验）
- `batch_materials`：批次—原料批号关联（保留投料顺序）
- `verdict_versions`：**裁决历史版本，只追加不覆盖**（含两类更正事件外键）
- `correction_events`：批次级更正审计（字段、前后值、重算批次清单）
- `material_correction_events`：原料主数据更正审计（可跨产线重算）
- `lines`：产线主数据

写操作使用 `BEGIN IMMEDIATE` 事务并在进程内加写锁；任何校验失败都会回滚，
数据库不会留下半截批次、孤立原料关联或缺失裁决的状态。
