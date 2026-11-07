# 0004. Playwright DOM 抓取采用项目独立 Profile

- **日期**：2026-10-16
- **状态**：Accepted

---

## 背景与痛点 (Context)

在小红书关键词搜索与部分反爬严重的页面（如包含复杂滑块或动态混淆）中，纯 HTTP API 抓取容易触发风控，需要结合 Playwright 进行 DOM 渲染采集。
在先前的版本实现中：
1. 爬虫尝试接管或连接开发机本地 Chrome 运行中的 User Data Profile。
2. 导致开发者日常使用的浏览器进程被意外抢占、甚至因 Playwright 退出被强制 kill。
3. 在无桌面环境的 Linux 服务器或 Docker 容器内运行时，由于找不到桌面环境的用户 Profile 导致报错退出。

---

## 决策内容 (Decision)

1. **默认采用项目沙箱级独立 Profile**：
   - Playwright 运行时默认在项目本地目录 `data/pw_profile_<hash>/` 建立独立的 Chromium 用户数据上下文。
   - 完全与宿主机的个人 Chrome / Chromium 进程隔离。
2. **保留显式接管开关**：
   - 仅当明确设置环境变量 `XHS_DOM_BROWSER_MODE=real_profile` 时，才允许尝试复用本地浏览器 Profile，供本地特殊调试使用。
3. **环境指纹模拟**：
   - 对独立的 Chromium Profile 预置防无头探测（stealth）脚本，抹除 `navigator.webdriver` 标记，并对齐主版本号。

---

## 影响与后果 (Consequences)

### 正面收益
- 无论本地运行还是 CI/Docker 自动化部署，均能零冲突、稳定启动。
- 绝不干扰开发者日常浏览器的会话、Cookie 与多标签页。

### 妥协与代价
- 独立的 Profile 初次访问时为空白状态，若需要已登录态，需引导用户在独立上下文中执行一次扫码登录或将 Cookies 写入配置。
