# 架构决策记录 (Architecture Decision Records, ADR)

本项目使用 ADR 记录关键的技术选型、架构权衡与设计决策。每个决策包含：
- **状态 (Status)**：Proposed / Accepted / Superseded / Deprecated
- **背景与痛点 (Context)**：面临的问题与约束条件
- **决策内容 (Decision)**：最终采纳的方案及理由
- **影响与效果 (Consequences)**：方案带来的收益、代价与潜在妥协

---

## ADR 索引列表

| 编号 | 标题 | 决策日期 | 状态 |
| :--- | :--- | :--- | :--- |
| [0001](0001-use-postgresql-as-primary-storage.md) | 使用 PostgreSQL 作为全栈主存储 | 2026-10-13 | Accepted |
| [0002](0002-deterministic-evidence-validation-for-ai-extraction.md) | 大模型商户抽取确定性证据校验机制 | 2026-10-12 | Accepted |
| [0003](0003-token-based-authentication-for-reader-api.md) | 阅读器 API 采用双 Token 换取与验证体系 | 2026-10-13 | Accepted |
| [0004](0004-playwright-isolated-browser-profile.md) | Playwright DOM 抓取采用项目独立 Profile | 2026-10-16 | Accepted |
| [0005](0005-resumable-workflow-and-cursor-idempotency.md) | 细粒度任务游标与全流程幂等断点续跑 | 2026-10-13 | Accepted |
| [0006](0006-platform-specific-concert-storage.md) | 演唱会数据按票务平台分表并通过统一视图读取 | 2026-11-06 | Accepted |
