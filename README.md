# 小红书爬虫与智能阅读器系统 (Xiaohongshu Scraper & Reader Stack)

[![PostgreSQL](https://img.shields.io/badge/Storage-PostgreSQL_16-336791.svg)](https://www.postgresql.org/)
[![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688.svg)](https://fastapi.tiangolo.com/)
[![Next.js](https://img.shields.io/badge/Frontend-Next.js_14-black.svg)](https://nextjs.org/)
[![MCP](https://img.shields.io/badge/Protocol-Model_Context_Protocol-purple.svg)](https://modelcontextprotocol.io/)
[![Playwright](https://img.shields.io/badge/Browser-Playwright_Chromium-2EAD33.svg)](https://playwright.dev/)

面向现代软件工程与 AI Agent 生态构建的**全栈社媒与票务情报基础设施**。
基于 PostgreSQL 单一可信存储，实现小红书笔记/评论/用户采集、Ticketmaster SG 与 HKT 香港演出监控、多模态智能分析、带证据链的大模型商户抽取、现代化 Web 阅读器、Model Context Protocol (MCP) 查询插件及飞书自动化推送。

---

## 📚 项目工程与运维文档导航

本项目遵循标准软件工程与运维通用文档架构，各类工程指南已全面归档：

| 规范文档 | 说明 | 核心内容 |
| :--- | :--- | :--- |
| **[README.md](README.md)** | 项目入口 | 系统简介、快速上手、常用命令与模块导航 |
| **[CHANGELOG.md](CHANGELOG.md)** | 版本更新日志 | 遵循 Keep a Changelog 规范，记录 v3.0 ~ v3.2 版本演进 |
| **[CONTRIBUTING.md](CONTRIBUTING.md)** | 开发规范指南 | 环境初始化、分支规范、代码风格、隔离测试准则 |
| **[AGENTS.md](AGENTS.md)** | AI Agent 开发指令 | 针对 Coding Agent 的核心架构约束、确定性门禁与开发铁律 |
| **[docs/PRD.md](docs/PRD.md)** | 产品需求文档 | 背景痛点、用户角色画像、各子系统功能与非功能性需求 |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | 系统架构设计 | 整体分层架构、系统时序图、组件职责边界与 ER 关系模型 |
| **[docs/DESIGN.md](docs/DESIGN.md)** | 详细设计规范 | 签名逆向、状态机与幂等断点、商户抽取门禁、双 Token 鉴权 |
| **[docs/openapi.yaml](docs/openapi.yaml)** | OpenAPI 3.0 接口契约 | FastAPI 后端完整 REST 规范（认证、笔记、商户、演出、设置） |
| **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** | 部署与运维说明 | Docker Compose、Systemd、MCP 配置、Cron 自动化与安全检查单 |
| **[docs/adr/](docs/adr/README.md)** | 架构决策记录 (ADR) | 存储选型、确定性验证、Token 鉴权、独立 Profile 等重大决策 |
| **[docs/runbooks/](docs/runbooks/README.md)** | 运维与故障应急手册 | 登录态失效、签名异常、数据库迁移、服务恢复与推送排查 SOP |

---

## ⚡ 核心能力

1. **小红书全要素采集与反爬对抗**：
   - 动态逆向签名沙箱（Node.js 调用 `xhs_a1.js` / `xhs_xray.js`），支持一键自动更新签名算法。
   - 支持笔记详情、博主时间线、关键词搜索（API 与 Playwright DOM 渲染模式）、二级嵌套评论采集。
   - 细粒度断点续跑：任务游标与执行预算彻底解耦，单次中断零数据丢失。
2. **多模态内容智能理解**：
   - **图片分析**：三层分析架构，Layer 1 离线 RapidOCR；Layer 2 Ollama/OpenAI 视觉场景理解；Layer 3 自动提取步骤生成 Mermaid 流程图。
   - **视频转录**：ffmpeg 抽取音频结合 Whisper 离线语音转写，视频关键帧 OCR 汇总。
3. **多平台演出日历追踪**：
   - Playwright 自动化绕过 Cloudflare EPS 反爬，解析内嵌 `calendarEvents` JSON 结构化数据。
   - 使用独立 Playwright Profile 读取 HKT 香港快達票公开接口，仅抓取“演唱會”分类。
   - Ticketmaster 与 HKT 按平台分表保存，通过统一视图向 API、MCP 和推送层提供演唱会数据。
4. **确定性证据链商户知识库**：
   - 大模型抽取结果强制进入暂存表，执行严格的**原文证据子串匹配算法**，杜绝大模型幻觉，未命中证据强制置空。
   - 支持受控分类字典与场景标签（餐饮、演出、出行、医疗、SPA、签证等）。
   - 智能商户名称归一化、去重合并 (`dedupe`) 与事务晋级 (`promote`)。
5. **服务交付与生态集成**：
   - **独立 Web 阅读器**：FastAPI 后端 + Next.js 14 前端，支持暗黑模式、分类检索、商户证据溯源与设置管理。
   - **Model Context Protocol (merchants-mcp)**：提供 6 大只读工具，让 Claude / Codex 等 AI 助手轻松消费商户与演出数据。
   - **飞书/Lark 增量推送代理 (Push Agent)**：自动发现最新入库商户与演出，大模型加工为精炼简报推送到群卡片。

---

## 🚀 快速上手

### 1. 安装基础运行依赖

```bash
# 1. 创建 Python 虚拟环境并安装核心依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest httpx

# 2. 安装 Node.js 运行时依赖与 Playwright 浏览器
cd assets && npm install && cd ..
cd merchants-mcp && npm install && cd ..
playwright install chromium
```

> **注意**：Node.js 用于签名计算与 MCP；视频分析需要系统安装 `ffmpeg`。

### 2. 初始化环境配置

复制 `.env.example` 为 `.env`，生成并配置高强度安全密钥：

```bash
cp .env.example .env

# 生成至少 32 字节的安全随机密钥，将输出填写至 .env 的 API_AUTH_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

确保配置了正确的 PostgreSQL 数据库连接账号密码。

---

## 💻 常用 CLI 运维命令

### 数据抓取与导出

```bash
# 登录小红书账号（优先使用终端/弹窗二维码扫码）
python scripts/xhs.py login --prefer qr --name acc1

# 抓取博主最近笔记（支持指定页数预算、自动下载媒体并执行多模态分析）
python scripts/xhs.py user <user_id> --pages 3 --download --analyze

# 关键词搜索抓取（断点续跑模式）
python scripts/xhs.py search "香港美食" --pages 2

# 抓取指定笔记的全部评论与展开子评论
python scripts/xhs.py comments <note_id>

# 导出已抓取笔记（支持 md / csv / json / xlsx）
python scripts/xhs.py export --format md
```

### 商户抽取、校验与晋级

```bash
# 初始化受控分类与标签字典
python scripts/merchant.py init

# 增量执行商户抽取（仅处理未完成笔记，支持 --limit 限制处理批次）
python scripts/merchant.py extract --limit 10

# 将校验通过的候选商户晋级至正式商户索引
python scripts/merchant.py promote

# 预览确定性同名或别名重复商户
python scripts/merchant.py dedupe

# 强制备份受影响商户后执行安全去重合并
python scripts/merchant.py dedupe --backup data/backups/merchant-dedupe-backup.json
```

### 演出与推送代理

```bash
# 增量抓取 Ticketmaster SG 近期演唱会
python scripts/ticketmaster.py crawl --max-pages 10

# 列表查看数据库中已收录的未开始演出
python scripts/ticketmaster.py list --limit 20

# 增量抓取 HKT 香港快達票演唱会
python scripts/hkticketing.py crawl --max-pages 10

# 列表查看 HKT 数据源中已收录的未开始演唱会
python scripts/hkticketing.py list --limit 20

# 预览飞书推送 Agent 生成的每日情报卡片（不实际触发发送）
python scripts/push_agent.py --dry-run

# 执行正式推送
python scripts/push_agent.py
```

### 系统诊断与统计

```bash
# 查看数据库各表数据概览与统计
python scripts/query_db.py stats

# 查看当前 IP 与账号的爬虫风控与限流状态
python scripts/xhs_risk_status.py --summary

# 探测并更新最新逆向签名 JS
python scripts/xhs.py update-js
```

---

## 🌐 独立 Web 阅读器与 API

项目提供完整的现代化全栈 Web 阅读器，直接读取 PostgreSQL 数据库：

```bash
# 一键通过 Docker Compose 启动全套服务（PostgreSQL + API + Frontend）
docker compose up -d --build
```

- **Web 阅读器界面**：<http://localhost:3000>
- **FastAPI 接口文档 (Swagger)**：<http://localhost:8000/docs>

### API 鉴权机制
所有业务接口均由 Bearer Token 保护。直接调用后端时，先用根密钥换取临时 Token：

```bash
# 1. 换取临时 Bearer Token
curl -X POST http://localhost:8000/api/auth/token \
  -H "X-API-Key: <.env 中的 API_AUTH_KEY>"
# 返回 {"access_token":"...","token_type":"bearer","expires_in":7200}

# 2. 携带 Token 查询商户
curl http://localhost:8000/api/merchants \
  -H "Authorization: Bearer <access_token>"
```

---

## 🤖 Model Context Protocol (merchants-mcp)

`merchants-mcp/` 是基于标准 MCP 协议开发的 Node.js 插件包，让外部 AI 助手无需直接连接数据库即可查询经过验证的商户与演出情报。

安装到 Codex CLI：
```bash
codex mcp add merchants \
  --env MERCHANTS_API_KEY="<.env 中的 API_AUTH_KEY>" \
  --env MERCHANTS_API_URL="http://localhost:8000" \
  -- npx -y merchants-mcp
```

详情请阅读 [merchants-mcp/README.md](merchants-mcp/README.md)。

---

## 🧪 自动化测试与验证

本项目采用测试安全隔离机制，测试套件会在 PostgreSQL 中动态生成临时 schema 运行，完成后自动清理，绝不修改线上业务数据：

```bash
# 1. 执行 Python 全量自动化回归测试
python -m pytest tests -q

# 2. 执行前端 API 鉴权测试 (需在 web/frontend 执行 npm install)
node --test tests/frontend-api.test.cjs

# 3. 执行 MCP 插件测试
cd merchants-mcp && npm test && cd ..

# 4. 代码格式与空白字符检查
git diff --check
```
