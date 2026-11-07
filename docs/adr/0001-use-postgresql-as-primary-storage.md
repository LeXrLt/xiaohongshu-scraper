# 0001. 使用 PostgreSQL 作为全栈主存储

- **日期**：2026-10-13
- **状态**：Accepted

---

## 背景与痛点 (Context)

在项目早期版本中，数据存储依赖于本地 SQLite 文件（`data/xhs.db`）。随着业务扩展，系统引入了：
1. 异步多并发爬虫与媒体下载器；
2. 离线多模态大模型商户抽取 Worker；
3. 基于 FastAPI 的 Web 后端接口与 Next.js 前端；
4. MCP 插件只读服务。

在此场景下，SQLite 暴露了显著缺陷：
- **并发锁竞争 (Database Locked)**：多进程或多线程同时写入时频繁报 `sqlite3.OperationalError: database is locked`。
- **缺乏高级 JSON 与递归查询**：分类树需要层级查询（父类展开到子类），商户具有多标签与多别名，SQLite 对复杂 JSONB 原生操作与递归 CTE 支持较弱。
- **异构同步负担**：此前曾尝试“SQLite 爬取 + 定期同步到 PostgreSQL”，导致双重维护、同步延迟与主键冲突。

---

## 决策内容 (Decision)

1. **废弃 SQLite**：彻底停止写入和读取 `data/xhs.db`，由 PostgreSQL 充当全系统唯一可信数据源。
2. **共库直读直写**：爬虫 CLI、商户抽取引擎、FastAPI 后端及推送 Agent 直接连接同一个 PostgreSQL 数据库实例。
3. **原生 JSONB 与递归树支持**：全面使用 PostgreSQL `JSONB` 字段存储结构化别名、标签数组和原始载荷；使用 `WITH RECURSIVE` 递归 CTE 高效处理多级商户分类。
4. **一键容器化**：在 `docker-compose.yml` 中默认集成 `postgres:16-alpine`，并挂载 `schema.sql` 实现首次自动初始化。

---

## 影响与后果 (Consequences)

### 正面收益
- 彻底解决了高并发写入的锁死问题，支持行级锁 (`FOR UPDATE SKIP LOCKED`)。
- 架构极致简化，消除了两套数据库之间脆弱的同步脚本。
- 测试环境通过 `tests/conftest.py` 动态创建独立 schema，实现轻量可靠的单元测试隔离。

### 妥协与代价
- 本地纯离线运行需依赖 PostgreSQL 守护进程（但可通过 `docker compose up -d postgres` 一行命令拉起，成本可控）。
- 部署需要配置 PostgreSQL 连接账号与环境变量。
