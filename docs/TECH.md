# 个人记账系统 · 技术设计文档（TDD）

> 版本：v1.0
> 更新：2026-11-07
> 关联：[PRD.md](./PRD.md)
> 状态：设计冻结，待实现

---

## 1. 架构总览

### 1.1 分层

单体进程，单容器部署。一个 Python 进程同时承担：HTTP 服务 + Excel 解析 + 去重计算 + 报表聚合 + 服务端渲染。

```
┌─────────────────────────────────────────────┐
│ 浏览器 (Jinja2 渲染 HTML + 原生 JS + Chart.js)│
└────────────────────┬────────────────────────┘
                     │ HTTP
┌────────────────────▼────────────────────────┐
│              FastAPI 路由层                  │
│   bills / dedup / reports / pages            │
└──────┬──────────────┬──────────────────┬─────┘
       │              │                  │
┌──────▼─────┐ ┌──────▼──────┐ ┌───────▼────────┐
│ parsers    │ │ dedup_engine│ │  aggregator    │
│ wechat/    │ │ 置信度分档  │ │  月度净额聚合  │
│ alipay/card│ └──────┬──────┘ └───────┬────────┘
└──────────────────────┼────────────────┼──────────────┐
                       │                │              │
        ┌──────────────▼───────────────▼────────┐ │
        │           SQLAlchemy ORM              │ │
        │   models.py → SQLite / Postgres      │ │
        └─────────────────────────────────────────┘ │
                       Alembic 迁移
```

### 1.2 数据流

```
上传 Excel → import_service
  → parser 解析（按 source 选 col_map）
  → transaction 落库（file_hash 防重）
  → dedup_engine：同文件去重 → 跨渠道去重 → merge_group + dedup_candidate
  → aggregator：按月聚合净额
  → 报表 / 待确认序列表
```

### 1.3 部署形态

- **本地 Windows**：venv + uvicorn，数据存 `./data/bill.db`，上传临时存 `./data/tmp/`
- **阿里云 Linux3 2核2G**：Docker 单容器（uvicorn --workers 1）或 systemd，数据挂载卷，SQLite 文件持久化

---

## 2. 项目结构

```
bill/
├── docs/
│   ├── PRD.md              # 需求文档
│   └── TECH.md             # 本文档
├── app/
│   ├── __init__.py
│   ├── main.py             # FastAPI 入口，挂载路由
│   ├── config.py           # 配置加载（.env）
│   ├── database.py         # SQLAlchemy engine / SessionLocal / Base
│   ├── models.py           # ORM 模型（5 张表）
│   ├── schemas.py          # Pydantic 请求/响应模型
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── bills.py        # 账单上传 / 列映射模板
│   │   ├── dedup.py        # 去重确认队列
│   │   ├── reports.py      # 月度报表 / 明细
│   │   └── pages.py        # Jinja2 页面路由
│   ├── services/
│   │   ├── __init__.py
│   │   ├── import_service.py   # 导入编排
│   │   ├── dedup_engine.py     # 去重引擎
│   │   ├── aggregator.py       # 月度聚合
│   │   └── file_hash.py        # sha256 工具
│   ├── parsers/
│   │   ├── __init__.py
│   │   ├── base.py         # 解析基类 + 列映射探测器
│   │   ├── wechat.py       # 微信支付账单解析器
│   │   ├── alipay.py       # 支付宝账单解析器
│   │   └── creditcard.py   # 信用卡账单解析器
│   └── templates/          # Jinja2 模板
│       ├── base.html       # 布局 + 导航 + Chart.js
│       ├── upload.html     # 上传页 + 列映射校准
│       ├── report.html     # 月度报表 + 图表
│       ├── transactions.html  # 明细列表
│       └── dedup_queue.html  # 去重确认队列
├── migrations/             # Alembic 迁移（含 env.py + versions/）
├── tests/
│   ├── test_parsers.py
│   ├── test_dedup.py
│   └── test_aggregator.py
├── scripts/
│   ├── run_local.bat       # Windows 一键启动
│   └── run.sh              # Linux 启动
├── data/                   # 运行时数据（git 忽略）
│   ├── bill.db
│   └── tmp/                # 上传暂存
├── requirements.txt
├── .env.example
├── alembic.ini
├── Dockerfile
└── docker-compose.yml      # 可选，简化部署
```

