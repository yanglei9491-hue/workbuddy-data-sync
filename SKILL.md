---
name: workbuddy-data-sync
description: WorkBuddy 数据同步（workbuddy-data-sync）——同一台电脑切换账号后，把账号A的历史会话和自动化任务归属迁移到账号B，让切换后新的登录账号重新看到旧账号的任务/空间。三段式——先 diagnose 只读深度诊断，用户确认后再 migrate 写入迁移。聊天内触发一次完成：AI 自动检测所有账号→智能推荐旧/新账号→确认后自动杀WorkBuddy进程→改库→重启，无需手动退出/双击脚本。用户在提到"workbuddy切换账号"、"workbuddy数据同步"、"workbuddy账号同步"、"workbuddy个人转企业号"、"workbuddy切换账号看不到历史会话"、"workbuddy账号数据转移"、"workbuddy账号恢复"、"workbuddy个人账号转企业账号后数据没了"、"sessions user_id 迁移"、"workbuddy账号数据迁移"、"workbuddy恢复历史任务"、"workbuddy换号后左侧任务不显示"、"workbuddy同机换号数据恢复"、"workbuddy工作历史丢失"、"workbuddy诊断"等场景时触发。核心操作是 UPDATE workbuddy.db 的 sessions.user_id、automations.owner_user_id 和 automation_delivery_outbox.owner_user_id 三张表字段。只处理同机换号，不处理技能库、连接器凭证和跨机路径差异；用户级记忆与人设文件为安装级全局，换号不受影响、无需迁移。
version: 3.1.1
license: MIT
compatibility: "WorkBuddy >= 1.0"
allowed-tools: [Read, Write, Bash, PowerShell, AskUserQuestion]
metadata:
  category: 08-business
---

# WorkBuddy 数据同步（聊天内触发 + 自动迁移）

**一句话**：在 WorkBuddy 聊天里发一句"workbuddy 数据同步"，AI 先**确认你登录的是新账号**、再跑**只读诊断**，确认后**自动迁移**，结束 WorkBuddy 会自动关闭并重启，你只需扫码登录新账号。

> 💡 **零风险选项**：如果只是"换错号看不到历史"，**先尝试重新登录旧账号**——多半数据压根没动，登回去即可，不必迁移。

> ⚠️ **高危提示（务必先读）**：迁移/回滚会执行 `taskkill /f /im WorkBuddy.exe` 强制关闭并重启 WorkBuddy。**当前聊天窗口会断开**，属预期行为，不是崩溃。执行前请先保存其他未完成的工作。

> 与本技能配套的三件事：① `diagnose`/`detect` **只读**可随时跑；② 迁移**自动备份可回滚**；③ 迁移时会**强制关闭并重启 WorkBuddy**（你当前看到的聊天窗口会断，属预期行为）。

## 定位
同一台电脑上切换账号后，把旧账号的历史数据（会话、自动化任务）归属迁移到新账号，让新账号登录后能看到旧账号的任务/空间。

**不处理**：
- 技能库 `~/.workbuddy/skills/`（全局共享，换号不丢，不碰）
- 连接器/凭证（本机已有，不碰）
- 项目级记忆 `项目/.workbuddy/memory/`（跟项目目录走，换号不丢）
- 用户级长期记忆 `~/.workbuddy/MEMORY.md` 与人格/身份文件（`SOUL.md` / `IDENTITY.md` / `USER.md`）—— **安装级全局文件，换号不受影响，无需迁移**。代码级实证：这些文件按 `~/.workbuddy/` 根目录定位，路径不含 userId；仅 `WORKBUDDY_CONFIG_DIR` 环境变量可改，那是专享版/私有化部署用途，与账号切换无关
- 跨机路径差异（本次定位同机，不做路径映射）

## 三段式工作流

```
diagnose（只读深度诊断） → 用户确认 → migrate（写入迁移）
```

