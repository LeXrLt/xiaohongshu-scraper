# 部署与运维指南 (DEPLOYMENT.md)

本指南详细说明小红书爬虫与阅读器系统在本地开发、Docker 容器化以及 Linux 生产环境下的部署流程、配置清单及日常运维方案。

---

## 1. 基础环境与依赖要求

### 1.1 硬件与系统要求
- **操作系统**：Linux (Ubuntu 22.04 LTS+ 推荐) / macOS (Apple Silicon 或 Intel) / Windows WSL2
- **最低配置**：2 核 CPU / 4GB 内存 / 20GB 可用磁盘空间
- **推荐配置**（开启 Whisper 离线语音分析与本地多模态时）：4 核+ CPU / 8GB+ 内存 / SSD 存储

### 1.2 软件依赖项
| 软件 / 组件 | 推荐版本 | 作用 |
| :--- | :--- | :--- |
| **Python** | 3.10 ~ 3.12 | 爬虫引擎、抽取调度器及 FastAPI 后端 |
| **Node.js** | ≥ 20.19 (LTS) | JS 逆向签名执行、Next.js 前端及 MCP 服务 |
| **PostgreSQL**| 16-alpine (≥ 14) | 核心关系型与 JSONB 数据库 |
| **Playwright**| 最新稳定版 | 驱动 Chromium 模拟人类浏览与通过反爬验证 |
| **ffmpeg** | ≥ 4.4 | 视频音频分离、抽帧与格式转换 |

---

## 2. 环境变量配置参考 (`.env`)

在项目根目录下复制 `.env.example` 为 `.env` 并按需调整参数：

```ini
# ==========================================
# 1. PostgreSQL 数据库连接
# ==========================================
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=xhs_reader
POSTGRES_PASSWORD=your_secure_password
POSTGRES_DB=xhs_reader

# ==========================================
# 2. Python API 与安全鉴权
# ==========================================
API_HOST=0.0.0.0
API_PORT=8000
FRONTEND_PORT=3000
FRONTEND_ORIGIN=http://localhost:3000

# 永久 API 管理密钥：必须至少 32 字节（用 python -c "import secrets; print(secrets.token_urlsafe(32))" 生成）
API_AUTH_KEY=your_generated_32_bytes_random_secret_string
# 临时 Bearer Token 有效期（秒），默认 2 小时
API_TOKEN_TTL_SECONDS=7200

# ==========================================
# 3. Next.js 前端阅读器
# ==========================================
# 浏览器直连 API 地址（若前端通过 Next.js 服务端反代则留空）
NEXT_PUBLIC_API_URL=

# ==========================================
# 4. 大模型通用配置 (LLM Agent)
# ==========================================
AGENT_PROVIDER=openai
AGENT_API_KEY=sk-your-model-api-key
AGENT_BASE_URL=https://api.openai.com/v1
AGENT_MODEL=gpt-4o-mini
AGENT_TIMEOUT_S=60
AGENT_RETRIES=2

# ==========================================
# 5. 商户抽取专有参数
# ==========================================
MERCHANT_AGENT_BATCH_SIZE=5
MERCHANT_AGENT_MAX_NOTES=100

# ==========================================
# 6. 飞书 / Lark 推送代理配置
# ==========================================
LARK_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxx
LARK_WEBHOOK_SECRET=your_lark_webhook_signature_secret
PUSH_AGENT_CHANNEL=feishu
PUSH_AGENT_MAX_ITEMS=30

# ==========================================
# 7. 票务平台浏览器配置（可选）
# ==========================================
# TICKETMASTER_CHROMIUM_PATH=/usr/bin/chromium
# TICKETMASTER_USER_AGENT=
# HKTICKETING_CHROMIUM_PATH=/usr/bin/chromium
# HKTICKETING_PROFILE_DIR=/opt/xiaohongshu-scraper/data/pw_profile_hkticketing
# HKTICKETING_API_BASE_URL=https://rest-sig.imaitix.com
```

---

## 3. 部署方案

### 方案 A：Docker Compose 一键容器化部署（推荐生产与演示）

项目内置完善的 `docker-compose.yml`，包含 `postgres`、`api` (FastAPI) 和 `frontend` (Next.js)。

```bash
# 1. 确保 .env 配置完毕且生成了 API_AUTH_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"

# 2. 构建镜像并后台启动全栈服务
docker compose up -d --build

# 3. 检查容器运行状态与健康检查
docker compose ps

# 4. 查看实时后端与数据库日志
docker compose logs -f api postgres
```

启动完成后：
- **Web 阅读器前端**：`http://localhost:3000`
- **FastAPI 接口服务**：`http://localhost:8000`
- **PostgreSQL 数据库**：`localhost:5432`

---

### 方案 B：混合部署（本地开发与调试）

在宿主机上开发，仅将 PostgreSQL 容器化：

