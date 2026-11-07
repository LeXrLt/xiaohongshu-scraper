# 详细设计文档 (DESIGN.md)

## 1. 采集与反爬签名核心设计

### 1.1 逆向签名与环境对齐机制
小红书 Web 端采用私有加密算法对关键请求头进行动态校验。签名体系位于 `assets/` 目录：
- **核心算法脚本**：`assets/xhs_a1.js`（环境初始化与设备指纹计算）与 `assets/xhs_xray.js`（动态载荷加密与 `x-s` / `x-t` 签名生成）。
- **Python-Node 通信桥接**：`scripts/xhs_sign.py` 通过系统标准输入/输出启动轻量 Node.js 进程，以 JSON 格式传递请求参数（`url`、`data`、`a1`、`api_type` 等），毫秒级获取签名结果，避免重度引入无头浏览器计算签名。
- **签名算法自动更新**：`scripts/xhs_update_js.py` 与 `updater/` 模块负责定期探测平台算法变化，自动拉取并格式化最新 JS 打包文件。

### 1.2 浏览器指纹与 DOM 搜索隔离
- **独立 Profile 机制**：Playwright DOM 搜索默认在 `data/pw_profile_<hash>/` 建立隔离用户数据目录，规避与其他自动化工具的指纹冲突，绝不占用或强杀用户主浏览器。
- **Ticketmaster EPS 挑战规避**：
  - Ticketmaster 新加坡站部署了 Cloudflare EPS 挑战，连续快速页面导航会触发验证阻断。
  - 调度器在抓取活动详情页之间强制注入 `1.5 - 3.0` 秒随机延迟。
  - 请求头伪造：自动检测并对其 UA 主版本与底层 Playwright Chromium 实例对齐，注入 `en-SG` 语言偏好与 `Asia/Singapore` 时区环境。
- **HKT Baxia 与 Profile 隔离**：
  - `scripts/hkticketing.py` 使用 `data/pw_profile_hkticketing` 持久化 Profile，默认不接管宿主机浏览器、不登录、不进入购票流程。
  - 浏览器入口使用 `https://hkt.hkticketing.com/#/allEvents/index`；该页面通过 `/api/proType/getProjectTypeList?langType=3` 动态解析“演唱會”分类及 `saleProjectCount`，不使用首页推荐数据。
  - 列表和详情请求复用页面的 Baxia 上下文，生成 `hecate/epeius` 校验头并携带 CSRF、会话和详情 `project-id`；这些敏感值只在浏览器上下文内使用。
  - 当前已核实的接口为 `/api/proType/getProjectTypeList?langType=3`、`/api/pro/customizableProjects?page=...&pageSize=...&projectClass=...&projectName=&areaId=&langType=3` 和 `/api/pro/project?projectToken=...&reqType=1&langType=3`；分页字段以响应中的 `nextPage/totalPage` 为准，并校验 `totalRow` 与分类总数一致。
  - HKT 的 `eventCaption` 按 `Asia/Hong_Kong` 解析为 UTC；`eventVoList.priceVoList.sellOut` 仅用于确定性推导票务状态。

---

## 2. 状态机与幂等断点续跑设计

