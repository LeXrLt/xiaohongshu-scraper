# 贡献指南 (CONTRIBUTING)

欢迎参与小红书爬虫与阅读器（Xiaohongshu Scraper & Reader）项目的开发与维护！为保证代码质量、系统稳定性和工程规范，请在提交代码前仔细阅读本指南。

---

## 1. 开发环境要求

- **操作系统**：Linux (Ubuntu 22.04+ 推荐) / macOS / WSL2 (Windows)
- **Python**：≥ 3.10
- **Node.js**：≥ 20.19 (LTS 推荐)
- **PostgreSQL**：≥ 14 (推荐 16)
- **Playwright**：Chromium 浏览器运行时
- **多媒体组件**：ffmpeg (用于音视频抽帧及音频提取)

---

## 2. 本地开发初始化

### 2.1 克隆仓库与创建 Python 虚拟环境

```bash
git clone <repository_url>
cd xiaohongshu-scraper

# 创建并激活虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装 Python 核心依赖与开发依赖
pip install --upgrade pip
pip install -r requirements.txt
pip install pytest httpx
```

### 2.2 安装 Node.js 运行时与前端依赖

```bash
# 安装签名脚本依赖
cd assets && npm install && cd ..

# 安装 MCP 依赖
cd merchants-mcp && npm install && cd ..

# 安装 Next.js 阅读器依赖
cd web/frontend && npm install && cd ..
```

### 2.3 安装浏览器自动化运行时

```bash
playwright install chromium
```

### 2.4 配置环境变量

复制 `.env.example` 并生成随机密钥：

```bash
cp .env.example .env

# 生成至少 32 字节的安全随机密钥，填写至 .env 的 API_AUTH_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

确保本地或 Docker 中的 PostgreSQL 已启动并配置了对应的用户与数据库名。

---

## 3. 分支管理与提交规范

### 3.1 分支命名规范

- `feature/<name>`：新功能开发，如 `feature/lark-push-agent`
- `fix/<issue-name>`：缺陷修复，如 `fix/dom-search-isolation`
- `refactor/<target>`：重构与架构优化，如 `refactor/postgres-storage`
- `docs/<topic>`：文档编写或更新，如 `docs/restructure-docs`
- `chore/<task>`：依赖升级、构建配置等微调

### 3.2 Commit 提交信息规范 (Conventional Commits)

提交说明必须遵循结构化约定：`<type>(<scope>): <subject>`

常用的 `<type>` 类型包括：
- `feat`：新增功能
- `fix`：修复 Bug
- `docs`：仅文档变动
- `style`：代码格式调整（不影响逻辑）
- `refactor`：既非新功能也非 Bug 修复的代码重构
- `perf`：提升性能的代码修改
- `test`：增加或修正测试用例
- `chore`：构建流程、依赖或辅助工具变动

**示例**：
```bash
feat(crawler): add resumable subcomment cursor pagination
fix(api): handle token expiration correctly on concurrent requests
docs(adr): record architectural decision on postgres migration
```

---

## 4. 编码与设计规范

### 4.1 Python 编码规范

- **严格类型注解**：新代码文件顶部必须包含 `from __future__ import annotations`，所有函数参数与返回值必须标注类型。
- **PEP 8 规范**：遵循 PEP 8 命名与格式要求，单行字符长度建议不超过 120 字符。
- **模块职责边界**：
  - 核心抓取逻辑保留在 `scripts/xhs_*.py`。
  - 数据模型与持久化交互收敛至 `scripts/xhs_storage.py` 或 `database.py`。
  - 外部服务请求需做防御性重试与超时控制，禁止无休止的死循环重试。
  - 库文件内部禁止直接使用 `print()` 输出运行时日志，必须使用 `scripts/xhs_log.py` 提供的结构化日志机制。

### 4.2 前端与 TypeScript 规范

- `web/frontend` 采用 Next.js App Router 架构与 React Server Components 模式。
- 组件必须具有严格的 TypeScript 类型定义，禁止滥用 `any`。
- API 鉴权 token 仅保存在客户端内存状态中，禁止写入 `localStorage` 避免 XSS 窃取。

### 4.3 数据库设计与 DDL 幂等性

- 所有 DDL 脚本必须保证**幂等执行**：
  - 建表语句必须使用 `CREATE TABLE IF NOT EXISTS`。
  - 建索引语句必须使用 `CREATE INDEX IF NOT EXISTS`。
  - 列补充必须使用 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`。
  - 禁止在未编写迁移脚本前直接对已有表做破坏性删除 (`DROP COLUMN`)。
- 外键引用、级联删除与唯一索引约束必须严密设计，特别是多对多关系表与实体完成状态表。

---

## 5. 测试规范与流程

严禁在未通过测试的情况下合并代码。

### 5.1 数据库测试隔离机制

本项目测试套件在 `tests/conftest.py` 中实现了基于**动态临时 schema** 的测试隔离机制：
- 每次测试会话会在测试数据库中动态生成随机 schema（例如 `test_schema_xxxxxx`）。
- 测试完成后会自动 drop 掉临时 schema，绝不影响生产或日常开发数据。
- 运行测试前，必须确保 `.env` 中的 PostgreSQL 用户拥有 `CREATE SCHEMA` 权限。

### 5.2 执行回归测试

```bash
# 1. 运行 Python 自动化测试套件
python -m pytest tests -q

# 2. 运行前端 API 与鉴权自动化测试 (需先在 web/frontend 执行 npm install)
node --test tests/frontend-api.test.cjs

# 3. 运行 MCP 扩展单元测试
cd merchants-mcp && npm test && cd ..

# 4. 代码格式与空白字符检查
git diff --check
```

### 5.3 单元测试与网络隔离准则

- 测试代码**严禁向小红书生产端发送真实的自动化网络请求**，避免触发风控或污染线上数据。
- 涉及网络响应、大模型输出或第三方平台调用的测试，必须使用 `unittest.mock` 进行打桩 (Mocking)。

---

## 6. Pull Request 提交核对清单

在向主分支提交 Pull Request 之前，请确认：

- [ ] 新建或修改的代码符合 Python PEP 8 与项目类型标注要求。
- [ ] 所有变更经过了本地完整测试（`pytest` 及 `node --test` 全部通过）。
- [ ] 涉及数据库变更已在 `schema.sql` 同步更新，并确保建表与补列语句的幂等性。
- [ ] 敏感信息（如 `.env` 中的真实密钥、Cookie、Webhook 地址）未被加入 Git 跟踪。
- [ ] 相关文档（如 `README.md`、`docs/` 下的接口规范或架构说明）已同步更新。
- [ ] Commit 信息结构清晰并遵循 Conventional Commits 规范。
