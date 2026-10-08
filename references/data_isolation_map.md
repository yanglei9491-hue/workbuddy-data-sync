# WorkBuddy 数据隔离全景图

> 一切路径以 `~/.workbuddy`（即 `Path.home()/.workbuddy`）表述。

## 哪些数据按 user_id 隔离（切号后消失，需迁移）

| 数据 | 存储位置 | 隔离字段 | 迁移方式 |
|------|---------|---------|---------|
| 会话/任务记录 | `~/.workbuddy/workbuddy.db` → `sessions` 表 | `user_id` | UPDATE |
| 自动化任务 | `workbuddy.db` → `automations` 表 | `owner_user_id` | UPDATE |
| 自动化投递记录 | `workbuddy.db` → `automation_delivery_outbox` | `owner_user_id` | UPDATE |
| 会话缓存 | `~/.workbuddy/app/sessions.json` | `userId` | 原子写回 |

> 实测：全库 10 张表，含归属字段的**只有上述 3 张**，`TABLES_FIELD` 已全覆盖。

## 账号类型从哪看（不参与迁移，仅用于辨认身份）

| 来源 | 位置 | 内容 | 覆盖范围 |
|------|------|------|---------|
| **权威** | `~/.workbuddy/storage/skeleton/account-snapshot.json` → `primary` | `uid` / `nickname` / `type`（`personal` 等）/ `editionType`（`free` 等）/ `isPro` / `isAdmin` | **仅当前主账号** |
| 间接线索 | `~/.workbuddy/storage/user-<uid>[-<后缀>]` 目录名 | `-personal` → 个人版；`-<slug>` → 企业版/团队版（slug 为租户标识） | 有该命名空间的账号 |

> ⚠️ **账号类型不影响迁移**：迁移只按 `workbuddy.db` 的 `user_id` 归属进行。
> 个人转企业号与个人转个人是**同一套流程**（`--from <旧uid> --to <新uid>`）。
> 上方两处来源只用于回答「这个 uuid 是哪个账号」，**不是**迁移的门槛或障碍。

## 身份文件与用户级记忆：安装级全局，与账号无关

**结论**：`SOUL.md` / `IDENTITY.md` / `USER.md` / `MEMORY.md` 换号**不受影响、不需要迁移**。

### 代码级证据

> **引用说明**：本段（含 `readLocalMemoryFiles` / `resolveConfigDir` 代码片段）摘自本机 WorkBuddy 客户端资源文件 `app.asar` 内的实现，仅用于对数据隔离机制做**本地事实核查**（合理引用 / 事实引用）。
> **用途**：佐证「身份文件 / 用户级记忆为安装级全局、与账号无关」这一判断。
> **权利归属**：相关代码版权归**腾讯 WorkBuddy** 所有；**本技能不对其主张任何权利**，该片段亦不随本技能对外分发。

WorkBuddy 身份文件读取逻辑（`app.asar` 内 `readLocalMemoryFiles`）：

```js
const dir = resolveConfigDir(configDir);
const [identity, user, soul, memory] = await Promise.all([
  readFileSafe(path.join(dir, "IDENTITY.md")),
  readFileSafe(path.join(dir, "USER.md")),
  readFileSafe(path.join(dir, "SOUL.md")),
  readFileSafe(path.join(dir, MEMORY_FILE_NAME)),   // MEMORY.md
]);
```

根目录由 `resolveConfigDir()` 决定：

```js
function resolveConfigDir() {
  const fromEnv = process.env.WORKBUDDY_CONFIG_DIR?.trim();
  if (fromEnv) return fromEnv;
  const folder = process.env.WORKBUDDY_DATA_FOLDER_NAME?.trim() || ".workbuddy";
  return path.join(os.homedir(), folder);
}
```

→ **只读 `WORKBUDDY_CONFIG_DIR` / `WORKBUDDY_DATA_FOLDER_NAME` 环境变量，默认 `~/.workbuddy`，路径不含 userId。**
（源码注释说明该环境变量是给**专享版 / 私有化部署**用的，如指向 `~/.aimea`，**不是给账号切换用的**。）

### 实测

- 全盘身份/记忆文件 **22 个，含 uid 的 0 个**；
- 4 个账号目录下均**无** `SOUL.md` / `MEMORY.md` 副本；
- 个人版与企业版**共用同一个** `~/.workbuddy` → 读同一份身份文件。

### 记忆分层归属

