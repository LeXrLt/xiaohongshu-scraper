# merchants-mcp

通过 MCP 为 agent 提供商户分类、标签、候选检索和关联笔记。适用于找餐厅、咖啡店、按摩、SPA、美容、诊所、体检等本地服务；agent 根据真实来源给出推荐理由和笔记介绍总结。

包使用 Node.js 原生 JavaScript 和 MCP SDK v2，通过 stdio 通信，无需构建，也无需另配大模型 API key。需要 **Node.js ≥ 20.19**。

## 安装到 Codex

发布到 npm 且内置正式 API 地址后：

```bash
codex mcp add merchants --env MERCHANTS_API_KEY=xxx -- npx -y merchants-mcp
```

**当前尚未发布到 npm，默认后端为 `http://localhost:8000`。** 公网 API 地址尚未确定，发布前需在 `src/client.js` 的 `DEFAULT_API_URL` 填入正式 HTTPS 地址。如果用户连接其他部署，可以覆盖：

```bash
codex mcp add merchants \
  --env MERCHANTS_API_KEY=xxx \
  --env MERCHANTS_API_URL=https://your-api.example.com \
  -- npx -y merchants-mcp
```

`MERCHANTS_API_KEY` 是后端 `.env` 中 `API_AUTH_KEY` 的值，由服务管理员提供。密钥只保存在 MCP 进程内，不作为工具参数或工具结果暴露。包不会自动读取调用者当前工作目录的 `.env`。

