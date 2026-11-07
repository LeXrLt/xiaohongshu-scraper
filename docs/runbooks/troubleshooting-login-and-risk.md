# 运维手册：登录态失效与风控限制排查 (troubleshooting-login-and-risk.md)

## 1. 故障现象与特征
- 抓取 CLI 或后台日志频繁输出：
  - `HTTP 461 / 30014`（触发滑动验证码或设备环境异常）；
  - `登录态失效`、`请先登录` 或 `Cookie expired`；
  - 搜索结果返回为空，或列表页被重定向至登录引导页。

---

## 2. 诊断步骤

### 2.1 检查本地风控与限流日志
运行诊断脚本查看近期的请求分布与拦截情况：
```bash
python scripts/xhs_risk_status.py --summary
```
重点观察输出中的：
- 失败率是否骤升至 50% 以上；
- 拦截类型是 `captcha`（滑块验证码）还是 `ip_ban`（IP 级封禁）。

### 2.2 检查数据库与本地 Cookie
确认当前配置的 Cookie 是否存在：
```bash
python scripts/query_db.py config
```
检查 `reader_config` 中的 `cookies` 字段是否为空或时间过早。

---

## 3. 处理方案 (SOP)

### 3.1 方案 A：重新扫码登录并更新凭证
在具有图形桌面或能够查看终端二维码的环境下：
```bash
# 执行扫码登录，指定账号别名
python scripts/xhs.py login --prefer qr --name acc1
```
1. 终端或弹出窗口中将展示小红书登录二维码；
2. 打开手机“小红书 App”扫一扫并确认登录；
3. 登录成功后，脚本会自动更新本地凭据（`data/accounts/`）并同步至数据库 `reader_config`。

### 3.2 方案 B：浏览器手动提取 Cookie 并在线填报
若运行在无桌面远程云服务器上：
1. 在本地电脑的 Chrome 打开 `https://www.xiaohongshu.com` 并完成正常登录；
2. 按 `F12` 打开开发者工具，在“网络 (Network)”面板中复制任意请求的完整 `Cookie` 字符串；
3. 访问 Web 阅读器界面 `http://<ip>:3000/settings`；
4. 将 Cookie 粘贴至输入框中并点击“保存配置”。

### 3.3 方案 C：IP 频次限制应对
若遇到 IP 限流（例如频繁请求触发 429）：
1. 检查 `data/config.json`，启用并调低抓取频次：
   ```json
   {
     "daily_hard_cap": 200,
     "ip_daily_cap": 500
   }
   ```
2. 暂停自动化采集任务 2-4 小时，等待平台封禁计数器自动重置；
3. 或在 `.env` 中配置代理出口节点。
