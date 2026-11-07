# AI Agent 开发指南 (AGENTS.md)

本文件是专为参与本项目开发、维护、重构与排障的 AI Coding Agent（如 Antigravity, Claude Code, Cursor, Codex 等）制定的操作指南与架构上下文约束。

---

## 1. 项目全貌与核心目标

`xiaohongshu-scraper` 是一个全功能的**小红书数据采集、多模态智能分析、商户知识图谱抽取、多平台演出情报汇总、独立 Web 阅读器与 MCP (Model Context Protocol) 服务体系**。

整个系统围绕 **PostgreSQL** 单一可信数据源构建，形成“采集入库 → 离线多模态分析 → AI 实体抽取与确定性校验 → 结构化索引 → API / MCP / 飞书推送”的完整数据价值闭环。

---

## 2. 核心架构约束与铁律 (Non-Negotiable Constraints)

在对本项目做任何代码修改时，必须严格遵守以下准则：

### 2.1 铁律一：确定性证据校验优先于模型幻觉 (Deterministic Evidence First)
- 商户抽取流程（`scripts/merchant.py`）使用大语言模型从自由文本中提取结构化商户字段。
- **严禁直接把大模型的输出写入正式商户表（`merchants`）**。
- 模型结果必须先进入暂存表 `merchant_extraction_candidates`。所有商户名称、价格、预约信息、地理位置等字段，必须在原始笔记正文中找到**完全匹配或空白归一化一致的证据子串 (`evidence_text`)**，通过校验后方可幂等晋级（`promote`）。缺失证据的字段必须置为空，不得让模型凭常识脑补。

### 2.2 铁律二：全流程幂等与断点续跑 (Idempotency & Resumability)
- 抓取与抽取任务随时可能因限流、网络中断或进程重启而终止。
- 任务提交粒度必须细化到单篇笔记、单个页面或单个分析阶段：
  - 任务游标存储于 `xhs_crawl_state`。
  - 抽取完成状态记录在 `merchant_extraction_state`（即便抽取结果为空也必须记录已完成）。
  - 推送状态与摘要快照保存在 `push_records`。
- 重复执行命令时**默认继续未完成任务**；绝不能静默清除断点。仅当用户显式传递 `--restart` 或 `--force` 时才允许重新开始。

### 2.3 铁律三：安全边界与敏感数据隔离 (Security Perimeter)
- 严禁在 MCP 协议层、对外 API 或大模型抽取提示词中暴露：
  - 小红书账号 Cookie、Session Token、`xsec_token`。
  - 用户鉴权密钥 `API_AUTH_KEY`。
  - 未脱敏的完整原始响应报文 (`raw_data`)。
- MCP 服务必须为只读服务，仅调用只读接口，禁止向 Agent 暴露写接口或执行任意 SQL 权限。

### 2.4 铁律四：浏览器环境隔离 (Isolated Browser Profile)
- Playwright 爬虫与自动化验证必须默认使用项目专属的独立浏览器目录（位于 `data/pw_profile_*`）。
- **严禁在未获用户显式指令的前提下接管、强制关闭或篡改宿主机的日常个人浏览器 Profile**。

### 2.5 铁律五：数据库迁移与测试隔离安全性 (Safe DDL & Test Schemas)
- 所有 DDL 修改必须保证幂等：包含 `CREATE TABLE IF NOT EXISTS`、`ADD COLUMN IF NOT EXISTS` 等。
- 测试套件（`tests/conftest.py`）会自动为每次测试生成独立的临时 PostgreSQL schema 并清理。**Agent 在编写新测试时严禁绕过此机制直接操作生产/默认 public schema**。

---

## 3. 代码库结构映射指南

