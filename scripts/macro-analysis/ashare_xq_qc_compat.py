# -*- coding: utf-8 -*-
"""
ashare_xq_qc_compat.py — 雪球后复权库的 QC 表兼容层

为什么需要这一层
----------------
2026-09-06 切换默认数据源（腾讯 → 雪球）后，`ashare_risk_verdict.py` 报：

    sqlite3.OperationalError: no such column: limit_used

根因不是脚本写错，而是**两个源的 hfq_qc 表 schema 不一致**：

    腾讯（9 列）：code collected_at n_bars nonpositive over_limit_days
                 ret_min ret_max **limit_used** bad
    雪球（12 列）：code collected_at n_bars nonpositive over_limit_days
                 ret_min ret_max coverage identity_p99 bad reason resume_exempt_days

`limit_used` 记录 QC 判定所用的涨跌停界限（分板块 0.10/0.20/0.30），
是 `ashare_badj_collect.py` / `ashare_agri_collect.py` 写入的事实字段，
被 `ashare_risk_verdict.gate_quality()` 作为**质量闸门**读取。

修法选择
--------
- 方案 A（采用）：在数据侧补齐 `limit_used` 列 ⇒ 下游 3 个脚本零改动，
  且未来任何依赖该列的脚本自动受益。**契约保持在数据层**，符合
  「闸门下沉到数据层」的项目纪律。
- 方案 B（否决）：改 risk_verdict 不读该列 ⇒ 每个新下游都要重踩一次，
  且丢失"该股判定时用的界限是多少"这一可审计信息。

用法
----
    python ashare_xq_qc_compat.py [--db outputs/ashare_csi800_hfq_xq.sqlite]

幂等：已存在该列时跳过；重复运行安全。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]
DEFAULT_DB = ROOT / "outputs" / "ashare_csi800_hfq_xq.sqlite"

# 与 ashare_xq_collect.limit_band 保持一致（分板块涨跌停界限）
def limit_band(code: str) -> float:
    if code.startswith(("300", "301", "302", "688", "689")):
        return 0.20          # 创业板（含新代码段 302）/ 科创板
    if code.startswith(("43", "83", "87", "92", "8")):
        return 0.30          # 北交所
    return 0.10              # 主板


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()
    db = Path(args.db)
    if not db.exists():
        print(f"库不存在: {db}")
        return 2

    conn = sqlite3.connect(str(db))
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(hfq_qc)")]
        if "limit_used" in cols:
            print(f"✓ {db.name} 已含 limit_used 列，无需处理（幂等）")
            return 0
        conn.execute("ALTER TABLE hfq_qc ADD COLUMN limit_used REAL")
        codes = [r[0] for r in conn.execute("SELECT code FROM hfq_qc")]
        conn.executemany(
            "UPDATE hfq_qc SET limit_used=? WHERE code=?",
            [(limit_band(c), c) for c in codes])
        conn.commit()
        # 回读校验（fail-loud：补齐后仍为空则报错，不许静默放行）
        null = conn.execute(
            "SELECT COUNT(*) FROM hfq_qc WHERE limit_used IS NULL").fetchone()[0]
        if null:
            raise RuntimeError(f"补齐后仍有 {null} 行 limit_used 为空")
        n10 = conn.execute(
            "SELECT COUNT(*) FROM hfq_qc WHERE limit_used=0.10").fetchone()[0]
        n20 = conn.execute(
            "SELECT COUNT(*) FROM hfq_qc WHERE limit_used=0.20").fetchone()[0]
        n30 = conn.execute(
            "SELECT COUNT(*) FROM hfq_qc WHERE limit_used=0.30").fetchone()[0]
    finally:
        conn.close()

    print(f"✓ 已为 {db.name} 补齐 limit_used 列，回填 {len(codes)} 行")
    print(f"   分板块分布：主板 ±10% = {n10} 只 | 创业/科创 ±20% = {n20} 只 "
          f"| 北交所 ±30% = {n30} 只")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
