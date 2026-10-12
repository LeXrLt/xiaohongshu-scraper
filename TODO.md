# 商户索引与 MCP 查询开发文档

> 文档状态：Phase 1 数据结构与候选抽取入口已实现，分类合并和 MCP 待后续阶段
> 目标：从已经抓取的小红书笔记中提取商户信息、建立分类和商户索引，并通过 MCP 为 Agent 提供只读查询能力。
> 数据源：现有 SQLite 笔记库，之后沿用 SQLite → PostgreSQL 同步链路。
> 当前约束：本项目已有笔记、用户、评论和阅读器 API；商户表与候选抽取入口已建立，商户合并、查询 API 和 MCP Server 尚未完成。

本次先落地 SQLite / PostgreSQL 的商户数据结构，以及统一的
`scripts/merchant.py` 入口。Agent 配置统一从 `.env` 读取，模型结果先进入
`merchant_extraction_candidates`，通过证据校验后幂等写入商户和来源关系表。

## 1. 背景与目标

当前数据以 `xhs_notes` 为中心。笔记正文中可能包含餐厅名称、别名、菜系、价格、预约方式和体验评价，但这些信息是自由文本，无法直接支持稳定的商户列表查询。

本阶段建立一个中间层，把自由文本转成带证据的商户候选和商户索引。Agent 的职责是理解用户意图、选择分类、比较候选商户和生成最终回答；后端的职责是保存规范化数据、去重、分页、过滤和返回来源证据。

目标包括：

1. 对已入库笔记进行增量商户抽取。
2. 为商户打上受控分类和标签。
3. 保留每个抽取结果对应的笔记、原文证据、模型和版本信息。
4. 支持商户级别的分类查询、列表查询和笔记查询。
5. 通过 MCP 暴露只读工具，供 Agent 组合查询。
6. 抽取过程可重试、可审计、可回滚，不影响原始笔记数据。

不在本阶段完成的内容：

- 不把小红书远程搜索直接放进 MCP 请求链路。
- 不把模型返回的文本直接当作数据库事实。
- 商户主表不保存门店地址；连锁店和搬迁会使门店地址无法代表商户。
- 不承诺从笔记中推断营业状态、真实评分或实时库存。
- 不开放 MCP 修改设置、Cookie 或抓取账号状态。

## 2. 总体架构

```text
                 ┌────────────────────┐
                 │  xhs_notes 原始笔记 │
                 └─────────┬──────────┘
                           │ 增量选择
                           ▼
                 ┌────────────────────┐
                 │ Python 抽取任务调度器 │
                 └─────────┬──────────┘
                           │ 笔记正文 + 受控提示词
                           ▼
                 ┌────────────────────┐
                 │ Agent / 模型结构化抽取 │
                 └─────────┬──────────┘
                           │ JSON 候选
                           ▼
                 ┌────────────────────┐
                 │ 确定性校验与候选入库 │
                 └─────────┬──────────┘
                           │ 去重、合并、分类
                           ▼
       ┌───────────────────┴───────────────────┐
       │                                       │
       ▼                                       ▼
┌───────────────┐                       ┌───────────────┐
│ SQLite 商户索引 │ ───── pg_sync ─────▶ │ PG 商户索引    │
└───────────────┘                       └───────┬───────┘
                                                │
                                                ▼
                                      ┌────────────────┐
                                      │ MCP 只读查询层  │
                                      └───────┬────────┘
                                              │
                                              ▼
                                      ┌────────────────┐
                                      │ Agent 最终判断  │
                                      └────────────────┘
```

推荐把抽取任务和 MCP 查询分开：抽取任务是离线或定时运行的写入任务，MCP 是低延迟的只读查询服务。

## 3. SDK 选择与职责边界

### 3.1 Codex Python SDK

官方 Python SDK 包名为 `openai-codex`，导入名为 `openai_codex`。它通过 JSON-RPC 控制本地 Codex app-server，要求 Python 3.10 或更高版本，并依赖本地 Codex CLI 运行时。

适合的用途：

- 快速验证抽取提示词。
- 处理低置信度候选或人工复核任务。
- 让本地 Agent 读取一批笔记并生成抽取结果文件。
- 运行一次性的修复或回填任务。

不建议让 Codex Agent 直接执行 SQL 或直接修改正式商户表。应限制其只能生成候选 JSON，再交给 Python 校验器和写入器处理。默认使用只读沙箱；只有写入临时结果文件时才允许工作区写权限。

### 3.2 OpenAI Python SDK

批量生产抽取更适合使用普通 OpenAI Python SDK 配合结构化输出。这样可以由 Python 程序统一控制：

- JSON Schema
- 超时和重试
- 并发度
- Token 和批大小
- 失败记录
- 模型和提示词版本

