# 运维手册：服务崩溃与容器恢复排查 (troubleshooting-service-recovery.md)

## 1. 故障现象与特征
- 访问 Web 前端 `http://<ip>:3000` 显示白屏、502 Bad Gateway 或一直在加载；
- 浏览器控制台出现大量 `401 Unauthorized` 循环；
- 抓取任务或无头浏览器启动报错：`Host system missing dependencies: libnss3.so / libX11.so`。

---

## 2. 诊断步骤

### 2.1 检查容器与服务状态
```bash
docker compose ps
docker compose logs --tail=100 api
docker compose logs --tail=100 frontend
```

### 2.2 验证 HTTP 接口可用性
```bash
# 测试公开健康检查接口
curl -i http://localhost:8000/api/health

# 测试密钥换取 Token
curl -X POST http://localhost:8000/api/auth/token \
  -H "X-API-Key: <你 .env 中的 API_AUTH_KEY>"
```
如果报 `401 detail: Invalid API key`，说明传入的密钥与当前运行的后端环境变量不一致。

---

## 3. 处理方案 (SOP)

### 3.1 方案 A：密钥不同步导致的 401 循环修复
Next.js 前端在构建或运行时需要与后端使用同一份 `API_AUTH_KEY`。如果修改了 `.env` 中的密钥：
```bash
# 重启后端并重新构建前端（将新密钥注入前端构建缓存）
docker compose up -d --build api frontend
```

### 3.2 方案 B：Playwright 缺少 Linux 系统依赖修复
若在裸机 Linux 服务器（如 Ubuntu）上直接运行爬虫报缺动态链接库：
```bash
# 安装 Playwright 所需的全部系统级 GUI / 字体 / 编解码库
playwright install-deps chromium
```

### 3.3 方案 C：单容器重启与日志追踪
```bash
# 重启异常容器
docker compose restart api

# 持续观察输出确认启动成功
docker compose logs -f api
```
输出出现 `Application startup complete` 与 `Uvicorn running on http://0.0.0.0:8000` 即表示服务已自愈。
