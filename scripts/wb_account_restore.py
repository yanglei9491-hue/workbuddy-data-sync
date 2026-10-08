#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 账号数据恢复工具（同机换号版）
============================================
定位：同一台电脑上，退出账号A、登录账号B之后，把账号A的历史会话、任务
     迁移归属到账号B，让账号B登录后能看到账号A的历史记录。

不处理（同机换号不需要）：
  - 技能库（~/.workbuddy/skills/ 全局共享，换号不丢，不碰）
  - 连接器/凭证（本机已有，不碰）
  - 项目级记忆（跟着项目目录走，换号不丢）
  - 跨机路径差异（本次定位同机，不做路径映射）
  - storage/ 配置命名空间（偏好/专家缓存，与库内会话归属无关，不碰）

注：个人版 ↔ 企业版之间的迁移与「个人转个人」**同一性质**，走同一流程，
    账号类型不影响迁移逻辑（迁移只按 workbuddy.db 的 user_id 归属进行）。

三段式工作流（v3.1.1）：
  diagnose（只读深度诊断）→ 用户确认 → migrate（写入迁移）

安全设计（5 道安全门）：
  ① 进程检测   ：WorkBuddy 必须已完全退出
  ② 多账号      ：列出全部账号，支持多账号识别+智能推荐 from/to（默认只迁移一对）
  ③ 完整性校验 ：迁移前 PRAGMA integrity_check
  ④ 备份       ：WAL checkpoint 后备份 db 三件套 + sessions.json
  ⑤ 原子事务   ：BEGIN IMMEDIATE，失败自动 rollback，落盘+源归零验证

用法：
  python wb_account_restore.py diagnose          # 只读深度诊断（推荐先跑）
  python wb_account_restore.py diagnose --cwd "<当前工作区路径>" --export
  python wb_account_restore.py detect            # 只看账号分布（只读）
  python wb_account_restore.py detect --cwd "<当前工作区路径>"
  python wb_account_restore.py restore           # 交互式：确认旧→新账号后执行（要求 WB 已退出）
  python wb_account_restore.py restore --from <旧uid> --to <新uid> --yes
  python wb_account_restore.py restore --from <旧uid> --to <新uid> --yes --dry-run
  python wb_account_restore.py migrate --from <旧uid> --to <新uid>   # 自动：杀WB→改库→重启（独立进程）
  python wb_account_restore.py rollback --tag <备份目录名>           # 要求 WB 已退出
  python wb_account_restore.py rollback_auto --tag <备份目录名>      # 自动：杀WB→回滚→重启（独立进程）
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
import subprocess
from pathlib import Path

# Encoding safeguard: Windows GBK terminal crashes on emoji/Chinese output
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

# 跨平台取用户主目录（Windows 用用户目录、macOS/Linux 用家目录）
WORKBUDDY_DIR = Path.home() / ".workbuddy"
DB_PATH = WORKBUDDY_DIR / "workbuddy.db"
SESSIONS_JSON = WORKBUDDY_DIR / "app" / "sessions.json"
BAK_ROOT = WORKBUDDY_DIR / "backup" / "account-restore"

# 需要迁移归属的字段（官方一致）
TABLES_FIELD = [
    ("sessions", "user_id"),
    ("automations", "owner_user_id"),
    ("automation_delivery_outbox", "owner_user_id"),
]

# 归属字段候选名（用于 diagnose 的归属完整性扫描）
_OWNER_FIELD_NAMES = ("user_id", "owner_user_id", "userId")

_TS_FMT = "%Y%m%d-%H%M%S"

# 退出码分级（默认 EXIT_RUNTIME=1，保持向后兼容：不传 code 时行为不变）
EXIT_RUNTIME = 1      # 执行中异常（默认）
EXIT_USAGE = 2        # 参数错误（缺参 / 参数非法）
EXIT_ENV = 3          # 环境不满足（WorkBuddy 在运行 / DB 缺失 / 进程未确认退出）
EXIT_NO_DATA = 4      # 数据无可迁移（幂等场景）


def bail(msg: str, code: int = EXIT_RUNTIME) -> None:
    print(f"[ERROR] {msg}")
    sys.exit(code)


# ---------------------------------------------------------------- 基本工具
def _is_workbuddy_running() -> bool | None:
    """跨平台检测 WorkBuddy 是否在运行（三态返回）。

    True  = 检测到在运行
    False = 确认未运行
    None  = 检测失败（未知，如 tasklist/pgrep 卡顿或超时）——**保守处理**：
            调用方须视为「可能仍在运行」，绝不可当作已退出。保留 timeout=10。
    """
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq WorkBuddy.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            return "WorkBuddy" in out.stdout
        else:
            # macOS / Linux
            names = ["WorkBuddy", "workbuddy"]
            for n in names:
                out = subprocess.run(
                    ["pgrep", "-f", n], capture_output=True, text=True, timeout=10
                )
                if out.stdout.strip():
                    return True
            return False
    except Exception as e:
        # 不吞异常：显式返回 None（未知），交调用方按"可能仍在运行"保守处理
        print(f"  [warn] WorkBuddy 进程检测失败（保守视为可能仍在运行）: {e}")
        return None


def connect(ro: bool = False) -> sqlite3.Connection:
    if not DB_PATH.exists():
        bail(f"找不到数据库: {DB_PATH}（请确认已安装并启动过 WorkBuddy）", code=EXIT_ENV)
    conn = (
        sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        if ro
        else sqlite3.connect(str(DB_PATH))
    )
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(cur, table: str) -> bool:
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def col_exists(cur, table: str, col: str) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    return any(r["name"] == col for r in cur.fetchall())


def _all_tables(cur) -> list[str]:
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return [r["name"] for r in cur.fetchall()]


def _sha256_file(p: Path) -> str:
    """对文件内容做 sha256（注意：仅对稳定的主库文件用，不要对 -wal 用）。"""
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _checkpoint(conn) -> None:
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    except Exception as e:
        print(f"  [warn] WAL checkpoint 未完全成功: {e}")


def _integrity_check(conn) -> bool:
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check")
        r = cur.fetchone()
        ok = r and str(r[0]).lower() == "ok"
        if not ok:
            print(f"  [!] integrity_check 未通过: {r[0] if r else 'unknown'}")
        return bool(ok)
    except Exception as e:
        print(f"  [warn] integrity_check 执行失败: {e}")
        return False


# ---------------------------------------------------------------- 账号枚举
def _collect_accounts(cur) -> list[dict]:
    stats: dict[str, dict] = {}
    for table, field in TABLES_FIELD:
        if not table_exists(cur, table) or not col_exists(cur, table, field):
            continue
        cur.execute(f"SELECT {field}, COUNT(*) c FROM {table} GROUP BY {field}")
        for r in cur.fetchall():
            uid = r[0] or "(NULL)"
            stats.setdefault(uid, {})[table] = r["c"]
    # 补充每账号时间字段（last_activity / created 毫秒时间戳）
    time_fields = {
        "max_last_activity": ("last_activity_at", "MAX"),
        "max_created": ("created_at", "MAX"),
        "min_created": ("created_at", "MIN"),
    }
    for uid, detail in stats.items():
        if uid == "(NULL)" or not table_exists(cur, "sessions"):
            continue
        real_uid = uid
        for key, (col, agg) in time_fields.items():
            if not col_exists(cur, "sessions", col):
                detail[key] = None
                continue
            try:
                cur.execute(f"SELECT {agg}({col}) FROM sessions WHERE user_id=?", (real_uid,))
                row = cur.fetchone()
                detail[key] = row[0] if row else None
            except Exception:
                detail[key] = None
    accs = []
    for uid, detail in stats.items():
        # total 只汇总表行计数，排除时间字段（max_last_activity/max_created/min_created）
        total = sum(v for k, v in detail.items()
                    if k not in ("max_last_activity", "max_created", "min_created"))
        accs.append({"uid": uid, "detail": detail, "total": total})
    accs.sort(key=lambda a: -a["total"])
    return accs