---

## 3. 数据库设计

### 3.1 表清单

| 表名 | 用途 |
|---|---|
| `bill_file` | 导入的账单文件元信息（防重指纹） |
| `transaction` | 单笔流水（去重主体） |
| `merge_group` | 去重合并组（代表一条真实消费） |
| `dedup_candidate` | 待人工确认的疑似重复对 |
| `import_template` | 渠道 Excel 列映射模板缓存 |

> C 模型不建 `account` / `balance` 表。

### 3.2 bill_file

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | int | PK | 主键 |
| source | varchar(16) | enum: wechat/alipay/card | 渠道 |
| file_name | varchar(255) | | 原始文件名 |
| file_hash | varchar(64) | **唯一索引** | sha256，防重复导入 |
| row_count | int | default 0 | 解析行数 |
| status | varchar(16) | enum: parsed/deduped/failed | 导入状态 |
| imported_at | datetime | default now | 导入时间 |

### 3.3 transaction

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | int | PK | |
| source | varchar(16) | FK→bill_file.source | 渠道 |
| file_id | int | FK→bill_file.id | 来源账单 |
| bill_type | varchar(16) | enum: consume/income/transfer/refund/fee/other | 交易类型 |
| amount | numeric(12,2) | 消费正/退款负 | |
| currency | varchar(8) | default 'CNY' | |
| tx_date | date | **索引列** | 交易日（聚合按此） |
| entry_date | date | nullable | 信用卡入账日（存储不聚合） |
| merchant | varchar(255) | | 商户名 |
| counterparty | varchar(255) | | 交易对方 |
| description | text | | 原始摘要 |
| source_row | int | | 在源文件的行号（排查用） |
| is_primary | bool | default true | 是否组内代表（聚合时只计代表） |
| merged_group_id | int | FK→merge_group.id nullable | 被合并时指向组 |
| confidence | varchar(8) | enum: high/medium/low/independent | 去重置信 |
| created_at | datetime | default now | |

索引：`(source, tx_date)` 复合索引、`(source, bill_type)`、`is_primary`。

### 3.4 merge_group

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | int | PK | |
| representative_tx_id | int | FK→transaction.id | 代表流水 |
| merged_count | int | | 该组合并了多少条 |
| auto_merged | bool | default false | 是否自动合并（high置信） |
| reason | text | | 合并依据说明 |
| created_at | datetime | default now | |

### 3.5 dedup_candidate

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | int | PK | |
| tx_a_id | int | FK→transaction.id | 疑似重复对 A |
| tx_b_id | int | FK→transaction.id | B |
| score | numeric(3,1) | | 匹配得分 |
| reason | text | | 匹配理由（卡名不符等） |
| status | varchar(16) | enum: pending/merged/discarded | **索引列** |
| created_at | datetime | default now | |

### 3.6 import_template

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | int | PK | |
| source | varchar(16) | | 渠道 |
| name | varchar(64) | name unique | 模板名 |
| col_map | JSON | | {原列名: 标准字段} 映射 |
| active | bool | default true | 是否启用 |
| created_at | datetime | default now | |

字段映射键：`amount/date/merchant/counterparty/description/bill_type/currency`。

### 3.7 初始数据

- 预置三条渠道的 `import_template` 默认映射（按各渠道官方导出模板首版）。
- 首次使用列映射自动探测覆盖，探测满意后存为 `active=true`。

---

## 4. 领域模型（schemas.py）

Pydantic 模型用于校验 API 输入输出，与 ORM `models.py` 分离。

