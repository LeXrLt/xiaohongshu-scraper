# 运维手册：数据库维护、迁移与备份回滚 (troubleshooting-database-and-migrations.md)

## 1. 故障现象与特征
- API 接口报错：`503 Service Unavailable: PostgreSQL unavailable`；
- 日志提示字段缺失：`column "xxx" of relation "xhs_notes" does not exist`；
- 执行商户合并 (`dedupe`) 后误合并了同名但不同区域的实体，需要紧急回滚。

---

## 2. 诊断步骤

### 2.1 检查 PostgreSQL 容器与进程连通性
```bash
# 检查 Docker 容器健康状态
docker compose ps postgres

# 执行健康探测指令
docker compose exec postgres pg_isready -U xhs_reader -d xhs_reader
```
如果输出不是 `accepting connections`，检查磁盘剩余空间 `df -h`（PostgreSQL 在磁盘满载时会自动进入只读或宕机保护模式）。

---

## 3. 处理方案 (SOP)

### 3.1 方案 A：补齐最新表结构与幂等迁移
当代码拉取最新版本后，若发现新增了表（如 `push_records`、`ticketmaster_*`、`hkticketing_*`）或补充了列：
```bash
# 触发系统幂等 DDL 执行，安全补充新字段，绝不损坏已有数据
python -c "from database import connect; conn = connect(initialize=True); conn.close(); print('DB Initialized successfully!')"
```

### 3.2 方案 B：商户误合并紧急数据回滚
在执行 `python scripts/merchant.py dedupe --backup ...` 时，系统会强制要求传入一个**不存在的文件路径**作为防灾快照。

若发生误合并：
1. 定位合并执行前生成的快照文件，例如：
   `data/backups/merchant-dedupe-20261013-160401.json`
2. 检查快照中受影响的商户记录及关联笔记；
3. 执行专用回滚命令或按 JSON 原始结构恢复被删除或合并的商户 ID：
   ```bash
   python scripts/merchant.py restore-dedupe --backup data/backups/merchant-dedupe-20261013-160401.json
   ```

### 3.3 方案 C：全库全量备份与灾难恢复
```bash
# 1. 紧急创建物理快照备份
docker compose exec -t postgres pg_dump -U xhs_reader -d xhs_reader -Fc > data/backups/emergency_dump_$(date +%Y%m%d_%H%M%S).dump

# 2. 灾难恢复：从指定 dump 恢复
docker compose exec -T postgres pg_restore -U xhs_reader -d xhs_reader --clean --if-exists < data/backups/emergency_dump_20261106_100000.dump
```
