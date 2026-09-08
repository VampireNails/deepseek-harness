# -*- coding: utf-8 -*-
"""
ashare_margin_neglist.py — 两融拥挤 neglist：单股拥挤度档位（个股级风控 overlay）

背景（SOP §14.13~14.14）：chg20（Δ20 交易日 ln 融资余额）是首个通过全预检的独立信息，
但篮子层面经济性 = 0 ⇒ 交付形态 = 个股级风险标签（risk_signal_not_alpha，对齐 agri_neglist）：
历史上处于 chg20 顶十分位的群体 CAGR 6.0% vs 13.5%、最大回撤 58.7% vs 39.5%（R=5 口径 72%）。

本工具回答 stock_diagnose 场景的一个问题：「这只股现在是否处于融资拥挤区？」

- chgN = ln(余额_t) − ln(余额_{t−N 交易日})（N 按**交易日**计，与 §14.13 预检口径一致，
  不是自然日）。
- 拥挤度 = 该股 chg20 在**当日全市场两融横截面**（t 与 t−20 均有余额者）中的分位：
  ≥90% = 拥挤（对齐 §14.14 回测 D10 剔除口径）；75~90% = 偏高。
- 信息集纪律：两融 T 日数据收盘后披露 ⇒ 只用 ≤asof 的已披露数据（默认最新）。
- fail-closed：非两融标的 / 无数据 → 输出 N/A（**无数据 ≠ 不拥挤**）；数据陈旧 → 明示。
- 档位是**历史统计风险标签，不是买卖建议、不是预测**（单股不可算 IC，见 Route S 纪律）。

用法：python ashare_margin_neglist.py 600519 [--db outputs/ashare_altdata.sqlite]
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import sqlite3
import sys
from pathlib import Path

ALTDB = "outputs/ashare_altdata.sqlite"
TOP_DECILE = 0.90      # ≥90 分位 = 拥挤（对齐回测 D10）
HIGH_DECILE = 0.75     # 75~90 = 偏高
STALE_DAYS = 7         # 最新两融日距今超过此天数 → 标注陈旧
N_CAL = 5              # 近 N 个交易日

def _db_path(db: str) -> str:
    if db:
        return db
    base = Path(__file__).resolve().parent
    return str(base / ALTDB) if not Path(ALTDB).exists() else ALTDB


def _calendar(c: sqlite3.Connection, asof: str | None) -> list[str]:
    q = ("SELECT DISTINCT trade_date FROM margin_daily WHERE rzye>0 "
         + ("AND trade_date<=?" if asof else "") + " ORDER BY trade_date")
    return [r[0] for r in c.execute(q, ([asof] if asof else []))]


def diagnose(code: str, db: str = "", asof: str | None = None) -> dict:
    c = sqlite3.connect(_db_path(db))
    cal = _calendar(c, asof)
    if not cal:
        c.close()
        return {"code": code, "verdict": "N/A", "reason": "两融库为空"}
    t = cal[-1]                      # asof 之前（或全局）最新的两融披露日
    idx = {d: i for i, d in enumerate(cal)}
    # 目标股在 t 的余额
    row_t = c.execute("SELECT rzye FROM margin_daily WHERE code=? AND trade_date=? AND rzye>0",
                      (code, t)).fetchone()
    if row_t is None:
        # 该股可能停牌/被移出两融名单：找它自己最近一条 ≤t
        r0 = c.execute("SELECT MAX(trade_date) FROM margin_daily WHERE code=? AND trade_date<=? AND rzye>0",
                       (code, t)).fetchone()
        if not r0[0]:
            c.close()
            return {"code": code, "verdict": "N/A",
                    "reason": "非两融标的或无两融记录（无数据≠不拥挤，fail-closed）",
                    "asof_margin_date": t}
        t = r0[0]
        row_t = c.execute("SELECT rzye FROM margin_daily WHERE code=? AND trade_date=? AND rzye>0",
                          (code, t)).fetchone()
    # 交易日偏移量
    ti = idx[t]
    def bal_at(code_, di):
        if ti - di < 0:
            return None
        d = cal[ti - di]
        r = c.execute("SELECT rzye FROM margin_daily WHERE code=? AND trade_date=? AND rzye>0",
                      (code_, d)).fetchone()
        return (d, r[0]) if r else None
    b5 = bal_at(code, 5)
    b20 = bal_at(code, 20)
    v5 = math.log(row_t[0]) - math.log(b5[1]) if b5 else None
    v20 = math.log(row_t[0]) - math.log(b20[1]) if b20 else None
    # 横截面（同日期的 t 与 t−20，一次查询）
    t20 = cal[ti - 20] if ti - 20 >= 0 else None
    cs: dict[str, float] = {}
    if t20 is not None:
        rows = c.execute(
            "SELECT code, trade_date, rzye FROM margin_daily "
            "WHERE trade_date IN (?,?) AND rzye>0", (t, t20)).fetchall()
        now = {x[0]: x[2] for x in rows if x[1] == t}
        past = {x[0]: x[2] for x in rows if x[1] == t20}
        for code_, rzye in now.items():
            p = past.get(code_)
            if p and p > 0 and rzye > 0:
                cs[code_] = math.log(rzye) - math.log(p)
    pct = None
    n_cs = len(cs)
    if v20 is not None and cs:
        # r = 横截面中 chg20 ≤ 该股的比例（0..1，越大越拥挤；≥0.90 = 顶十分位）
        above = sum(1 for v in cs.values() if v > v20)
        r = 1.0 - above / n_cs
        pct = r
    # 陈旧度（最新披露日距 today/asof 的自然日）
    today = asof or dt.date.today().isoformat()
    stale = (dt.date.fromisoformat(today) - dt.date.fromisoformat(t)).days
    # 档位（r = 拥挤分位：0.90=顶十分位，对齐 §14.14 D10；0.75~0.90 = 偏高）
    if v20 is None:
        verdict, reason = "N/A", ("chg20 不可计算（该股近 20 个交易日有余额缺口，"
                                  "可能新入两融名单/停牌/移出）")
    elif pct is None:
        verdict, reason = "N/A", "横截面不可计算（t−20 无全市场两融日）"
    elif pct >= TOP_DECILE:
        verdict = "CROWDED"
        reason = (f"chg20 拥挤分位 {pct:.0%}（≥90%，顶十分位）—— 融资拥挤区。"
                  "历史统计（2014-2026 CSI800 两融池，幸存者偏差使危险被低估）：该群体"
                  " CAGR 6.0% vs 全池 13.5%、最大回撤 58.7% vs 39.5% —— 拥挤脆弱标签。")
    elif pct >= HIGH_DECILE:
        verdict = "ELEVATED"
        reason = f"chg20 拥挤分位 {pct:.0%}（75%~90% 区），融资拥挤度偏高。"
    else:
        verdict = "NORMAL"
        reason = f"chg20 拥挤分位 {pct:.0%}（<75%），融资余额变化处于常态区。"
    c.close()
    return {
        "code": code,
        "verdict": verdict,
        "reason": reason,
        "asof_margin_date": t,
        "stale_days": stale,
        "chg5_ln": v5,
        "chg20_ln": v20,
        "crowding_percentile": pct,   # 越大越拥挤；0.90 = 顶十分位
        "cross_section_n": n_cs,
        "disclaimer": "风险标签，非买卖建议；基于历史统计，单股不可推断因果。",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("code")
    ap.add_argument("--db", default="")
    ap.add_argument("--asof", default=None)
    args = ap.parse_args()
    d = diagnose(args.code.strip(), args.db, args.asof)
    print(f"code: {d['code']}")
    for k in ("verdict", "reason", "asof_margin_date", "stale_days", "chg5_ln",
              "chg20_ln", "crowding_percentile", "cross_section_n", "disclaimer"):
        if k in d:
            v = d[k]
            if isinstance(v, float):
                v = f"{v:+.4f}" if k.endswith("_ln") else f"{v:.1%}"
            print(f"{k}: {v}")
    sys.exit(0)


if __name__ == "__main__":
    main()
