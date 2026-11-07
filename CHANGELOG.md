# 更新日志 (CHANGELOG)

本项目的所有显著变更都将记录在此文件中。

本格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，并且本项目遵循 [语义化版本 2.0.0](https://semver.org/lang/zh-CN/)。

---

## [未发布]

### 新增 (Added)
- 接入 HKT 香港快達票公开演唱会数据，新增 `scripts/hkticketing.py`，支持分类确认、分页、香港时区场次解析、售罄状态和断点续跑。
- 新增 `hkticketing_concerts` 与 `hkticketing_concert_occurrences` 分表，并通过 `concerts_unified` / `concert_occurrences_unified` 统一提供 API、MCP 和推送查询。
- 新增 HKT 解析、幂等写入、跨平台视图和 API 回归测试。

### 变更 (Changed)
- Ticketmaster 和 HKT 按平台分别写入来源表；现有 Ticketmaster 表与推送记录保持兼容。
- HKT 抓取改用全量演出页的动态分类接口，复用页面校验请求头，并校验分类总数，避免首页推荐数据不完整导致漏抓。

## [3.2.0] - 2026-11-02

### 新增 (Added)
- **推送代理 (Push Agent) 与记录表**：
  - 新增 `scripts/push_agent.py`，支持扫描未推送的商户及演唱会增量数据，经由 LLM 总结为高可读性简报后通过飞书/Lark 自定义机器人 Webhook 发送富文本卡片。
  - 新增 `schema.sql` 中的 `push_records` 表及 `scripts/push_records.py`，多态记录实体推送状态、内容哈希、时间戳快照与重试次数。
- **Ticketmaster SG 演出抓取与查询**：
  - 新增 `scripts/ticketmaster.py`，使用 Playwright 自动化处理 Ticketmaster 浏览器反爬 EPS 验证，抽取演出及各场次购票链接 (`calendarEvents` JSON)。
  - 新增 `ticketmaster_concerts` 与 `ticketmaster_concert_occurrences` 数据表，结构化入库演出、艺人、场馆、起止时间与票务状态。
  - API 新增 `/api/concerts/upcoming` 与 `/api/concerts/{concert_id}` 接口，支持按艺人、场馆、时间范围检索未开始演唱会。
- **Model Context Protocol (MCP) 扩展**：
  - `merchants-mcp` (v0.1.1) 升级，新增演唱会查询工具 `list_upcoming_concerts` 与 `get_concert_details`，供 AI Agent 只读组合调用。

### 优化 (Changed)
- **DOM 搜索模式隔离**：默认使用项目独立的 Playwright profile，避免占用或关闭宿主机系统浏览器；保留 `XHS_DOM_BROWSER_MODE=real_profile` 备用选项。
- **抓取配额与限流策略**：将单账号与单 IP 的每日硬上限设为可选配置 (`daily_hard_cap`, `ip_daily_cap`)，并在 `data/config.json` 统一管理。
- **Fetcher 会话控制**：限制爬虫单次会话的睡眠时长上限，防止因等待时间过长阻塞任务执行。

---

## [3.1.0] - 2026-10-13

### 新增 (Added)
- **独立 Web 阅读器与鉴权体系**：
  - 新增 FastAPI 后端 (`web/backend`) 与 Next.js 前端 (`web/frontend`)。
  - 实现基于 `API_AUTH_KEY` 的临时 Bearer JWT 换取与验证机制（`/api/auth/token`），并内置 TTL 自动刷新。
  - 增加 Web 设置页面，支持在线更新关键词、博主订阅列表及 Cookie。
- **商户智能抽取与索引体系 (Merchant Intelligence)**：
  - 新增 `scripts/merchant.py`，包含商户表初始化、增量抽取、证据校验、去重合并 (`dedupe`) 与晋级 (`promote`)。
  - 新增受控分类与标签字典 (`merchant_categories`, `merchant_tags`)，多对多映射商户与来源笔记。
  - 新增候选暂存表 `merchant_extraction_candidates` 与完成状态表 `merchant_extraction_state`，实现严格基于笔记原文子串的确定性校验。
- **商户推荐 MCP 服务 (merchants-mcp)**：
  - 新增 `merchants-mcp` npm 包，基于 Node.js 原生 JavaScript 和 MCP SDK v2，通过 stdio 协议向 Codex、Claude 等提供只读商户推荐工具。
- **流程幂等与断点续跑 (Resumability Overhaul)**：
  - 全面重构抓取任务游标 (`xhs_crawl_state`)，主评论与子评论游标独立持久化。
  - 媒体下载、图片 OCR、视频分析实现失败独立入队和断点续跑，空结果与错误状态明确区分。

### 重构 (Changed)
- **存储架构统一**：彻底迁移至 PostgreSQL 作为唯一主存储，废弃原本地 SQLite (`data/xhs.db`)，实现爬虫、抽取器与 Web 服务共库直读。
- **移除商户核实状态**：移除历史 `merchants.status` 字段，以确定性证据链条与置信度替代主观核实标记。

---

## [3.0.0] - 2026-06-15

### 新增 (Added)
- **图片智能分析引擎** (`scripts/xhs_image.py`)：
  - 三层分析架构：Layer 1 离线 OCR (RapidOCR)；Layer 2 多模态视觉理解 (Ollama / OpenAI-compatible Vision API)；Layer 3 攻略路线图与流程图生成 (Mermaid Markdown)。
  - 支持 `image_config.json` 灵活组合纯文字、本地模型与云端视觉。
- **视频智能转录与摘要** (`scripts/xhs_video.py`)：
  - 支持 ffmpeg 抽音频、Whisper 语音转写、关键帧 RapidOCR 以及多模态内容摘要。
- **小红书基础爬虫功能完整覆盖**：
  - 支持笔记详情抓取、用户信息与发布列表抓取、关键词搜索、评论与嵌套二级评论抓取。
  - 支持媒体文件（高清图、短视频）本地自动命名与归档。
  - 支持导出格式：Markdown、CSV、JSON、XLSX。
- **安全与逆向签名支持**：
  - 集成 Node.js 签名执行环境 (`assets/xhs_a1.js`, `assets/xhs_xray.js`)，支持自动更新签名算法。
  - 支持本地原生 QR 扫码登录、WSL 登录适配及风控状态监测。