建议采用混合方案：Codex SDK 用于开发和复核，批处理 worker 使用结构化模型调用，最终写入全部经过本地确定性校验。

## 4. 数据模型

### 4.1 `merchant_categories`

受控分类表，分类 ID 由后端维护，模型只能从已有分类中选择。

| 字段 | 类型 | 说明 |
|---|---|---|
| `category_id` | TEXT | 稳定 ID，例如 `restaurant`、`japanese` |
| `parent_id` | TEXT | 父分类，可为空 |
| `name` | TEXT | 展示名称 |
| `description` | TEXT | 分类含义和边界 |
| `aliases` | JSON | 同义词和繁简体别名 |
| `sort_order` | INTEGER | 展示顺序 |
| `status` | TEXT | `active` / `deprecated` |
| `taxonomy_version` | TEXT | 分类体系版本 |
| `created_at` | TIMESTAMP | 创建时间 |
| `updated_at` | TIMESTAMP | 更新时间 |

第一版可以使用以下层级：

```text
餐饮
├── 餐厅
│   ├── 日料
│   ├── Omakase
│   ├── 粤菜
│   ├── 火锅
│   ├── 西餐
│   └── 米其林
├── 咖啡店
├── 甜品店
├── 酒吧
└── 茶饮店
```

“适合约会”“海景”“高性价比”等更适合作为标签或特征，不应和菜系分类混在同一层级。

### 4.2 `merchants`

规范化商户主表。

| 字段 | 类型 | 说明 |
|---|---|---|
| `merchant_id` | TEXT | 内部稳定 ID |
| `canonical_name` | TEXT | 标准名称 |
| `aliases` | JSON | 别名、英文名、繁简体名称 |
| `city` | TEXT | 城市，必须有证据才写入 |
| `district` | TEXT | 区域 |
| `cuisine` | JSON | 菜系或经营类型 |
| `price_min` | INTEGER | 抽取到的最低价格，可为空 |
| `price_max` | INTEGER | 抽取到的最高价格，可为空 |
| `currency` | TEXT | 价格币种 |
| `price_basis` | TEXT | `per_person`、`set`、`unknown` |
| `reservation_info` | TEXT | 预约信息摘要 |
| `status` | TEXT | `active`、`uncertain`、`merged` |
| `confidence` | REAL | 综合置信度 |
| `first_seen_at` | TIMESTAMP | 首次发现时间 |
| `last_seen_at` | TIMESTAMP | 最近一次被笔记提及的时间 |
| `updated_at` | TIMESTAMP | 索引更新时间 |

价格、营业状态等字段都必须允许为空。没有证据时写空值，不允许模型补全；商户主表不保存门店地址。

### 4.3 `merchant_category_links`

商户与分类的多对多关系表。

字段建议：

```text
merchant_id
category_id
source_note_id
evidence_text
confidence
is_primary
taxonomy_version
created_at
```

### 4.4 `merchant_notes`

商户与笔记的多对多关系表。

字段建议：

```text
merchant_id
note_id
relation_type
evidence_text
evidence_start
evidence_end
confidence
created_at
```

`relation_type` 必须区分：

- `primary_review`：笔记主要评价该商户。
- `mentioned`：正文中顺带提到。
- `listicle`：清单或汇总文章中的商户。
- `comparison`：与其他商户进行比较。

### 4.5 `merchant_extraction_candidates`

模型输出的暂存表，不直接等同于正式商户。

字段建议：

```text
candidate_id
note_id
source_content_hash
candidate_name
aliases_json
category_candidates_json
city
district
price_json
reservation_info
relation_type
evidence_text
confidence
model_name
prompt_version
extractor_version
status                 -- pending / accepted / rejected / needs_review
error_message
created_at
updated_at
```

同一 `note_id + source_content_hash + extractor_version` 的每个候选名称应具有幂等约束，笔记正文未变化时不重复写入；同一笔记的多个商户通过 `candidate_name` 区分。

## 5. 抽取协议

模型只允许输出 JSON，不允许输出 SQL、Markdown 或额外解释。建议使用如下顶层结构：

```json
{
  "note_id": "...",
  "merchants": [
    {
      "name": "Paper Moon",
      "aliases": ["香港 Paper Moon"],
      "category_ids": ["restaurant", "italian", "sea_view"],
      "city": "香港",
      "district": "尖沙咀",
      "price": {
        "min": 500,
        "max": 600,
        "currency": "HKD",
        "basis": "per_person"
      },
      "reservation_info": "可通过 OpenRice 预约",
      "relation_type": "primary_review",
      "evidence": [
        "Paper Moon",
        "预约用 Openrice"
      ],
      "confidence": 0.93
    }
  ]
}
```

抽取规则：

