# WorkBuddy 换号排查取证（只读方法论）

> 本文件由本技能**独立设计**，作为 `diagnose` 子命令的判定依据。
> （说明：早期版本曾表述为"吸收『只读排查方案』的骨架"，经核查该来源已不可考，现明确更正为**本技能自研设计**；如日后补得原始出处，将在此补注。）
> 一切路径以 `<WB>` = `Path.home()/.workbuddy` 表述，**不含任何私有绝对路径**。

## 一、数据分层

| 层 | 位置 | 归属字段 | 是否随换号消失 |
|----|------|---------|---------------|
| 元数据库 | `<WB>/workbuddy.db`（SQLite） | `sessions.user_id` / `automations.owner_user_id` / `automation_delivery_outbox.owner_user_id` | 是（被 user_id 逻辑过滤） |
| 会话索引缓存 | `<WB>/app/sessions.json` | `sessions[].userId` | 是 |
| 会话正文 | `<WB>/projects/<slug>/<conversationId>.jsonl` | 无字段，靠 `sessions.id` 关联 | 否（正文不丢，归属由 DB 决定） |
| 配置命名空间 | `<WB>/storage/user-<uid>[-<后缀>]` | 目录名 | 否（存偏好/专家缓存，与会话归属无关） |
| **账号档案（权威）** | `<WB>/storage/skeleton/account-snapshot.json` → `primary` | 字段 `uid` / `type` / `nickname` / `editionType` | 否（只记录当前主账号，用于辨认账号类型） |
| 侧边栏快照 | `<WB>/<userId>/sidebar-list-snapshot.json` | 目录名即 uid | 否 |
| 身份文件 / 用户级记忆 | `<WB>/SOUL.md`、`IDENTITY.md`、`USER.md`、`MEMORY.md` | 无（**安装级全局文件**） | 否（路径不含 uid，换号不受影响，**无需迁移**） |
| 遗留标记 | `<WB>/.legacy-localstorage-migration.done` | 无 | 否 |
| 安全/插件态 | `<WB>/security/<uid>/`、`<WB>/plugin-account-state/<uid>.json` | 目录/文件名 | 否 |

`<slug>` = 工作区绝对路径转小写、`:\` 替换为 `-`（如 `E:\some\workspace` → `e-some-workspace`）。

## 二、只读打开铁律

```python
sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)   # 必须 mode=ro + uri=True
```

- **必须**用 `mode=ro` 且 `uri=True`，保证连接层拒绝任何写。
- **禁用 `immutable=1`**：它会让 SQLite **忽略 `-wal`**，从而把 WAL 中尚未 checkpoint 的新数据当成"不存在"，导致**误判数据丢失**。本机 `-wal` 可达数 MB，必须让它被读取。
- 只读诊断**不得**执行 `UPDATE` / `DELETE` / `PRAGMA wal_checkpoint`。
- 所有列/目录/文件的存在性一律用 `col_exists()` / `table_exists()` / `os.path.exists()` / `os.path.isdir()` / `os.path.isfile()` 守卫，缺失即优雅降级。

## 三、A/B/C 完整性矩阵

对每个账号，取其 `sessions.id`（小写）与 `<WB>/projects/**/*.jsonl` 文件名（去扩展名、小写）比对：

| 记号 | 定义 | 含义 |
|------|------|------|
| **A_有正文** | `sessions.id` 能在 jsonl 索引中命中 | 元数据与正文齐备 |
| **B_仅元数据** | `sessions.id` 无对应 jsonl | 会话可能为云端/空会话/正文被清理 |
| **C_孤儿正文** | jsonl 存在但库内无 `sessions.id` 对应 | 元数据被删/未迁入，正文残留 |
| **正文命中率** | `A / (A + B)` | 该账号正文完整度 |

`projects` 目录不存在时**优雅跳过**整段。索引只 `os.listdir` 一层（不读文件内容、不递归）。

## 四、口径对平

DB 该账号 `sessions` 行数 vs `<WB>/<uid>/sidebar-list-snapshot.json` 的 `items` 长度：

- 差值 ≠ 0 的常见原因：**快照滞后**（快照按 UI 刷新而非每次写库）、**已软删除**（`deleted_at` 非空仍占行）、**云端行**（`transport=cloud` 不在本地侧边栏）。
- 快照文件不存在时，仅报 DB 行数，不报差值。

## 五、遗留数据源核查

读 `<WB>/.legacy-localstorage-migration.done`（`os.path.isfile` 守卫）：

- 若 JSON 中 `status == "pending"` → 告警「可能存在尚未迁入的更早会话数据」。
- 该标记来自旧版本本地存储迁移，pending 意味着更早会话尚未并入当前库。

## 六、异常分支表

| 现象 | 判定 | 处置 |
|------|------|------|
| `last_activity_at` 全空 | 纯登录未发消息 | 先登录目标账号发一条消息再重跑 |
| 含归属字段的表 > `TABLES_FIELD` | 迁移表未覆盖 | 人工复核是否需纳入迁移 |
| 同一 base id 出现多后缀 storage 目录 | 多登录态的配置目录 | 与会话归属无关，**不影响迁移**（迁移只按 user_id） |
| `projects` 目录不存在 | 无本地正文 | 跳过完整性矩阵，不报错 |
| `transport='cloud'` 行 > 0 | 云端持有 | 本地改归属可能被下次同步覆盖 |
| 快照 items > DB 行数 | 快照超前/DB 已清理 | 以 DB 为准 |
