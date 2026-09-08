#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ashare_registry_migrate.py —— 删除前的数据资产迁出（幂等）。

为什么需要这一步
----------------
`strategy_registry` 里混装了三类东西，物理删除判死条目时会误伤前两类：

1. **实测统计量** `gate1_sigma_true` —— 功效预检的输入（`ashare_newpool_preflight.py`）。
   它是"这个池的横截面离散度有多大"的**实测事实**，与"某个策略能不能赚钱"无关。
   策略判死了，σ_true 依然是真值。
2. **风险标签** `agri_np_yoy_neglist` / `csi800_margin_crowding_neglist`
   —— 风控 overlay，从未打算过第三关（不产生超额收益，判据是"标记脆弱群体"）。
   它们是**在用资产**：`ashare_risk_verdict.py` [3][4] 段、`ashare_risk_scan.py`
   的已验证标签全靠它们。
3. 策略结论本身 —— 这才是判死后该删的。

本脚本把 1、2 迁到独立表，使第 3 类可以安全删除。

幂等性
------
所有迁移用 `INSERT OR REPLACE`，重跑不会产生重复行。

用法
----
    python ashare_registry_migrate.py              # 执行迁出
    python ashare_registry_migrate.py --verify     # 只校验，不改数据
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_MARKER = "outputs/ashare_strategy_registry.sqlite"
ROOT = _HERE
for _p in _HERE.parents[:6]:
    if (_p / _MARKER).exists():
        ROOT = _p
        break
else:
    raise RuntimeError("未找到工作区根（向上 6 层内无 %s）" % _MARKER)
OUT = ROOT / "outputs"
ASHARE_DB = OUT / "ashare_strategy_registry.sqlite"

NOW = datetime.now().isoformat(timespec="seconds")

# ---------------------------------------------------------------- 目标表定义

DDL_STAT = """
CREATE TABLE IF NOT EXISTS stat_observations (
    obs_key     TEXT PRIMARY KEY,   -- 唯一观测键（source_key + '/' + stat_kind）
    source_key  TEXT NOT NULL,      -- 原 strategy_key（策略删了也能追溯来源）
    pool_key    TEXT,
    stat_kind   TEXT NOT NULL,      -- price_volume_sigma / fundamental_sigma
    sigma_true  REAL NOT NULL,      -- 实测横截面 σ_true（功效预检的输入）
    n_obs       INTEGER,            -- 估计该 σ 所用的观测数
    effect      REAL,               -- 当时实测到的效应（如实记录，不因判死而删改）
    mde         REAL,               -- 当时算出的 MDE
    measured_at TEXT,               -- 原条目的 validated_at
    note        TEXT,
    migrated_at TEXT NOT NULL
)
"""

DDL_RISKLABEL_EV = """
CREATE TABLE IF NOT EXISTS risk_label_evidence (
    -- 风控标签的证据链。随标签一起从 strategy_evidence 迁出，
    -- 使 risk_label_* 自成体系，不再依赖 strategy_* 表。
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label_key   TEXT NOT NULL,
    gate        TEXT,
    artifact    TEXT,
    note        TEXT,
    migrated_at TEXT NOT NULL
)
"""

DDL_RISKLABEL = """
CREATE TABLE IF NOT EXISTS risk_label_registry (
    -- 风险标签·在用：不产生超额收益，判据是"标记脆弱群体"。
    -- 不计入「策略」计数，但判决书与批量扫描直接依赖，不得随判死条目删除。
    label_key    TEXT PRIMARY KEY,
    label        TEXT,
    pool_key     TEXT,
    verdict      TEXT,
    basis        TEXT,              -- 判据（label_kind=validated 时必填）
    label_kind   TEXT NOT NULL,     -- validated（已验证） / descriptive（描述性）
    in_use       INTEGER NOT NULL DEFAULT 1,
    used_by      TEXT,              -- 调用方清单，逗号分隔
    validated_at TEXT,
    boundary     TEXT,              -- 原封不动搬过来
    payload      TEXT,              -- 原条目的完整 JSON（保留全字段，防信息丢失）
    migrated_at  TEXT NOT NULL
)
"""