1. 一个笔记可以返回多个商户。
2. 商户名称必须来自正文、标题、话题或结构化原始字段。
3. 价格、预约方式必须带正文证据；城市和区域只有在正文有证据时才写入。
4. 没有证据的字段返回 `null` 或空数组。
5. 不根据常识补全城市、菜系或营业信息。
6. 话题只能作为候选分类依据，不能单独证明商户事实。
7. 清单文章中的每个商户都标记为 `listicle`，不能默认是作者亲自体验。
8. `confidence` 只表达抽取可信度，不表达商户质量。

校验器还应验证每个 `evidence` 是原始文本的子串，或经过明确的空白归一化后能够定位到原文。

## 6. 抽取任务流程

### 6.1 选择待处理笔记

按以下条件增量选择：

- `content_hash` 为空。
- `content_hash` 与上次抽取记录不同。
- 没有对应的成功抽取记录。
- 手工标记为 `needs_review` 或 `reprocess`。

优先处理有正文的笔记。正文为空时只使用标题和话题，并降低置信度。

### 6.2 批次和并发

第一版采用小批次，避免一次请求混入过多长正文。每个批次记录：

```text
job_id
note_ids
model_name
prompt_version
extractor_version
started_at
finished_at
success_count
failure_count
```

并发度、最大正文长度、重试次数和超时时间都应配置化。模型调用失败不应阻塞其他笔记。

### 6.3 候选校验

校验顺序：

1. JSON Schema 校验。
2. 字段类型、长度和枚举值校验。
3. 分类 ID 是否存在且为 `active`。
4. 证据是否存在于原文。
5. 价格、城市等字段是否有对应证据。
6. 过滤空名称、纯泛化名称和明显噪声。
7. 写入 `merchant_extraction_candidates`。

校验失败的结果保存原始响应和错误原因，状态置为 `needs_review`，不要静默丢弃。

### 6.4 商户去重与合并

去重顺序建议为：

1. 标准化 Unicode、空白、大小写和繁简体。
2. 清理营销前缀、emoji 和无意义后缀。
3. 使用名称和城市/区域做候选匹配。
4. 使用别名和原文证据辅助合并。
5. 对不确定的合并保留两个商户，进入人工复核。

模型不得自行生成永久 `merchant_id`。ID 由数据库或确定性应用逻辑生成。

## 7. 分类与标签策略

分类体系必须版本化。分类变更时，新任务使用新版本，历史关系保留原版本，避免重新解释历史数据。

建议区分三类信息：

1. **主分类**：餐厅、咖啡店、酒吧等经营类型。
2. **菜系分类**：日料、粤菜、意大利菜、火锅等。
3. **场景标签**：约会、海景、米其林、适合多人聚餐等。

主分类和菜系分类可用于后端过滤；场景标签需要附带证据和置信度，供 Agent 做最终判断。

## 8. SQLite、PostgreSQL 与 API

当前项目的抓取数据先进入 SQLite，再通过 `pg_sync.py` 同步 PostgreSQL。新增商户表时必须同时完成：

1. SQLite schema 和迁移逻辑。
2. PostgreSQL `schema.sql`。
3. `pg_sync.py` 的 upsert 和 JSON 字段映射。
4. 数据库索引和唯一约束。
5. 回滚或重建索引的运维命令。

建议新增只读 HTTP API：

```text
GET /api/merchant-categories
GET /api/merchants?category_id=&city=&district=&limit=&cursor=
GET /api/merchants/{merchant_id}
GET /api/merchants/{merchant_id}/notes?limit=&cursor=
GET /api/merchant-stats
```

接口返回应包含：

- `items`
- `next_cursor`
- `total`（能够低成本计算时返回）
- `taxonomy_version`
- `data_as_of`

不要在这些接口中返回 `xsec_token`、Cookie 或完整 `raw_data`。笔记详情只返回 MCP 所需的正文摘要、原文链接、发布时间、互动数据和证据字段。

## 9. MCP 工具设计

MCP 只包装只读查询，不负责抽取和写入。

### `list_merchant_categories`

参数：

```text
parent_id?: string
include_counts?: boolean
```

返回分类 ID、名称、说明、子分类、商户数量和分类版本。

### `list_merchants`

参数：

```text
category_id?: string
city?: string
district?: string
tags?: string[]
limit?: integer
cursor?: string
```

返回商户摘要、分类、地区、笔记数量、最近更新时间和置信度。

### `get_merchant`

按 `merchant_id` 返回商户详情、别名、分类、价格证据和来源统计。

### `get_merchant_notes`

返回商户关联笔记、关系类型、正文摘要、证据片段、原文链接和抓取时间。

### `health`

返回数据库、索引同步和最近一次抽取任务状态。

