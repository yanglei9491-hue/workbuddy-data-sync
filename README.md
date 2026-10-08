# WorkBuddy 数据同步

**v3.1.1**

> 同一台电脑上切换账号后，把旧账号的历史会话 / 自动化任务**归属**迁移到新账号，让新账号登录后重新看到旧账号的任务与空间。

---

## ⚠️ 先读这一段

**本技能会强制关闭并重启 WorkBuddy。**

迁移与回滚过程中会执行 `taskkill /f /im WorkBuddy.exe`，然后自动重新拉起 WorkBuddy。
**你当前的聊天窗口会断掉，这是预期行为，不是崩溃。** 执行前请先保存其他未完成的工作。

---

## 为什么换号后历史会"消失"

WorkBuddy 多账号**共用同一个数据库** `~/.workbuddy/workbuddy.db`，账号之间靠字段做**逻辑隔离**：

| 表 | 隔离字段 |
|----|---------|
| `sessions` | `user_id` |
| `automations` | `owner_user_id` |
| `automation_delivery_outbox` | `owner_user_id` |
| `~/.workbuddy/app/sessions.json` | `userId` |

登录新账号 = 拿到新 `user_id` → 前端按新 `user_id` 过滤 → 旧数据"看不见"。

**数据一直在磁盘上，只是被过滤掉了。** 本技能做的事就是把旧 `user_id` 的归属字段 `UPDATE` 成新 `user_id`。

⚠️ 注意：迁移的是 `user_id` 归属，**不做数据去重和内容过滤**。迁移后两个账号的历史会合并在同一个账号下。
⚠️ 技能库、连接器凭证、项目级记忆 **不受 user_id 隔离，换号不会丢**，本技能不碰。
⚠️ **用户级长期记忆 `~/.workbuddy/MEMORY.md` 与人格/身份文件（`SOUL.md` / `IDENTITY.md` / `USER.md`）是安装级全局文件**，换号同样不受影响、**无需迁移**（详见 `references/data_isolation_map.md`）。

---

## 安装到 WorkBuddy

把本技能放进 WorkBuddy 的技能目录即可：

```bash
git clone https://github.com/yanglei9491-hue/workbuddy-data-sync.git \
  ~/.workbuddy/skills/08-business/workbuddy-data-sync
```

Windows 下即 `C:\Users\<你的用户名>\.workbuddy\skills\08-business\workbuddy-data-sync`。

放好后重启 WorkBuddy，在聊天里触发即可（触发词见下）。

---

## 快速开始（推荐：聊天内触发）

在 WorkBuddy 聊天窗口直接说：

```
workbuddy数据同步
```

AI 会走**三段式**（诊断 → 确认 → 迁移），具体步骤：

0. **先确认你在【新账号】下操作**
   请务必登录**新账号**（想看到历史的那个）再触发本技能。自动推荐靠「本次会话 cwd 自证」判定迁入目标；若你在旧账号下触发，工具会因「当前就是数据最多的主号」而**不再自动判定目标**，需要你手动指定 `--to <完整uid>`，或切到新账号发一条消息后重新触发。
1. **只读深度诊断**（`diagnose`）：列出所有账号 + transport 分布 / 正文命中率 / 口径差值 / 账号类型识别（这一步安全，WorkBuddy 不用退出）
2. **智能推荐并等你确认**：迁出账号 = 数据最多的那个；迁入账号 = 本次会话 cwd 自证 / 最近活动的那个（可改序号，如「1→3」）
3. **先看迁移计划**（`--dry-run`，不改库）
4. **自动迁移**：杀 WorkBuddy 进程 → 等它完全退出 → 备份 → 原子事务改归属 → 重启

重启后扫码登录目标账号即可看到全部历史。

> 💡 **零风险选项**：如果只是换错号看不到历史，**先尝试重新登录旧账号**——多半数据压根没动，登回去即可。

### 常见触发词

`workbuddy切换账号` · `workbuddy数据同步` · `workbuddy账号同步` · `workbuddy个人转企业号` ·
`workbuddy切换账号看不到历史会话` · `workbuddy账号数据转移` · `workbuddy账号恢复` ·
`workbuddy个人账号转企业账号后数据没了` · `workbuddy账号数据迁移` · `workbuddy恢复历史任务` ·
`workbuddy换号后左侧任务不显示` · `workbuddy同机换号数据恢复` · `workbuddy工作历史丢失` ·
`workbuddy诊断` · `sessions user_id 迁移`