# 迁出的风险标签：key → 附加元信息
RISK_LABELS = {
    "agri_np_yoy_neglist": dict(
        label_kind="validated",
        basis=("农业池 IC −0.111 / t(HAC) −3.33 / MDE 0.0877，逐年显著；"
               "仅池内成立，CSI800 换池检验失败（月度口径符号翻转）"),
        used_by="ashare_risk_verdict.py[4], ashare_risk_scan.py",
    ),
    "csi800_margin_crowding_neglist": dict(
        label_kind="validated",
        basis=("群体特征对比：top10 CAGR 6.004% vs 全池 13.477%、"
               "最大回撤 58.72% vs 39.45%。⚠ 该对比本身未做显著性检验，"
               "证据强度弱于 agri_np_yoy_neglist（后者有 IC 检验）"),
        used_by="ashare_risk_verdict.py[3], ashare_risk_scan.py",
    ),
}


def connect() -> sqlite3.Connection:
    c = sqlite3.connect(ASHARE_DB)
    c.row_factory = sqlite3.Row
    return c


def migrate_stat(c: sqlite3.Connection, dry: bool) -> int:
    """迁出实测 σ_true。"""
    c.execute(DDL_STAT)
    rows = list(c.execute(
        "SELECT strategy_key, pool_key, gate1_sigma_true, gate1_n_obs, "
        "gate1_effect, gate1_mde, validated_at, verdict "
        "FROM strategy_registry WHERE gate1_sigma_true IS NOT NULL"))
    n = 0
    for r in rows:
        kind = "fundamental_sigma" if "np_yoy" in (r["strategy_key"] or "") \
            else "price_volume_sigma"
        obs_key = "%s/%s" % (r["strategy_key"], kind)
        if dry:
            print("    [dry] %s  σ=%.4f  n=%s" % (obs_key, r["gate1_sigma_true"],
                                                  r["gate1_n_obs"]))
        else:
            c.execute(
                "INSERT OR REPLACE INTO stat_observations "
                "(obs_key, source_key, pool_key, stat_kind, sigma_true, n_obs, "
                " effect, mde, measured_at, note, migrated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (obs_key, r["strategy_key"], r["pool_key"], kind,
                 r["gate1_sigma_true"], r["gate1_n_obs"], r["gate1_effect"],
                 r["gate1_mde"], r["validated_at"],
                 "原 verdict=%s（策略结论已删，实测统计量保留）" % r["verdict"],
                 NOW))
        n += 1
    if not dry:
        c.commit()
    return n


def migrate_labels(c: sqlite3.Connection, dry: bool) -> int:
    """迁出风控标签（整行 payload + 证据链）。

    ★ 证据链必须跟着标签走：strategy_evidence 会随判死策略一起清空，
      若标签证据留在里面，风控标签的证据链就断了（判据失去可追溯来源）。
    """
    import json
    c.execute(DDL_RISKLABEL)
    c.execute(DDL_RISKLABEL_EV)
    n = 0
    for key, meta in RISK_LABELS.items():
        r = c.execute("SELECT * FROM strategy_registry WHERE strategy_key=?",
                      (key,)).fetchone()
        if r is None:
            print("    ⚠ 源条目缺失：%s（可能已迁出）" % key)
            continue
        d = dict(r)
        payload = json.dumps(d, ensure_ascii=False, default=str)
        evs = list(c.execute(
            "SELECT gate, artifact, note FROM strategy_evidence "
            "WHERE strategy_key=?", (key,)))
        if dry:
            print("    [dry] %s  kind=%s  in_use=1  evidence=%d"
                  % (key, meta["label_kind"], len(evs)))
        else:
            c.execute(
                "INSERT OR REPLACE INTO risk_label_registry "
                "(label_key, label, pool_key, verdict, basis, label_kind, in_use, "
                " used_by, validated_at, boundary, payload, migrated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, d.get("label"), d.get("pool_key"), d.get("verdict"),
                 meta["basis"], meta["label_kind"], 1, meta["used_by"],
                 d.get("validated_at"), d.get("boundary"), payload, NOW))
            # 证据链：先清后插，保证幂等且不会因重跑产生重复
            c.execute("DELETE FROM risk_label_evidence WHERE label_key=?", (key,))
            for e in evs:
                c.execute(
                    "INSERT INTO risk_label_evidence "
                    "(label_key, gate, artifact, note, migrated_at) "
                    "VALUES (?,?,?,?,?)",
                    (key, e["gate"], e["artifact"], e["note"], NOW))
        n += 1
    if not dry:
        c.commit()
    return n


