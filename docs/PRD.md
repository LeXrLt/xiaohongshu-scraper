# 产品需求文档 (PRD.md)

## 1. 文档信息

- **项目名称**：小红书爬虫与阅读器系统 (Xiaohongshu Scraper & Reader Stack)
- **文档版本**：v3.2
- **最后更新**：2026-11-06
- **状态**：已落地并持续迭代

---

## 2. 背景与痛点

小红书（RED）作为领先的生活方式与消费决策平台，沉淀了海量关于餐厅探店、旅行攻略、生活服务和文化活动的真实体验。Ticketmaster SG、HKT 香港快達票等票务平台则汇集了不同地区的文娱演出资讯。然而，这些高价值数据在消费与智能化利用上面临以下痛点：

1. **非结构化与信息碎片化**：正文中杂糅了店名、别名、菜系、人均价格、排队与预约信息，缺乏统一的数据模型，无法实现结构化过滤与对比。
2. **大模型幻觉与不可信证据**：传统 AI 抽取容易在商户名称、价格及地址上产生事实性幻觉；外部 Agent 在推荐时缺乏追溯到原笔记句子的证据链条。
3. **平台封闭与反爬屏障**：小红书具有严格的动态签名、设备指纹与风控策略；Ticketmaster 部署了 EPS 浏览器反爬屏障，HKT 使用 Baxia 风控，数据均需在隔离浏览器环境中稳定获取。
4. **缺乏对 AI 友好的标准接入**：AI Agent（如 Claude, Codex 等）无法直接高效、安全地查询本地沉淀的商户与演出情报。

---

## 3. 产品愿景与定位

构建**高可用、强幂等、带可溯源证据链的社媒与票务情报基础设施**。
通过统一的 PostgreSQL 数据底座，实现数据从“自动化采集 → 多模态解析 → 确定性商户抽取 → Web 视觉交互 / MCP 智能调用 / 飞书主动推送”的全链路打通。

---

## 4. 用户角色画像 (Personas)

| 用户角色 | 核心诉求 | 典型使用场景 |
| :--- | :--- | :--- |
| **内容消费者 / 探索者** | 告别算法信息茧房与广告干扰，专注阅读笔记干货、查看商户清单与演出日历。 | 通过 Web 阅读器按分类/标签筛选餐厅；查询下月新加坡开唱的艺人与场次。 |
| **AI 智能助手 (Agent)** | 获取零幻觉、有真实笔记佐证的商户与演出数据，为终端用户提供旅行或消费决策。 | Codex/Claude 通过 `merchants-mcp` 调用 `search_merchants` 并生成带来源链接的推荐。 |
| **运营分析师 / 知识工程师** | 监控数据采集质量、补充特定博主或关键词、微调商户分类、审核歧义商户候选。 | 登录 Web 设置页维护博主池，定期触发增量抽取与去重合并，执行飞书简报推送。 |

---

## 5. 功能模块需求详述

### 5.1 采集引擎 (Ingestion Engine)

- **PRD-F1.1：小红书多维度采集**
  - **笔记抓取**：根据笔记 ID 获取正文、标签、互动量（赞/藏/评/转）、IP 归属地、封面与图片列表。
  - **博主主页**：按博主 User ID 抓取个人资料及发布笔记时间线，支持多页预算控制。
  - **关键词检索**：支持 API 搜索与基于 Playwright 的 DOM 真实渲染搜索，具备搜索结果分页缓存 (`xhs_search_cache`)。
  - **深度评论抓取**：支持主评论与二级展开子评论采集，两级游标独立记录断点。
- **PRD-F1.2：Ticketmaster SG 演出采集**
  - 自动化驱动 Playwright 完成 EPS 验证，解析 `https://ticketmaster.sg/categories/concerts` 列表及内嵌 `calendarEvents` JSON。
  - 自动抽取演出场次、开始/结束时间、场馆及购票链接，入库至 `ticketmaster_concerts` 和 `ticketmaster_concert_occurrences`。
- **PRD-F1.3：HKT 香港演出采集**
  - 使用独立 Playwright Profile 访问 HKT 公开 JSON 接口，动态确认“演唱會”分类后分页抓取项目详情。
  - 只接入 `hkticketing_concerts` 和 `hkticketing_concert_occurrences`，解析场次、香港时区、主办方、场馆、购票入口和票档售罄状态。
  - 平台分表保存，统一视图 `concerts_unified` / `concert_occurrences_unified` 提供跨平台读取。
- **PRD-F1.4：反爬防御与环境隔离**
  - 默认采用独立的 Playwright Chromium Profile，不污染系统主浏览器。
  - 支持单账号每日请求硬上限 (`daily_hard_cap`) 与单 IP 每日上限 (`ip_daily_cap`) 配置。
  - 支持会话休眠时间上限控制，避免脚本由于过长等待而挂死。