```python
# schemas.py 核心结构（示意）

class UploadResult(BaseModel):
    file_id: int
    source: str
    rows_imported: int
    dedup_candidates: int

class DedupCandidateOut(BaseModel):
    id: int
    tx_a: dict      # {date, amount, source, merchant, description}
    tx_b: dict
    score: float
    reason: str
    status: str

class MonthlyReport(BaseModel):
    month: str          # YYYY-MM
    income: float
    expense: float
    net: float          # income - expense
    by_channel: dict    # {wechat, alipay, card: amount}
    pending_candidates: int
```

---

## 5. 模块详述

### 5.1 import_service（导入编排）

`upload_file(source, file, Session)`:

1. 计算 `file_hash`，查 `bill_file`，命中则直接返回"已导入"，不重复入库。
2. 按 source 取 `import_template.active` 的 `col_map`；若无模板则调用 `base.detect_columns()` 自动探测并落一条新模板。
3. 调对应 parser（`wechat/alipay/creditcard`）解析 → 得到 `list[transaction]`。
4. 落 `bill_file` 行 + 批次落 `transaction`。
5. 事务合并：parser + file 写入一个 DB 事务。
6. 返回导入结果，前端后续异步触发去重或同步去重。

### 5.2 parsers（渠道解析器）

#### base.py
- `BaseParser(source)`：定义标准字段输出契约。
- `detect_columns(df: DataFrame) -> candidate_map`：根据列名/格式特征回填建议映射。
- `normalize_amount(v) -> float`: 收入为负出口径统一消费正、退款负。

#### wechat.py / alipay.py / creditcard.py
- 各渠道 Excel 列名不同（微信「交易时间 / 商品名称 / 金额」，支付宝「交易创建时间 / 交易对方 / 收/支」，信用卡「交易日期 / 交易摘要 / 交易金额」），每个 parser 用其 `col_map` 从原列名映射到标准字段。
- `bill_type` 识别逻辑：
  - 微信/支付宝：`收/支/transfer` 列结合金额正负判断。
  - 信用卡：仅消费/退款/手续费，无收入（退款冲减）。
- 信用卡额外识别：`entry_date` 入账日期。

### 5.3 dedup_engine（去重引擎，核心）

#### 5.3.1 输入输出

- 输入：一批新导入的 transaction + 已有全量 transaction。
- 输出：更新 `transaction.is_primary/merged_group_id/confidence`，写入 `merge_group` 与 `dedup_candidate`。

#### 5.3.2 去重分两步

**第一步：同文件内去重**
- 同一 `file_id` 内，对 `(amount, tx_date, merchant, counterparty)` 完全相同（容差 0.01）的行分组，组 size>1 → 建 `merge_group`，代表行 `is_primary=true`，其余合并入组，`confidence=high`，`reason="同文件重复"`。

**第二步：跨渠道去重**
- 遍历新导入 tx，与**另一 source** 的 tx 比对，条件：
  - `abs(amount_a - amount_b) <= 0.01`
  - `abs(date_a - date_b) <= 3 天`
  - 商户名模糊匹配（见 5.3.3）
- 命中则计算匹配得分，分档写入 `merge_group` 或 `dedup_candidate`。

#### 5.3.3 商户名模糊匹配

`normalize_merchant(s) -> str`：
- 去首尾空白、全角/半角统一、去标点、小写。

`merchant_score(a, b) -> float`：
- 归一后 a、b 均做字符集合：
  - **完全相同**：score = 1.0
  - **包含关系**（一方包含另一方）：score = 0.8
  - **Jaccard 字符重合**：score = len(∩)/len(∪)，阈值 ≥ 0.5 视为匹配
  - **关键词提取**：信用卡 `description` 常含「商户名 + MCC」，提取前 N 个中文/英文 token，与对方 `merchant` 子串匹配，命中加权 0.3
- 双向任一命中即返回该 score 与 direction。

> MVP 阶段用朴素字符级去重 + 手动校准；扩展期可加编辑距离、商户名别名库。

#### 5.3.4 得分分档