| 阶段 | 命令 | 是否改数据 |
|------|------|-----------|
| ① 诊断 | `diagnose --cwd "<当前工作区路径>"` | **否**，全程 `mode=ro` |
| ② 确认 | 向用户呈现账号/风险，等其拍板 | 否 |
| ③ 迁移 | `migrate --from <旧uid> --to <新uid>`（可先 `--dry-run` 预演） | 是，含备份+原子事务 |

> 判据顺序（可靠性降序）：**本次会话 cwd 自证** → `last_activity_at` 最大 → `created_at` 最大 → `settings.json` 的 `legacyOwnerUid`。

## 文件结构

| 文件 | 用途 |
|------|------|
| `SKILL.md` | 技能入口：工作流、命令、约束、范围说明 |
| `README.md` | 面向人的使用说明（原理/快速开始/CLI/备份回滚/FAQ/许可证） |
| `LICENSE` | MIT 许可证文本 |
| `CHANGELOG.md` | 版本变更记录（含 3.x 各版本功能与不兼容变更说明） |
| `scripts/wb_account_restore.py` | 核心脚本（detect / diagnose / migrate / restore / rollback / rollback_auto / guide） |
| `scripts/run_restore.py` | macOS / Linux 启动器（透传参数，无参数时进 `guide`） |
| `scripts/账号恢复一键.bat` | Windows 手动备用入口（AI 不可用时双击） |
| `references/data_isolation_map.md` | 数据隔离全景表（user_id 隔离字段、账号类型来源、transport 语义、storage 命名空间、jsonl 归属原理、范围说明） |
| `references/forensics.md` | 只读取证方法（只读打开铁律、A/B/C 完整性矩阵、口径对平、遗留数据源核查、异常分支表） |

> 需了解"哪些数据会被隔离 / 为什么换号后数据没丢" → 读 `references/data_isolation_map.md`；需了解只读排查方法论 → 读 `references/forensics.md`；均不进入主流程。

## 前置条件

| 项 | 要求 |
|----|------|
| 平台 | **仅 Windows 支持自动迁移**（依赖 `taskkill`）。macOS / Linux 可跑 `detect` / `diagnose` 与手动 `restore` / `rollback`（需 WB 已退出），但**无自动杀进程与重启**。 |
| Python | 任意 Python 3.8+（脚本仅用标准库 `sqlite3` / `json` / `shutil` / `subprocess` / `pathlib` / `hashlib` / `csv`），**无需 pip 安装任何包**。 |
| 可选依赖 | `python-docx` —— 仅 `guide` 路径用它生成桌面操作手册；未安装则静默跳过，不影响迁移与回滚。 |
| 数据库 | 需已安装并至少启动过 WorkBuddy，存在 `~/.workbuddy/workbuddy.db`。 |
| 登录状态 | ⚠️ **必须在【新账号】（想看到历史的那个）下触发本技能**。自动推荐依赖「本次会话 cwd 自证」判定迁入目标——若你在旧账号下触发，工具会因「当前就是数据最多的主号」而不再自动判定目标。此时请切到新账号 → 任意项目发一条消息 → 再触发；或手动指定 `--to <完整uid>`。 |
| 网络 | 无需联网。 |
| 权限 | 需有写 `~/.workbuddy/` 的权限（默认满足）。 |

> **路径占位说明**：下文命令中的 `<技能目录>` 指**本技能的实际根目录**（本轮系统注入的 `Base directory for this skill:` 路径）。**不要写死** Python 安装路径或 `08-business` 分类层——市场安装后技能通常直接落在 `~/.workbuddy/skills/<技能名>/`，没有分类目录。

## 前提（重要）
- WorkBuddy 用 `~/.workbuddy/workbuddy.db`（SQLite）做账号逻辑隔离，靠 `sessions.user_id` / `automations.owner_user_id` / `automation_delivery_outbox.owner_user_id` **共 3 张表字段**区分（与脚本 `TABLES_FIELD` 一致）。登录新账号=user_id 变→旧数据被过滤看不见。**数据没丢，只是被 user_id 隔离。**
- ⚠️ **`last_activity_at` 只在有会话活动（发消息/开新会话）时更新**。纯登录、不发消息 → 该账号显示"无活动记录"，detect 无法识别它。**要让目标账号被识别，先登录它发一条消息**。