| 层 | 位置 | 粒度 | 换号影响 |
|----|------|------|---------|
| 云端记忆（自动注入 `<memory>`） | 服务端 | 按账号 | 云端自带，本地无需迁 |
| **用户级本地记忆** | `~/.workbuddy/MEMORY.md` | **安装级全局** | **不受影响** |
| 项目级记忆 | `<工作区>/.workbuddy/memory/` | 跟目录 | 不受影响 |
| 云端记忆本地缓存 | `~/.workbuddy/memory/<uid>_memory.md` | 按 uid | 由云端管理，**无需迁** |

### ⚠️ 易误判点

- `.fallback.bak` 后缀**与换号无关**。其生产者为 `win32-fallback-data-migration`（源码注释："fallback period and the **post-rollback** period"），配合 `.legacy-localstorage-migration.done`（`sourceSessionPath: %APPDATA%\WorkBuddy`）—— 那是**旧版目录迁移**的一次性产物，**不是**账号隔离证据。
- `<uid>_memory.md` 是**云端记忆的本地缓存**，不是本地长期记忆，无需人工迁移。

### 什么情况才需要搬这些文件

仅 **换电脑 / 换 Windows 用户 / 切到不同的 `WORKBUDDY_CONFIG_DIR`** —— 那属于**文件复制**（用备份拷过去即可），不是「改归属」，**不在本技能范围**。

## `transport` 列语义

`sessions.transport` 取值：

| 值 | 含义 | 迁移注意 |
|----|------|---------|
| `local` | 本地持有 | 直接改归属即可 |
| `cloud` | **归云端持有** | 本地改归属可能被**下次同步覆盖**；可用 `--skip-cloud` 跳过 |

- 默认（不加 `--skip-cloud`）**不跳过** cloud 行，保持历史行为。
- 无论开关如何，迁移计划都会打印 `其中 transport=cloud 的 N 行归云端持有…`。

## storage 配置命名空间

`~/.workbuddy/storage/` 下的目录按**登录态**划分，存偏好与专家缓存：

| 目录形态 | 命名空间 |
|---------|---------|
| `user-<uid>` | 默认命名空间 |
| `user-<uid>-personal` | 个人版命名空间 |
| `user-<uid>-<slug>` | 团队命名空间(slug) |

- `~/.workbuddy/<userId>/sidebar-list-snapshot.json` 为侧边栏快照（目录名即 uid），其 `items` 长度可用于与 DB 行数做**口径对平**。

## 范围说明：账号类型不影响迁移

> ✅ **个人版 ↔ 企业版之间的迁移与「个人转个人」同一性质，走同一流程**（`--from <旧uid> --to <新uid>`）。
> 迁移只按 `workbuddy.db` 的 `user_id` 归属进行，**与账号是个人版/企业版/团队版无关**。
>
> `storage/` 下那些命名空间目录存的是**偏好与专家缓存**（`preferences.json` / `expert.json` / `conversations.json` 等），
> **不参与会话归属**，本工具不处理它们，也不影响迁移结果。

## 哪些数据不隔离（切号不丢，不用动）

| 数据 | 存储位置 |
|------|---------|
| 技能库 | `~/.workbuddy/skills/`（全局共享） |
| 连接器配置/凭证 | `~/.workbuddy/mcp.json` 等 |
| 用户级长期记忆 | `~/.workbuddy/MEMORY.md`（**安装级全局**，换号不丢） |
| 云端记忆本地缓存 | `~/.workbuddy/memory/<uid>_memory.md`（按 uid 隔离，**由云端管理，无需人工迁移**） |
| 项目级记忆 | `项目目录/.workbuddy/memory/`（跟项目走） |
| 人格/身份文件 | `SOUL.md` / `IDENTITY.md` / `USER.md` |
| 会话正文 jsonl | `~/.workbuddy/projects/<slug>/<id>.jsonl`（正文不丢） |

## 原理

WorkBuddy 多账号共用同一个 `workbuddy.db`，账号间靠字段逻辑隔离（**非物理分目录**）。
登录新账号 = 拿到新 `user_id` → 前端按新 `user_id` 过滤查询 → 旧 `user_id` 的数据"看不见"。
数据仍在磁盘上，只是被过滤。恢复 = 把旧 user_id 的归属字段 UPDATE 成新 user_id。

## 会话内容文件与 user_id 的关系

- 会话正文在 `~/.workbuddy/projects/{slug}/{conversationId}.jsonl`
- 文件名 = `sessions.id`，归属由 `sessions.user_id` 决定（与文件系统无关）
- `{slug}` = 工作空间路径转小写、`:\` 替换成 `-`（如 `E:\some\workspace` → `e-some-workspace`）
- **所有账号的 jsonl 混在同一目录，归属靠 DB 决定**

## 只读取证

深度排查方法（只读打开铁律、A/B/C 完整性矩阵、口径对平、遗留数据源核查）见 `references/forensics.md`。
