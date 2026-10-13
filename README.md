# 小红书爬虫与阅读器

抓取小红书笔记、用户、搜索结果和评论，保存到本地 SQLite，并同步到独立的 PostgreSQL 阅读器。支持媒体下载、视频转录、图片 OCR、评论分析和 Markdown / CSV / JSON / XLSX 导出。

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd assets && npm install && cd ..
playwright install chromium
```

Node.js 用于签名脚本，Playwright 用于登录和浏览器搜索；视频分析还需要 ffmpeg。

## 配置

复制 `.env.example` 为 `.env`，填写 PostgreSQL 连接信息。爬虫即使没有 PostgreSQL 也可以只使用本地 SQLite；配置了 PostgreSQL 后，数据写入命令会自动同步。

```env
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=xhs_reader
POSTGRES_PASSWORD=your_password
POSTGRES_DB=xhs_reader
```

商户抽取 Agent 的配置也放在 `.env`，模板见 `.env.example`。运行
`python scripts/merchant.py extract` 前至少需要配置
`MERCHANT_AGENT_API_KEY`、`MERCHANT_AGENT_BASE_URL` 和 `MERCHANT_AGENT_MODEL`。
抽取结果先进入候选表，经过证据校验后会自动幂等写入商户索引；已有候选可单独运行
`python scripts/merchant.py promote`。

## 常用命令

```bash
# 登录、抓取和导出
python scripts/xhs.py login --prefer qr --name acc1
python scripts/xhs.py user <user_id> --pages 3 --download --analyze
python scripts/xhs.py search "关键词" --pages 2
python scripts/xhs.py comments <note_id>
python scripts/xhs.py export --format md

# 查看本地运行日志
python scripts/xhs_risk_status.py --summary
python scripts/query_db.py stats
python scripts/merchant.py init
python scripts/merchant.py extract --limit 10
python scripts/merchant.py promote
```

`merchant.py init` 会初始化内置商户分类和标签字典。当前分类覆盖餐饮、文娱演出、交通出行、医疗健康、美容休闲和签证服务；标签按场景、环境、消费、预约、特色和便利分组。抽取提示词只允许 Agent 从这些字典中选择 `category_ids` 和 `tag_ids`，通过校验后分别写入商户分类关系和商户标签关系。

### 手动同步 PostgreSQL

写入类命令结束时会自动同步。需要单独同步时运行：

```bash
python pg_sync.py
```

同步模块只读 `data/xhs.db` 并幂等写入 `xhs_notes`、`xhs_users`、`xhs_comments` 等表，不依赖外部服务。

商户不设核实状态。旧 SQLite 库在下次运行爬虫或 `python scripts/merchant.py init` 时自动移除商户状态字段；旧 PostgreSQL 库在运行 `python pg_sync.py` 时自动移除。迁移保留商户信息和来源关系。

## 独立阅读器

项目包含 PostgreSQL、Python API 和 Next.js 前端：

```bash
cp .env.example .env
docker compose up -d --build
```

打开 <http://localhost:3000> 阅读已经同步到 PostgreSQL 的笔记。只运行数据库时使用 `docker compose up -d postgres`，本地运行 API 和前端：

```bash
python -m uvicorn main:app --app-dir web/backend --reload --port 8000
cd web/frontend && npm install && npm run dev
```

设置页保存的关键词、博主和 cookies 会写入 PostgreSQL 的 `reader_config` 表；可以从项目根目录执行：

```bash
python scripts/xhs.py crawl-config --max-pages 2
```

## 目录结构

```
├── scripts/xhs.py         # 爬虫 CLI
├── scripts/merchant.py    # 商户表初始化、Agent 抽取与候选校验
├── scripts/merchant_prompt.md # 商户 Agent 抽取提示词
├── scripts/xhs_storage.py # SQLite 存储与导出
├── pg_sync.py             # SQLite → PostgreSQL 同步
├── schema.sql             # PostgreSQL 表结构
├── web/backend/           # FastAPI API
├── web/frontend/          # Next.js 阅读器
├── assets/                # 签名脚本
└── data/                  # SQLite、媒体和运行日志
```

## 故障排查

- PostgreSQL 无数据：检查 `.env`，然后运行 `python pg_sync.py`。
- 签名失效：运行 `python scripts/xhs.py update-js`。
- 风控或限流：运行 `python scripts/xhs_risk_status.py --summary` 查看本地日志。
