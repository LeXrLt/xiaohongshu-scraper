# 小红书爬虫与阅读器

抓取小红书笔记、用户、搜索结果和评论，直接保存到 PostgreSQL，由阅读器访问同一数据库。支持媒体下载、视频转录、图片 OCR、评论分析和 Markdown / CSV / JSON / XLSX 导出。

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

复制 `.env.example` 为 `.env`，填写 PostgreSQL 连接信息。爬虫、商户抽取和阅读器共用这些配置；运行数据命令前需要启动数据库。

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

所有抓取和抽取命令默认继续已保存的任务进度。商户抽取按笔记保存完成记录，
包括没有商户、候选需人工复核的结果；失败笔记保留错误并在后续运行重试。
已有候选会承接为历史进度。`--limit` 限制本次待处理笔记数，完成笔记在限额之前排除，
因此重复运行会继续处理后面的笔记。改模型或版本不会自动重抽；确需重抽时使用
`python scripts/merchant.py extract --note-id <id> --force`。

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
# 预览重复商户；仅规范化同名或已有明确别名关系会匹配
python scripts/merchant.py dedupe
# 先备份受影响商户和全部来源关系，再执行合并（备份文件必须不存在）
python scripts/merchant.py dedupe --backup data/merchant-dedupe-backup.json
```

`merchant.py init` 会初始化内置商户分类和标签字典。当前分类覆盖餐饮、文娱演出、交通出行、医疗健康、美容休闲和签证服务；标签按场景、环境、消费、预约、特色和便利分组。抽取提示词只允许 Agent 从这些字典中选择 `category_ids` 和 `tag_ids`，通过校验后分别写入商户分类关系和商户标签关系。

### 数据存储

所有抓取、分析、商户抽取、查询和导出都直接读写 PostgreSQL，无需额外同步。数据命令首次连接时会自动初始化或升级表结构。媒体文件、登录凭据和导出文件保存在本地 `data/`。

`search` / `user` / `feed` 复用对应的 `crawl-*` 续跑流程；`crawl-config`、`run`、
`serve` 也使用同一组断点。`--pages` / `--max-pages` 是每次运行的页数预算，
达到预算会保存下一页，源数据耗尽才标记完成。任务身份不绑定账号，轮换账号继续
同一任务，并兼容旧版账号断点。`--resume` 保留兼容，默认已开启；主动从第一页
重新抓取需传 `--restart`。列表已经抓完后，如要发现新的内容，也需显式重新抓取。
单篇 `note` 默认复用已有详情并继续媒体处理，主动更新详情使用 `--force`；
`refresh` 则按笔记上次抓取时间轮流更新。

主评论和每条评论的子评论分别保存游标；子评论达到单次预算后，下次继续该分支。
页面中断时会重放尚未提交游标的那一页，已入库的数据通过主键复用。
下载跳过完整的已有文件；新媒体目录包含笔记 ID，已有目录继续复用，避免同名笔记
混用文件或改标题后重新下载。下载和分析失败会单独保存为待重试任务；后续启用相应
选项的抓取命令会先继续这些任务，即使原抓取列表已经完成。图片 OCR 按图片内容保存进度，视频保留音频、抽帧、
转录、逐帧 OCR 和纠错缓存；成功的空结果也记为完成，失败或缺依赖的阶段留待重试。
视频文件或分析参数变化后会重新分析。分步分析的空结果不会清除已有分析和人工纠错。
`cleanup` 保留任务断点和未完成搜索的页内进度，不再按时间清除它们。

商户候选只晋级尚未处理或发生变化的记录；同名或明确别名对应的商户复用现有身份，
匹配多个商户时进入人工复核。查询、统计分析和导出仍按当前数据生成结果；导出是
可重复生成的文件，不参与抓取任务游标。

商户不设核实状态。升级前的本地数据库应先备份并完成一次性数据迁移，再停用旧版爬虫；新版不再读取或创建 `data/xhs.db`。

## 独立阅读器

项目包含 PostgreSQL、Python API 和 Next.js 前端：

```bash
cp .env.example .env
docker compose up -d --build
```

打开 <http://localhost:3000> 阅读 PostgreSQL 中的笔记。只运行数据库时使用 `docker compose up -d postgres`，本地运行 API 和前端：

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
├── database.py            # 共用 PostgreSQL 连接与表结构初始化
├── scripts/xhs_storage.py # PostgreSQL 存储与导出
├── schema.sql             # PostgreSQL 表结构
├── web/backend/           # FastAPI API
├── web/frontend/          # Next.js 阅读器
├── assets/                # 签名脚本
└── data/                  # 媒体、登录凭据、导出文件和运行日志
```

## 故障排查

- PostgreSQL 连接失败：检查 `.env` 并确认数据库已启动；爬虫与阅读器使用同一个 `POSTGRES_DB`。
- 签名失效：运行 `python scripts/xhs.py update-js`。
- 风控或限流：运行 `python scripts/xhs_risk_status.py --summary` 查看本地日志。

## 测试

启动 PostgreSQL 并配置 `.env` 后运行：

```bash
pip install pytest
python -m pytest tests -q
```

数据库测试在独立的临时 schema 中运行，结束后自动清理，不修改站点数据。连接账号需要创建 schema 的权限。