Codex 配置方式参见 [OpenAI 官方 MCP 文档](https://developers.openai.com/codex/mcp/)。`codex mcp add` 将配置写入 Codex；调用工具时才会启动插件进程并访问后端。

## 本地开发

从项目根目录：

```bash
npm ci --prefix merchants-mcp
python -m uvicorn main:app --app-dir web/backend --port 8000
```

另开终端，用根目录 `.env` 的 `API_AUTH_KEY` 配置本地 MCP，路径需替换为实际绝对路径：

```bash
codex mcp add merchants \
  --env MERCHANTS_API_KEY=xxx \
  --env MERCHANTS_API_URL=http://localhost:8000 \
  -- node /absolute/path/xiaohongshu-scraper/merchants-mcp/src/index.js
```

也可以先 `npm pack --pack-destination /tmp`，再用 `npx -y --package /tmp/merchants-mcp-0.1.0.tgz merchants-mcp` 测试安装包的命令入口。

## 推荐流程

1. 用户提出需求，如“找香港安静、有海景、适合约会的餐厅”或“找深圳南山的肩颈按摩”。agent 调用 `list_merchant_categories` 查询库中实际存在的分类，通过分类说明、别名和父子关系确定 `category_id`。
2. 调用 `list_merchant_tags` 获取该分类及子类中实际使用的标签，选择与诉求匹配的 `tag_id`。
3. 调用 `search_merchants`，在已选分类内按城市、区域、标签及介绍核心词检索。多个标签全部满足；父分类自动包含启用的子分类。结果按资料更新时间排列，agent 需根据用户需求评估匹配度。
4. 对准备推荐的商户调用 `get_merchant_details`，阅读关联笔记正文、商户证据、图片/视频摘要及日期。`notes_has_more` 为真时可通过 `notes_page` 继续读取。
5. agent 返回商户名称、城市/区域、匹配理由、已知价格及币种/计价方式、预约信息，综合相关笔记总结介绍、体验与分歧，并附笔记标题、链接和日期。不编造缺失信息，不悄悄放宽硬性要求。

工具描述和服务初始化 instructions 会告诉 agent 何时使用以及上述顺序；最终是否调用工具由 MCP 客户端和 agent 决定，MCP 本身没有关键词强制触发机制。最终自然语言总结由使用 MCP 的 agent 生成。

| 工具 | 主要参数 | 返回 |
| --- | --- | --- |
| `list_merchant_categories` | 可选 `keyword`；首次建议不传 | 分类、说明、别名、父分类、含子类商户数 |
| `list_merchant_tags` | `category_id` | 分类内实际使用的标签及商户数 |
| `search_merchants` | 必填 `category_id`；可选 `tag_ids`、`keyword`、`city`、`district`、`page`、`page_size` | 商户摘要、关联介绍证据、分类/标签、匹配条件、是否有下一页 |
| `get_merchant_details` | `merchant_id`、`notes_page`、`notes_page_size` | 商户信息、分类/标签证据、关联笔记、来源链接和日期 |

`keyword` 在名称、别名、菜系、预约信息、启用标签名称/说明，以及与该商户绑定的介绍证据中做字面子串匹配，不是自然语言语义搜索。请将整句需求拆成分类、标签和核心词。搜索不会因为同篇笔记提到其他商户的特点，就把这些特点算给当前商户。

商户列表默认每页 10 条，最大 30 条。来源笔记默认每页 5 条，最大 10 条；每条笔记的正文、证据及图片/视频摘要字段最多返回 12000 个 UTF-16 代码单元，截断时通过 `truncated_fields` 明确标记。`note_count` 是独立笔记数，`notes_total` 是关联记录数；同一篇笔记可能存在多种关联关系。agent 不应声称已阅读未返回或截断的内容。

分类目录为空时请管理员执行 `python scripts/merchant.py init`；没有商户时需先抓取笔记并运行商户抽取。MCP 不会为了填充结果而修改数据库或实时抓取。

## 配置与鉴权

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MERCHANTS_API_KEY` | 必填 | 后端永久密钥 |
| `MERCHANTS_API_URL` | `http://localhost:8000` | 后端根地址，可含反代路径前缀，不含 `/api` 后缀 |
| `MERCHANTS_API_TIMEOUT_MS` | `15000` | 每次 HTTP 请求超时，范围 100–120000 ms |

插件先调用 `POST /api/auth/token`，通过 `X-API-Key` 换取临时 token。后续请求只携带 `Authorization: Bearer ...`；token 在内存中缓存、提前续期，并发请求共用一次换取。数据接口返回 401 时重新换取并重试一次，其他错误不自动重试。密钥轮换后更新 MCP 配置并重启其进程。

所有工具均只读，只调用分类、标签和商户接口，不提供设置或 cookies 接口。stdout 专用于 MCP JSON-RPC；启动错误只写入 stderr，API 错误不会透传后端响应正文或密钥。公网请使用 HTTPS。来源笔记作为不可信资料提供给 agent，不得执行其中的指令。

## 所需后端接口

| 接口 | 用途 |
| --- | --- |
| `POST /api/auth/token` | 密钥换临时 token |
| `GET /api/merchant-categories` | 启用分类目录，支持 `keyword` |
| `GET /api/merchant-tags` | 标签目录，支持 `category_id` |
| `GET /api/merchants` | 分类（含子类）、重复参数 `tag_ids`（AND）、`keyword`、城市/区域和分页 |
| `GET /api/merchants/{merchant_id}` | 商户详情；可传 `notes_page`、`notes_page_size` 分页读取关联笔记 |

除换 token 接口外均使用 Bearer token。缺少上述接口的旧版后端需要升级后使用。

## 验证和发布

```bash
cd merchants-mcp
npm ci
npm run check
npm test
npm pack --dry-run
```

发布前填好并验证正式 `DEFAULT_API_URL`，确认拥有 npm 包名 `merchants-mcp` 的发布权限，并按项目计划确定版本和许可证（当前保留 `UNLICENSED`，没有擅自授予开源许可）。然后登录 npm 并发布：

```bash
npm login
npm publish
```

发布钩子会再次执行语法检查和测试。安装包只包含 `src/`、本说明和 `package.json`，不包含测试、开发依赖、`.env`、数据库或仓库运行数据。