系统的核心韧性建立在精细化的持久化断点状态机之上。相关详细推导亦可参见历史记录 [idempotency-review.md](file:///home/lele/WorkingSpace/hongkong/xiaohongshu-scraper/docs/idempotency-review.md)。

### 2.1 任务游标状态模型 (`xhs_crawl_state`)
对于列表搜索、博主时间线、Feed 流抓取，状态表统一记录：
- `task_id`：规范化键名（如 `search:香港美食:sort_default` 或 `user:5a...`）。
- `cursor`：平台返回的下一次请求游标。
- `status`：`running` / `completed` / `paused` / `failed`。

**续跑铁律**：
- 每次传入的 `--pages` 或 `--max-pages` 仅作为**单次执行的增量预算**，而非全局累计页数。
- 当页数据成功写入并提交事务后，立即持久化更新 `cursor` 为下一页游标。
- 只有当平台返回源数据彻底耗尽时，才将 `status` 标记为 `completed`；非耗尽终止仅保留当前游标供下次继续。
- 启动默认带断点续跑，若要强制从第 1 页拉取最新内容，必须显式传递 `--restart`。

### 2.2 树状两级评论游标设计
评论抓取分为根评论与子评论：
1. 根评论分页拉取，以单篇笔记为单位记录 `comment_cursor`。
2. 每条包含未展开子评论的根评论，独立在状态中记录 `sub_comment_cursor`。
3. 即使单次抓取预算耗尽中断，下次重入时将先定位到未完成的子评论分支继续拉取，避免重复遍历已入库评论。

### 2.3 抽取完成状态独立表 (`merchant_extraction_state`)
商户抽取调度器以**单篇笔记**为事务原子单位：
- 当某篇笔记经由大模型抽取，即使返回商户列表为空（未提到商户），也必须在 `merchant_extraction_state` 中写入 `status='completed'`。
- 后续增量拉取通过 `LEFT JOIN merchant_extraction_state` 排除已完成笔记，防止空结果笔记长期占住队头导致重复调用大模型。

---

## 3. 多模态内容智能分析设计

详细演进分析见 [technical-report-image-analysis.md](file:///home/lele/WorkingSpace/hongkong/xiaohongshu-scraper/docs/technical-report-image-analysis.md)。

### 3.1 图片三层分析架构
图片分析设计采用松耦合的三层管道模型：
```text
Layer 1: 离线 OCR (RapidOCR)
         提取图片中的纯文本、招牌、菜单价格、标签等，完全离线运行。
Layer 2: 多模态视觉理解 (Ollama / OpenAI-compatible Vision API)
         调用视觉大模型（如 llama3.2-vision 或 gpt-4o），输出结构化图文描述。
Layer 3: 流程图/路线图生成 (Mermaid Synthesis)
         针对攻略型、地图型、步骤型笔记，将 OCR 识别的拓扑节点转换为标准 Mermaid 流程图代码。
```

### 3.2 视频音视频离线流水线
1. **音频分离与转写**：调用本地 `ffmpeg` 抽取 16kHz 单声道音频文件，提交本地轻量 `Whisper` 模型执行离线语音转写。
2. **场景关键帧截取与 OCR**：按固定帧率（如 1 FPS）抽帧，利用 RapidOCR 识别画面字幕与展示信息。
3. **跨模态总结**：将音频文本与画面文字聚合，提交 LLM 提炼短视频核心摘要。

---

## 4. 商户情报知识抽取与确定性门禁

### 4.1 受控字典设计 (Controlled Taxonomy)
系统杜绝大模型随意创造分类与标签：
- **分类 (`merchant_categories`)**：层级树状结构，根分类覆盖餐饮、文娱演出、交通出行、医疗健康、美容休闲、签证服务；子分类收敛至具体业态（如 `restaurant`、`japanese`、`cafe`、`spa`）。
- **标签 (`merchant_tags`)**：按 `场景`（约会、海景、聚餐）、`便利`（近地铁、可预约）、`特色`（老字号、米其林）等维度分组，名称严格限制在 6 个汉字以内。

### 4.2 确定性校验算法 (Deterministic Substring Verification)
模型返回 JSON 候选后，Python 校验引擎执行以下严密规则：
```python
def verify_candidate_evidence(note_content: str, candidate: dict) -> bool:
    # 1. 商户名称必须在正文中出现（支持 Unicode 归一化后匹配）
    if not is_normalized_substring(candidate["name"], note_content):
        return False
    
    # 2. 价格与币种证据校验
    if candidate.get("price") and not candidate.get("price_evidence"):
        candidate["price"] = None  # 缺失证据直接清空，杜绝臆测
        
    # 3. 预约信息证据校验
    if candidate.get("reservation_info") and not any(
        is_normalized_substring(ev, note_content) for ev in candidate.get("evidence", [])
    ):
        candidate["reservation_info"] = ""
        
    return True
```

### 4.3 商户去重与晋级 (Dedupe & Promote)
- **归一化算法**：商户名称在比对前进行统一全角转半角、消除无意义空格、英文字符转小写，并剔除营销前缀（如“【必吃榜】”）。
- **确定性合并规则**：当候选商户名称与现有已存在商户完全一致，或属于其已登记的 `aliases` 之一时，复用该商户的 `merchant_id`，仅追加新增的分类/标签证据与关联笔记关系；若出现跨分类歧义，则标记为 `needs_review` 待人工复核。

---

## 5. 安全与 API 鉴权详细设计

### 5.1 双 Token 鉴权架构
```text
可信客户端 / 管理员                  FastAPI 认证网关                      业务数据接口
        │                                  │                                  │
        │─── 1. POST /api/auth/token ──────>│                                  │
        │    Header: X-API-Key: <永久密钥>  │                                  │
        │                                  │─── 校验 HMAC-SHA256 签名          │
        │                                  │─── 生成 HS256 JWT Token (TTL)    │
        │<── 2. 返回 {access_token, ttl} ──│                                  │
        │                                  │                                  │
        │─── 3. GET /api/merchants ───────────────────────────────────────────>│
        │    Header: Authorization: Bearer <access_token>                     │
        │                                                                     │─── 校验 JWT 载荷
        │<── 4. 返回商户数据 ──────────────────────────────────────────────────│
```

- **永久密钥 (`API_AUTH_KEY`)**：长度至少 32 字节，由服务器通过加密随机函数生成，仅通过环境变量配置给可信调用者。
- **短期 Token (`access_token`)**：
  - 标准 HS256 签名，载荷包含 `sub: api-client`, `iss: xhs-reader-api`, `aud: xhs-reader`, `exp: now + TTL`。
  - 默认有效期 7200 秒（2 小时），可在 `.env` 中通过 `API_TOKEN_TTL_SECONDS` 动态调整。
  - 数据接口仅接受 Bearer 凭证，永久密钥无法直接调用数据接口，降低密钥在网络中明文传输的暴露风险。

### 5.2 前端无缝刷新机制
- Next.js 前端统一在内存中维护 Token 状态与过期时间戳；
- 发起数据请求前检查 Token 有效期，在剩余寿命不足 5 分钟时**静默发起并发保护的续期请求**；
- 若偶发 `401 Unauthorized`，客户端自动触发清空缓存重新换取 Token 并透明重放原请求一次，保证用户无感。

---

## 6. Model Context Protocol (MCP) 架构设计

`merchants-mcp` 作为独立的 Node.js 原生客户端程序，实现了标准 MCP 协议：

### 6.1 工具接口规范与防御边界
| 工具名称 | 核心职责 | 防御性策略 |
| :--- | :--- | :--- |
| `list_merchant_categories` | 查询受控商户分类目录 | 强制缓存，只读输出，避免无谓数据库查询 |
| `list_merchant_tags` | 按分类查询实际使用标签 | 校验 `category_id` 合法性 |
| `search_merchants` | 多条件检索商户知识库 | 强制分页（单页最大 30 条），多标签 AND 关系 |
| `get_merchant_details` | 读取商户证据链与真实笔记 | 单笔记正文与证据字符截断（≤ 12000 代码单元） |
| `list_upcoming_concerts` | 检索各已接入平台未开始演出 | 强制过滤 `start_at > NOW()`，按日期升序 |
| `get_concert_details` | 查看演唱会场次与购票链接 | 场次最大分页 100 条 |

### 6.2 管道通信隔离
- MCP 插件仅使用 `process.stdout` 输出 JSON-RPC 协议报文。
- 所有的运行时日志与错误堆栈强制重定向至 `process.stderr`，杜绝协议层反序列化异常。

---

## 7. 飞书增量推送代理 (Push Agent) 设计

### 7.1 多态增量探测
`push_records` 表通过 `(entity_type, entity_id, channel)` 联合唯一键管理跨实体的推送生命周期：
```sql
SELECT m.merchant_id FROM merchants m
LEFT JOIN push_records pr 
  ON pr.entity_type = 'merchant' AND pr.entity_id = m.merchant_id AND pr.channel = 'feishu'
WHERE pr.record_id IS NULL OR m.updated_at > pr.entity_updated_at
LIMIT 30;
```

### 7.2 智能简报与富文本卡片
- **LLM 语义加工**：收集待推送商户的分类、人均价格、特色标签及原文真实评价，提示词约束模型用生动、亲切的情报助手口吻生成精炼要点。
- **飞书卡片组装**：通过 Webhook 发送包含颜色主题、多栏排版、直达链接与 Markdown 格式的交互式卡片，推送成功后以数据库事务原子更新 `push_records` 的推送状态与内容摘要快照。
