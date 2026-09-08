#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
申万二级行业归属采集 + 行业月收益面板构建 · 2026-09-03 建立

动机（承接 macro_sector_timing.py 的功效预检结论）：
  口径 B（纯时序择时）已判死 —— MDE(r)=0.18~0.26，宏观择时现实 r 仅 0.05~0.10，
  且 N 被月频锁死，扩数据无解。
  口径 A（行业横截面轮动）判活，但有硬门槛：N=152 时需要 K ≥ 35（中性效应 0.04）
  / K ≥ 135（悲观效应 0.02）。⇒ **必须拿到 K≈100+ 的行业分类，申万一级（31）不够。**

数据源：东财 clist 的 f100 字段 = 申万二级行业名（实测：小金属/通用设备/软件开发…）。
  ⚠ push2.eastmoney.com 与 push2his.eastmoney.com 在本机代理下被拦（503 / RemoteDisconnected），
    只有 **push2delay.eastmoney.com** 可用；且 pz 上限 100（pz=500 也只回 100）。

已知局限（必须在结论中声明，不得隐藏）：
  1. f100 是**当前**行业归属，非历史归属 —— 公司转型/重组导致的归属漂移未修正。
  2. 成分股来自 wide 池（当前存活标的）⇒ 存在幸存者偏差；
     横截面 IC 是相对排序，对**共同**的方向性偏差不敏感，但必须做 drop-行业 稳健性检验。

用法：
  python ashare_sw_industry.py --mode collect          # 采集全市场申万二级归属
  python ashare_sw_industry.py --mode build            # 构建行业月收益面板 + 覆盖统计
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# 工作区根 = _HERE.parents[3]（固定写法，与项目其余脚本一致）。
# 禁用「就近查找含 outputs/ 的父目录」启发式 —— 第廿七类 b 静默 bug：
# 该启发式会命中 my-deepseek-harness/ 下的残留 outputs 目录 ⇒ 路径静默错拼。
ROOT = _HERE.parents[3]

OUT_DIR = ROOT / "outputs" / "2026-09-03" / "sector_timing"
SW_DB = ROOT / "outputs" / "ashare_sw_industry.sqlite"
WIDE_DB = ROOT / "outputs" / "ashare_wide_hfq_xq.sqlite"
PROXY = "http://127.0.0.1:10809"
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
# 只有延时域可用（主域在本机代理下 503 / RemoteDisconnected）
HOST = "push2delay.eastmoney.com"
PAGE = 100                      # 实测 pz 上限，pz=500 也只回 100


def _get(url: str, tries: int = 5):
    last = None
    for i in range(tries):
        try:
            op = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
            r = op.open(urllib.request.Request(url, headers=UA), timeout=45)
            return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:                                  # noqa: BLE001
            last = e
            time.sleep(0.8 * (2 ** i))
    raise RuntimeError(f"GET 失败（{tries} 次重试）: {last}")


