#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
宏观 → 行业择时：前置功效预检 · 2026-09-03 建立

背景（项目定案 2026-08-30 路线乙，见 equity-research/workflow.md §1/§4——宏观退出个股横截面）：
  宏观因子对齐【个股】横截面时，同一时点是同一个值 ⇒ 横截面零区分度（原理性），
  已退出个股横截面。正确用法 = 指数/行业【择时】独立子系统。
  ⇒ 当前 macro_ppi_yoy_ar nowcast 只到"预测数值"，是半截工程。

本脚本回答先决问题（在投入采集工程之前）：
  **这条路建起来，能不能检出我们真正在找的效应量？**

两条候选口径，功效差异极大，必须都算：
  A 行业横截面轮动：因子 = 行业 PPI-beta × PPI surprise，每月在 K 个行业上算 IC
      Var_noise(IC) ≈ 1/(K-1)   ← K 越大噪声越小，扩截面直接降 MDE
      N_eff = 月数（行业月度收益非重叠）
  B 纯时序择时：用 surprise 预测单一指数/行业未来收益，算相关系数
      se(r) ≈ 1/sqrt(N-3)，MDE(r) = Z/sqrt(N-3)   ← 与 K 无关，N 被月频锁死

纪律依据（quant-power-preflight skill + 本项目 SOP）：
  · 第①关三件套 = MDE + 效应量 + |效应|/MDE，比值 <1 判死
  · 判定类布尔表达式必须枚举全部取值做负向测试（第十类静默 bug）
  · 现实效应量必须给区间（悲观/中性/乐观），不给单点

用法：
  python macro_sector_timing.py --mode probe                # 板块可得性探查（联网）
  python macro_sector_timing.py --mode preflight            # 功效预检（离线，用 probe 结果或手工 K/N）
  python macro_sector_timing.py --mode preflight --K 86 --months 208
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# 工作区根 = _HERE.parents[3]（固定写法，与项目其余脚本一致）。
# 禁用「就近查找含 outputs/ 的父目录」启发式 —— 第廿七类 b 静默 bug：
# 该启发式会命中 my-deepseek-harness/ 下的残留 outputs 目录 ⇒ 路径静默错拼。
ROOT = _HERE.parents[3]

OUT_DIR = ROOT / "outputs" / "2026-09-03" / "sector_timing"
MACRO_DB = ROOT / "outputs" / "macro_cn_long.sqlite"
PROXY = "http://127.0.0.1:10809"
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
Z = 2.8006          # z(0.975) + z(0.80)：α=.05 双侧 + power=.8

# 现实效应量基准（横截面 IC 口径）。不给单点，给区间：
#   悲观 0.02 / 中性 0.04 / 乐观 0.07 —— 行业轮动的公开现实区间
#   另加两个本项目实测锚点：CSI800 基本面 IC 0.031（已证伪量级）、农业 0.111（板块周期，超常）
EFFECT_SCENARIOS = [
    ("悲观 0.020（行业轮动弱效应）", 0.020),
    ("中性 0.040（行业轮动典型）", 0.040),
    ("乐观 0.070（行业轮动强效应）", 0.070),
    ("锚点：CSI800 基本面实测 0.031", 0.031),
    ("锚点：农业板块周期实测 0.111（超常，不作为预期）", 0.111),
]


# ---------------------------------------------------------------- 网络
def _get(url: str, tries: int = 5):
    """带指数退避的 GET —— 代理抖动会间歇 RemoteDisconnected，必须重试。"""
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


def _get_text(url: str, tries: int = 5) -> str:
    last = None
    for i in range(tries):
        try:
            op = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
            r = op.open(urllib.request.Request(url, headers=UA), timeout=45)
            return r.read().decode("utf-8", "replace")
        except Exception as e:                                  # noqa: BLE001
            last = e
            time.sleep(0.8 * (2 ** i))
    raise RuntimeError(f"GET 失败（{tries} 次重试）: {last}")


# ---------------------------------------------------------------- probe
def fetch_boards(kind: str = "t:2") -> list[dict]:
    """东财板块列表。t:2 = 行业板块；t:3 = 概念板块。f12 已是完整板块码。"""
    d = _get("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=500&po=1&np=1"
             f"&fltt=2&invt=2&fid=f3&fs=m:90+{kind}&fields=f12,f13,f14,f2,f3,f20,f124")
    di = d.get("data") or {}
    out = []
    for r in (di.get("diff") or []):
        out.append(dict(code=r.get("f12"), market=r.get("f13"),
                        name=r.get("f14"), total_mkt=r.get("f20")))
    return out