```
score范围                           动作
高(amount=1 且 merchant>=0.5)         自动并入 merge_group，auto_merged=True
中(amount=1 但 merchant<0.5)         写 ledup_candidate(pending) 待人工确认
低(仅 amount=1 或 仅 date=1)          不入 candidate，标 low，报表层提示
```

- 同一 `(tx_a, tx_b)` 去重：防止重复生成候选对。
- 合并时以金额绝对值较大者为代表行（代表=is_primary=true）；其余 `is_primary=False`、`merged_group_id` 指向组、`confidence=high`。

### 5.4 aggregator（聚合引擎）

#### 5.4.1 月度净额 SQL（SQLite）

```sql
SELECT
  strftime('%Y-%m', tx_date) AS month,
  SUM(CASE WHEN bill_type='income'      AND is_primary=1 THEN amount ELSE 0 END) AS income,
  SUM(CASE WHEN bill_type='consume'    AND is_primary=1 THEN amount ELSE 0 END) AS consume,
  SUM(CASE WHEN bill_type='refund'     AND is_primary=1 THEN amount ELSE 0 END) AS refund,
  SUM(CASE WHEN bill_type='fee'        AND is_primary=1 THEN amount ELSE 0 END) AS fee
FROM transaction
WHERE NOT (bill_type='transfer')
GROUP BY month
ORDER BY month;
```

- `net = income - (consume - refund) - fee`
- 被合并记录 `is_primary=false`，不计入（只计代表行一次）。
- 退款 `amount` 存为负，`consume - refund` 时还原支出净额；也可以直接对 consume、refund 绝对值相减。按口径：refund 金额在报表服务里取 `ABS` 后减。

#### 5.4.2 渠道占比

```sql
SELECT source, SUM(amount) AS total
FROM transaction
WHERE bill_type='consume' AND is_primary=1 AND tx_date BETWEEN :m_start AND :m_end
GROUP BY source;
```

Chart.js 饼图展示。

#### 5.3.3 month 月度明细列表

按月、渠道、商户、类型筛选，分页返回。

---

## 6. API 路由表

### 6.1 管理模式接口（返回 JSON）

| Method | Path | 说明 |
|---|---|---|
| POST | `/api/bills/upload` | multipart 上传 Excel，含 source 字段 |
| GET | `/api/templates` | 列映射模板列表 |
| POST | `/api/templates/detect` | 探测新文件列映射，返回建议 |
| PUT | `/api/templates/{id}` | 保存/编辑列映射 |
| GET | `/api/dedup/queue` | 合并确认列表（pending） |
| POST | `/api/dedup/{id}/merge` | 确认合并 |
| POST | `/api/dedup/{id}/discard` | 解除/放弃 |
| GET | `/api/reports/monthly?month=YYYY-MM` | 月度净额 |
| GET | `/api/transactions?month=YYYY-MM&source=&merchant=&type=&page=` | 明细列表 |

### 6.2 页面路由（渲染 HTML）

| Path | 模板 | 说明 |
|---|---|---|
| `/` | `report.html` | 仪表盘/报表首页 |
| `/upload` | `upload.html` | 上传页 |
| `/dedup` | `dedup_queue.html` | 去重确认队列 |
| `/transactions` | `transactions.html` | 交易明细 |

### 6.3 上传请求示例

```
POST /api/bills/upload
Form:
  source=wechat
  file=@wechat_bill.xlsx
Response 200:
{
  "file_id": 12,
  "source": "wechat",
  "rows_imported": 87,
  "dedup_candidates": 3,
  "file_hash": "abc..."
}
```

---

## 7. 前端页面

### 7.1 base.html
- 布局：顶部导航（报表 / 上传 / 去重 / 明细），容器 + 内联 CSS（参考 Tailwind via CDN，或纯 CSS）。
- 引入 Chart.js（CDN，无需构建步骤）。
- 引入 Chart.js 与原生 fetch 调用 API。