### 5.2 多模态智能增强 (Multi-Modal Enrichment)

- **PRD-F2.1：离线与云端图片分析**
  - **Layer 1 离线 OCR**：基于 RapidOCR 实现本地高精度图文识别，结果缓存于笔记表中。
  - **Layer 2 视觉理解**：支持对接本地 Ollama（如 llama3.2-vision）或远程 OpenAI 兼容 Vision API，生成图片场景与内容描述。
  - **Layer 3 流程与路线图合成**：识别图文中的地理拓扑或步骤，自动转换为标准 Mermaid 流程图并嵌入 Markdown。
- **PRD-F2.2：视频智能转录与摘要**
  - 使用 ffmpeg 提取音频，基于 Whisper 转换音轨文本；抽取关键帧结合 RapidOCR，产出视频综合摘要。

### 5.3 商户情报与证据抽取 (Merchant Intelligence Pipeline)

- **PRD-F3.1：结构化增量抽取**
  - 扫描未抽取或正文发生变动的笔记，批量提交至大模型（基于 `scripts/merchant_prompt.md`）。
  - 抽取商户名、别名、受控分类、受控标签、城市、区域、人均消费、预约信息及原文证据。
- **PRD-F3.2：确定性证据校验 (Deterministic Verification)**
  - 抽取结果落入 `merchant_extraction_candidates` 暂存表。
  - 运行确定性校验器：商户名与所有证据字段必须为原始笔记正文的子串（或仅包含可解释空白差异）。未命中证据的字段强制置空，杜绝大模型脑补。
- **PRD-F3.3：去重、合并与晋级 (Deduplication & Promotion)**
  - 规范化商户名称（全半角、空白与大小写统一）。
  - 自动识别同名或明确别名商户；歧义商户进入人工复核队列；通过校验者原子晋级写入 `merchants` 主表及多对多关系表。

### 5.4 服务与生态交付 (Delivery & Ecosystem)

- **PRD-F4.1：独立 Web 阅读器**
  - 基于 Next.js 14 开发现代化响应式界面，支持笔记浏览、关键词/博主过滤、图文详情抽屉。
  - 商户知识库浏览：支持按分类树、受控标签组及地域筛选商户，查看关联笔记来源及原句证据。
  - 演出日历：查看已接入平台的最新演唱会及官方购票链接，并显示来源。
  - 在线配置中心：维护采集关键词、博主监控列表及 Cookie。
- **PRD-F4.2：统一 HTTP API 与安全鉴权**
  - 基于 FastAPI 暴露 RESTful 接口，所有业务数据接口均受临时 Bearer JWT 保护。
  - 通过静态密钥 `API_AUTH_KEY` 换取临时 Token，具备独立 TTL 并支持客户端静默自动续期。
- **PRD-F4.3：Model Context Protocol (merchants-mcp)**
  - 遵循标准 MCP 规范，通过 stdio 管道向 Claude Desktop、Codex 等 AI 客户端暴露 6 大只读工具：
    - `list_merchant_categories` / `list_merchant_tags` / `search_merchants` / `get_merchant_details`
    - `list_upcoming_concerts` / `get_concert_details`
  - 严格限制工具仅具备只读权限，单字段最大返回 12000 字符并显式标记截断字段。
- **PRD-F4.4：飞书/Lark 增量推送代理 (Push Agent)**
  - 扫描未推送的优质商户与即将举行的演出，由大语言模型生成生动的情报简报。
  - 通过 Webhook 发送飞书交互式卡片，状态记录于 `push_records`，杜绝重复骚扰。

---

## 6. 非功能性需求 (NFR)

1. **绝对幂等性 (Strict Idempotency)**：
   - 所有抓取、抽取、媒体转码和推送操作必须支持安全重入。
   - 进程意外终止后，再次启动必须从最近提交的游标处继续，不得重复写入数据库，不得重复调用付费模型。
2. **数据安全性与隐私边界 (Security Perimeter)**：
   - 生产部署中，小红书账号 Cookie、外部模型 API Key 及数据库密码严禁外泄。
   - 前端若面向公网开放，必须配置网关鉴权或将 API 密钥下沉至服务端代理。
3. **高扩展性 (Scalability)**：
   - 采用 PostgreSQL 关系型与 JSONB 混合架构，支持递归 CTE 高效检索分类树。
   - 核心检索字段（名称、分类、标签、时间戳、状态）建立复合索引，单表百万级笔记场景下维持毫秒级响应。
4. **可观测性与审计能力 (Observability & Auditability)**：
   - 所有抓取状态、风险拦截、模型抽取版本、原始候选及推送状态全量落表归档。
