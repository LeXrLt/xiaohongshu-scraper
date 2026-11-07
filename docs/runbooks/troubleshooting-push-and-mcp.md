# 运维手册：飞书推送与 MCP 协议异常排查 (troubleshooting-push-and-mcp.md)

## 1. 故障现象与特征
- 飞书群机器人未收到每日简报，`push_agent.py` 输出错误：
  - `HTTP Error 19001 / 19002`（签名错误或 Webhook 地址无效）；
  - `Failed to generate summary with model`（大模型调用异常）。
- AI Agent (如 Claude Desktop / Codex) 调用 MCP 工具失败：
  - 提示 `Tool call failed`、`MCP timeout` 或 `JSON-RPC parse error`。

---

## 2. 诊断步骤

### 2.1 诊断飞书推送代理 (Push Agent)
执行带 `--dry-run` 标志的手工触发测试：
```bash
python scripts/push_agent.py --dry-run
```
- 若 `--dry-run` 成功打印出大模型生成的 Markdown 摘要，说明数据库增量探测与 LLM 总结链路正常，问题出在飞书 Webhook 网络或签名环节；
- 若 `--dry-run` 报大模型调用失败，检查 `.env` 中的 `AGENT_API_KEY` 与 `AGENT_BASE_URL` 连通性。

### 2.2 诊断 MCP stdio 通信
在终端中直接运行 MCP 进程入口测试启动：
```bash
node merchants-mcp/src/index.js
```
正常情况下进程会静默等待 stdin 输入 JSON-RPC 报文；若启动即崩溃，检查输出的错误信息。

---

## 3. 处理方案 (SOP)

### 3.1 方案 A：修复飞书 Webhook 密钥与时间戳漂移
飞书签名校验要求请求时间戳与服务端时间误差在 1 小时内：
1. 检查服务器系统时间与时区：
   ```bash
   timedatectl
   ```
   若时间偏差较大，同步 NTP 服务器：`sudo systemctl restart systemd-timesyncd`；
2. 登录飞书群“机器人设置”，重新复制“Webhook 地址”与“签名密钥 (Secret)”，更新至 `.env` 中的 `LARK_WEBHOOK_URL` 与 `LARK_WEBHOOK_SECRET`。

### 3.2 方案 B：MCP 服务超时与 Token 刷新排查
MCP 服务依赖后端 API：
1. 检查 MCP 客户端环境变量是否配置了正确的 `MERCHANTS_API_URL`（如 `http://localhost:8000`）；
2. 检查 `MERCHANTS_API_KEY` 是否与后端 `.env` 的 `API_AUTH_KEY` 一致；
3. 测试 MCP 专用单元测试：
   ```bash
   cd merchants-mcp && npm test && cd ..
   ```
4. 如果因网络延迟导致 AI 客户端超时，可增加超时环境变量：
   ```bash
   export MERCHANTS_API_TIMEOUT_MS=30000
   ```