---

## 启动即呈现（AI 必须执行，勿只看文档）

用户触发本技能 → **第一条回复的第一屏**就完成以下动作：

### Phase 0：前置确认（先问，再诊断）
**第一屏先确认登录状态，不要直接跑 diagnose。**
- 问用户：「你现在登录的是**想把历史迁过去的那个账号（新账号）**吗？」
- **是** → 进入 Phase 1。
- **否**（当前是旧账号/主号）→ **不要继续**，引导：先扫码登录新账号 → 在任意项目新建会话发一条消息（刷新 `last_activity_at`）→ 再回来触发；或直接给出 `--to <完整uid>` 跳过自动判定。
- **不确定** → 跑只读 `detect`（`python "<技能目录>/scripts/wb_account_restore.py" detect --cwd "<当前工作区路径>"`，不改数据）列出全部账号，让用户从列表里指认哪个是新账号。

### Phase 1：只读深度诊断 + 呈现账号列表
执行（只读，WorkBuddy 仍运行，安全）：
```bash
python "<技能目录>/scripts/wb_account_restore.py" diagnose --cwd "<当前工作区路径>"
```
把输出整理成易读列表贴进聊天（每账号：**账号类型** / 条数 / transport 分布 / 最后活动 / 创建 / 线索 / 昵称），并转述 8 段诊断的核心结论（归属完整性、账号类型识别、正文命中率、口径差值、遗留数据源、云端风险）。

### Phase 2：智能推荐 + 用户确认
- 旧账号(from/迁出) = **数据最多**的那个（主号）
- 新账号(to/迁入) = **本次会话 cwd 自证 / 最近活动**的那个（★）
- 用 AskUserQuestion 确认：「把旧账号的若干条历史迁移给新账号？」或让用户输入序号指定（如「1→3」）。条数用诊断输出代入。
- ⚠️ 若**当前登录账号就是数据最多的主号**，工具**不再猜目标**：需用户登录新账号发一条消息后重跑，或手动 `--to <完整uid>`。

### Phase 3：先预演，再确认后自动迁移
先给用户看**迁移计划（dry-run，不改库）**——在**前台同步执行**，输出直接可见：
```bash
python "<技能目录>/scripts/wb_account_restore.py" migrate --from <完整旧uid> --to <完整新uid> --dry-run
```
> dry-run 不杀进程、不改库，可在前台直接跑以查看计划输出；**真实迁移**才需要 `Start-Process` 起独立进程（因为它会杀掉 WorkBuddy）。

用户确认后，用 **PowerShell 启动独立进程**（脱离 WorkBuddy 进程树，避免被自己杀掉）：
```powershell
Start-Process -WindowStyle Hidden -FilePath 'python' -ArgumentList '"<技能目录>\scripts\wb_account_restore.py"','migrate','--from','<完整旧uid>','--to','<完整新uid>'
```
> 若 `python` 不在 PATH，把 `-FilePath 'python'` 换成实际的 python.exe 全路径。`migrate` 本身非交互，**不支持也不需要** `--yes`。

脚本独立完成：`taskkill WorkBuddy` → **轮询确认进程完全退出（每秒测1次，最多15秒）** → 等3秒WAL落盘 → 备份 → 原子事务改归属 → 源归零验证 → 重启 WorkBuddy → 写日志。

> ⏱️ **超时行为**：轮询等待最多 15 秒；若超时未确认退出（含进程检测失败、状态未知），则**中止迁移（不执行任何 UPDATE，零数据改动）**，但**仍重新启动 WorkBuddy 并告警**——因为进程可能已被关闭，不能把 WorkBuddy 留在关闭状态。遇到此情况请完全退出 WorkBuddy 后重试。