MCP 工具应设置超时、分页上限和返回字段白名单。不要暴露设置接口、Cookie 接口、抓取控制接口或任意 SQL 接口。

## 10. Agent 查询流程

用户提出需求后，Agent 按以下顺序调用：

```text
理解用户意愿
    ↓
list_merchant_categories
    ↓
选择分类、城市、区域和场景标签
    ↓
list_merchants
    ↓
根据候选数量和置信度决定是否扩大分类范围
    ↓
get_merchant_notes
    ↓
比较价格、位置、体验和证据
    ↓
输出推荐，并标注信息来源和不确定项
```

Agent 不应把“笔记中提到过”直接表述为“商户当前真实情况”。涉及价格、营业和预约时，应引用笔记发布时间，并提醒用户信息可能变化。

## 11. 安全与可靠性

### 数据安全

- 抽取提示词不发送 Cookie、`xsec_token` 或无关的 `raw_data`。
- MCP 默认只绑定本机或受保护网络。
- MCP 只开放白名单字段和 GET 查询。
- 日志中不记录 Cookie、访问令牌和完整原始响应。

### 写入安全

- 模型只生成候选，不能直接写正式表。
- 候选写入使用事务。
- 正式索引写入必须可幂等。
- 所有合并操作记录来源候选和操作版本。
- 失败任务可重试，成功任务不重复生成关系。

### 事实可靠性

- 所有结构化字段保留证据。
- 证据缺失时使用空值。
- 低置信度进入复核队列。
- 汇总文章和主要评价文章必须区分。
- 商户质量评分不由抽取置信度替代。

## 12. 开发阶段

### Phase 1：抽取原型

- 定义第一版分类字典。
- 定义抽取 JSON Schema。
- 编写只读笔记选择器。
- 使用少量真实笔记验证多商户、价格和别名抽取。
- 实现证据子串校验。
- 结果只写候选 JSON 或候选表。

### Phase 2：候选入库与增量机制

- 增加 `merchant_extraction_candidates`。
- 增加 `content_hash + extractor_version` 幂等约束。
- 增加失败、重试和复核状态。
- 建立抽取任务日志和指标。

### Phase 3：商户规范化索引

- 增加 `merchants`、`merchant_categories`、`merchant_category_links`、`merchant_notes`。
- 实现名称标准化和候选合并。
- 增加分类版本和来源证据。
- 更新 SQLite、PostgreSQL 和 `pg_sync.py`。

### Phase 4：只读 API 与 MCP

- 增加商户分类和商户列表 API。
- 增加商户详情和关联笔记 API。
- 实现 MCP stdio Server。
- 过滤敏感字段。
- 验证分页、超时、空结果和数据库不可用时的错误行为。

### Phase 5：质量提升

- 增加人工复核界面或复核 CLI。
- 增加别名和繁简体归一化。
- 增加全文检索、同义词和可选向量召回。
- 增加商户信息过期提醒。
- 建立固定评测集和回归测试。

## 13. 测试与验收标准

### 抽取准确性

- 商户名称必须能在原文中定位。
- 价格字段不能无证据生成，商户数据结构不保存地址。
- 一篇笔记包含多家商户时不能只返回第一家。
- 清单文章不能全部标记为 `primary_review`。
- 空正文笔记不会生成高置信度详细商户信息。

### 数据一致性

- 同一笔记重复运行不会重复创建关系。
- 同一商户的明显别名能够合并或进入待复核。
- 分类 ID 只能来自有效分类表。
- SQLite 与 PostgreSQL 同步后数量和关系一致。

### MCP 行为

- 分类查询能返回固定 ID 和版本。
- 商户列表支持分页和分类过滤。
- 商户详情能追溯到笔记和证据。
- MCP 不返回 Cookie、`xsec_token` 和完整 `raw_data`。
- 数据库不可用时返回清晰错误，不触发抓取或写入。

### 推荐质量

建立至少包含以下类型的评测问题：

- 指定城市和菜系。
- 指定区域和预算。
- 约会、海景、多人聚餐等场景标签。
- 使用繁简体、同义词和自然语言描述。
- 只有汇总文章、没有主要评价证据的商户。

评测时分别统计商户召回率、商户去重准确率、证据准确率和最终推荐可解释性。

## 14. 第一版完成定义

第一版不要求覆盖所有餐饮类别，但必须满足：

1. 能对已入库笔记执行增量抽取。
2. 每个商户候选都有来源笔记和原文证据。
3. 商户、分类和笔记关系可重复写入且不重复。
4. MCP 能完成“查分类 → 查商户 → 查笔记”的完整流程。
5. Agent 可以根据用户意愿选择分类并比较候选商户。
6. 敏感字段不会进入 MCP 返回结果。
7. 抽取失败和低置信度结果可追踪、可重试、可复核。