```bash
# 1. 启动 PostgreSQL 单独容器
docker compose up -d postgres

# 2. 初始化宿主机 Python 虚拟环境与依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cd assets && npm install && cd ..

# 3. 启动本地 FastAPI 后端（带代码热重载）
python -m uvicorn main:app --app-dir web/backend --reload --port 8000

# 4. 另开终端，启动本地 Next.js 前端
cd web/frontend
npm install
npm run dev
```

---

### 方案 C：Linux 生产系统服务 (Systemd)

在独立云服务器上长期运行 Web 后端与定时任务：

#### 1. 创建后端 Systemd 服务 (`/etc/systemd/system/xhs-api.service`)
```ini
[Unit]
Description=Xiaohongshu Reader FastAPI Backend
After=network.target postgresql.service

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/xiaohongshu-scraper
EnvironmentFile=/opt/xiaohongshu-scraper/.env
ExecStart=/opt/xiaohongshu-scraper/.venv/bin/uvicorn main:app --app-dir web/backend --host 127.0.0.1 --port 8000 --workers 4
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

#### 2. Nginx 反向代理与 SSL 终止配置
```nginx
server {
    listen 80;
    server_name reader.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name reader.example.com;

    ssl_certificate /etc/letsencrypt/live/reader.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/reader.example.com/privkey.pem;

    # Next.js 前端
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # API 接口直接路由
    location /api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## 4. MCP (Model Context Protocol) 插件集成

`merchants-mcp` 可直接配置进 Codex 或 Claude Desktop 客户端：

### 4.1 安装到 Codex CLI
```bash
codex mcp add merchants \
  --env MERCHANTS_API_KEY="<你的 API_AUTH_KEY>" \
  --env MERCHANTS_API_URL="https://reader.example.com" \
  -- npx -y merchants-mcp
```

### 4.2 配置到 Claude Desktop (`claude_desktop_config.json`)
```json
{
  "mcpServers": {
    "merchants": {
      "command": "npx",
      "args": ["-y", "merchants-mcp"],
      "env": {
        "MERCHANTS_API_KEY": "your_api_auth_key_here",
        "MERCHANTS_API_URL": "http://localhost:8000"
      }
    }
  }
}
```

---

## 5. 定时调度与自动化任务 (Cron)

通过系统的 `crontab -e` 安排全自动数据流转任务：

```bash
# 激活项目虚拟环境执行各流水线
# 每天凌晨 02:00：执行根据设置项（博主/关键词）的增量抓取
0 2 * * * cd /opt/xiaohongshu-scraper && .venv/bin/python scripts/xhs.py crawl-config --max-pages 3 >> data/cron_crawl.log 2>&1

# 每天凌晨 03:00：增量抓取 Ticketmaster SG 演唱会
0 3 * * * cd /opt/xiaohongshu-scraper && .venv/bin/python scripts/ticketmaster.py crawl --max-pages 10 >> data/cron_ticketmaster.log 2>&1

# 每天凌晨 03:20：增量抓取 HKT 香港快達票演唱会
20 3 * * * cd /opt/xiaohongshu-scraper && .venv/bin/python scripts/hkticketing.py crawl --max-pages 10 >> data/cron_hkticketing.log 2>&1

# 每天凌晨 04:00：执行未处理笔记的商户抽取与晋级
0 4 * * * cd /opt/xiaohongshu-scraper && .venv/bin/python scripts/merchant.py extract --limit 50 && .venv/bin/python scripts/merchant.py promote >> data/cron_merchant.log 2>&1

# 每天早上 09:00：调用 Push Agent 向飞书群推送昨日情报早报
0 9 * * * cd /opt/xiaohongshu-scraper && .venv/bin/python scripts/push_agent.py >> data/cron_push.log 2>&1
```

---

## 6. 数据库日常备份与恢复

```bash
# 备份全部 PostgreSQL 数据库至本地文件
pg_dump -h localhost -p 5432 -U xhs_reader -d xhs_reader -Fc > data/backups/xhs_reader_$(date +%Y%m%d_%H%M%S).dump

# 从备份文件恢复数据库
pg_restore -h localhost -p 5432 -U xhs_reader -d xhs_reader --clean --if-exists data/backups/xhs_reader_20261106_120000.dump
```

---

## 7. 生产安全核对检查单 (Security Checklist)

- [ ] **强随机密钥**：`API_AUTH_KEY` 使用了密码学安全的随机字符串，长度不少于 32 字节。
- [ ] **公网边界隔离**：PostgreSQL 默认端口 `5432` 仅监听在 `127.0.0.1` 或 Docker 内部网络，禁止暴露于公网。
- [ ] **HTTPS 强制**：在生产公网部署时，配置了 SSL 证书并强制启用 HTTPS。
- [ ] **Cookie 保护**：爬虫登录 Cookie 存储于数据库 `reader_config` 与本地 `data/` 中，未被暴露在只读 API 或 MCP 工具中。
- [ ] **大模型开销熔断**：配置了合理的大模型超时时间 (`AGENT_TIMEOUT_S`) 与单次批处理上限 (`MERCHANT_AGENT_BATCH_SIZE`)。
