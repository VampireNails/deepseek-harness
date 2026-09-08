#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ashare_xq_reqc.py — 不重采、只按修正后的 QC 规则重算质检结论
==========================================================
为什么需要这个脚本
------------------
QC 规则在采集过程中被发现有两处把【市场制度事实】当成【数据错误】：
  ① 涨跌停阈值用 lim+1e-6，未考虑涨停价四舍五入到分（应为 lim+0.005）
  ② 未豁免停牌后复牌首日 / 恢复上市首日（制度上不设涨跌幅）
     —— 实测误剔 000155(-28.9%)、000629(+25.1%)、000792(+306.1%)

修正后无需重新联网采集（780 只 × 1 次请求 ≈ 40 分钟），只需按新规则重算。
★ 自检路径 ≠ 生产路径（第廿三类）：本脚本与 ashare_xq_collect.py 共用
  同一个 qc_check 函数（import 而来），不另写一份，防止两处规则漂移。

恒等式 QC 不在此重算（需要联网拉不复权序列，成本同采集），沿用采集时的
抽检结果；本脚本只重算可离线判定的 QC1 覆盖率与 QC2 涨跌停。

★ 覆盖率轴的两级来源（2026-09-06 加）
------------------------------------
csi800 池：本地 raw 库有该股记录 ⇒ 用【该股自身】的 raw 日期集合（最准）。
其它池（农业/半导体/宽池/single）：本地 raw 无记录，采集时覆盖率判据会
退化为 1.0（等于失效）。此处用两遍法补回：
    axis_i = { d ∈ 池级并集 U : f_i ≤ d ≤ l_i }
即【池内所有股票交易日的并集】与该股【上市首日~末日】区间的交集。
    · 排除上市前 / 退市后 ⇒ 不会结构性误判晚上市股
    · 停牌期仍计入分母 ⇒ 故阈值保持宽松（0.90），且只作预警不单独判坏

用法：python ashare_xq_reqc.py [--db outputs/ashare_agri_hfq_xq.sqlite]
输出：outputs/<今天>/<库名>_reqc.json  + 更新该库 hfq_qc 表
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from ashare_xq_collect import (COVERAGE_MIN, NEW_DB, OUT_DIR, RESUME_GAP_DAYS,
                               WORKSPACE, parse_dt, pool_axis, qc_check)


def effective_axis(own: set, union: set, gap_days: int = 30) -> set:
    """该股的有效日期轴 = 池级并集 ∩ [上市首日, 末日] − 停牌区间。

    两步都很必要，缺一就会把市场事实判成数据错误：
      · 截到 [首日,末日]：否则晚上市 / 早退市股被结构性误判覆盖率不足
      · 剔除停牌区间：连续缺失 >gap_days 天是【停牌】不是【漏采】
        （实测 300268 停牌 109 天、300313 停牌 94 天，误判覆盖率 0.84/0.87）
        漏采在雪球"单次返回全量"模式下不成立（V1 已验证），停牌才是常态。
    """
    ds = sorted(own)
    f_i, l_i = ds[0], ds[-1]
    axis = {d for d in union if f_i <= d <= l_i}
    for a, b in zip(ds, ds[1:]):
        if (parse_dt(b) - parse_dt(a)).days > gap_days:
            axis = {d for d in axis if not (a < d < b)}
    return axis or own