> 触发词统一带 `workbuddy` 前缀（或技术限定语），避免与「任意账号找回 / MySQL 主从 / rsync」等无关场景误触发。

---

## 没有 AI 时的手动流程

1. **完全退出 WorkBuddy**
   托盘图标（Windows 右下角 `^` 隐藏区）右键 → **完全退出**。macOS 用菜单栏图标退出或 `Cmd+Q`。
   ⚠️ 点右上角 `X` **只是关窗口**，进程仍在后台、数据库仍被锁。
2. 打开 WorkBuddy，**登录目标账号**（想让历史归过去的那个）。
3. **再次完全退出 WorkBuddy**。
4. 运行脚本：
   - Windows：双击 `scripts/账号恢复一键.bat`
   - macOS / Linux：`python3 scripts/run_restore.py`（启动器会自动探测 `python3`，回退 `python`）
5. 菜单选 `[2] 执行数据恢复`，核对脚本推荐的一对账号，确认。
6. 重新打开 WorkBuddy 登录目标账号。

---

## 命令行用法

所有命令中的 `<技能目录>` 指**本技能的实际根目录**（WorkBuddy 每轮会注入 `Base directory for this skill:`）。

```bash
# 只读深度诊断（推荐先跑，全程只读不修改数据）
python "<技能目录>/scripts/wb_account_restore.py" diagnose --cwd "<当前工作区路径>"

# 只读诊断并导出三件交付物（CSV/指纹JSON/Markdown，默认写桌面）
python "<技能目录>/scripts/wb_account_restore.py" diagnose --export
python "<技能目录>/scripts/wb_account_restore.py" diagnose --export --out-dir <目录>

# 只读：查看账号分布 + 智能推荐（WorkBuddy 运行中也能跑）
python "<技能目录>/scripts/wb_account_restore.py" detect
python "<技能目录>/scripts/wb_account_restore.py" detect --cwd "<当前工作区路径>"

# 预演迁移计划（不杀进程、不改库、不重启）
python "<技能目录>/scripts/wb_account_restore.py" migrate --from <旧uid> --to <新uid> --dry-run

# 自动迁移：杀WB → 等退出 → 改库 → 重启（独立进程，聊天内推荐用这个）
python "<技能目录>/scripts/wb_account_restore.py" migrate --from <旧uid> --to <新uid>

# 跳过云端持有行（默认不跳过）
python "<技能目录>/scripts/wb_account_restore.py" migrate --from <旧uid> --to <新uid> --skip-cloud

# 手动改库（要求 WorkBuddy 已完全退出）
python "<技能目录>/scripts/wb_account_restore.py" restore --from <旧uid> --to <新uid> --yes

# 列出可用备份
python "<技能目录>/scripts/wb_account_restore.py" rollback

# 手动回滚（要求 WorkBuddy 已完全退出）
python "<技能目录>/scripts/wb_account_restore.py" rollback --tag <备份目录名>

# 自动回滚：杀WB → 等退出 → 回滚 → 重启（独立进程）
python "<技能目录>/scripts/wb_account_restore.py" rollback_auto --tag <备份目录名>

# 交互式引导菜单
python "<技能目录>/scripts/wb_account_restore.py" guide
```

> Windows 上如需在聊天内跑，用 PowerShell 起独立进程，避免被自己杀的 WB 进程连带终止：
> ```powershell
> Start-Process -WindowStyle Hidden -FilePath 'python' -ArgumentList '"<技能目录>\scripts\wb_account_restore.py"','migrate','--from','<旧uid>','--to','<新uid>'
> ```

---

## 参数说明

| 参数 | 说明 |
|------|------|
| `--from <uid>` | 迁出账号（旧账号，历史数据所在）。**必须传完整 UID** |
| `--to <uid>` | 迁入账号（新账号，想看历史的那个） |
| `--yes` | 仅 `restore` 支持，跳过交互确认 |
| `--tag <名>` | 备份目录名，用于 `rollback` / `rollback_auto` |
| `--no-restart` | 仅测试用：迁移 / 回滚后不重启 WorkBuddy |
| `--cwd <路径>` | `detect` / `diagnose`：本次会话工作区绝对路径，作为**确定性判定源**（最高优先级）；不传则按 `last_activity_at` → `created_at` → `legacyOwnerUid` 顺序推断 |
| `--skip-cloud` | `restore` / `migrate`：跳过 `transport=cloud` 的行（默认关闭＝不跳过，保持原行为） |
| `--dry-run` | `restore` / `migrate`：只预演迁移计划，**不备份、不改库、不重启、不杀进程**；仅在 `~/.workbuddy/backup/account-restore/latest-migrate.log` 追加一行 dry-run 记录 |
| `--export` | `diagnose`：导出三件交付物（CSV + 指纹基线 JSON + Markdown），默认写 `~/Desktop`，**opt-in** |
| `--out-dir <目录>` | `diagnose`：指定导出目录（默认 `~/Desktop`，不存在则回落到备份根目录） |