def fetch_board_hist(code: str) -> dict:
    """板块指数日线：返回首/末日期与根数。用于判定历史长度是否够。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           f"secid=90.{code}&fields1=f1,f2,f3,f4,f5,f6"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
           "&klt=101&fqt=1&beg=19900101&end=20500101&lmt=100000")
    d = _get(url)
    kl = (((d.get("data") or {}).get("klines")) or [])
    if not kl:
        return dict(code=code, n=0, first=None, last=None)
    first = kl[0].split(",")[0]
    last = kl[-1].split(",")[0]
    return dict(code=code, n=len(kl), first=first, last=last)


def mode_probe(sample: int = 12) -> dict:
    boards = fetch_boards("t:2")
    print(f"[probe] 东财行业板块数 = {len(boards)}")
    for b in boards[:10]:
        print(f"        {b['code']}  {b['name']}")
    # 抽样验历史长度（全量 86 个太慢，抽样即可判断是否同构）
    step = max(1, len(boards) // sample)
    picks = boards[::step][:sample]
    hist = []
    for b in picks:
        try:
            h = fetch_board_hist(b["code"])
            h["name"] = b["name"]
            hist.append(h)
            print(f"[hist ] {b['code']} {b['name']:<10} n={h['n']:>6}  {h['first']} ~ {h['last']}")
        except Exception as e:                                  # noqa: BLE001
            print(f"[hist ] {b['code']} {b['name']} 失败: {str(e)[:60]}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    res = dict(kind="t:2", n_boards=len(boards), boards=boards, hist=hist)
    (OUT_DIR / "probe_boards.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[out  ] {OUT_DIR / 'probe_boards.json'}")

    # 宏观可用月数
    if MACRO_DB.exists():
        c = sqlite3.connect(MACRO_DB)
        for ind in ("ppi_yoy", "cpi_yoy", "manufacturing_pmi"):
            row = c.execute("select count(*),min(period),max(period) from macro_indicators "
                            "where indicator_name=?", (ind,)).fetchone()
            print(f"[macro] {ind:<18} n={row[0]}  {row[1]} ~ {row[2]}")
    return res


# ---------------------------------------------------------------- 功效预检
def mde_cross_section(sigma_true: float, K: int, N: int) -> float:
    """口径 A：横截面 IC 的 MDE。Var_noise ≈ 1/(K-1)（坑 5）。"""
    if K < 2 or N < 2:
        return float("nan")
    var_noise = 1.0 / (K - 1)
    sigma_obs = math.sqrt(sigma_true ** 2 + var_noise)
    return Z * sigma_obs / math.sqrt(N)


def mde_timeseries(N: int) -> float:
    """口径 B：时序相关系数 MDE。se(r)≈1/sqrt(N-3)。"""
    if N <= 3:
        return float("nan")
    return Z / math.sqrt(N - 3)


def min_K(sigma_true: float, effect: float, N: int):
    """反解：达到 |effect|/MDE>=1 所需的最小 K。返回 None 表示 K→∞ 也无解。"""
    rhs = (effect * math.sqrt(N) / Z) ** 2 - sigma_true ** 2
    if rhs <= 0:
        return None
    return int(math.ceil(1 + 1.0 / rhs))


def min_N(sigma_true: float, effect: float, K: int):
    """反解：达到 |effect|/MDE>=1 所需的最小月数。"""
    var_obs = sigma_true ** 2 + 1.0 / (K - 1)
    n = (Z * math.sqrt(var_obs) / effect) ** 2
    return int(math.ceil(n))


def mode_preflight(Ks: list[int], Ns: list[int]) -> dict:
    """配置矩阵：对每个 (K, N) 组合算 MDE 与 |效应|/MDE 比值。

    顺序纪律：**先定门槛，再找数据**。不要"找到什么数据就用什么"——
    那会在采集完之后才发现测不出（半截工程高发路径）。
    """
    print("=" * 88)
    print("宏观 → 行业择时 · 前置功效预检（配置矩阵）")
    print("=" * 88)
    print(f"K 候选 = {Ks}")
    print(f"N 候选 = {Ns}  （月数；行业月度收益非重叠 ⇒ N_eff = 月数）")
    print()

    # ---- 口径 B：纯时序择时（与 K 无关，只随 N 变）
    print("-" * 88)
    print("口径 B｜纯时序择时（macro surprise → 单一指数未来收益，相关系数）")
    print("-" * 88)
    print(f"{'N':>6}{'MDE(r)':>10}{'r=0.02':>10}{'r=0.04':>10}{'r=0.07':>10}  判定")
    b_ok = []
    for N in Ns:
        m = mde_timeseries(N)
        row = {e: e / m for e in (0.020, 0.040, 0.070)}
        ok = any(v >= 1.0 for v in row.values())
        b_ok.append(ok)
        print(f"{N:>6}{m:>10.4f}"
              f"{row[0.020]:>10.2f}{row[0.040]:>10.2f}{row[0.070]:>10.2f}"
              f"  {'✅' if ok else '❌ 全部测不出'}")

    # ---- 口径 A：横截面轮动（K, N 矩阵）
    print()
    print("-" * 88)
    print("口径 A｜行业横截面轮动（因子 = 行业 PPI-beta × PPI surprise）")
    print("-" * 88)
    hdr = f"{'K':>6}{'N':>6}{'Var_noise':>11}{'MDE@0.02':>11}{'MDE@0.04':>11}" \
          f"{'|e|/MDE .02':>13}{'|e|/MDE .04':>13}{'|e|/MDE .07':>13}"
    print(hdr)
    grid = []
    for K in Ks:
        for N in Ns:
            vn = 1.0 / (K - 1)
            mdes = {e: mde_cross_section(e, K, N) for e in (0.020, 0.040, 0.070)}
            ratios = {e: e / mdes[e] for e in mdes}
            grid.append(dict(K=K, N=N, var_noise=vn, mde=mdes, ratio=ratios))
            print(f"{K:>6}{N:>6}{vn:>11.5f}"
                  f"{mdes[0.020]:>11.4f}{mdes[0.040]:>11.4f}"
                  f"{ratios[0.020]:>13.2f}{ratios[0.040]:>13.2f}{ratios[0.070]:>13.2f}")

    # ---- 门槛反解：给定 N，达到可测所需的最小 K（σ_true 取 0.8×效应量）
    print()
    print("-" * 88)
    print("门槛反解｜达到 |效应|/MDE ≥ 1 所需的最小行业数 K（σ_true 取 0.8×效应量）")
    print("-" * 88)
    print(f"{'N':>6}{'效应 0.02':>14}{'效应 0.04':>14}{'效应 0.07':>14}")
    for N in Ns:
        cells = []
        for e in (0.020, 0.040, 0.070):
            k = min_K(0.8 * e, e, N)
            cells.append("∞ 无解" if k is None else str(k))
        print(f"{N:>6}{cells[0]:>14}{cells[1]:>14}{cells[2]:>14}")

    # ---- 结论（判定布尔必须枚举全部取值，禁止子串匹配）
    a_ok = [g for g in grid if g["ratio"][0.040] >= 1.0]        # 中性效应 0.04
    a_ok_pess = [g for g in grid if g["ratio"][0.020] >= 1.0]   # 悲观效应 0.02
    print()
    print("=" * 88)
    print("结论")
    print("=" * 88)
    if a_ok:
        kmin = min(g["K"] for g in a_ok)
        best = max(a_ok, key=lambda g: g["ratio"][0.040])
        print(f"口径 A（横截面轮动）：【判活】中性效应 0.040 下 {len(a_ok)}/{len(grid)} 组合可测")
        print(f"  · 最小可行 K = {kmin}（N={min(g['N'] for g in a_ok if g['K']==kmin)} 起）")
        print(f"  · 最优组合 K={best['K']}, N={best['N']} → 比值 {best['ratio'][0.040]:.2f}")
        if not a_ok_pess:
            print(f"  · ⚠ 悲观效应 0.020 下**无一组合可测** → 这是 0.02~0.04 之间的赌局，"
                  f"不是确定性机会")
    else:
        print("口径 A（横截面轮动）：【判死】中性效应 0.040 下无一组合可测")
    if any(b_ok):
        print("口径 B（纯时序择时）：【判活】")
    else:
        print("口径 B（纯时序择时）：【判死】—— MDE(r)≈0.16~0.26，"
              "而宏观择时现实 r 仅 0.05~0.10；月频锁死 N，扩数据无解")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    res = dict(Ks=Ks, Ns=Ns, grid=grid, B=dict(Ns=Ns, mde=[mde_timeseries(n) for n in Ns]))
    (OUT_DIR / "preflight.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[out  ] {OUT_DIR / 'preflight.json'}")
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["probe", "preflight"], required=True)
    ap.add_argument("--K", default="10,31,50,86,124,200,300", help="行业数（可逗号分隔）")
    ap.add_argument("--months", default="120,152,208,240", help="可用月数 N_eff（可逗号分隔）")
    ap.add_argument("--sample", type=int, default=12, help="probe 抽样板块数")
    a = ap.parse_args()
    if a.mode == "probe":
        mode_probe(a.sample)
    else:
        Ks = [int(x) for x in str(a.K).split(",")]
        Ns = [int(x) for x in str(a.months).split(",")]
        mode_preflight(Ks, Ns)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