def pool_union_axis(conn) -> set:
    """池级日期并集：库内所有股票出现过的交易日。"""
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily_quotes_hfq")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="目标库（默认 csi800 雪球库）")
    args = ap.parse_args()

    db = Path(args.db) if args.db else NEW_DB
    if not db.is_absolute():
        db = WORKSPACE / db
    if not db.exists():
        raise SystemExit(f"库不存在: {db}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 报告名随库变化，避免多池互相覆盖（第十八类）
    report_path = OUT_DIR / f"{db.stem}_reqc.json"

    axis_local = pool_axis()          # 本地 raw（仅 csi800 覆盖得到）

    conn = sqlite3.connect(str(db))
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(hfq_qc)")]
        if "resume_exempt_days" not in cols:
            conn.execute("ALTER TABLE hfq_qc ADD COLUMN resume_exempt_days INTEGER")
        if "axis_source" not in cols:
            conn.execute("ALTER TABLE hfq_qc ADD COLUMN axis_source TEXT")
        # 沿用采集时的恒等式抽检结果（联网成本高，不重跑）
        ident = {r[0]: r[1] for r in conn.execute(
            "SELECT code, identity_p99 FROM hfq_qc WHERE identity_p99 IS NOT NULL")}

        union = pool_union_axis(conn)
        codes = [r[0] for r in conn.execute(
            "SELECT DISTINCT code FROM daily_quotes_hfq ORDER BY code")]
        before = {r[0]: r[1] for r in conn.execute("SELECT code, bad FROM hfq_qc")}
        rows_out, flips = [], []
        n_local_axis = 0
        for code in codes:
            rows = [(d, None, cl, None, None, None) for d, cl in conn.execute(
                "SELECT trade_date, close FROM daily_quotes_hfq "
                "WHERE code=? ORDER BY trade_date", (code,))]
            own = {r[0] for r in rows}
            if code in axis_local:
                axis, src = axis_local[code], "local_raw"
                n_local_axis += 1
            else:
                axis = effective_axis(own, union)
                src = "pool_union"
            bad, reason, st = qc_check(code, rows, axis, ident.get(code))
            st["axis_source"] = src
            conn.execute(
                "UPDATE hfq_qc SET n_bars=?, nonpositive=?, over_limit_days=?, "
                "ret_min=?, ret_max=?, coverage=?, bad=?, reason=?, "
                "resume_exempt_days=?, axis_source=? WHERE code=?",
                (st["n_bars"], st["nonpositive"], st["over_limit_days"],
                 st["ret_min"], st["ret_max"], st["coverage"], bad, reason,
                 st.get("resume_exempt_days", 0), src, code))
            rows_out.append({"code": code, **st})
            if before.get(code) != bad:
                flips.append({"code": code, "before": before.get(code), "after": bad,
                              "reason": reason})
        conn.commit()
    finally:
        conn.close()

    bad_n = sum(1 for r in rows_out if r["bad"])
    report = {
        "db": str(db),
        "rule": (f"tol=lim+0.005；豁免上市前5日；豁免停牌>{RESUME_GAP_DAYS}天后复牌首日；"
                 f"判坏窗口>=2014-01-01；覆盖率下限 {COVERAGE_MIN}"),
        "n_codes": len(rows_out), "n_bad": bad_n,
        "bad_rate": round(bad_n / max(len(rows_out), 1), 4),
        "axis_source_counts": {
            "local_raw": n_local_axis,
            "pool_union": len(rows_out) - n_local_axis,
        },
        "flips": flips,
        "bad_list": [r for r in rows_out if r["bad"]],
        "low_coverage": sorted(
            [{"code": r["code"], "coverage": r["coverage"]}
             for r in rows_out if (r["coverage"] or 1.0) < 0.98],
            key=lambda x: x["coverage"])[:20],
        "resume_exempt_total": sum(r.get("resume_exempt_days", 0) for r in rows_out),
        "identity_sampled": len(ident),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")

    print("=" * 70)
    print(f"按修正规则重算 QC  →  {db.name}")
    print("=" * 70)
    print(f"  股票 {len(rows_out)} 只   判坏 {bad_n} 只 ({report['bad_rate']:.2%})")
    print(f"  覆盖率轴来源: local_raw {n_local_axis} 只 / pool_union "
          f"{len(rows_out) - n_local_axis} 只")
    print(f"  复牌首日豁免总天数 {report['resume_exempt_total']}")
    print(f"  判定翻转 {len(flips)} 只: "
          + ", ".join(f"{f['code']}({f['before']}→{f['after']})" for f in flips[:12]))
    for r in report["bad_list"][:10]:
        print(f"   [bad] {r['code']}  cov={r['coverage']} over={r['over_limit_days']} "
              f"| {r['reason']}")
    if report["low_coverage"]:
        print("  覆盖率 <0.98 的个股（前若干）: " + ", ".join(
            f"{x['code']}({x['coverage']:.3f})" for x in report["low_coverage"][:10]))
    print(f"\n已写出: {report_path}")


if __name__ == "__main__":
    main()