def _table_exists(c: sqlite3.Connection, name: str) -> bool:
    r = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                  (name,)).fetchone()
    return r is not None


def verify(c: sqlite3.Connection) -> int:
    """迁出完整性校验：逐条比对源与目标。"""
    import json
    err = 0

    # 表尚未创建 ≠ 校验失败，但必须说清楚，否则会被读成"数据丢了"
    for t in ("stat_observations", "risk_label_registry", "risk_label_evidence"):
        if not _table_exists(c, t):
            print("  ✗ 目标表 %s 尚未创建 —— 请先不加 --verify 执行一次迁出" % t)
            return 1

    # 1. σ_true：条数与数值必须与源一致
    src = {r["strategy_key"]: (r["gate1_sigma_true"], r["gate1_n_obs"])
           for r in c.execute(
               "SELECT strategy_key, gate1_sigma_true, gate1_n_obs "
               "FROM strategy_registry WHERE gate1_sigma_true IS NOT NULL")}
    dst = {r["source_key"]: (r["sigma_true"], r["n_obs"])
           for r in c.execute("SELECT source_key, sigma_true, n_obs "
                              "FROM stat_observations")}
    if set(src) - set(dst):
        print("  ✗ σ_true 缺失：%s" % (set(src) - set(dst)))
        err += 1
    for k, v in src.items():
        if k in dst and abs(dst[k][0] - v[0]) > 1e-12:
            print("  ✗ σ_true 数值不符 %s: %s vs %s" % (k, v, dst[k]))
            err += 1
    print("  %s σ_true：源 %d 条 → 目标 %d 条"
          % ("✓" if not err else "✗", len(src), len(dst)))

    # 2. 风险标签：payload 必须能还原源行的关键字段
    e2 = 0
    for key in RISK_LABELS:
        s = c.execute("SELECT * FROM strategy_registry WHERE strategy_key=?",
                      (key,)).fetchone()
        d = c.execute("SELECT * FROM risk_label_registry WHERE label_key=?",
                      (key,)).fetchone()
        if d is None:
            print("  ✗ 风险标签未迁出：%s" % key)
            e2 += 1
            continue
        if s is not None:
            if (d["boundary"] or "") != (s["boundary"] or ""):
                print("  ✗ boundary 不符：%s" % key)
                e2 += 1
            try:
                p = json.loads(d["payload"])
                if p.get("verdict") != s["verdict"]:
                    print("  ✗ payload.verdict 不符：%s" % key)
                    e2 += 1
            except Exception as ex:
                print("  ✗ payload 无法解析 %s: %s" % (key, ex))
                e2 += 1
        if not d["basis"]:
            print("  ✗ basis 为空：%s" % key)
            e2 += 1
    print("  %s 风险标签：%d 条（kind/basis/used_by 齐备）"
          % ("✓" if not e2 else "✗", len(RISK_LABELS)))

    # 3. 证据链：每条标签在 risk_label_evidence 的条数必须与源一致
    e3 = 0
    for key in RISK_LABELS:
        n_src = c.execute(
            "SELECT COUNT(*) FROM strategy_evidence WHERE strategy_key=?",
            (key,)).fetchone()[0]
        n_dst = c.execute(
            "SELECT COUNT(*) FROM risk_label_evidence WHERE label_key=?",
            (key,)).fetchone()[0]
        if n_dst < n_src:
            print("  ✗ 证据链缺失 %s：源 %d → 目标 %d" % (key, n_src, n_dst))
            e3 += 1
    print("  %s 证据链：风控标签证据已随标签迁出"
          % ("✓" if not e3 else "✗"))
    return err + e2 + e3