**AI 立即回复用户**：
> 迁移已启动为独立进程。WorkBuddy 将在几秒后自动关闭并重启。重启后请扫码登录新账号，左侧即显示全部历史。迁移结果已写入日志，重启后可核对。如需回滚：重新打开聊天，说「回滚数据同步」。

### Phase 4：重启后核对（下次对话）
WorkBuddy 重启后，用户再次触发时，AI 读取迁移日志确认结果：
```bash
# Windows
type "%USERPROFILE%\.workbuddy\backup\account-restore\latest-migrate.log"
# macOS / Linux
cat ~/.workbuddy/backup/account-restore/latest-migrate.log
```

---

## ⚠️ 无活动记录账号（关键，易踩坑）
某个账号标记「无活动记录」= 它登录后**没发过消息**，`last_activity_at` 未更新，detect 识别不到它。
- **若它就是要迁移的目标账号**：先登录该账号 → 在任意项目新建会话发一条消息 → 再次触发本技能 → detect 就能认出它。
- 原因：`last_activity_at` 只在有会话活动时刷新。

## 回滚（迁移错了用）
用户说「回滚数据同步」→ AI 按两步走：

**① 先列可用备份**（只读，WB 运行中也能跑）：
```bash
python "<技能目录>/scripts/wb_account_restore.py" rollback
```

**② 取输出里的备份目录名，聊天内自动回滚**（杀WB→等退出→回滚→重启，独立进程）：
```powershell
Start-Process -WindowStyle Hidden -FilePath 'python' -ArgumentList '"<技能目录>\scripts\wb_account_restore.py"','rollback_auto','--tag','<备份目录名>'
```

> ⚠️ 裸 `rollback --tag <备份>` **要求 WorkBuddy 已完全退出**，WB 运行中执行会被直接中止（脚本内有进程检查）。**聊天内一律走 `rollback_auto`**。
> 每次迁移自动备份到 `~/.workbuddy/backup/account-restore/<时间戳>/`，目录内附 `恢复说明.txt`。

## 脚本位置与命令
```
<技能目录>/scripts/
├── wb_account_restore.py   核心脚本（detect/diagnose/migrate/restore/rollback/rollback_auto/guide）
├── run_restore.py          Mac/Linux 启动器
└── 账号恢复一键.bat         Windows 手动备用入口（AI 不可用时用）
```
子命令：
- `diagnose [--cwd <路径>] [--export] [--out-dir <目录>]` — **只读**深度诊断（8 段）；`--export` 导出 CSV/指纹 JSON/Markdown 三件交付物（默认写桌面，opt-in）
- `detect [--cwd <路径>]` — 只读列出全部账号+最近活动/创建时间+标签+智能推荐
- `migrate --from <旧uid> --to <新uid> [--skip-cloud] [--dry-run]` — 聊天内迁移（杀WB→等退出→改库→重启，独立进程）；`--no-restart` 仅测试用
- `restore --from <旧uid> --to <新uid> --yes [--skip-cloud] [--dry-run]` — 手动改库（要求 WB 已退出）
- `rollback` / `rollback --tag <备份>` — 列出备份 / 手动回滚（要求 WB 已退出）
- `rollback_auto --tag <备份>` — 聊天内自动回滚（杀WB→等退出→回滚→重启，独立进程）
- `guide` — 交互式引导菜单（含桌面手册生成）

参数：
- `--cwd <路径>` — 本次会话工作区绝对路径，作为**确定性判定源**（最高优先级）；不传则按 `last_activity_at` → `created_at` → `legacyOwnerUid` 顺序推断
- `--skip-cloud` — 跳过 `transport=cloud` 的行（默认关闭＝不跳过）
- `--dry-run` — 只预演迁移计划，**不备份、不改库、不重启、不杀进程**；仅在 `~/.workbuddy/backup/account-restore/latest-migrate.log` 追加一行 dry-run 记录
- `--export` / `--out-dir <目录>` — `diagnose` 导出开关与输出目录