```text
├── scripts/
│   ├── xhs.py                  # 爬虫核心 CLI（抓取、登录、导出、媒体处理入口）
│   ├── xhs_fetcher.py          # 小红书 HTTP API 封装与网络请求
│   ├── xhs_sign.py             # 逆向签名通信模块（与 assets/ 下的 JS 通信）
│   ├── xhs_storage.py          # PostgreSQL 读写、断点记录与数据导出
│   ├── xhs_image.py            # 图片 OCR (RapidOCR) 与多模态视觉理解
│   ├── xhs_video.py            # 视频抽帧、Whisper 语音转写与内容摘要
│   ├── merchant.py             # 商户抽取、校验、去重合并与晋级 CLI
│   ├── merchant_prompt.md      # 商户 Agent 结构化抽取提示词
│   ├── ticketmaster.py         # Ticketmaster SG 演唱会抓取与查询 CLI
│   ├── hkticketing.py          # HKT 香港快達票演唱会抓取与查询 CLI
│   ├── concert_storage.py      # 各票务平台共用的演唱会幂等存储逻辑
│   ├── push_agent.py           # 飞书/Lark 增量情报推送 Agent
│   ├── push_records.py         # 推送状态追踪辅助模块
│   └── query_db.py             # 数据库快速统计与排查工具
├── web/
│   ├── backend/                # FastAPI 后端服务
│   │   ├── main.py             # API 路由与业务逻辑（笔记、商户、演出、设置）
│   │   ├── auth.py             # API Key 换取 Bearer JWT 与鉴权中间件
│   │   └── reader_config.py    # PostgreSQL 单例配置读写
│   └── frontend/               # Next.js 14 Web 前端阅读器
├── merchants-mcp/              # Node.js 原生 MCP 包 (Model Context Protocol)
│   ├── src/index.js            # stdio 入口
│   ├── src/server.js           # MCP 工具声明与分发
│   └── src/client.js           # 后端 API 客户端与 Token 自动续期
├── assets/                     # 逆向 JavaScript 运行时 (xhs_a1.js, xhs_xray.js)
├── docs/                       # 项目标准工程与运维文档库
├── tests/                      # pytest 与 Node.js 自动化测试集
├── schema.sql                  # PostgreSQL 幂等数据表结构定义
├── docker-compose.yml          # 一键部署编排文件
└── requirements.txt            # Python 核心依赖定义
```

---

## 4. 关键开发与排查命令

### 4.1 运行测试
```bash
# 运行全部 Python 自动化测试
python -m pytest tests -q

# 运行特定模块测试（如商户 API 或推送模块）
python -m pytest tests/test_merchant_api.py -v
python -m pytest tests/test_push_agent.py -v

# 运行前端 API 鉴权测试
node --test tests/frontend-api.test.cjs

# 运行 MCP 工具包测试
cd merchants-mcp && npm test && cd ..
```

### 4.2 本地运行服务调试
```bash
# 启动 PostgreSQL 容器
docker compose up -d postgres

# 本地启动 FastAPI 后端（热重载）
python -m uvicorn main:app --app-dir web/backend --reload --port 8000

# 本地启动 Next.js 前端
cd web/frontend && npm run dev
```

### 4.3 常用运维与诊断指令
```bash
# 检查数据库记录统计
python scripts/query_db.py stats

# 查看本地爬虫风控与限流状态
python scripts/xhs_risk_status.py --summary

# 模拟增量商户抽取与晋级
python scripts/merchant.py extract --limit 5
python scripts/merchant.py promote

# 检查 Ticketmaster SG 抓取
python scripts/ticketmaster.py list --limit 10

# 检查 HKT 香港快達票抓取
python scripts/hkticketing.py list --limit 10

# 预览推送 Agent 生成的简报（不实际发送 webhook）
python scripts/push_agent.py --dry-run
```

---

## 5. Agent 编码常见避坑准则

1. **不要盲目修改签名 JS 逻辑**：`assets/` 中的签名逻辑与小红书反爬直接绑定，若无必要切勿随意修改。如遇签名失效，优先指导用户执行 `python scripts/xhs.py update-js`。
2. **严禁引入破坏性数据库变更**：修改 `schema.sql` 时必须确保旧数据向后兼容；如果在生产环境运行，必须先有备份方案。
3. **防止截断与大文本爆炸**：商户和笔记的原文证据可能非常长，返回给 MCP 或模型提示词时必须设定安全的截断上限（例如 12000 字符以内），并显式记录截断字段 `truncated_fields`。
4. **多进程并发控制**：在执行商户抽取或抓取时，多进程可能触碰同一篇笔记或相同商户，使用 PostgreSQL 行级锁 (`SELECT FOR UPDATE SKIP LOCKED`) 或原子 upsert 避免竞态条件。