# ---------------------------------------------------------------- 判死条目删除

# 宏观库中唯一保留的条目
MACRO_KEEP = {"macro_ppi_yoy_ar"}

# 留档目录（删前必须存在，否则拒绝执行）
ARCHIVE_DIR = OUT / "_archive"


def _precheck_purge() -> list:
    """删除前置检查。任一不通过即拒绝执行 —— 把「先记录后删除」变成机器强制。"""
    errs = []
    if not ASHARE_DB.exists():
        errs.append("A股注册表不存在：%s" % ASHARE_DB)
        return errs
    c = connect()
    try:
        for t in ("stat_observations", "risk_label_registry"):
            if not _table_exists(c, t):
                errs.append("未迁出：%s 表不存在（先执行不带 --purge 的迁出）" % t)
        if _table_exists(c, "stat_observations"):
            n = c.execute("SELECT COUNT(*) FROM stat_observations").fetchone()[0]
            if n == 0:
                errs.append("stat_observations 为空 —— σ_true 未迁出，拒绝删除")
        if _table_exists(c, "risk_label_registry"):
            n = c.execute("SELECT COUNT(*) FROM risk_label_registry").fetchone()[0]
            if n == 0:
                errs.append("risk_label_registry 为空 —— 风控标签未迁出，拒绝删除")
    finally:
        c.close()
    # 留档必须存在
    if not ARCHIVE_DIR.exists():
        errs.append("留档目录不存在：%s（先运行 ashare_registry_archive.py）" % ARCHIVE_DIR)
    else:
        hits = sorted(ARCHIVE_DIR.rglob("已判死策略归档记录.md"))
        if not hits:
            errs.append("未找到留档文件 已判死策略归档记录.md（先运行 ashare_registry_archive.py）")
    return errs