def _recent_user_id_from_db(cur):
    if col_exists(cur, "sessions", "last_activity_at") and col_exists(cur, "sessions", "user_id"):
        cur.execute(
            "SELECT user_id FROM sessions WHERE last_activity_at IS NOT NULL "
            "ORDER BY last_activity_at DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row and row["user_id"]:
            return row["user_id"]
    return None


# ---------------------------------------------------------------- 账号识别（多账号）
def _short_uid(uid: str, n: int = 10) -> str:
    """a1b2c3d4-0000-1111-2222-333344445555 → a1b2c3d4…5555"""
    if not uid or uid == "(NULL)":
        return uid or "?"
    uid = str(uid)
    return uid[:n] + "…" + uid[-4:] if len(uid) > n + 4 else uid


def _format_ts_ms(ms) -> str:
    """毫秒时间戳 → 'YYYY-MM-DD HH:MM:SS'；None/非法 → '无记录'。"""
    if not ms:
        return "无记录"
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "无记录"


def _norm_cwd(x) -> str:
    """cwd 归一化：正反斜杠统一为反斜杠、去尾斜杠、转小写。"""
    return str(x).replace("/", "\\").rstrip("\\").lower()


def _current_session_user_id(cur, cwd) -> str | None:
    """cwd 自证：用『本次会话的工作区路径』反查它属于哪个 user_id（确定性判定源）。

    归一化后在 SQL 侧同样归一化比较（大小写、分隔符都不统一，见 references/forensics.md）。
    取 last_activity_at 最新一行；该列不存在时按 rowid 兜底。命中返回完整 uid，否则 None。
    """
    if not cwd:
        return None
    try:
        if not (col_exists(cur, "sessions", "cwd") and col_exists(cur, "sessions", "user_id")):
            return None
        needle = _norm_cwd(cwd)
        order = ("last_activity_at DESC"
                 if col_exists(cur, "sessions", "last_activity_at") else "rowid DESC")
        cur.execute(
            "SELECT user_id FROM sessions WHERE REPLACE(LOWER(cwd), '/', '\\') = ? "
            f"ORDER BY {order} LIMIT 1",
            (needle,),
        )
        row = cur.fetchone()
        if row and row["user_id"]:
            return row["user_id"]
    except Exception:
        pass
    return None


def _read_legacy_owner_uid() -> str | None:
    """settings.json → claw.legacyOwnerUid（遗留主号标记，作参照/冲突提示）。"""
    try:
        p = WORKBUDDY_DIR / "settings.json"
        if not p.exists():
            return None
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("claw", {}).get("legacyOwnerUid")
    except Exception:
        return None


def _read_configured_users() -> set[str]:
    """settings.json → claw.users 的 keys（哪些账号配置过通道，判定"真实账号"）。"""
    try:
        p = WORKBUDDY_DIR / "settings.json"
        if not p.exists():
            return set()
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return set((data.get("claw", {}).get("users") or {}).keys())
    except Exception:
        return set()


# ---------------------------------------------------------------- 账号类型识别
# 权威来源：storage/skeleton/account-snapshot.json → primary.{uid,type,nickname,editionType}
# 注意：WorkBuddy 目前只记录「当前主账号」一份，历史账号在此文件里查不到。
# 账号类型（个人版/企业版）**不影响迁移逻辑** —— 迁移只按 workbuddy.db 的 user_id 归属进行。
# 识别类型只是为了帮用户「认出哪个 uuid 是哪个账号」。
_ACCOUNT_TYPE_LABEL = {
    "personal": "个人版",
    "enterprise": "企业版",
    "team": "团队版",
    "company": "企业版",
    "corp": "企业版",
    "organization": "企业版",
    "org": "企业版",
}


def _account_type_label(t) -> str:
    if not t:
        return "未知"
    s = str(t).strip()
    return _ACCOUNT_TYPE_LABEL.get(s.lower(), s)


def _read_account_snapshot() -> dict | None:
    """读 storage/skeleton/account-snapshot.json 的 primary 账号档案。

    返回 {uid, nickname, type, type_label, edition, isPro, isAdmin}；
    文件缺失/损坏 → None（优雅降级，不抛异常）。
    """
    try:
        p = WORKBUDDY_DIR / "storage" / "skeleton" / "account-snapshot.json"
        if not p.is_file():
            return None
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        pri = data.get("primary") if isinstance(data, dict) else None
        if not isinstance(pri, dict):
            return None
        t = pri.get("type")
        return {
            "uid": pri.get("uid"),
            "nickname": pri.get("nickname"),
            "type": t,
            "type_label": _account_type_label(t),
            "edition": pri.get("editionType"),
            "isPro": pri.get("isPro"),
            "isAdmin": pri.get("isAdmin"),
        }
    except Exception:
        return None


def _namespace_type_hint(uid: str) -> str:
    """据 storage 命名空间后缀推断账号类型（**间接线索**，非权威）。

    后缀 `-personal` → 个人版；`-<slug>`（团队/企业租户标识）→ 企业版/团队版。
    权威来源是 account-snapshot.json，但它只记录当前主账号，历史账号只能靠本线索。
    """
    if not uid:
        return ""
    storage = WORKBUDDY_DIR / "storage"
    if not storage.is_dir():
        return ""
    labels = []
    try:
        for d in sorted(os.listdir(storage)):
            if not d.startswith("user-" + uid):
                continue
            suffix = d[len("user-" + uid):]
            if suffix == "-personal":
                labels.append("个人版")
            elif suffix.startswith("-") and suffix[1:]:
                labels.append(f"企业版/团队版(slug:{suffix[1:]})")
    except OSError:
        pass
    return "; ".join(sorted(set(labels)))


def _account_type_for(uid: str, snapshot: dict | None) -> str:
    """账号类型：优先用 account-snapshot（权威），否则退回命名空间后缀（线索）。"""
    if snapshot and snapshot.get("uid") == uid and snapshot.get("type"):
        label = snapshot.get("type_label") or "未知"
        ed = snapshot.get("edition")
        return f"{label}({snapshot.get('type')})" + (f"/{ed}" if ed else "")
    return _namespace_type_hint(uid)


def _recent_created_user_id_from_db(cur) -> str | None:
    """MAX(created_at) 对应 user_id（兜底：纯登录未发消息时 created_at 可当最近登录线索）。"""
    try:
        if col_exists(cur, "sessions", "created_at") and col_exists(cur, "sessions", "user_id"):
            cur.execute(
                "SELECT user_id FROM sessions WHERE created_at IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1"
            )
            row = cur.fetchone()
            if row and row["user_id"]:
                return row["user_id"]
    except Exception:
        pass
    return None


def _find_wb_exe() -> str | None:
    """检测 WorkBuddy 安装路径（3 候选）。

    候选顺序沿用配套技能「workbuddy皮肤」（SKILL.md 声明 license: MIT）中
    `WorkBuddy一键换肤.bat` 的 ProgramFiles → LOCALAPPDATA\\Programs →
    ProgramFiles(x86) 探测逻辑；本技能同为 MIT 许可，来源与许可一致。
    """
    cands = [
        os.path.join(os.environ.get("ProgramFiles", ""), "WorkBuddy", "WorkBuddy.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "WorkBuddy", "WorkBuddy.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "WorkBuddy", "WorkBuddy.exe"),
    ]
    for p in cands:
        if p and os.path.exists(p):
            return p
    return None


def _detect_recent(cur, cwd: str | None = None) -> dict:
    """综合判定「最近活动/登录账号」。返回 verdict 字典。

    cwd 未传时（向后兼容 v2.2.0）：selected = activity or created。
    传入 cwd 时：新增最高优先级来源 self_cwd，selected = self_cwd or activity or created。
    """
    activity = _recent_user_id_from_db(cur)        # 主判：最近活动
    created = _recent_created_user_id_from_db(cur) # 兜底：最近开新会话
    legacy = _read_legacy_owner_uid()              # 参看：遗留主号
    configured = _read_configured_users()          # 参看：配置过通道的账号
    self_cwd = _current_session_user_id(cur, cwd) if cwd else None
    verdict = {
        "recent_activity": activity,
        "recent_created": created,
        "legacy": legacy,
        "configured": configured,
        "self_cwd": self_cwd,
        "selected": self_cwd or activity or created,
        "conflicts": [],
    }
    if self_cwd and activity and self_cwd != activity:
        verdict["conflicts"].append(
            f"本次会话 cwd 自证账号是 {_short_uid(self_cwd)}，但库内最近活动账号是 "
            f"{_short_uid(activity)} —— 以【本次会话 cwd 自证结果优先】。"
        )
    if activity and legacy and activity != legacy:
        verdict["conflicts"].append(
            f"库内最近活动账号是 {_short_uid(activity)}，但 settings.json 标记的 "
            f"legacyOwnerUid 是 {_short_uid(legacy)}。请确认真正想迁移的账号。"
        )
    if not self_cwd and not activity and created:
        verdict["conflicts"].append(
            "没有任何账号有 last_activity 记录（纯登录未发消息）；已退化为用 created_at 判定。"
            "建议登录目标账号后发一条消息再 detect 确认。"
        )
    if not self_cwd and not activity and not created:
        verdict["conflicts"].append(
            "无法从数据库识别最近活动的账号。请登录目标账号后发一条消息，再运行 detect。"
        )
    return verdict


# ---------------------------------------------------------------- detect
def _account_hint(cur, uid: str) -> str:
    """给账号一个可识别的线索（取最近会话的 cwd/title 片段）。"""
    try:
        if col_exists(cur, "sessions", "user_id"):
            cur.execute(
                "SELECT cwd FROM sessions WHERE user_id=? AND cwd IS NOT NULL "
                "ORDER BY last_activity_at DESC LIMIT 1", (uid,)
            )
            row = cur.fetchone()
            if row and row["cwd"]:
                # 取路径最后一段作为线索
                p = str(row["cwd"]).replace("\\", "/").rstrip("/")
                last = p.split("/")[-1] if p else ""
                if last and len(last) > 3:
                    return last[:25]
    except Exception:
        pass
    return ""


def detect(cwd: str | None = None) -> None:
    conn = connect(ro=True)
    cur = conn.cursor()
    print("=" * 72)
    print("WorkBuddy 账号数据分布（多账号识别）")
    print("=" * 72)
    accs = _collect_accounts(cur)
    if not accs:
        conn.close()
        bail("库中没有账号数据。")
    verdict = _detect_recent(cur, cwd)
    configured = verdict["configured"]
    legacy = verdict["legacy"]
    acc_list = _print_acc_list(cur, accs, verdict)
    # 智能推荐
    from_id, to_id, advise = _smart_recommend_multi(accs, verdict)
    print("\n" + "=" * 72)
    print("【智能推荐】(自动识别，无需手填 user_id)")
    print(f"  旧账号(迁出/历史所在) = 数据最多 → {_short_uid(from_id)}")
    if to_id:
        print(f"  新账号(迁入/想看历史) = 当前触发账号(★最近活动) → {_short_uid(to_id)}")
    else:
        print("  新账号(迁入/想看历史) = ⚠ 无法自动判定（见下方提示）")
    if verdict.get("self_cwd"):
        print(f"  (本次会话 cwd 自证账号 = {_short_uid(verdict['self_cwd'])}，优先于活动时间)")
    elif cwd:
        print("  ⚠ 已传入 --cwd，但库中未匹配到该路径 → 本次退化为按活动时间判定")
    print("=" * 72)
    # 冲突与提示
    for c in verdict["conflicts"]:
        print(f"\n[提示] {c}")
    for a in advise:
        print(f"[提示] {a}")
    conn.close()


def _print_acc_list(cur, accs, verdict) -> list[dict]:
    """逐账号打印明细表格，返回账号列表（供推荐用）。

    账号类型优先取自 account-snapshot.json（权威，仅当前主账号有），
    其余账号退回 storage 命名空间后缀推断（线索，标注为推断）。
    """
    configured = verdict["configured"]
    legacy = verdict["legacy"]
    selected = verdict["selected"]
    snapshot = _read_account_snapshot()
    for i, a in enumerate(accs, 1):
        detail = ", ".join(f"{t}:{v}" for t, v in a["detail"].items()
                           if t not in ("max_last_activity", "max_created", "min_created"))
        hint = _account_hint(cur, a["uid"])
        d = a["detail"]
        last_act = d.get("max_last_activity")
        created = d.get("max_created")
        tags = []
        typeinfo = _account_type_for(a["uid"], snapshot)
        if typeinfo:
            tags.append(typeinfo)
        if selected and a["uid"] == selected:
            tags.append("★最近活动")
        if i == 1:  # accs 已按 total 降序排序，序号 1 即数据最多的主号
            tags.append("数据最多(主号)")
        if last_act is None:
            tags.append("无活动记录")
        if legacy and a["uid"] == legacy:
            tags.append("遗留主号")
        if a["uid"] in configured:
            tags.append("库外有通道配置")
        tagstr = ("  <" + " | ".join(tags) + ">") if tags else ""
        hintstr = f"  线索:{hint}" if hint else ""
        nick = ""
        if snapshot and snapshot.get("uid") == a["uid"] and snapshot.get("nickname"):
            nick = f"  昵称:{snapshot['nickname']}"
        act_str = f"最后活动:{_format_ts_ms(last_act)}"
        cre_str = f"创建:{_format_ts_ms(d.get('min_created'))}~{_format_ts_ms(created)}"
        print(f"  [{i}] {_short_uid(a['uid'])}{tagstr}")
        print(f"      条数:{a['total']} ({detail})  {act_str}")
        print(f"      {cre_str}{hintstr}{nick}")
    return accs


def _smart_recommend_multi(accs, verdict) -> tuple[str, str | None, list[str]]:
    """多账号智能推荐：from=数据最多，to=最近活动（含缺失降级）。

    返回 (from_id, to_id, advise[])；当无法确定迁入目标时 to_id 为 None（不再猜测）。
    """
    advise: list[str] = []
    if not accs:
        bail("库中没有账号数据。")
    # from = 数据最多（老账号/主号）
    from_id = accs[0]["uid"]
    selected = verdict.get("selected")  # ★最近活动（或 cwd 自证）= 当前触发技能的账号（新账号）
    # to = 当前触发账号（最近活动/自证）；若与主号相同或无法判定 → None（交给用户手动指定）
    if selected and selected != from_id:
        return from_id, selected, advise
    if len(accs) <= 1:
        advise.append("库里只有 1 个账号，无可迁移目标。")
    else:
        advise.append(
            "当前登录账号就是数据最多的主号，无法自动判定迁入目标。可能原因："
            "① 你还没登录新账号 —— 请登录新账号并在任意项目发一条消息后重跑；"
            "② 目标账号尚未产生会话记录，detect 无法识别它。"
            "请用 --to <完整uid> 手动指定目标。"
            "（个人转企业号与个人转个人同一流程，不受账号类型影响。）"
        )
    return from_id, None, advise


# ---------------------------------------------------------------- 只读诊断辅助
def _build_body_index() -> dict[str, str]:
    """建 WB/projects/**/*.jsonl 索引（只 listdir 一层，不读文件内容、不递归）。

    返回 {会话id(小写): jsonl 绝对路径}。projects 目录缺失时返回空 dict（优雅降级）。
    """
    index: dict[str, str] = {}
    proj = WORKBUDDY_DIR / "projects"
    if not proj.is_dir():
        return index
    try:
        slugs = os.listdir(proj)
    except OSError:
        return index
    for slug in slugs:
        sub = proj / slug
        try:
            if not sub.is_dir():
                continue
            for fn in os.listdir(sub):
                if fn.lower().endswith(".jsonl"):
                    stem = fn[: -len(".jsonl")]
                    index[stem.lower()] = str(sub / fn)
        except OSError:
            continue
    return index


def _detect_variants() -> dict[str, list[str]]:
    """扫 WB/storage/ 下 user-<uid>[-<后缀>] 目录，按 base uid 归组后缀。"""
    variants: dict[str, list[str]] = {}
    storage = WORKBUDDY_DIR / "storage"
    if not storage.is_dir():
        return variants
    try:
        names = os.listdir(storage)
    except OSError:
        return variants
    for name in names:
        if not name.startswith("user-"):
            continue
        rest = name[len("user-"):]
        segs = rest.split("-")
        if len(segs) >= 5:
            base = "-".join(segs[:5])
            suffix = "-".join(segs[5:])
        else:
            base = rest
            suffix = ""
        variants.setdefault(base, []).append(suffix)
    return variants


def _variant_label(suffix: str) -> str:
    """storage 命名空间后缀 → 账号类型线索（间接推断，非权威）。

    权威来源是 account-snapshot.json 的 primary.type，但它只记录当前主账号。
    """
    if suffix == "personal":
        return "个人版"
    if suffix == "":
        return "无后缀（类型未知）"
    return f"企业版/团队版(slug:{suffix})"


def _transport_breakdown(cur, uid: str) -> str:
    """返回该账号 sessions 的 transport 分组字符串，如 'local:1471, cloud:6'。"""
    if not (table_exists(cur, "sessions") and col_exists(cur, "sessions", "transport")):
        return ""
    try:
        cur.execute(
            "SELECT COALESCE(transport,'(空)') t, COUNT(*) c FROM sessions "
            "WHERE user_id=? GROUP BY transport", (uid,))
        return ", ".join(f"{r['t']}:{r['c']}" for r in cur.fetchall())
    except Exception:
        return ""


# ---------------------------------------------------------------- diagnose（只读）
def _print_section(idx: int, title: str) -> None:
    print(f"\n[{idx}] {title}")


def diagnose(cwd: str | None = None, export: bool = False,
             out_dir: str | None = None) -> None:
    """只读深度诊断（8 段）。全程 connect(ro=True)，禁止任何写操作。"""
    conn = connect(ro=True)
    cur = conn.cursor()
    ts = datetime.datetime.now().strftime(_TS_FMT)
    print("=" * 72)
    print("WorkBuddy 只读深度诊断（diagnose · 全程只读，不修改任何数据）")
    print("=" * 72)

    accs = _collect_accounts(cur)
    verdict = _detect_recent(cur, cwd)
    body_index = _build_body_index()

    # ---- [1] 归属完整性 ----
    _print_section(1, "归属完整性（扫全库含归属字段的列，与迁移覆盖表对比）")
    owned_cols: list[tuple[str, str]] = []
    for t in _all_tables(cur):
        if t.startswith("sqlite_"):
            continue
        try:
            cur.execute(f"PRAGMA table_info({t})")
            for r in cur.fetchall():
                if r["name"] in _OWNER_FIELD_NAMES:
                    owned_cols.append((t, r["name"]))
        except Exception:
            continue
    covered = {(t, f) for t, f in TABLES_FIELD}
    uncovered = [(t, f) for t, f in owned_cols if (t, f) not in covered]
    print(f"  含归属字段的表: {len(owned_cols)} 张，"
          f"其中 {len(owned_cols) - len(uncovered)} 张已覆盖，{len(uncovered)} 张未覆盖")
    for t, f in owned_cols:
        mark = "✓已覆盖" if (t, f) in covered else "⚠未覆盖"
        print(f"    - {t}.{f}  {mark}")
    if not uncovered:
        print(f"  结论: {len(owned_cols)} 张表，全部已覆盖（无需扩展迁移表）。")
    else:
        print("  结论: 存在未覆盖的归属字段，请人工复核是否需纳入迁移。")

    # ---- [2] 账号清单（含 transport 分组）----
    _print_section(2, "账号清单（含 transport 分组）")
    if not accs:
        print("  (无账号数据)")
    for i, a in enumerate(accs, 1):
        uid = a["uid"]
        detail_parts = []
        for t, v in a["detail"].items():
            if t in ("max_last_activity", "max_created", "min_created"):
                continue
            if t == "sessions":
                tb = _transport_breakdown(cur, uid)
                detail_parts.append(f"sessions:{v}" + (f" [{tb}]" if tb else ""))
            else:
                detail_parts.append(f"{t}:{v}")
        detail = ", ".join(detail_parts)
        sel = " ★当前" if verdict.get("selected") and a["uid"] == verdict["selected"] else ""
        print(f"  [{i}] {_short_uid(uid)}{sel}  条数:{a['total']} ({detail})")
    if verdict.get("self_cwd"):
        print(f"  (本次会话 cwd 自证账号 = {_short_uid(verdict['self_cwd'])}，优先于活动时间)")
    elif cwd:
        print("  ⚠ 已传入 --cwd，但库中未匹配到该路径 → 本次退化为按活动时间判定")

    # ---- [3] 账号类型识别 ----
    _print_section(3, "账号类型识别（个人版 / 企业版）")
    snapshot = _read_account_snapshot()
    if snapshot:
        print(f"  权威来源: storage/skeleton/account-snapshot.json（仅记录当前主账号）")
        print(f"    uid      = {snapshot.get('uid')}")
        print(f"    账号类型 = {snapshot.get('type_label')} ({snapshot.get('type')})"
              + (f"   版本 = {snapshot.get('edition')}" if snapshot.get("edition") else ""))
        if snapshot.get("nickname"):
            print(f"    昵称     = {snapshot['nickname']}")
    else:
        print("  storage/skeleton/account-snapshot.json 不存在或无法解析 → 当前账号类型未知。")
    print("  其余账号类型（依据 storage 命名空间后缀，**间接线索**）：")
    variants = _detect_variants()
    if not variants:
        print("    （未发现 storage 命名空间目录）")
    for base, suffixes in variants.items():
        if snapshot and snapshot.get("uid") == base:
            continue
        uniq = sorted(set(s for s in suffixes if s))
        if uniq:
            hint = "; ".join(_variant_label(s) for s in uniq)
        else:
            hint = "无类型线索"
        print(f"    - {_short_uid(base)}: {hint}")
    print("  说明: 账号类型（个人版/企业版/团队版）**不影响迁移逻辑** —— 迁移只按 "
          "workbuddy.db 的 user_id 归属进行，个人转企业号与个人转个人是同一套流程。")
    print("        此项仅用于帮你认出「哪个 uuid 对应哪个账号」。")
    print("        storage/ 本身只存偏好与专家缓存，不参与迁移，也不会被本工具修改。")

    # ---- [4] 正文完整性矩阵 ----
    _print_section(4, "正文完整性矩阵（projects/**/*.jsonl 与 sessions.id 小写比对）")
    if not (WORKBUDDY_DIR / "projects").is_dir():
        print("  projects 目录不存在，跳过。")
    else:
        # 全局 session id 集合（小写）
        all_ids: set[str] = set()
        if table_exists(cur, "sessions") and col_exists(cur, "sessions", "id"):
            cur.execute("SELECT LOWER(id) sid FROM sessions WHERE id IS NOT NULL")
            all_ids = {r["sid"] for r in cur.fetchall()}
        orphan_ids = [k for k in body_index if k not in all_ids]
        print(f"  jsonl 正文文件总数: {len(body_index)}；库内会话总数: {len(all_ids)}")
        for a in accs:
            uid = a["uid"]
            if not (table_exists(cur, "sessions") and col_exists(cur, "sessions", "user_id")):
                continue
            cur.execute("SELECT id FROM sessions WHERE user_id=? AND id IS NOT NULL", (uid,))
            ids = [r["id"].lower() for r in cur.fetchall()]
            a_cnt = sum(1 for sid in ids if sid in body_index)
            b_cnt = len(ids) - a_cnt
            rate = (a_cnt / len(ids) * 100.0) if ids else 0.0
            print(f"  - {_short_uid(uid)}: A_有正文={a_cnt} / B_仅元数据={b_cnt} / "
                  f"正文命中率={rate:.1f}%")
        print(f"  C_孤儿正文(无对应 session 行)={len(orphan_ids)}")

    # ---- [5] 口径对平 ----
    _print_section(5, "口径对平（DB sessions 行数 vs sidebar-list-snapshot.json items）")
    for a in accs:
        uid = a["uid"]
        db_cnt = a["detail"].get("sessions")
        snap = WORKBUDDY_DIR / uid / "sidebar-list-snapshot.json"
        if not snap.is_file():
            print(f"  - {_short_uid(uid)}: DB sessions={db_cnt}  无侧边栏快照文件")
            continue
        n_snap = None
        try:
            with open(snap, encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items") if isinstance(data, dict) else None
            n_snap = len(items) if items is not None else None
        except Exception as e:
            print(f"  - {_short_uid(uid)}: 快照读取失败 {e}")
            continue
        if db_cnt is None or n_snap is None:
            print(f"  - {_short_uid(uid)}: DB sessions={db_cnt}  快照 items={n_snap}")
            continue
        diff = db_cnt - n_snap
        print(f"  - {_short_uid(uid)}: DB sessions={db_cnt}  快照 items={n_snap}  差值={diff}"
              + ("  (快照滞后/已删除/云端行均可能造成差异)" if diff != 0 else ""))

    # ---- [6] 遗留数据源 ----
    _print_section(6, "遗留数据源（.legacy-localstorage-migration.done）")
    legacy_done = WORKBUDDY_DIR / ".legacy-localstorage-migration.done"
    if not legacy_done.is_file():
        print("  未发现遗留迁移标记文件，跳过。")
    else:
        try:
            with open(legacy_done, encoding="utf-8") as f:
                ldata = json.load(f)
        except Exception:
            ldata = {}
        status = ldata.get("status") if isinstance(ldata, dict) else None
        print(f"  status = {status}")
        if status == "pending":
            print("  ⚠ 告警: 可能存在尚未迁入的更早会话数据。")

    # ---- [7] 风险预警 ----
    _print_section(7, "风险预警（云端持有行）")
    if col_exists(cur, "sessions", "transport"):
        cur.execute("SELECT COUNT(*) c FROM sessions WHERE transport='cloud'")
        n_cloud = cur.fetchone()["c"]
        print(f"  transport='cloud' 共 {n_cloud} 行：归云端持有，"
              "本地改归属可能被下次同步覆盖。")
    else:
        print("  sessions 无 transport 列，跳过。")

    # ---- [8] 隐私 ----
    _print_section(8, "隐私")
    print("  本诊断仅输出条数与字段名，绝不打印任何消息正文。")

    print("\n" + "=" * 72)

    # ---- 可选导出 ----
    if export:
        try:
            _export_diagnostics(cur, accs, verdict, body_index, ts, out_dir)
        except Exception as e:
            print(f"  [!] 导出失败: {e}")

    conn.close()


def _resolve_out_dir(out_dir: str | None) -> Path:
    """导出目录：显式指定则用之；否则默认 Desktop，不存在则回落到 BAK_ROOT。"""
    if out_dir:
        p = Path(out_dir)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return p
    desktop = Path.home() / "Desktop"
    if desktop.is_dir():
        return desktop
    try:
        BAK_ROOT.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return BAK_ROOT


def _export_diagnostics(cur, accs, verdict, body_index, ts: str,
                        out_dir: str | None) -> None:
    """导出三件交付物：CSV 明细 + JSON 指纹基线 + Markdown 摘要（全部 opt-in）。"""
    target = _resolve_out_dir(out_dir)
    print(f"\n[导出] 输出目录: {target}")

    # ① CSV 明细
    csv_path = target / f"WorkBuddy换号诊断_{ts}.csv"
    fields = [c for c in ["id", "user_id", "title", "cwd", "created_at",
                          "last_activity_at", "transport"] if col_exists(cur, "sessions", c)]
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        f.write("session_id,user_id,title,cwd,created_at,last_activity_at,"
                "transport,body_exists,body_path\n")
        import csv as _csv
        w = _csv.writer(f)
        if fields:
            cur.execute(f"SELECT {', '.join(fields)} FROM sessions")
            for r in cur.fetchall():
                sid = str(r["id"]) if "id" in r.keys() and r["id"] is not None else ""
                body_exists = "1" if (sid and sid.lower() in body_index) else "0"
                body_path = body_index.get(sid.lower(), "") if sid else ""
                w.writerow([
                    sid,
                    r["user_id"] if "user_id" in r.keys() else "",
                    r["title"] if "title" in r.keys() else "",
                    r["cwd"] if "cwd" in r.keys() else "",
                    r["created_at"] if "created_at" in r.keys() else "",
                    r["last_activity_at"] if "last_activity_at" in r.keys() else "",
                    r["transport"] if "transport" in r.keys() else "",
                    body_exists,
                    body_path,
                ])
    print(f"  ✓ CSV  → {csv_path}")

    # ② JSON 指纹基线（wal/shm 只记 size/mtime，绝不 sha256）
    db_files = {}
    for name in ("workbuddy.db", "workbuddy.db-wal", "workbuddy.db-shm"):
        p = WORKBUDDY_DIR / name
        if p.exists():
            st = p.stat()
            db_files[name] = {"size": st.st_size, "mtime": int(st.st_mtime)}
    counts = {
        "tables": len([t for t in _all_tables(cur) if not t.startswith("sqlite_")]),
        "accounts": len(accs),
        "sessions_total": None,
        "sessions_cloud": None,
        "body_files": len(body_index),
        "per_account": {a["uid"]: a["total"] for a in accs},
    }
    if table_exists(cur, "sessions"):
        cur.execute("SELECT COUNT(*) c FROM sessions")
        counts["sessions_total"] = cur.fetchone()["c"]
        if col_exists(cur, "sessions", "transport"):
            cur.execute("SELECT COUNT(*) c FROM sessions WHERE transport='cloud'")
            counts["sessions_cloud"] = cur.fetchone()["c"]
    fp = {
        "generated_at": ts,
        "db_main_sha256": _sha256_file(DB_PATH) if DB_PATH.exists() else None,
        "db_files_size_mtime": db_files,
        "counts": counts,
    }
    json_path = target / f"WorkBuddy指纹基线_{ts}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(fp, f, ensure_ascii=False, indent=2)
    print(f"  ✓ JSON → {json_path}")

    # ③ Markdown 摘要
    md = []
    md.append(f"# WorkBuddy 换号诊断摘要\n")
    md.append(f"- 生成时间: {ts}\n")
    md.append(f"- 主库 sha256: `{fp['db_main_sha256']}`\n")
    md.append(f"- 账号数: {counts['accounts']}，会话总数: {counts['sessions_total']}，"
              f"云端持有: {counts['sessions_cloud']}\n")
    md.append("\n## 账号清单\n")
    md.append("| 账号 | 条数 | transport |\n|------|------|-----------|\n")
    for a in accs:
        tb = _transport_breakdown(cur, a["uid"]) or "-"
        md.append(f"| {_short_uid(a['uid'])} | {a['total']} | {tb} |\n")
    md.append("\n## 正文完整性\n")
    md.append(f"- jsonl 正文文件总数: {len(body_index)}\n")
    md.append("\n## 范围说明\n")
    md.append("- 个人版 ↔ 企业版迁移与「个人转个人」同一流程，账号类型不影响迁移逻辑。\n")
    md.append("- storage/ 配置命名空间（偏好/专家缓存）不参与迁移，只改 workbuddy.db 的 user_id。\n")
    snap = _read_account_snapshot()
    if snap:
        md.append(f"- 当前主账号类型: {snap.get('type_label')} ({snap.get('type')})"
                  + (f" / {snap.get('edition')}" if snap.get("edition") else "") + "\n")
    md.append("- transport=cloud 的行归云端持有，本地改归属可能被下次同步覆盖。\n")
    md_path = target / f"WorkBuddy换号诊断_{ts}.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("".join(md))
    print(f"  ✓ MD   → {md_path}")


# ---------------------------------------------------------------- 备份
def _backup(conn) -> Path:
    ts = datetime.datetime.now().strftime(_TS_FMT)
    bak_dir = BAK_ROOT / ts
    if bak_dir.exists():
        for i in range(2, 100):
            c = BAK_ROOT / f"{ts}-{i}"
            if not c.exists():
                bak_dir = c
                break
    bak_dir.mkdir(parents=True, exist_ok=True)
    _checkpoint(conn)

    copied = []
    for name in ("workbuddy.db", "workbuddy.db-wal", "workbuddy.db-shm"):
        src = WORKBUDDY_DIR / name
        if src.exists():
            shutil.copy2(src, bak_dir / name)
            copied.append(f"{name} ({src.stat().st_size} bytes)")
    if SESSIONS_JSON.exists():
        shutil.copy2(SESSIONS_JSON, bak_dir / "sessions.json")
        copied.append(f"sessions.json ({SESSIONS_JSON.stat().st_size} bytes)")

    readme = bak_dir / "恢复说明.txt"
    readme.write_text(
        "WorkBuddy 账号数据恢复 —— 自动备份\n"
        f"备份时间: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"已备份:\n" + "".join(f"  - {c}\n" for c in copied) +
        "\n【如何回滚】\n"
        "1) 完全退出 WorkBuddy（托盘右键 → 完全退出）\n"
        "2) 把本目录下文件复制回覆盖对应位置\n"
        f"   数据库 → {WORKBUDDY_DIR}\n"
        f"   sessions.json → {SESSIONS_JSON}\n"
        "3) 重新打开 WorkBuddy\n"
        "或: python wb_account_restore.py rollback --tag {ts}\n",
        encoding="utf-8",
    )
    print(f"[备份] 已备份到: {bak_dir}")
    for c in copied:
        print(f"        {c}")
    return bak_dir


# ---------------------------------------------------------------- rollback
def rollback(tag: str | None = None, skip_process_check: bool = False) -> None:
    if not BAK_ROOT.exists():
        bail(f"没有找到备份目录: {BAK_ROOT}")
    backups = sorted([d for d in BAK_ROOT.iterdir() if d.is_dir()], reverse=True)
    if not backups:
        bail("备份目录为空。")

    if not tag:
        print("可用备份（最新在前）：")
        for i, d in enumerate(backups, 1):
            print(f"  [{i}] {d.name}")
        print("\n用法: rollback --tag <目录名>")
        return

    target = BAK_ROOT / tag
    if not target.is_dir():
        bail(f"备份不存在: {target}")
    if not skip_process_check:
        st = _is_workbuddy_running()
        if st is None:
            bail("无法确认 WorkBuddy 是否已退出（进程检测失败）；为安全起见已中止，"
                 "请完全退出后重试。", code=EXIT_ENV)
        if st:
            bail("WorkBuddy 仍在运行，回滚前请完全退出（托盘右键→完全退出）。", code=EXIT_ENV)

    # 回滚前先另存当前状态
    safety = BAK_ROOT / f"{datetime.datetime.now():%Y%m%d-%H%M%S}-before-rollback"
    safety.mkdir(parents=True, exist_ok=True)
    for name in ("workbuddy.db", "workbuddy.db-wal", "workbuddy.db-shm"):
        src = WORKBUDDY_DIR / name
        if src.exists():
            shutil.copy2(src, safety / name)
    print(f"[保险] 当前状态已另存: {safety}")

    restored = []
    for name in ("workbuddy.db", "workbuddy.db-wal", "workbuddy.db-shm"):
        src = target / name
        dst = WORKBUDDY_DIR / name
        if src.exists():
            shutil.copy2(src, dst)
            restored.append(name)
        elif dst.exists():
            dst.unlink()

    # 恢复 sessions.json
    bak_sj = target / "sessions.json"
    if bak_sj.exists():
        shutil.copy2(bak_sj, SESSIONS_JSON)
        restored.append("sessions.json")

    print(f"[完成] 已回滚: {', '.join(restored)}")
    try:
        conn = connect()
        if _integrity_check(conn):
            print("  ✓ 回滚后 integrity_check 通过")
        conn.close()
    except Exception as e:
        print(f"  [warn] 回滚后校验失败: {e}")
    print("\n请重新打开 WorkBuddy 验证。")


# ---------------------------------------------------------------- restore
def restore(from_id: str, to_id: str, yes: bool = False,
            skip_process_check: bool = False, skip_cloud: bool = False,
            dry_run: bool = False) -> None:
    if not from_id or not to_id:
        bail("必须提供来源与目标 user_id", code=EXIT_USAGE)
    if from_id == to_id:
        bail("来源与目标相同，无需迁移。", code=EXIT_USAGE)
    if not skip_process_check:
        st = _is_workbuddy_running()
        if st is None:
            bail("无法确认 WorkBuddy 是否已退出（进程检测失败）；为安全起见已中止，"
                 "请完全退出后重试。", code=EXIT_ENV)
        if st:
            bail("WorkBuddy 仍在运行！必须完全退出（托盘右键→完全退出）后重跑。", code=EXIT_ENV)

    conn = connect()
    cur = conn.cursor()
    total_changed = 0
    cloud_total = 0
    has_transport = col_exists(cur, "sessions", "transport")
    plan = []
    for table, field in TABLES_FIELD:
        if not table_exists(cur, table) or not col_exists(cur, table, field):
            continue
        cur.execute(f"SELECT COUNT(*) c FROM {table} WHERE {field}=?", (from_id,))
        n = cur.fetchone()["c"]
        if n:
            plan.append(f"  {table}.{field}: {from_id} → {to_id}  ({n} 行)")
            total_changed += n
        if table == "sessions" and has_transport:
            cur.execute(
                "SELECT COUNT(*) c FROM sessions WHERE user_id=? AND COALESCE(transport,'')='cloud'",
                (from_id,))
            cloud_total = cur.fetchone()["c"]

    if not plan:
        conn.close()
        bail(f"没有找到属于 {from_id} 的可迁移数据。", code=EXIT_NO_DATA)

    print("=" * 72)
    print(f"恢复计划：{from_id}  →  {to_id}")
    print("=" * 72)
    for line in plan:
        print(line)
    print(f"共影响 {total_changed} 行数据库记录。")
    # 无论开关如何都要打印云端提示
    print(f"其中 transport=cloud 的 {cloud_total} 行归云端持有，"
          "本地改归属可能被下次同步覆盖（加 --skip-cloud 可跳过）")
    if dry_run:
        print("\n[dry-run] 未做任何修改")
        conn.close()
        return

    if not yes:
        print("\n确认恢复？输入 yes 继续：", end="")
        try:
            ans = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            conn.close()
            bail("非交互环境无法确认（检测到输入结束/中断），已取消；"
                 "如需跳过确认请加 --yes。", code=EXIT_USAGE)
        if ans not in ("yes", "y", "是"):
            conn.close()
            bail("已取消。")

    # ① 完整性校验
    print("\n[①校验] 迁移前 integrity_check ...")
    if not _integrity_check(conn):
        conn.close()
        bail("完整性检查未通过，已中止。")
    print("  ✓ 通过")

    # ② 备份
    bak_dir = _backup(conn)

    # ③ 原子事务改归属
    print("\n[③执行] 原子事务更新归属 ...")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table, field in TABLES_FIELD:
            if not table_exists(cur, table) or not col_exists(cur, table, field):
                continue
            sql = f"UPDATE {table} SET {field}=? WHERE {field}=?"
            if skip_cloud and col_exists(cur, table, "transport"):
                sql += " AND COALESCE(transport,'') <> 'cloud'"
            cur.execute(sql, (to_id, from_id))
            print(f"  ✓ {table}.{field} 更新 {cur.rowcount} 行")
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        bail(f"迁移出错已回滚: {e}\n备份仍在: {bak_dir}")

    # ⑤ 源归零验证
    print("\n[⑤验证] 源账号数据是否迁移完整 ...")
    residual = 0
    for table, field in TABLES_FIELD:
        if not table_exists(cur, table) or not col_exists(cur, table, field):
            continue
        sql = f"SELECT COUNT(*) c FROM {table} WHERE {field}=?"
        if skip_cloud and col_exists(cur, table, "transport"):
            sql += " AND COALESCE(transport,'') <> 'cloud'"
        cur.execute(sql, (from_id,))
        left = cur.fetchone()["c"]
        if left:
            print(f"  [!] {table}.{field} 残留 {left} 行")
            residual += left
    print("  ✓ 源账号已归零" if residual == 0 else f"  [!] 残留 {residual} 行（含被 --skip-cloud 跳过的云端行）")

    # ⑥ 落盘
    print("\n[⑥落盘] WAL checkpoint ...")
    _checkpoint(conn)
    if _integrity_check(conn):
        print("  ✓ 迁移后完整")
    conn.close()

    # ⑦ sessions.json 原子写入 —— 失败不静默提示
    try:
        _rewrite_sessions_json(from_id, to_id)
    except Exception as e:
        print(f"  [!] sessions.json 更新失败: {e}")
        print("      ⚠ 数据库已迁移，但会话索引未同步。请立即回滚：")
        print(f"         python wb_account_restore.py rollback --tag {bak_dir.name}")

    print("\n" + "=" * 72)
    print("恢复完成。下一步：")
    print("  1) 重新打开 WorkBuddy → 左侧任务/空间显示全部历史")
    print(f"  2) 如需回滚: python wb_account_restore.py rollback --tag {bak_dir.name}")
    print("=" * 72)


# ---------------------------------------------------------------- migrate
def _migrate_log(msg: str, log_path: Path) -> None:
    """追加一行迁移日志（供 WorkBuddy 重启后读取确认结果）。"""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _has_migratable_data(from_id: str) -> bool:
    """只读预检：源账号在迁移覆盖的 3 张表里是否仍有可迁移行。

    用于 migrate 杀进程【之前】的幂等拦截——返回 False 表示源账号已无数据可迁，
    应直接拒绝，绝不下 taskkill（避免"关掉 WB 却不重启"）。
    """
    conn = connect(ro=True)
    try:
        cur = conn.cursor()
        for table, field in TABLES_FIELD:
            if not table_exists(cur, table) or not col_exists(cur, table, field):
                continue
            cur.execute(f"SELECT COUNT(*) c FROM {table} WHERE {field}=?", (from_id,))
            if cur.fetchone()["c"]:
                return True
        return False
    finally:
        conn.close()


def migrate(from_id: str, to_id: str, no_restart: bool = False,
            skip_cloud: bool = False, dry_run: bool = False) -> None:
    """聊天内触发的自动迁移（独立进程）：杀WB→等WAL落盘→改库→重启WB。

    由 AI 通过 PowerShell Start-Process 启动为独立进程，脱离 WB 进程树，
    因此 taskkill WorkBuddy 不会连带杀死本脚本。

    可靠性保障（v3.1.0）：
      · 杀进程【前】只读幂等预检——源账号已无可迁移数据则直接拒绝，绝不关闭 WorkBuddy；
      · 重启逻辑置于 try/finally——只要执行过 taskkill，任何中止/异常路径都保证把 WB 拉起。
    """
    log_path = BAK_ROOT / "latest-migrate.log"
    if not from_id or not to_id:
        bail("必须提供 --from <旧uid> 与 --to <新uid>。先运行 detect 查看账号。",
             code=EXIT_USAGE)
    if from_id == to_id:
        bail("来源与目标相同，无需迁移。", code=EXIT_USAGE)

    # dry-run 必须在杀进程【之前】返回：只走校验与计划打印。
    if dry_run:
        _migrate_log("migrate --dry-run（不杀进程、不改库）", log_path)
        print("[dry-run] 仅预演，不杀进程、不改库、不重启。")
        restore(from_id, to_id, yes=True, skip_process_check=True,
                skip_cloud=skip_cloud, dry_run=True)
        return

    _migrate_log(f"migrate from={_short_uid(from_id)} to={_short_uid(to_id)}"
                 + (" skip_cloud" if skip_cloud else ""), log_path)

    # 0. 【幂等预检·只读】源账号是否仍有可迁移数据？无 → 杀进程前直接拒绝。
    #    修复：旧版对已迁移账号重跑会先 taskkill，再在 restore 内 bail，
    #    结果 WorkBuddy 已被杀掉却永远不重启（P0）。
    if not _has_migratable_data(from_id):
        msg = (f"账号 {_short_uid(from_id)} 已无可迁移数据，可能已迁移过；"
               "未做任何改动，WorkBuddy 未被关闭。")
        _migrate_log(f"幂等预检未通过，已在杀进程前中止：{msg}", log_path)
        print(f"[跳过] {msg}")
        sys.exit(EXIT_NO_DATA)

    # 1. 检测 WB 安装路径（供重启用）
    wb_exe = _find_wb_exe()
    _migrate_log(f"WB exe = {wb_exe or '未找到（将跳过重启）'}", log_path)

    # taskkill 之后的操作全部包进 try/finally：任何中止路径都要把 WB 拉起
    wb_closed = False
    try:
        # 2. taskkill WorkBuddy
        print("[① 杀进程] 关闭 WorkBuddy ...")
        try:
            subprocess.run(["taskkill", "/f", "/im", "WorkBuddy.exe"],
                           capture_output=True, text=False)
            _migrate_log("taskkill WorkBuddy.exe OK", log_path)
        except Exception as e:
            _migrate_log(f"taskkill 失败(继续，若WB未运行): {e}", log_path)
        print("  ✓ 已发出关闭命令")
        wb_closed = True

        # 3. 轮询确认进程真正退出（最多等 15 秒，通常 1-3 秒）
        print("[② 等待退出] 确认 WorkBuddy 完全退出 ...")
        exited_in = None
        for i in range(1, 16):
            time.sleep(1)
            if _is_workbuddy_running() is False:  # None=检测失败，保守视为"可能仍在运行"，继续等
                exited_in = i
                break
        if exited_in:
            _migrate_log(f"进程已完全退出（等待 {exited_in}s）", log_path)
            print(f"  ✓ 进程已完全退出（{exited_in} 秒）")
        else:
            # 超时未确认退出 → 中止改库（零数据改动），但仍在 finally 里重启 WB + 告警（P0）
            msg = ("⚠ 15 秒内未能确认 WorkBuddy 完全退出，已中止迁移（未改动任何数据），"
                   "并已重新启动 WorkBuddy；请完全退出后重试")
            _migrate_log(msg, log_path)
            print(f"  [!] {msg}")
            sys.exit(EXIT_ENV)

        # 4. 额外等待 WAL 落盘
        print("[③ 落盘] 等待 3 秒让 WAL 完全落盘 ...")
        time.sleep(3)
        _migrate_log("sleep 3 落盘 OK", log_path)

        # 4-9. 复用 restore() 全部安全门（其内部已含：wal_checkpoint→integrity→备份→原子事务→源归零→sessions.json）
        print("[④ 迁移] 执行数据迁移（含备份+原子事务+完整性校验）...")
        try:
            restore(from_id, to_id, yes=True, skip_process_check=True, skip_cloud=skip_cloud)
            _migrate_log("restore 完成，备份已生成", log_path)
        except SystemExit as e:
            _migrate_log(f"migrate 中止（restore 未完成）: {e}", log_path)
            raise
        except Exception as e:
            _migrate_log(f"migrate 失败: {e}", log_path)
            raise
    finally:
        # 【兜底】只要执行过 taskkill（WB 可能已被关闭），任何中止/异常路径都保证重启 WB
        if wb_closed and not no_restart:
            if wb_exe:
                print("[⑤ 重启] 重新启动 WorkBuddy ...")
                try:
                    subprocess.Popen([wb_exe], shell=False)
                    _migrate_log(f"restart WorkBuddy OK: {wb_exe}", log_path)
                except Exception as e:
                    _migrate_log(f"restart 失败(请手动启动): {e}", log_path)
                    print(f"  [!] 自动重启失败，请手动启动: {wb_exe}")
            else:
                _migrate_log("未找到 WB 安装路径，跳过重启，请手动启动", log_path)
                print("  [!] 未找到 WorkBuddy 安装路径，请手动启动。")

    print("\n" + "=" * 72)
    print("迁移完成。")
    if no_restart:
        print("  (--no-restart 已跳过重启)")
    else:
        print("  WorkBuddy 已重启，请扫码登录目标账号查看全部历史。")
    print(f"  迁移日志: {log_path}")
    print("=" * 72)


def _rewrite_sessions_json(from_id: str, to_id: str) -> None:
    if not SESSIONS_JSON.exists():
        return
    try:
        with open(SESSIONS_JSON, encoding="utf-8") as f:
            data = json.load(f)
        changed = 0
        for s in data.get("sessions", []):
            if s.get("userId") == from_id:
                s["userId"] = to_id
                changed += 1
        tmp = SESSIONS_JSON.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(str(tmp), str(SESSIONS_JSON))
        print(f"  ✓ app/sessions.json 更新 {changed} 条 userId（原子写入）")
    except Exception as e:
        print(f"  [warn] sessions.json 更新失败: {e}")


# ---------------------------------------------------------------- rollback_auto
def rollback_auto(tag: str, no_restart: bool = False) -> None:
    """聊天内触发的自动回滚（独立进程）：杀WB→等退出→回滚→重启WB。

    由 AI 通过 PowerShell Start-Process 启动为独立进程，脱离 WB 进程树，
    因此 taskkill WorkBuddy 不会连带杀死本脚本。

    可靠性保障（v3.1.0）：重启逻辑置于 try/finally——只要执行过 taskkill，
    任何中止/异常路径都保证把 WB 拉起；超时未确认退出则中止回滚但重启 WB。
    """
    log_path = BAK_ROOT / "latest-migrate.log"
    if not tag:
        bail("必须提供 --tag <备份目录名>。先运行 rollback 查看可用备份。",
             code=EXIT_USAGE)
    _migrate_log(f"rollback_auto tag={tag}", log_path)

    # 1. 检测 WB 安装路径（供重启用）
    wb_exe = _find_wb_exe()
    _migrate_log(f"WB exe = {wb_exe or '未找到（将跳过重启）'}", log_path)

    wb_closed = False
    try:
        # 2. taskkill WorkBuddy
        print("[① 杀进程] 关闭 WorkBuddy ...")
        try:
            subprocess.run(["taskkill", "/f", "/im", "WorkBuddy.exe"],
                           capture_output=True, text=False)
            _migrate_log("taskkill WorkBuddy.exe OK", log_path)
        except Exception as e:
            _migrate_log(f"taskkill 失败(继续，若WB未运行): {e}", log_path)
        print("  ✓ 已发出关闭命令")
        wb_closed = True

        # 3. 轮询确认进程真正退出（最多 15 秒）
        print("[② 等待退出] 确认 WorkBuddy 完全退出 ...")
        exited_in = None
        for i in range(1, 16):
            time.sleep(1)
            if _is_workbuddy_running() is False:  # None=检测失败，保守视为"可能仍在运行"，继续等
                exited_in = i
                break
        if exited_in:
            _migrate_log(f"进程已完全退出（等待 {exited_in}s）", log_path)
            print(f"  ✓ 进程已完全退出（{exited_in} 秒）")
        else:
            msg = ("⚠ 15 秒内未能确认 WorkBuddy 完全退出，已中止回滚（未改动任何数据），"
                   "并已重新启动 WorkBuddy；请完全退出后重试")
            _migrate_log(msg, log_path)
            print(f"  [!] {msg}")
            sys.exit(EXIT_ENV)

        # 4. 额外等待 WAL 落盘
        print("[③ 落盘] 等待 3 秒让 WAL 完全落盘 ...")
        time.sleep(3)
        _migrate_log("sleep 3 落盘 OK", log_path)

        # 5. 执行回滚（跳过进程检查）
        print("[④ 回滚] 执行数据回滚 ...")
        try:
            rollback(tag, skip_process_check=True)
            _migrate_log("rollback 完成", log_path)
        except SystemExit as e:
            _migrate_log(f"rollback_auto 中止: {e}", log_path)
            raise
        except Exception as e:
            _migrate_log(f"rollback_auto 失败: {e}", log_path)
            raise
    finally:
        # 【兜底】只要执行过 taskkill（WB 可能已被关闭），任何中止/异常路径都保证重启 WB
        if wb_closed and not no_restart:
            if wb_exe:
                print("[⑤ 重启] 重新启动 WorkBuddy ...")
                try:
                    subprocess.Popen([wb_exe], shell=False)
                    _migrate_log(f"restart WorkBuddy OK: {wb_exe}", log_path)
                except Exception as e:
                    _migrate_log(f"restart 失败(请手动启动): {e}", log_path)
                    print(f"  [!] 自动重启失败，请手动启动: {wb_exe}")
            else:
                _migrate_log("未找到 WB 安装路径，跳过重启，请手动启动", log_path)
                print("  [!] 未找到 WorkBuddy 安装路径，请手动启动。")

    print("\n" + "=" * 72)
    print("回滚完成。")
    if no_restart:
        print("  (--no-restart 已跳过重启)")
    else:
        print("  WorkBuddy 已重启，请扫码登录查看状态。")
    print(f"  迁移日志: {log_path}")
    print("=" * 72)


# ---------------------------------------------------------------- main
def main() -> None:
    p = argparse.ArgumentParser(description="WorkBuddy 账号数据恢复（同机换号）")
    sub = p.add_subparsers(dest="cmd", required=False)

    sd = sub.add_parser("detect", help="查看账号分布（只读）")
    sd.add_argument("--cwd", dest="cwd", default=None,
                    help="本次会话的工作区绝对路径（cwd 自证，提高账号识别准确度）")
    sd.set_defaults(fn=detect)

    sdi = sub.add_parser("diagnose", help="只读深度诊断（推荐先跑，全程只读不修改数据）")
    sdi.add_argument("--cwd", dest="cwd", default=None,
                     help="本次会话的工作区绝对路径（cwd 自证，提高账号识别准确度）")
    sdi.add_argument("--export", action="store_true",
                     help="导出诊断交付物（CSV + 指纹基线 JSON + Markdown 摘要）")
    sdi.add_argument("--out-dir", dest="out_dir", default=None,
                     help="导出目录（默认 ~/Desktop，目录不存在则回落到备份根目录）")
    sdi.set_defaults(fn=diagnose)

    sr = sub.add_parser("restore", help="交互式恢复（确认旧→新后执行）")
    sr.add_argument("--from", dest="from_id", help="旧账号 user_id")
    sr.add_argument("--to", dest="to_id", help="新账号 user_id")
    sr.add_argument("--yes", action="store_true", help="跳过确认直接执行")
    sr.add_argument("--skip-cloud", dest="skip_cloud", action="store_true",
                    help="跳过 transport=cloud 的行（默认不跳过，保持原行为）")
    sr.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="只预演迁移计划，不备份、不改库、不重启")
    sr.set_defaults(fn=restore)

    sm = sub.add_parser("migrate", help="聊天内触发的自动迁移（杀WB→改库→重启WB，独立进程）")
    sm.add_argument("--from", dest="from_id", help="旧账号 user_id")
    sm.add_argument("--to", dest="to_id", help="新账号 user_id")
    sm.add_argument("--no-restart", action="store_true", help="迁移后不重启 WorkBuddy（测试用）")
    sm.add_argument("--skip-cloud", dest="skip_cloud", action="store_true",
                    help="跳过 transport=cloud 的行（默认不跳过，保持原行为）")
    sm.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="只预演迁移计划，不杀进程、不改库、不重启")
    sm.set_defaults(fn=migrate)

    srb = sub.add_parser("rollback", help="从备份回滚")
    srb.add_argument("--tag", dest="tag", help="备份目录名；省略则列出")
    srb.set_defaults(fn=rollback)

    sra = sub.add_parser("rollback_auto", help="聊天内自动回滚（杀WB→等退出→回滚→重启WB，独立进程）")
    sra.add_argument("--tag", dest="tag", help="备份目录名")
    sra.add_argument("--no-restart", action="store_true", help="回滚后不重启 WorkBuddy（测试用）")
    sra.set_defaults(fn=rollback_auto)

    sg = sub.add_parser("guide", help="启动引导（显示步骤说明+选择操作）")
    sg.set_defaults(fn=guide)

    args = p.parse_args()

    if args.cmd == "restore":
        from_id = args.from_id
        to_id = args.to_id
        # 无 from/to 参数 → 走引导式（多账号智能推荐）
        if not from_id or not to_id:
            _restore_guided()
        else:
            restore(from_id, to_id, yes=args.yes, skip_cloud=args.skip_cloud,
                    dry_run=args.dry_run)
    elif args.cmd == "migrate":
        migrate(args.from_id, args.to_id, no_restart=args.no_restart,
                skip_cloud=args.skip_cloud, dry_run=args.dry_run)
    elif args.cmd == "rollback":
        rollback(args.tag)
    elif args.cmd == "rollback_auto":
        rollback_auto(args.tag, no_restart=args.no_restart)
    elif args.cmd == "detect":
        detect(getattr(args, "cwd", None))
    elif args.cmd == "diagnose":
        diagnose(getattr(args, "cwd", None), export=args.export, out_dir=args.out_dir)
    elif args.cmd == "guide":
        guide()
    else:
        # 无参数 → 启动引导
        guide()