⚠️ **UID 必须完整**，如 `a1b2c3d4-0000-1111-2222-333344445555`。
`detect` 输出里显示的是缩写（`a1b2c3d4…5555`），完整值可用下面任一方式查：

**方式一 · SQL**（需已安装 `sqlite3` 命令行工具）

```sql
SELECT DISTINCT user_id FROM sessions;
```

**方式二 · 一条 python 命令**（与技能同样**零依赖、跨平台、只读**，推荐）

```bash
python -c "import sqlite3,os;print(*{r[0] for r in sqlite3.connect('file:'+os.path.join(os.path.expanduser('~'),'.workbuddy','workbuddy.db').replace(chr(92),'/')+'?mode=ro',uri=True).execute('SELECT DISTINCT user_id FROM sessions')},sep=chr(10))"
```

> 输出为每个账号的完整 uid，每行一个（顺序不保证）。

---

## 备份与回滚

- 每次迁移**自动备份**到 `~/.workbuddy/backup/account-restore/<YYYYMMDD-HHMMSS>/`
- 备份内容：`workbuddy.db` + `-wal` + `-shm` + `app/sessions.json`，并附 `恢复说明.txt`
- 迁移是**单事务原子提交**，中途失败自动 `rollback`，数据库不会被改坏
- 回滚前还会把当前状态另存一份 `*-before-rollback`，防止回滚本身出错

日志位置：`~/.workbuddy/backup/account-restore/latest-migrate.log`

判断迁移是否成功：日志里出现「进程已完全退出（等待 Xs）」**且**「restore 完成」。
只出现「migrate from=...」而没有「restore 完成」= 失败（通常是 WB 进程未退净导致中止）。

**超时保护**：杀进程后最多等 **15 秒**；若 15 秒内未确认 WorkBuddy 完全退出，会**中止迁移（不执行任何 UPDATE，零改动）**，但仍会**重新启动 WorkBuddy 并告警**（不会把你留在 WorkBuddy 已关闭的状态）。遇到此情况请**先完全退出 WorkBuddy 再重试**。

**幂等安全**：重复对同一账号执行 `migrate` 是安全的——若源账号已无可迁移数据，脚本会在**杀进程前直接拒绝**（提示「WorkBuddy 未被关闭」，零改动），退出码 `4`。

**进程检测保守策略**：进程探测（`tasklist` / `pgrep`）若卡顿或超时，按「**可能仍在运行**」保守处理，**绝不提前判定为已退出**。

**读写口径**：`diagnose` / `detect` **全程只读、不写任何目录**；`--dry-run` 仅向 `~/.workbuddy/backup/account-restore/latest-migrate.log` 追加一行；只有 `migrate` / `rollback_auto` 才写备份目录与日志。

**退出码**：

| 退出码 | 含义 |
|--------|------|
| `0` | 成功 |
| `1` | 执行中异常（默认） |
| `2` | 参数错误 |
| `3` | 环境不满足（WorkBuddy 正在运行 / 数据库缺失 / 超时） |
| `4` | 数据无可迁移（跳过，零改动） |

---

## 前置条件

| 项 | 要求 |
|----|------|
| 平台 | **仅 Windows 支持自动迁移**（依赖 `taskkill`）。macOS / Linux 支持 `detect` / `diagnose` 与手动 `restore` / `rollback`（需 WB 已退出），无自动杀进程与重启。 |
| Python | 任意 Python 3.8+。脚本只依赖标准库，**无需 pip 安装任何包**。 |
| 可选 | `python-docx` —— 仅 `guide` 路径用来生成桌面操作手册；未安装则静默跳过。 |
| 数据库 | 已安装并至少启动过 WorkBuddy，存在 `~/.workbuddy/workbuddy.db`。 |
| 网络 | 不需要。 |

---

## FAQ

**Q：检测说某个账号"无活动记录"怎么办？**
该账号登录后没发过消息，`last_activity_at` 没更新，所以识别不到。
让它先登录目标账号 → 新建一个会话随便发一条消息 → 再触发本技能即可。