def purge(assume_yes: bool = False) -> int:
    """物理删除判死条目。返回错误数。"""
    errs = _precheck_purge()
    if errs:
        print("✗ 前置检查未通过，拒绝删除：")
        for e in errs:
            print("    - %s" % e)
        return 1

    # 删除清单（先算出来给用户看）
    c = connect()
    plan = []
    try:
        for r in c.execute("SELECT strategy_key, verdict FROM strategy_registry "
                           "ORDER BY strategy_key"):
            v = r["verdict"] or ""
            keep = "risk_signal_not_alpha" in v         # 已迁出为风险标签 → 从策略表移除
            plan.append(("ashare/strategy_registry", r["strategy_key"], v,
                         "删除(已迁为风险标签)" if keep else "删除(判死)"))
        for r in c.execute("SELECT factor_key FROM ashare_factor_registry "
                           "ORDER BY factor_key"):
            plan.append(("ashare/ashare_factor_registry", r["factor_key"], "", "删除(判死)"))
    finally:
        c.close()

    mdb = OUT / "macro_strategy_registry.sqlite"
    if mdb.exists():
        c = sqlite3.connect(mdb)
        c.row_factory = sqlite3.Row
        try:
            for r in c.execute("SELECT strategy_key, verdict FROM strategy_registry "
                               "ORDER BY strategy_key"):
                keep = r["strategy_key"] in MACRO_KEEP
                plan.append(("macro/strategy_registry", r["strategy_key"],
                             r["verdict"] or "",
                             "**保留(可交付)**" if keep else "删除(判死)"))
        finally:
            c.close()

    n_del = sum(1 for p in plan if p[3] != "**保留(可交付)**")
    n_keep = len(plan) - n_del
    print("删除计划：共 %d 条 → 删除 %d，保留 %d" % (len(plan), n_del, n_keep))
    print("-" * 78)
    for tbl, key, verdict, act in plan:
        print("  %-32s %-40s %s" % (tbl, key, act))
    print("-" * 78)
    if not assume_yes:
        ans = input("确认执行？输入 yes 继续，其它取消：").strip().lower()
        if ans != "yes":
            print("已取消。")
            return 0

    # 执行删除
    c = connect()
    try:
        keys_del = [k for t, k, v, a in plan
                    if t == "ashare/strategy_registry" and a != "**保留(可交付)**"]
        fac_del = [k for t, k, v, a in plan if t == "ashare/ashare_factor_registry"]
        for k in keys_del:
            c.execute("DELETE FROM strategy_registry WHERE strategy_key=?", (k,))
            # evidence 一并删除 —— 风控标签的证据已迁到 risk_label_evidence，
            # 判死策略的证据已进留档，两者都不会丢。
            c.execute("DELETE FROM strategy_evidence WHERE strategy_key=?", (k,))
        for k in fac_del:
            # ★ strategy_evidence 只有 strategy_key 列，没有 factor_key（2026-09-04 实测）。
            #   且因子名下 evidence 实测为 0 条，故不处理证据表。
            c.execute("DELETE FROM ashare_factor_registry WHERE factor_key=?", (k,))
        c.commit()
        rem_s = c.execute("SELECT COUNT(*) FROM strategy_registry").fetchone()[0]
        rem_f = c.execute("SELECT COUNT(*) FROM ashare_factor_registry").fetchone()[0]
        rem_e = c.execute("SELECT COUNT(*) FROM strategy_evidence").fetchone()[0]
        rem_l = c.execute("SELECT COUNT(*) FROM risk_label_registry").fetchone()[0]
        rem_o = c.execute("SELECT COUNT(*) FROM stat_observations").fetchone()[0]
    finally:
        c.close()

    rem_m = None
    if mdb.exists():
        c = sqlite3.connect(mdb)
        try:
            mk = [k for t, k, v, a in plan
                  if t == "macro/strategy_registry" and a != "**保留(可交付)**"]
            for k in mk:
                c.execute("DELETE FROM strategy_registry WHERE strategy_key=?", (k,))
                c.execute("DELETE FROM strategy_evidence WHERE strategy_key=?", (k,))
            c.commit()
            rem_m = c.execute("SELECT COUNT(*) FROM strategy_registry").fetchone()[0]
        finally:
            c.close()

    print()
    print("删除完成。剩余：")
    print("  ashare/strategy_registry     : %d 条" % rem_s)
    print("  ashare/ashare_factor_registry: %d 条" % rem_f)
    print("  ashare/strategy_evidence     : %d 条（含保留项的证据）" % rem_e)
    print("  ashare/risk_label_registry   : %d 条（风控标签，已迁出保留）" % rem_l)
    print("  ashare/stat_observations     : %d 条（实测 σ_true，已迁出保留）" % rem_o)
    if rem_m is not None:
        print("  macro/strategy_registry      : %d 条" % rem_m)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="删除前的数据资产迁出 / 判死条目删除")
    ap.add_argument("--verify", action="store_true", help="只校验迁出完整性，不改数据")
    ap.add_argument("--purge", action="store_true",
                    help="物理删除判死条目（含前置检查：未迁出/未留档则拒绝）")
    ap.add_argument("--yes", action="store_true", help="--purge 时跳过交互确认")
    args = ap.parse_args()

    if not ASHARE_DB.exists():
        print("✗ 注册表不存在：%s" % ASHARE_DB)
        return 2

    if args.purge:
        return purge(assume_yes=args.yes)

    c = connect()
    try:
        if args.verify:
            print("校验模式（不改数据）：")
            return 1 if verify(c) else 0
        print("迁出 → %s" % ASHARE_DB.name)
        n1 = migrate_stat(c, dry=False)
        n2 = migrate_labels(c, dry=False)
        print()
        print("  stat_observations  : %d 条实测 σ_true" % n1)
        print("  risk_label_registry: %d 条风控标签" % n2)
        print()
        print("校验：")
        return 1 if verify(c) else 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