def _generate_desktop_manual() -> str | None:
    """在桌面生成操作手册 DOCX（退出 WorkBuddy 后用户可照着做）。返回文件路径或 None。"""
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
    except ImportError:
        return None  # python-docx 未安装则跳过

    from pathlib import Path as _P
    here = _P(__file__).resolve().parent
    _home = _P.home().as_posix().rstrip("/")
    here_tilde = here.as_posix()
    if here_tilde.startswith(_home):
        here_tilde = "~" + here_tilde[len(_home):]

    # 桌面路径（跨平台）
    import sys
    if sys.platform == "win32":
        desktop = _P.home() / "Desktop"
    elif sys.platform == "darwin":
        desktop = _P.home() / "Desktop"
    else:
        desktop = _P.home()
    desktop.mkdir(parents=True, exist_ok=True)
    out = desktop / "WorkBuddy账号数据同步操作手册.docx"

    doc = Document()

    # 标题
    t = doc.add_heading("WorkBuddy 账号数据同步操作手册", level=0)
    t.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # 适用范围
    doc.add_heading("适用范围", level=1)
    p = doc.add_paragraph()
    r = p.add_run("支持多账号识别并推荐迁移目标（默认一次迁移一对账号，其余账号数据保持不动）。")
    r.bold = True
    r.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
    r.font.size = Pt(12)
    doc.add_paragraph(
        "场景：同一台电脑上切换账号后，把旧账号A的历史会话/任务归属转到新账号B，"
        "让B登录后能看到A的历史。"
    )
    doc.add_paragraph(
        "本工具不处理：技能库(全局共享)、连接器/凭证(本机已有)、"
        "项目级记忆(跟项目走)、跨机路径差异(非本次定位)。"
    )

    # 术语
    doc.add_heading("先弄清两个账号（关键，别搞反）", level=1)
    tbl = doc.add_table(rows=3, cols=3, style="Light Grid Accent 1")
    for i, (a, b, c) in enumerate([
        ("名称", "含义", "怎么确定"),
        ("旧账号 A (from)", "历史数据所在的那个", "脚本自动标注'数据最多(旧主号)'"),
        ("新账号 B (to)", "你希望看到历史的那个", "脚本自动标注'★当前登录'"),
    ]):
        tbl.rows[i].cells[0].text = a
        tbl.rows[i].cells[1].text = b
        tbl.rows[i].cells[2].text = c
    doc.add_paragraph(
        "你不需要记 user_id 字符串。脚本会列出所有账号，自动标注旧/新，"
        "智能推荐后你只需回车确认（或改序号）。"
    )

    # 操作步骤
    doc.add_heading("操作步骤（退出 WorkBuddy 后照这个做）", level=1)

    # Step 1 —— 完全退出（红色醒目警告）
    doc.add_heading("Step 1. 完全退出 WorkBuddy", level=2)
    warn1 = doc.add_paragraph()
    r1 = warn1.add_run(
        "【注意】完全退出 ≠ 点右上角 X 关闭窗口！\n"
        "点 X 只是关闭窗口，WorkBuddy 仍在后台运行，数据库仍被锁定！"
    )
    r1.bold = True
    r1.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
    r1.font.size = Pt(12)
    doc.add_paragraph(
        "正确做法：\n"
        "  Windows：右下角托盘（系统时钟旁边的隐藏图标区域 ^）\n"
        "           → 找到 WorkBuddy 图标 → 右键 → 完全退出\n"
        "  Mac：右上角菜单栏 WorkBuddy 图标 → 退出（或 Cmd+Q）\n\n"
        "此时通常还登着旧账号A（刚在用的），直接完全退出即可。\n"
        "不强制登哪个号，只是让数据库安全写入。"
    )
    warn1b = doc.add_paragraph()
    r1b = warn1b.add_run(
        "如何确认已完全退出：Windows 任务管理器里搜 WorkBuddy.exe 应该找不到；\n"
        "Mac 活动监视器里搜 WorkBuddy 应该无进程。"
    )
    r1b.italic = True
    r1b.font.size = Pt(10)

    # Step 2
    doc.add_heading("Step 2. 打开 WorkBuddy 登录新账号B", level=2)
    doc.add_paragraph(
        "扫码/账密登录账号B（想让历史归过去的那个）。\n"
        "只有登录B后数据库里才出现B的 user_id。"
    )

    # Step 3 —— 再次完全退出（红色醒目警告）
    doc.add_heading("Step 3. 再次完全退出 WorkBuddy", level=2)
    warn3 = doc.add_paragraph()
    r3 = warn3.add_run(
        "同 Step 1，必须从托盘/菜单栏完全退出，不是点 X 关窗口！\n"
        "这一步不能省，否则数据库仍可能在写入。"
    )
    r3.bold = True
    r3.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
    r3.font.size = Pt(11)

    # Step 4
    doc.add_heading("Step 4. 运行恢复脚本（不在聊天窗口里）", level=2)
    doc.add_paragraph(
        "Windows：双击桌面或技能目录里的「账号恢复一键.bat」\n"
        f"Mac/Linux：打开终端，输入：\n"
        f"  cd \"{here_tilde}\"\n"
        "  python3 wb_account_restore.py\n\n"
        "脚本进入引导菜单，选 [2] 执行数据恢复：\n"
        " - 自动列出账号（标注旧/新）\n"
        " - 智能推荐 from/to，回车确认或改序号\n"
        " - 确认后自动：备份→迁移→验证→落盘"
    )

    # Step 5
    doc.add_heading("Step 5. 重新打开 WorkBuddy 登录账号B", level=2)
    doc.add_paragraph(
        "左侧任务/空间即显示旧账号的全部历史。\n"
        "注意：最后这一步要登B查看，不是登A。"
    )

    # 重要提醒
    doc.add_heading("重要提醒", level=1)
    warnings = [
        "本工具是独立脚本，不在 WorkBuddy 聊天窗口里运行。"
        "登录新账号后聊天窗口变空是正常的，不影响脚本操作。",
        "每次迁移前脚本自动备份数据库到 backup/ 目录，可随时回滚。",
        "迁移是原子事务，失败自动回滚，数据库不会被改坏。",
        "如果选反了 from/to，用 rollback 命令即可回滚。",
    ]
    for w in warnings:
        p = doc.add_paragraph(style="List Bullet")
        p.add_run(w)

    # 回滚
    doc.add_heading("回滚（恢复错了用）", level=1)
    doc.add_paragraph(
        "运行脚本，选 [3] 从备份回滚，或在终端执行：\n"
        "  python wb_account_restore.py rollback\n"
        "列出可用备份后：\n"
        "  python wb_account_restore.py rollback --tag <备份目录名>"
    )

    # 脚本位置
    doc.add_heading("脚本位置", level=1)
    doc.add_paragraph(f"脚本目录：{here}")
    doc.add_paragraph(
        "  - wb_account_restore.py  核心脚本\n"
        "  - run_restore.py         Mac/Linux 启动器\n"
        "  - 账号恢复一键.bat        Windows 双击启动"
    )

    doc.save(str(out))
    return str(out)