**Q：`restore` 报"没有找到属于 X 的可迁移数据"？**
多半是 UID 传了截断的前缀。用上文「参数说明」节里那条 python 命令取完整值。

**Q：可以在 WorkBuddy 运行时跑 `restore` / `rollback` 吗？**
不行，会被进程检查中止。聊天内请用 `migrate` / `rollback_auto`（它们会自己杀进程）。

**Q：跨电脑能用吗？**
不能。本技能定位同机换号。跨机需要先复制整个 `~/.workbuddy` 目录再处理路径映射，不在本技能范围。

**Q：迁移会不会丢数据？**
不会。改归属前先做完整性校验与备份，改归属用单事务，失败自动回滚。

**Q：个人转企业号（或企业转个人）要迁吗？怎么迁？**
**要迁，而且和「个人转个人」完全同一个性质。** 迁移只按 `workbuddy.db` 的 `user_id` 归属进行，账号是个人版、企业版还是团队版**不影响迁移流程** —— 照常 `--from <旧uid> --to <新uid>` 即可。

**Q：怎么知道哪个 uuid 是个人版、哪个是企业版？**
`diagnose` 的 **[3] 账号类型识别** 段会直接告诉你：
- **权威来源**：`~/.workbuddy/storage/skeleton/account-snapshot.json` 的 `primary`，含 `uid` / `type`（如 `personal`）/ `nickname` / `editionType`（如 `free`）。⚠️ 该文件**只记录当前主账号**，历史账号查不到。
- **间接线索**（历史账号）：`storage/` 命名空间后缀 —— `-personal` → 个人版；`-<slug>`（如 `acme-team`）→ 企业版/团队版。

识别类型**只为帮你认出身份**，不改变迁移流程。

**Q：`diagnose` 里看到的 `storage/user-<uid>` 与 `user-<uid>-personal` 是什么？会影响迁移吗？**
那是 `storage/` 下的**配置命名空间**（按登录态分目录，存 `preferences.json` / `expert.json` / 技能缓存等），**与会话归属无关**。本工具不处理它，也**不影响**迁移结果 —— 迁移只改 `workbuddy.db`。

**Q：`transport=cloud` 的行为什么特殊？**
`transport=cloud` 表示该行**归云端持有**。本地改它们的归属，可能被**下次同步覆盖**。**默认会一并迁移 `transport=cloud` 的行**（保持原行为）；加 `--skip-cloud` 则**跳过**这些云端持有的行，也可用 `diagnose` 先看云端行数量。

**Q：侧边栏显示的条数为什么和库里（DB）对不上？**
侧边栏读的是 `~/.workbuddy/<uid>/sidebar-list-snapshot.json` 的 `items`，是**快照**，会滞后于 DB；此外 **软删除**（`deleted_at` 非空仍占 DB 行）和 **云端行** 也会造成差值。以 **DB 为准**，用 `diagnose` 的「口径对平」段可看到具体差值。

---

## 文件结构

```
workbuddy-data-sync/
├── SKILL.md                      技能入口（AI 读这个）
├── README.md                     本文件
├── CHANGELOG.md                  版本更新日志（Keep a Changelog 风格）
├── LICENSE                       MIT 许可证
├── scripts/
│   ├── wb_account_restore.py     核心脚本（detect/diagnose/migrate/restore/rollback/rollback_auto/guide）
│   ├── run_restore.py            macOS / Linux 启动器
│   └── 账号恢复一键.bat            Windows 手动备用入口
└── references/
    ├── data_isolation_map.md     数据隔离全景表（含 transport 语义、storage 命名空间、范围说明）
    └── forensics.md              只读取证方法（只读铁律、A/B/C 矩阵、口径对平）
```

---

## 许可证

本项目采用 **MIT** 许可证，详见 [`LICENSE`](LICENSE)。
Copyright (c) 2026 leileibulei

### 致谢

`scripts/wb_account_restore.py` 中 `_find_wb_exe` 检测 WorkBuddy 安装路径的候选顺序
（`ProgramFiles` → `LOCALAPPDATA\Programs` → `ProgramFiles(x86)`）沿用配套技能
**workbuddy皮肤**（MIT 许可）里 `WorkBuddy一键换肤.bat` 的实现，特此致谢。
（详见 `scripts/wb_account_restore.py:413-419` 的函数 docstring 来源说明。）
