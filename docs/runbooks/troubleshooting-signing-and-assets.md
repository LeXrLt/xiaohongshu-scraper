# 运维手册：反爬签名与 JS 逆向资源更新 (troubleshooting-signing-and-assets.md)

## 1. 故障现象与特征
- 接口请求返回 `HTTP 403 Forbidden` 或 `HTTP 460`；
- 日志提示：`Sign failed`、`x-s generation error` 或 `Cannot find module 'crypto-js'`；
- 抓取请求全部返回 `Illegal request signature`。

---

## 2. 诊断步骤

### 2.1 验证本地 Node.js 运行环境与依赖
小红书签名计算依赖 `assets/` 目录下的 Node.js 脚本：
```bash
# 1. 检查 Node.js 版本（必须 >= 20.19）
node -v

# 2. 检查 assets 依赖是否完整
cd assets
npm ls
cd ..
```
若提示模块缺失，执行：
```bash
cd assets && npm install && cd ..
```

### 2.2 测试独立签名通信
在 Python 环境中测试独立签名通信：
```bash
python -c "from scripts.xhs_sign import get_signature; print(get_signature('/api/sns/web/v1/feed', {'cursor_score': ''}, 'test_a1'))"
```
若正常打印出 `{'x-s': '...', 'x-t': '...'}` 结构，说明本地 Node 签名执行器正常；若抛出异常，继续执行下述方案。

---

## 3. 处理方案 (SOP)

### 3.1 方案 A：拉取最新小红书加密 JS 脚本
小红书官方会不定期升级前端 `xray` 混淆加密算法。执行内置更新指令：
```bash
python scripts/xhs.py update-js
```
该命令会自动从线上探测最新的加密打包文件并写回 `assets/xhs_xray.js` 与 `assets/xhs_a1.js`。

### 3.2 方案 B：对齐浏览器内核与 TLS 指纹
如果平台加强了 TLS/JA3 指纹检测：
1. 检查 `updater/` 目录下的版本定义；
2. 运行指纹检查工具：
   ```bash
   python -m updater.cffi_probe
   ```
3. 确保 Playwright 使用的 Chromium 为最新支持版本：
   ```bash
   playwright install chromium --force
   ```