### 7.2 upload.html
- 文件选择 + 渠道 select，提交上传。
- 上传成功后展示结果，并可在"列映射校准"卡片显示自动探测的映射，支持勾选确认后保存模板。

### 7.3 report.html
- 月度切换（月选择器）。
- 数字卡：本月收入 / 支出 / 净额 / 待确确认数。
- Chart.js 饼图：渠道占比。
- 趋势（后续扩展）。

### 7.4 transactions.html
- 表格 + 筛选（月/渠道/类型）。
- 展示 merge_group 归属 + confidence 标签。

### 7.5 dedup_queue.html
- 待确认列表：tx_a / tx_b 对照，一键"合并" / "不合并"。
- 实时反馈到报表。

---

## 8. 配置与启动

### 8.1 配置项（.env）

```bash
DATABASE_URL=sqlite:///./data/bill.db   # 本地；部署用 sqlite:////data/bill.db
SECRET_KEY=change-me                     # 预留
HOST=127.0.0.1
PORT=8000
UPLOAD_MAX_MB=10
```

### 8.2 依赖（requirements.txt 核心）

```
fastapi>=0.108
uvicorn[standard]>=0.29
sqlalchemy>=2.0
alembic>=1.13
pandas>=2.1
openpyxl>=3.1
jinja2>=3.1
python-multipart>=0.0.6
python-dotenv>=1.0
pydantic>=2.5
chart.js  # 前端 CDN，无需进依赖
```

### 8.3 本地一键启动（scripts/run_local.bat）

```bat
@echo off
if exist .venv (echo [venv exists]) else python -m venv .venv
call .venv\Scripts\activate.bat
pip install -r requirements.txt
set DATABASE_URL=sqlite:///./data/bill.db
mkdir data\tmp
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### 8.4 部署（Dockerfile）

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["alembic","upgrade","head"] && ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]
```

- 阿里云上：`docker build` + `docker run -p 8000:8000 -v /data/bill:/data`。

---

## 9. 安全与运维

| 项 | 设计 |
|---|---|
| 上传校验 | 限制扩展名 .xlsx/.xls + 大小 ≤10MB + 解析异常捕获 |
| 文件去重 | `file_hash` 唯一索引，同文件二次导入短路 |
| 文件暂存 | 上传后解析入库，不长期保留明文；`data/tmp/` 定期清理 |
| 日志 | structlog/标准 logging，记录导入、去重、确认操作 |
| 迁移 | Alembic，本地/部署前 `alembic upgrade head` |
| CORS | 不开放跨域（仅本地访问，无跨站需求） |
| 鉴权 | MVP 阶段不做登录（本地部署单用户）；后续如需可加 Streamlit/简单 auth |

---

## 10. 实现顺序建议

1. 底座：`config / database / models / alembic`，建 5 张表 + 默认模板。
2. Parser：微信 parser 端到端（含 detect_columns），传真 Mock 数据通过。
3. 导入 + 防重：`import_service` + `file_hash`。
4. 去重引擎：同文件去重 → 跨渠道去重分档。
5. 聚合：`aggregator` 月度净额 SQL。
6. API：签订单/去重/报表/明细路由。
7. 前端 政：base/report/upload/dedup/transactions 五页 + Chart.js。
8. 本地跑通脚本 + 测试。
9. 收尾后部署 Docker 上阿里云。

---

## 11. 风险与开放问题

| 项 | 状态 | 处理 |
|---|---|---|
| Excel 渠道版本变更致列错位 | 中 | 列映射模板 + 手动校准 UI；开放问题：是否保存每个用户的校准？ |
| 商户名别名表维护 | MVP 暂无 | 扩展期加 |
| 正负号规范：微信/支付宝/信用卡三源出入方向不一致 | 已规范 | parser 层统一消费正/退款负 |
| 跨年跨月退款归属 | 已定 | 按退款所在月 |
| 是否需要登录？ | MVP 不做；若后续多人用需重议 | — |
| SQLite 并发 | 单用户足够；若扩展期多用户再迁 MySQL/Postgres | — |
