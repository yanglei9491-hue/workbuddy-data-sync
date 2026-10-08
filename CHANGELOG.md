# 更新日志

本文件记录「WorkBuddy 数据同步」技能的版本变更。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [3.1.1] - 2026-10-08

### 文档
- 补齐**前置条件缺口**：明确「必须在【新账号】（想看到历史的那个）下触发本技能」。
  - `SKILL.md`：前置条件表新增「登录状态」行；新增 **Phase 0 前置确认**（引导语第一屏先确认账号，再执行诊断）。
  - `README.md`：聊天内自动流程新增「先确认你在新账号下操作」步骤。
- 变更原因：自动推荐依赖「本次会话 cwd 自证」判定迁入目标，该前提此前**未在文档中言明**；在旧账号下触发会导致工具不再自动判定目标（安全降级，不会迁错数据，但流程中断）。

## [3.1.0] - 2026-10-08

### 新增
- 新增 `LICENSE`（MIT），明确版权归属。
- 新增 `references/` 归因声明：对引用自 WorkBuddy 客户端资源的代码段与取证方法论补充来源、用途与权利归属说明。
- 新增 `CHANGELOG.md`（本文件）。

### 变更
- **命名合规修复**：`name` 由中文 `workbuddy数据同步` 改为 slug `workbuddy-data-sync`，中文显示名「WorkBuddy 数据同步」移入 `description` 首句。
- **写盘口径统一**：明确 `diagnose` / `detect` 全程只读不写盘；`--dry-run` 仅在 `~/.workbuddy/backup/account-restore/latest-migrate.log` 追加一行；`migrate` / `rollback_auto` 才写备份目录与日志。
- **迁移字段口径统一**：迁移范围明确为 3 张表 —— `sessions.user_id`、`automations.owner_user_id`、`automation_delivery_outbox.owner_user_id`。
- **平台能力口径统一**：Windows 全自动（`taskkill` + 重启）；macOS / Linux 支持 `detect` / `diagnose` 与手动 `restore` / `rollback`（需 WorkBuddy 已退出），无自动杀进程与重启。
- **触发词收敛**：过泛裸词（如「账号恢复」「数据同步」）统一加 `workbuddy` 前缀或场景限定，降低误触发。
- `scripts/run_restore.py` 启动器由硬编码 `python` 改为**优先探测 `python3`、回退 `python`**，兼容仅提供 `python3` 的环境。

### 修复
- 修复**重复 `migrate` 幂等缺陷**：此前对同一账号重复迁移会杀掉 WorkBuddy 进程却不重新启动；现改为——若已无可迁移数据，**在杀进程前直接安全拒绝**，不关闭 WorkBuddy。
- 修复**进程等待超时行为**：15 秒内进程未退净时，由原行为改为「**中止迁移（零数据改动）+ 重新启动 WorkBuddy + 告警**」。
- 修复 `_is_workbuddy_running` 的**异常误判**：改为三态判定，避免进程探测异常被误当作「未运行」。

### 已知限制
- 仅支持同机换号，不处理跨机路径差异（跨机需先复制整个 `~/.workbuddy`）。
- `transport=cloud` 的行使本地改归属可能被下次同步覆盖，可用 `--skip-cloud` 跳过。

## [3.0.1] - 2026-09-30

### 变更
- 脱敏 5 处真实标识（4 个 uid + 企业版租户 slug）为示例值，保留「必须传完整 UID」知识点。
- 删除触发词「记忆也要跟过去」；「不处理」清单补充记忆说明。
- 身份文件 / 用户级记忆归属论证入档：安装级全局、与账号无关（`readLocalMemoryFiles` / `resolveConfigDir` 代码证据 + 四层记忆归属表）。
- 命令按平台区分（Windows `type` / macOS·Linux `cat`）；查完整 UID 改为可复制的一行命令（`?mode=ro`）。
- 正文去版本叙事（`--cwd` 参数说明改为现在时）。

### 新增
- 新增 `references/forensics.md`（数据分层取证参考）。

## [更早版本]

- 3.0.0 及更早版本的变更未归档。
