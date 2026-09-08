# -*- coding: utf-8 -*-
"""
ashare_hfq_access.py — 后复权行情的【统一访问层 + 强制 QC 闸门】

为什么要有这一层（2026-09-03 用户质询后建立）
---------------------------------------------
用户主张：「数据质量是量化 agent 的基石，数据无法保证则所有回测结论无效。」

原则成立。但原实现把 QC 做成了「每个回测脚本自己写一遍
`SELECT code FROM hfq_qc WHERE bad=1`」，于是必然漏：实测
`equity_layer_backtest.py`（扣成本可行性）/ `equity_quant.py`（截面 IC）
/ `ashare_agri_validate.py`（三关验证）/ `quant_engine.py` 四个脚本均未过滤。
漏网的脚本会拿含坏数据的面板跑出结论，而结论本身看不出有没有过滤。

修法不是去逐个补四个脚本（下次还会漏第五个），而是**把闸门下沉到数据层**：
所有行情读取必须过本模块，默认强制 QC；QC 缺失时 fail-loud 而非静默放行。

三条硬纪律
----------
1. **默认不放行**：`apply_qc=True` 是默认值；显式传 False 必须书面说明。
2. **QC 缺失 = 数据未质检 = 不可用**：库里没有 hfq_qc 表时抛异常，
   绝不退化成「全量放行」。宁可跑不起来，也不能跑出来源不明的结论。
3. **剔除留痕**：返回对象带 `n_bad` / `bad_codes` / `qc_source`，
   任何下游结论都必须能回答「剔了几只、为什么剔」。

用法
----
    from ashare_hfq_access import load_hfq_panel
    p = load_hfq_panel(Path("outputs/ashare_csi800_hfq_xq.sqlite"), since="2021-01-01")
    p.dates, p.codes, p.close        # close 形状 (n_codes, n_dates)
    p.n_bad, p.bad_codes             # 剔除留痕

    # 只要坏代码集合（用于给已有脚本打补丁）
    from ashare_hfq_access import qc_bad_codes
    bad = qc_bad_codes(db)

⚠️ 已废弃源（腾讯 ashare_csi800_hfq.sqlite）默认拒绝放行，抛 DeprecatedSource。
   默认后复权库 = 雪球 ashare_csi800_hfq_xq.sqlite（2026-09-06 起）。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


class QcMissing(RuntimeError):
    """库中没有 hfq_qc 表（或其等价质检记录）——数据未质检，拒绝放行。"""


class DeprecatedSource(RuntimeError):
    """数据源已被证实不可用，拒绝放行（除非显式 allow_deprecated=True）。"""


# ---------------------------------------------------------------------------
# 废弃数据源黑名单（fail-loud）
#
# 为什么必须做到"拦截"而不只是"改默认路径"：
#   改默认值只能拦住【不传参】的调用；任何脚本显式传 --price-db 仍可绕过。
#   已废弃的源一旦被静默读取，产出的结论与干净数据的结论外观上无法区分——
#   这正是第廿四类（腾讯 hfq 静默污染）能潜伏到 2026-09-05 才暴露的原因。
#
# 判定依据（2026-09-05 跨源核对，outputs/2026-09-05/换源复测结题报告.md）：
#   无除权日存在数学恒等式 hfq 日收益 == raw 日收益。以本地已验证 raw 为基准：
#     雪球 max 偏差 2.8e-6（4 位小数舍入噪声级）
#     腾讯 max 偏差 3.57%、p50 0.20%、6% 的观测偏差 >1%
#   另有 600595 后复权价为负、2019-08 月收益 +23575%（真值 +8.03%）。
# ---------------------------------------------------------------------------
DEPRECATED_SOURCES: dict[str, str] = {
    "ashare_csi800_hfq.sqlite": (
        "腾讯 fqkline 后复权：2026-09-05 跨源核对已证实系统性污染。"
        "无除权日恒等式偏差 max 3.57%（对照雪球 2.8e-6）；"
        "600595 后复权价为负、月收益 +23575%（真值 +8.03%）。"
        "禁止用于任何新计算。替代品：ashare_csi800_hfq_xq.sqlite（雪球）。"
        "若确需读取（仅限考古/对照），请显式传 allow_deprecated=True 并书面说明。"),
}
# 2026-09-06 补齐：其余四个腾讯源库同批重采为雪球源后拉黑。
# 判坏率（COUNT(DISTINCT code) 口径，旧源 → 雪球新源）：
#   农业 1.05%(1/95) → 0%(0/95)；半导体 3.80%(3/79) → 0%(0/79)
#   宽池 7.02%(75/1068) → 0.28%(3/1068)；single 25.00%(1/4) → 0%(0/4)
#
# ⚠ 雪球源并非零缺陷，但缺陷形态与腾讯根本不同（2026-09-06 三方裁决）：
#   腾讯 = **序列级静默漂移**（无除权日恒等式偏差 p50 0.20%、max 3.57%，
#          涨跌停 QC 抓不到，只能靠跨源核对发现）；
#   雪球 = **个股级单日跳变**（2025-04-30 的 600166 +21.02%/600307 +23.04%、
#          2025-05-07 的 000736 +50.87%，腾讯不复权真值分别 +3.56%/−2.14%/+0.97%，
#          600307 连方向都反了）⇒ 越涨跌停 ⇒ 被 QC 100% 抓到并判坏剔除。
#   即：雪球的错误**可被同一道闸门拦截**，腾讯的错误**不能**。这是切换的依据。
for _name, _repl, _why in [
    ("ashare_agri_hfq.sqlite", "ashare_agri_hfq_xq.sqlite",
     "农业池；agri_np_yoy_neglist 的基础数据"),
    ("ashare_semi_hfq.sqlite", "ashare_semi_hfq_xq.sqlite", "半导体池"),
    ("ashare_wide_hfq.sqlite", "ashare_wide_hfq_xq.sqlite",
     "宽池，旧源坏股率最高 7.02%"),
    ("ashare_single_hfq.sqlite", "ashare_single_hfq_xq.sqlite",
     "单只诊断池，旧源坏股率 25%"),
]:
    DEPRECATED_SOURCES[_name] = (
        f"腾讯 fqkline 后复权（{_why}）：与 csi800 同批被证实系统性污染，"
        f"2026-09-06 已用雪球重采。禁止用于任何新计算。"
        f"替代品：{_repl}。"
        f"若确需读取（仅限考古/对照），请显式传 allow_deprecated=True 并书面说明。")


def _check_source(db, allow_deprecated: bool = False) -> Path:
    """数据源准入检查：已废弃的源默认拒绝放行。"""
    p = Path(db)
    why = DEPRECATED_SOURCES.get(p.name)
    if why and not allow_deprecated:
        raise DeprecatedSource(f"数据源已废弃，拒绝放行：{p.name}\n  原因：{why}")
    return p


@dataclass
class Panel:
    dates: list
    codes: list
    close: np.ndarray          # (n_codes, n_dates)，缺失为 NaN
    n_bad: int = 0
    bad_codes: list = field(default_factory=list)
    qc_source: str = ""
    table: str = ""
    # 数据源留痕：True 表示本次读取的是【已废弃源】（显式 allow_deprecated），
    # 由此得出的任何结论都必须标注为考古/对照用途，不得作为生产结论。
    deprecated: bool = False
    source: str = ""


def qc_bad_codes(db, table: str = "hfq_qc", conn: sqlite3.Connection | None = None,
                 allow_deprecated: bool = False) -> set:
    """读取 QC 判坏的代码集合。QC 表不存在时抛 QcMissing（fail-loud）。

    同时做数据源准入检查：已废弃源抛 DeprecatedSource（fail-loud）。
    """
    _check_source(db, allow_deprecated)
    own = conn is None
    if own:
        conn = sqlite3.connect(str(db))
    try:
        has = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        if not has:
            raise QcMissing(
                f"库 {db} 缺少质检表 '{table}'：该库数据未经过质检，"
                f"按数据质量纪律拒绝放行。请先运行 ashare_badj_collect.py 完成采集与 QC，"
                f"或显式传入 apply_qc=False 并书面说明后果。")
        return {r[0] for r in conn.execute(f"SELECT code FROM {table} WHERE bad = 1")}
    finally:
        if own:
            conn.close()


def load_hfq_panel(db, since: str | None = None, codes: list | None = None,
                   table: str = "daily_quotes_hfq", qc_table: str = "hfq_qc",
                   apply_qc: bool = True,
                   drop_first_n_days: int = 0,
                   allow_deprecated: bool = False) -> Panel:
    """读取后复权收盘价面板，默认强制剔除 QC 判坏的股票。

    drop_first_n_days: 剔除每只股票序列的前 N 个交易日。
        新股上市前 5 日（科创/创业）不设涨跌幅，日收益可达 ±100%+，
        这是市场事实不是数据错误，但会污染日频因子（实测 688615 上市第 3 日
        +96.0%）。做日频信号时建议设 5~20。

    allow_deprecated: 允许读取已废弃的数据源。仅限考古/对照用途，
        读取会在 Panel.qc_source 留痕。默认 False = 拒绝放行。
    """
    db = _check_source(db, allow_deprecated)
    conn = sqlite3.connect(str(db))
    try:
        bad = qc_bad_codes(db, qc_table, conn,
                           allow_deprecated=allow_deprecated) if apply_qc else set()

        sql = f"SELECT code, trade_date, close FROM {table} " \
              f"WHERE close IS NOT NULL AND close > 0"
        args: list = []
        if since:
            sql += " AND trade_date >= ?"
            args.append(since)
        if codes is not None:
            cs = sorted(set(codes))
            sql += f" AND code IN ({','.join('?' * len(cs))})"
            args.extend(cs)
        sql += " ORDER BY trade_date, code"

        rows = conn.execute(sql, args).fetchall()
        # 新股前 N 日剔除：需要每只股的日期排序，先按 code 分组过滤
        if drop_first_n_days > 0 and rows:
            by_code: dict[str, list] = {}
            for c, d, cl in rows:
                by_code.setdefault(c, []).append((d, cl))
            rows = [(c, d, cl) for c, seq in by_code.items()
                    for d, cl in sorted(seq)[drop_first_n_days:]]
    finally:
        conn.close()

    dep = db.name in DEPRECATED_SOURCES
    if not rows:
        return Panel(dates=[], codes=[], close=np.zeros((0, 0)),
                     n_bad=len(bad), bad_codes=sorted(bad), qc_source=qc_table,
                     table=table, deprecated=dep, source=db.name)

    dates = sorted({r[1] for r in rows})
    di = {d: i for i, d in enumerate(dates)}
    keep_codes = sorted({r[0] for r in rows} - bad)
    ci = {c: i for i, c in enumerate(keep_codes)}
    M = np.full((len(keep_codes), len(dates)), np.nan)
    for c, d, cl in rows:
        j = ci.get(c)
        if j is not None:
            M[j, di[d]] = float(cl)

    return Panel(dates=dates, codes=keep_codes, close=M,
                 n_bad=len(bad), bad_codes=sorted(bad), qc_source=qc_table,
                 table=table, deprecated=dep, source=db.name)


def assert_qc_gate(db, table: str = "hfq_qc") -> set:
    """给已有脚本打补丁用的最小侵入入口：返回坏代码集合，缺 QC 表即报错。

    用法（替换掉原来的手工查询）：
        - qc_bad = {r[0] for r in conn.execute("SELECT code FROM hfq_qc WHERE bad=1")}
        + qc_bad = assert_qc_gate(db)
    """
    return qc_bad_codes(db, table)