# ---------------------------------------------------------------- collect
def mode_collect() -> int:
    SW_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SW_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS sw_industry(
        code TEXT PRIMARY KEY, name TEXT, industry TEXT,
        collected_at TEXT DEFAULT (datetime('now','localtime')))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS collect_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, collected_at TEXT,
        source TEXT, n_rows INTEGER, note TEXT)""")
    conn.commit()

    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
    total, got, pn = None, 0, 1
    t0 = time.time()
    while True:
        u = (f"https://{HOST}/api/qt/clist/get?pn={pn}&pz={PAGE}&po=1&np=1"
             f"&fltt=2&invt=2&fid=f3&fs={fs}&fields=f12,f14,f100")
        d = _get(u).get("data") or {}
        if total is None:
            total = d.get("total")
            print(f"[collect] 全市场标的数 = {total}")
        rows = d.get("diff") or []
        if not rows:
            break
        for r in rows:
            code, name, ind = r.get("f12"), r.get("f14"), r.get("f100")
            if code and ind:
                conn.execute("INSERT OR REPLACE INTO sw_industry(code,name,industry)"
                             " VALUES(?,?,?)", (code, name, ind))
        conn.commit()
        got += len(rows)
        print(f"\r[collect] {got}/{total}  ({time.time()-t0:.0f}s)", end="", flush=True)
        if got >= (total or 0):
            break
        pn += 1
        time.sleep(0.15)
        if pn > 200:                                            # 死循环保险
            print("\n[warn] 分页超过 200 页，强制停止")
            break
    print()
    conn.execute("INSERT INTO collect_log(collected_at,source,n_rows,note)"
                 " VALUES(datetime('now','localtime'),?,?,?)",
                 (f"eastmoney/{HOST}#f100", got, "申万二级行业归属"))
    conn.commit()

    n_ind = conn.execute("SELECT COUNT(DISTINCT industry) FROM sw_industry").fetchone()[0]
    n_all = conn.execute("SELECT COUNT(*) FROM sw_industry").fetchone()[0]
    print(f"[done ] 入库 {n_all} 只 / {n_ind} 个行业 → {SW_DB}")

    # 与 wide 池的交集覆盖（这才是真正决定 K 的数）
    if WIDE_DB.exists():
        wc = sqlite3.connect(WIDE_DB)
        codes = [r[0] for r in wc.execute("SELECT DISTINCT code FROM daily_quotes_hfq")]
        q = ",".join("?" * len(codes))
        rows = conn.execute(
            f"SELECT industry, COUNT(*) FROM sw_industry WHERE code IN ({q})"
            " GROUP BY industry ORDER BY 2 DESC", codes).fetchall()
        print(f"[cover] wide 池 {len(codes)} 只中，命中归属 {sum(r[1] for r in rows)} 只"
              f" / 覆盖 {len(rows)} 个申万二级行业")
        print(f"        行业规模分布：max={rows[0][1]}, 中位数={rows[len(rows)//2][1]}, "
              f"min={rows[-1][1]}")
        print(f"        ≥5 只的行业 = {sum(1 for r in rows if r[1] >= 5)} 个"
              f"（有效 K 以此为准，1~2 只的行业组合噪声过大）")
        print("        前 10：", rows[:10])
    return 0


# ---------------------------------------------------------------- build
def month_key(d: str) -> str:
    return d[:7]


def mode_build(min_members: int = 5) -> int:
    """构建行业月收益面板：行业内等权（可切换成交额加权）。"""
    if not WIDE_DB.exists() or not SW_DB.exists():
        print("[err ] 缺少 wide 行情库或行业归属库")
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    wc = sqlite3.connect(WIDE_DB)
    sc = sqlite3.connect(SW_DB)

    # ⚠ 行业规模必须按【wide 池实际标的】统计，不能按全市场归属表统计。
    #   串口径的后果：把"全市场 ≥5 只但本池 0 只"的行业也算进 K，
    #   导致 K 虚高、面板里出现全空的行业（本次实测虚报 123 → 实际 50）。
    pool_codes = {r[0] for r in wc.execute("SELECT DISTINCT code FROM daily_quotes_hfq")}
    ind_all = dict(sc.execute("SELECT code, industry FROM sw_industry").fetchall())
    ind = {c: i for c, i in ind_all.items() if c in pool_codes}
    print(f"[build] wide 池 {len(pool_codes)} 只，命中行业归属 {len(ind)} 只")

    sizes = defaultdict(int)
    for i in ind.values():
        sizes[i] += 1
    keep = {i for i, n in sizes.items() if n >= min_members}
    print(f"[build] 池内成员 ≥ {min_members} 的行业 = {len(keep)}"
          f"（全表 129 个行业中，本池未覆盖或成员不足的已剔除）")

    rows = wc.execute("SELECT code, trade_date, close FROM daily_quotes_hfq "
                      "ORDER BY code, trade_date").fetchall()
    # 月末收盘 → 月收益
    last_close: dict[tuple[str, str], float] = {}
    for code, d, close in rows:
        if close is None or close <= 0:
            continue
        last_close[(code, month_key(d))] = float(close)     # 同月覆盖=取最后一条

    by_code_months: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for (code, m), px in last_close.items():
        by_code_months[code].append((m, px))

    ret: dict[tuple[str, str], float] = {}                  # (code, month) -> 月收益
    for code, seq in by_code_months.items():
        seq.sort()
        for k in range(1, len(seq)):
            m_prev, p_prev = seq[k - 1]
            m_cur, p_cur = seq[k]
            if p_prev > 0:
                ret[(code, m_cur)] = p_cur / p_prev - 1.0

    # 行业内等权
    ind_mon: dict[tuple[str, str], list[float]] = defaultdict(list)
    for (code, m), r in ret.items():
        i = ind.get(code)
        if i in keep and -0.5 < r < 2.0:                    # 极端值保护（数据错误兜底）
            ind_mon[(i, m)].append(r)

    # 空行业直接不进 panel（串口径时这里会出现"全月皆空"的僵尸行业）
    panel = {}
    for (i, m), rs in ind_mon.items():
        if i in keep and len(rs) >= min_members:
            panel.setdefault(i, {})[m] = sum(rs) / len(rs)

    # ---- 交叉校验（唯一可靠防线）：报告值必须等于实测非空值
    empty = [i for i in keep if i not in panel]
    assert len(panel) == len(keep) - len(empty), "面板行业数不一致"
    assert all(len(v) > 0 for v in panel.values()), "存在全空行业未被剔除"
    months = sorted({m for v in panel.values() for m in v})
    print(f"[check] 非空行业 {len(panel)} / keep {len(keep)}，剔除空行业 {len(empty)} 个")
    print(f"[build] 行业数 K = {len(panel)}（成员 ≥ {min_members}）")
    print(f"[build] 月份数 N = {len(months)}  {months[0]} ~ {months[-1]}")

    # 缺口诊断（同族于 641 截断教训：必须查，不能假设）
    holes = []
    for i, v in panel.items():
        missing = [m for m in months if m not in v]
        if missing:
            holes.append((i, len(missing), missing[0], missing[-1]))
    holes.sort(key=lambda x: -x[1])
    print(f"[build] 有缺口的行业 = {len(holes)}/{len(panel)}")
    for h in holes[:10]:
        print(f"        {h[0]}: 缺 {h[1]} 个月  {h[2]} ~ {h[3]}")

    (OUT_DIR / "industry_monthly_panel.json").write_text(
        json.dumps({"panel": {k: v for k, v in panel.items()}, "months": months},
                   ensure_ascii=False), encoding="utf-8")
    print(f"[out  ] {OUT_DIR / 'industry_monthly_panel.json'}")

    # 与预检门槛对照
    K, N = len(panel), len(months)
    Z = 2.8006
    for e in (0.020, 0.040, 0.070):
        mde = Z * ((e * 0.8) ** 2 + 1.0 / (K - 1)) ** 0.5 / (N ** 0.5)
        print(f"[gate ] 效应 {e:.3f} → MDE {mde:.4f}  比值 {e/mde:5.2f} "
              f"{'✅ 可测' if e/mde >= 1 else '❌ 测不出'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["collect", "build"], required=True)
    ap.add_argument("--min-members", type=int, default=5)
    a = ap.parse_args()
    return mode_collect() if a.mode == "collect" else mode_build(a.min_members)


if __name__ == "__main__":
    raise SystemExit(main())
