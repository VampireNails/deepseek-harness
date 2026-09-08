#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ashare_xq_slice2014.py — 生成【检验窗口】切片库，供 A/B 同跨度对照
================================================================
为什么必须切片
--------------
雪球返回【全历史】（老股 8000 根，最早 1992-11-06），而腾讯库只有
2014-01-02 起（196 万行）。若直接把新库喂给检验脚本：
  1. 面板时间轴 T 从 3160 扩大到 8300+，内存与耗时成倍上升
     （每个因子面板 8300×780，四个因子 + close/volume/tradable/ret_h ≈ 1GB）
  2. 更致命的是【对照不公平】：T 不同会改变 min_history/tradable 的判定
     基准，A/B 差异就不再能 100% 归因于数据源

故切片到与腾讯库同一时间窗（>= 2014-01-01），使 A/B 的日期轴、股票数、
可交易判定完全对等，差异只能来自后复权数值本身。

★ 文件名必须带 _2014 标识（第十八类：跨池/跨窗口静默覆盖）——若下游
  误用全量库跑出不同结论而无从追溯，属于产物混淆事故。

用法：python ashare_xq_slice2014.py [--since 2014-01-01]
输出：outputs/ashare_csi800_hfq_xq_2014.sqlite
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[4]
SRC = WORKSPACE / "outputs" / "ashare_csi800_hfq_xq.sqlite"
DST = WORKSPACE / "outputs" / "ashare_csi800_hfq_xq_2014.sqlite"
TX = WORKSPACE / "outputs" / "ashare_csi800_hfq.sqlite"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2014-01-01")
    args = ap.parse_args()
    if not SRC.exists():
        raise SystemExit(f"源库不存在: {SRC}")
    if DST.exists():
        DST.unlink()

    s = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
    d = sqlite3.connect(str(DST))
    d.execute("""CREATE TABLE daily_quotes_hfq(
        code TEXT, trade_date TEXT, open REAL, close REAL, high REAL, low REAL,
        volume REAL, source TEXT, collected_at TEXT)""")
    d.execute("CREATE INDEX ix_hfq_code ON daily_quotes_hfq(code)")
    d.execute("CREATE INDEX ix_hfq_date ON daily_quotes_hfq(trade_date)")

    n = 0
    for row in s.execute("SELECT code,trade_date,open,close,high,low,volume,source,"
                         "collected_at FROM daily_quotes_hfq WHERE trade_date >= ? "
                         "ORDER BY trade_date, code", (args.since,)):
        d.execute("INSERT INTO daily_quotes_hfq VALUES(?,?,?,?,?,?,?,?,?)", row)
        n += 1
    d.commit()

    # QC 表整体复制（判坏结论与窗口无关，且闸门需要它）
    cols = [r[1] for r in s.execute("PRAGMA table_info(hfq_qc)")]
    d.execute("CREATE TABLE hfq_qc(%s)" % ",".join(
        f'"{c}" TEXT' if c in ("code", "collected_at", "reason") else f'"{c}" REAL'
        for c in cols))
    rows = s.execute(f"SELECT {','.join(cols)} FROM hfq_qc").fetchall()
    if rows:
        d.executemany("INSERT INTO hfq_qc VALUES(%s)" % ",".join("?" * len(cols)), rows)
    d.commit()
    d.close()

    # 与腾讯库做对等性核对
    t = sqlite3.connect(f"file:{TX}?mode=ro", uri=True)
    td = t.execute("SELECT count(*), min(trade_date), max(trade_date), "
                   "count(DISTINCT code) FROM daily_quotes_hfq").fetchone()
    t.close()
    dd = sqlite3.connect(f"file:{DST}?mode=ro", uri=True).execute(
        "SELECT count(*), min(trade_date), max(trade_date), count(DISTINCT code) "
        "FROM daily_quotes_hfq").fetchone()
    print("=" * 70)
    print("切片库生成完毕（供 A/B 同跨度对照）")
    print("=" * 70)
    print(f"  腾讯库   rows={td[0]:<9} codes={td[3]:<5} {td[1]} ~ {td[2]}")
    print(f"  切片库   rows={dd[0]:<9} codes={dd[3]:<5} {dd[1]} ~ {dd[2]}")
    print(f"  复制行情行 {n}")
    print(f"  输出: {DST}")


if __name__ == "__main__":
    main()
