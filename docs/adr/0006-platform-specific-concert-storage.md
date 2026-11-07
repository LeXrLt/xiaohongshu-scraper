# ADR 0006：演唱会数据按票务平台分表并通过统一视图读取

- **状态**：Accepted
- **日期**：2026-11-06

## 背景

Ticketmaster SG 与 HKT 香港快達票都提供演唱会数据，但项目/场次模型不同。Ticketmaster 有结构化艺人字段，HKT 以项目、场次和票档为核心，且艺人字段并不稳定。将两者直接写入同一张物理表会迫使来源特有语义变成大量空字段，也可能误合并跨平台的同一场演出。

## 决策

每个平台维护独立的活动表和场次表：

- `ticketmaster_concerts` / `ticketmaster_concert_occurrences`
- `hkticketing_concerts` / `hkticketing_concert_occurrences`

API、MCP 和推送通过 `concerts_unified`、`concert_occurrences_unified` 两个 `UNION ALL` 视图读取。来源适配器分别负责接口解析、状态和写入；公共存储层负责幂等 upsert。

## 影响

- 来源特有字段和状态规则可以独立演进，现有 Ticketmaster 数据无需迁移。
- 统一视图保持跨平台查询和现有接口格式，列表响应增加 `source` 以标识来源。
- HKT 使用 `hkt:<projectToken>` 作为全局活动 ID，避免统一视图中与其他平台发生 ID 碰撞。
- 跨平台演出暂不自动去重；如以后需要建立统一演出实体，再增加明确的关联模型。
