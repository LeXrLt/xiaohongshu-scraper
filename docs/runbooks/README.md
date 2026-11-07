# 运维与故障手册目录 (Runbooks)

本目录提供系统日常运维、突发线上故障应对、性能排查及紧急灾备恢复的标准化操作程序 (Standard Operating Procedures, SOP)。

---

## 1. 故障分级与应急响应流程

| 故障级别 | 影响范围 | 响应时效 | 典型故障场景 |
| :--- | :--- | :--- | :--- |
| **P1 (致命)** | 全站不可用、数据无法入库、API 持续返回 500/503 | 15 分钟内 | PostgreSQL 崩溃、全局 API 鉴权失效、签名被风控彻底拦截 |
| **P2 (严重)** | 部分核心链路中断，如单账号被封、爬虫任务挂死、MCP 无法查询 | 1 小时内 | 登录态过期、大模型 API 欠费超时、Ticketmaster/HKT 浏览器验证阻断 |
| **P3 (一般)** | 辅助功能降级，如飞书简报未发送、部分多模态图片识别失败 | 4 小时内 | Webhook 密钥错误、单个媒体下载失败、样式异常 |

```mermaid
flowchart TD
    A[监控告警 / 用户上报] --> B{排查问题类型}
    B -->|账号封控 / 滑块验证| C["查看: troubleshooting-login-and-risk.md"]
    B -->|接口 403 / 签名异常| D["查看: troubleshooting-signing-and-assets.md"]
    B -->|数据库连接断开 / 慢查| E["查看: troubleshooting-database-and-migrations.md"]
    B -->|API 宕机 / 容器崩溃| F["查看: troubleshooting-service-recovery.md"]
    B -->|推送失败 / MCP 超时| G["查看: troubleshooting-push-and-mcp.md"]
    C & D & E & F & G --> H[执行对应 SOP 并验证恢复]
    H --> I[记录事后复盘报告 (Post-Mortem)]
```

---

## 2. 运维手册检索索引

- [登录态失效与风控限制排查手册](troubleshooting-login-and-risk.md) (`troubleshooting-login-and-risk.md`)
- [反爬签名与 JS 逆向资源更新手册](troubleshooting-signing-and-assets.md) (`troubleshooting-signing-and-assets.md`)
- [数据库维护、迁移与备份回滚手册](troubleshooting-database-and-migrations.md) (`troubleshooting-database-and-migrations.md`)
- [服务崩溃与容器恢复排查手册](troubleshooting-service-recovery.md) (`troubleshooting-service-recovery.md`)
- [飞书推送与 MCP 协议异常排查手册](troubleshooting-push-and-mcp.md) (`troubleshooting-push-and-mcp.md`)