## 约束
- 迁移必须通过 `migrate`（会强制关闭并重启 WB）；`restore` 手动路径要求 WB 已退出。
- 迁移是单事务原子提交，失败自动回滚。
- 每次迁移自动备份，可随时回滚。
- ⚠️ 迁移/回滚会强制关闭当前 WorkBuddy（含当前聊天窗口），属预期行为，重启后数据已迁移/回滚。
- ⚠️ **`diagnose` 全程只读**：`mode=ro`，禁止任何 `UPDATE`/`DELETE`/`wal_checkpoint`。
- ⚠️ **写盘口径（统一）**：`diagnose`/`detect` **全程只读、不写任何目录**；`--dry-run` **仅**向 `~/.workbuddy/backup/account-restore/latest-migrate.log` 追加一行；只有 `migrate`/`rollback_auto` 才写备份目录与日志。导出类副作用必须显式加 `--export`（默认不写桌面）。
- ♻️ **幂等/重复执行**：重复对同一账号执行 `migrate` 是安全的——若源账号已无可迁移数据，会在**杀进程前**直接拒绝并提示（`WorkBuddy 未被关闭`），**零数据改动**。
- `migrate` 内部已把 `restore(skip_process_check=True, skip_cloud=...)` 透传跳过二次进程检测（migrate 自己负责杀进程+等退出）。

## 范围说明
- ✅ **个人版 ↔ 企业版之间的迁移与「个人转个人」同一性质，走同一流程。** 迁移只按 `workbuddy.db` 的 `user_id` 归属进行，账号是个人版、企业版还是团队版**不影响迁移逻辑**。
- 🔍 **账号类型识别**（帮你认出「哪个 uuid 是哪个账号」）：`diagnose` 的 [3] 段从 `~/.workbuddy/storage/skeleton/account-snapshot.json` 的 `primary` 读取**权威**账号档案（`uid` / `type` / `nickname` / `editionType`），但该文件**只记录当前主账号**；历史账号只能依据 `storage/` 命名空间后缀**间接推断**（`-personal` → 个人版；`-<slug>` → 企业版/团队版）。识别类型**只为辨认身份**，不改变迁移流程。
- `storage/` 下的 `user-<uid>[-<后缀>]` 是**配置命名空间**（偏好/专家/技能缓存），与会话归属无关，本工具**不处理**它，也不影响迁移结果。
- 只处理同机换号；跨电脑/新环境迁移需先复制整个 `~/.workbuddy` 再处理路径映射，本次不支持。
- ⚠️ `transport='cloud'` 的行**归云端持有**，本地改归属可能被下次同步覆盖（可用 `--skip-cloud` 跳过）。
- `last_activity_at` 仅记录"会话活动"，纯登录不发消息的目标账号需先发消息才能被 detect 识别。
- ⚠️ **UID 必须传完整**（如 `a1b2c3d4-0000-1111-2222-333344445555`），截断的前缀匹配不到任何记录。detect 输出展示的是缩写，完整 UID 用下面这条**零依赖只读命令**列出（README「查完整 UID」节同）：
  ```bash
  python -c "import sqlite3,os;print(*{r[0] for r in sqlite3.connect('file:'+os.path.join(os.path.expanduser('~'),'.workbuddy','workbuddy.db').replace(chr(92),'/')+'?mode=ro',uri=True).execute('SELECT DISTINCT user_id FROM sessions')},sep=chr(10))"
  ```
- ⚠️ **`rollback` 子命令没有 `--yes` 参数**（只有 `--tag`），且要求 WB 已退出。WB 运行中要自动回滚，须用 `rollback_auto`。
- **判断迁移是否成功**：日志出现「进程已完全退出（等待 Xs）」且「restore 完成」即成功；成功后源账号应从列表消失、目标账号条数 = 原主号数 + 原目标数。仅出现「migrate from=...」而无「restore 完成」= 失败（多为进程未退净被 bail）。