def guide() -> None:
    """启动引导：显示操作步骤说明（含脚本位置与Mac操作），让用户选择操作。"""
    from pathlib import Path as _P
    here = _P(__file__).resolve().parent  # 当前脚本真实目录（跨平台动态）
    # Mac/Linux 用家目录简写 ~，Windows 用实际路径；自动替换
    _home = _P.home().as_posix().rstrip("/")
    here_tilde = here.as_posix()
    if here_tilde.startswith(_home):
        here_tilde = "~" + here_tilde[len(_home):]

    # 生成桌面操作手册
    manual = _generate_desktop_manual()
    if manual:
        print(f"[已生成] 操作手册已保存到桌面：{manual}")
        print("         退出后可照着手册步骤操作。")
        print("")

    print("!" * 72)
    print("!!!  推荐在 WorkBuddy 聊天里触发「workbuddy数据同步」  !!!")
    print("!!!  会自动检测账号→智能推荐→确认后迁移并重启，无需手动退出  !!!")
    print("!" * 72)
    print("")
    print("=" * 72)
    print("WorkBuddy 账号数据恢复 —— 同机换号引导")
    print("=" * 72)
    print("")
    print("【用途】同一台电脑退出账号A、登录账号B后")
    print("        把账号A的历史会话/任务归属转到账号B")
    print("        让新账号登录后重新看到旧账号的任务/空间")
    print("")
    print("【操作步骤】")
    print("  ① 完全退出 WorkBuddy")
    print("     ╔══════════════════════════════════════════════════════╗")
    print("     ║  完全退出 ≠ 点右上角 X 关闭窗口！                   ║")
    print("     ║  点 X 只是关窗口，WorkBuddy 仍在后台运行！          ║")
    print("     ║  正确做法：                                         ║")
    print("     ║  Windows → 右下角托盘隐藏图标(^) → WorkBuddy → 右键 ║")
    print("     ║            → 完全退出                               ║")
    print("     ║  Mac → 右上角菜单栏 WorkBuddy 图标 → 退出           ║")
    print("     ╚══════════════════════════════════════════════════════╝")
    print("     · 此时通常还登着【旧账号A】(刚在用的)，直接完全退出即可")
    print("     · 不强制登哪个号，只是让数据库安全写入")
    print("  ② 打开 WorkBuddy 登录【新账号B】(想让历史归过去的那个)")
    print("     · 必须是B，且B是你想接收旧历史的账号")
    print("  ③ 再次完全退出 WorkBuddy（同步骤①，从托盘/菜单栏退出，不是点X！）")
    print("  ④ 运行本脚本恢复，选 [2] 执行数据恢复")
    print("     · 脚本会让你选【旧账号=from】和【新账号=to】")
    print("     · 旧账号填A(数据要迁出的)，新账号填B(数据要迁入的)")
    print("  ⑤ 重新打开 WorkBuddy 登录【新账号B】，左侧即显示旧账号的全部历史")
    print("")
    print("【账号核对(最关键,别搞反)】")
    print("  · from(旧/被迁出) = 账号A = 历史数据所在的那个账号")
    print("  · to(新/被迁入)   = 账号B = 你希望看到历史数据的那个账号")
    print("  · 如果填反了，历史会被迁到另一个账号，用 [3] 回滚即可")
    print("")
    print("【本脚本在哪？怎么运行？】(本机实际路径如下)")
    print(f"  脚本目录：{here}")
    print("    - wb_account_restore.py   核心脚本（本程序）")
    print("    - run_restore.py          Mac/Linux 启动器")
    print("    - 账号恢复一键.bat         Windows 双击启动")
    print("")
    print("  ▶ Windows：双击上面的『账号恢复一键.bat』即可")
    print("  ▶ Mac/Linux：打开终端(访达→实用工具→终端)，输入：")
    print(f"       cd \"{here_tilde}\"")
    print("       python3 wb_account_restore.py")
    print("     或直接一步到位：")
    print(f"       python3 \"{here_tilde}/run_restore.py\"")
    print("")
    print("   提示：Mac/Linux 里 ~ 表示用户家目录，")
    print("         上面已自动帮你写成 ~ 简写，直接复制粘贴即可。")
    print("")
    print("【注意】必须在 WorkBuddy 已完全退出的状态下运行，否则会终止")
    print("")
    print("=" * 72)
    print("请选择操作：")
    print("  [1] 查看账号分布（只读，确认旧/新账号）")
    print("  [2] 执行数据恢复（把旧账号数据合并给新账号）")
    print("  [3] 从备份回滚（恢复错了用）")
    print("  [4] 深度诊断（只读，推荐先跑）")
    print("  [0] 退出")
    print("=" * 72)
    while True:
        try:
            sel = input("\n请输入序号: ").strip()
            if sel == "1":
                detect()
            elif sel == "2":
                _restore_guided()
            elif sel == "3":
                rollback(None)
            elif sel == "4":
                diagnose()
            elif sel == "0" or sel == "":
                print("已退出。")
                return
            else:
                print("无效输入，请输入 0-4。")
                continue
            # 操作完成后询问是否继续
            again = input("\n还要做别的吗？[Y/n]: ").strip().lower()
            if again in ("n", "no", "否"):
                print("已退出。")
                return
        except (KeyboardInterrupt, EOFError):
            print("\n已退出。")
            return


def _restore_guided() -> None:
    """引导式恢复：智能推荐旧/新账号（自动填好），用户核对确认即可。"""
    conn = connect(ro=True)
    cur = conn.cursor()
    accs = _collect_accounts(cur)
    if not accs:
        conn.close()
        bail("库中没有账号数据。")

    verdict = _detect_recent(cur)
    print("\n[当前账号列表]")
    _print_acc_list(cur, accs, verdict)

    # 智能推荐（多账号）
    from_id, to_id, advise = _smart_recommend_multi(accs, verdict)
    old_idx = next((i for i, a in enumerate(accs) if a["uid"] == from_id), 0) + 1
    if to_id:
        to_idx = next((i for i, a in enumerate(accs) if a["uid"] == to_id), 0) + 1
    else:
        to_idx = 0  # 无法自动判定，序号为 0，必须由用户手动指定

    print("\n【智能推荐，请核对】(不对可改序号)")
    print(f"  旧账号(迁出/历史所在) = 序号{old_idx}  → {from_id}")
    if to_id:
        print(f"  新账号(迁入/想看历史) = 序号{to_idx}  → {to_id}")
    else:
        print("  新账号(迁入/想看历史) = ⚠ 无法自动判定")
        print("  请手动输入『旧序号 新序号』两个数字来指定目标")
    print("  · 说明：旧账号通常=数据最多的那个；新账号通常=最近活动的那个")
    print("  · 判断依据：你想让【哪个账号】看到历史，它就是新账号to")
    for a in advise:
        print(f"  [提示] {a}")
    for c in verdict["conflicts"]:
        print(f"  [提示] {c}")
    print("=" * 72)

    prompt = "  直接回车用推荐值，或输入『旧序号 新序号』两个数字空格分隔: "
    if to_idx == 0:
        prompt = "  请手动输入『旧序号 新序号』两个数字来指定目标: "
    try:
        sel = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        return
    if sel:
        parts = sel.split()
        if len(parts) == 2:
            try:
                old_idx = int(parts[0])
                to_idx = int(parts[1])
            except ValueError:
                bail("输入无效，请输入两个数字。")
                return
        else:
            bail("输入无效，请输入两个数字（空格分隔）。")
            return
    if to_idx == 0:
        bail("未提供有效的新账号序号，已取消。请用『旧序号 新序号』指定目标。")
        return
    if not (1 <= old_idx <= len(accs) and 1 <= to_idx <= len(accs)):
        bail("序号越界。")
        return
    from_id = accs[old_idx - 1]["uid"]
    to_id = accs[to_idx - 1]["uid"]
    if from_id == to_id:
        bail("旧账号和新账号不能相同。")
        return
    conn.close()

    # 最终核对（防填反）
    print("\n" + "=" * 72)
    print("【再次确认，别搞反】")
    print(f"  from(旧/迁出) = {from_id}")
    print(f"  to  (新/迁入) = {to_id}")
    print("  → 恢复后，上面『from』的历史会显示在『to』账号里")
    print("=" * 72)
    ans = input("确认无误？(yes继续/其他取消): ").strip().lower()
    if ans not in ("yes", "y", "是"):
        bail("已取消。")
        return
    restore(from_id, to_id, yes=False)


if __name__ == "__main__":
    main()
