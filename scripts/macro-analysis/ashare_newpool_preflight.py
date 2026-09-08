#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新池前置功效预检（建池决策）· 2026-09-03 建立

用途：在投入采集工程之前，用【已实测的 σ_true】外推候选池配置的 MDE，
回答一个先决问题：**这个池子建起来，能不能检出我们真正在找的效应量？**

触发场景：SOP §13.0 判定某股为 TIER_D_OUT_OF_POOL（不在任何已验证池），
考虑为它新建一个同类池时，必须先过这一关。

纪律依据（quant-power-preflight skill）：
  第①关功效三件套 = MDE + 效应量 + |效应|/MDE
  坑 2  σ_true 退化解 → 双口径输出，**判定以保守口径为准**（非零分解值的中位数）
  坑 10 A 股披露集中 → N_eff = 2 × 年数（年报+一季报撞车），**不是** 4 × 年数
  坑 5  Var_noise ≈ 1/(N_cross − 1)，扩截面收益递减极快

用法：
  python ashare_newpool_preflight.py                      # 默认矩阵
  python ashare_newpool_preflight.py --effect 0.111 0.031 # 自定义基准效应量
  python ashare_newpool_preflight.py --probe-industry     # 实测行业成分股数量
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# 工作区根 = _HERE.parents[3]（固定写法，与项目其余脚本一致）。
# 禁用「就近查找含 outputs/ 的父目录」启发式 —— 第廿七类 b 静默 bug：
# 该启发式会命中 my-deepseek-harness/ 下的残留 outputs 目录 ⇒ 路径静默错拼。
ROOT = _HERE.parents[3]

REGISTRY = ROOT / "outputs" / "ashare_strategy_registry.sqlite"
PROXY = "http://127.0.0.1:10809"
UA = {"User-Agent": "Mozilla/5.0"}
Z = 2.8006          # z(0.975) + z(0.80)，α=.05 双侧 + power=.8
PPY_FUND = 2        # 基本面：A 股披露集中，每年仅 2 个非重叠观测（坑 10）
PPY_PRICE = 12      # 价量：月频非重叠口径（保守，日频重叠另算）


# ---------------------------------------------------------------- σ_true 来源
def load_sigma_true() -> dict:
    """读取已实测的 σ_true 分解值（不重新估计，只用实测）。

    ★ 数据源已改为 `stat_observations`（2026-09-04）。
    原因：σ_true 是**实测统计量**（这个池的横截面离散度有多大），不是"某策略能否
    赚钱"的结论。判死策略被物理删除时，σ_true 必须保留 —— 否则新池预检会失去基线。
    迁出脚本：`ashare_registry_migrate.py`。

    降级路径（旧库无 stat_observations 表时）会**显式 WARN**，不静默。
    见项目纪律：第十五类静默 bug「读不到就跳过」一律 fail-loud。
    """
    import sqlite3
    rows = []
    if not REGISTRY.exists():
        return dict(rows=[], cons=None, opt=None, note="注册表缺失", source="none")
    conn = sqlite3.connect(str(REGISTRY))
    source = "stat_observations"
    try:
        conn.row_factory = sqlite3.Row
        has_new = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='stat_observations'").fetchone() is not None
        if has_new:
            for r in conn.execute(
                    "SELECT source_key, pool_key, sigma_true, n_obs, effect, mde "
                    "FROM stat_observations WHERE sigma_true IS NOT NULL"):
                rows.append(dict(strategy=r["source_key"], pool=r["pool_key"],
                                 sigma_true=r["sigma_true"], n_obs=r["n_obs"],
                                 effect=r["effect"], mde=r["mde"]))
        else:
            # 降级：旧库未迁移。必须 WARN，不能静默回退。
            source = "strategy_registry(降级)"
            print("WARN: stat_observations 表不存在，已降级读 strategy_registry。"
                  "请运行 ashare_registry_migrate.py 迁出实测统计量。",
                  file=sys.stderr)
            for r in conn.execute(
                    "SELECT strategy_key, pool_key, gate1_sigma_true, gate1_n_obs, "
                    "gate1_effect, gate1_mde FROM strategy_registry "
                    "WHERE gate1_sigma_true IS NOT NULL"):
                rows.append(dict(strategy=r["strategy_key"], pool=r["pool_key"],
                                 sigma_true=r["gate1_sigma_true"],
                                 n_obs=r["gate1_n_obs"],
                                 effect=r["gate1_effect"], mde=r["gate1_mde"]))
    finally:
        conn.close()
    vals = [r["sigma_true"] for r in rows if r["sigma_true"] and r["sigma_true"] > 0]
    if not vals:
        return dict(rows=rows, cons=None, opt=None, note="无有效 σ_true", source=source)
    # 坑 2：保守口径 = 非零分解值的中位数；乐观口径 = 最小值
    return dict(rows=rows, cons=statistics.median(vals), opt=min(vals),
                n_sources=len(vals), source=source,
                note="保守口径=中位数（skill 坑2）")


def mde(sigma_true: float, n_cross: int, n_eff: float) -> float:
    """MDE = Z · sqrt(σ_true² + Var_noise) / sqrt(N_eff)"""
    var_noise = 1.0 / max(n_cross - 1, 1)
    sigma_obs = (sigma_true ** 2 + var_noise) ** 0.5
    return Z * sigma_obs / (n_eff ** 0.5)


def mde_matrix(sigma_true: float, widths: list[int], years: list[int],
               ppy: int, effects: list[tuple[str, float]]) -> list[dict]:
    out = []
    for n in widths:
        for y in years:
            n_eff = y * ppy
            m = mde(sigma_true, n, n_eff)
            rec = dict(n_cross=n, years=y, n_eff=n_eff, mde=m,
                       var_noise=1.0 / max(n - 1, 1))
            for lab, eff in effects:
                rec[f"ratio_{lab}"] = abs(eff) / m
            out.append(rec)
    return out


def min_width(sigma_true: float, effect: float, years: int, ppy: int,
              cap: int = 4000) -> int | None:
    """检出 |效应| ≥ effect 所需的最小截面宽度（|效应|/MDE ≥ 1）。"""
    n_eff = years * ppy
    for n in range(8, cap + 1):
        if mde(sigma_true, n, n_eff) <= abs(effect):
            return n
    return None


def classify(m: float) -> str:
    if m <= 0.02:
        return "功效充足"
    if m <= 0.05:
        return "部分可检出"
    if m <= 0.15:
        return "功效不足"
    return "功效严重不足"


# ---------------------------------------------------------------- 行业成分实测
def _get(url: str, tries: int = 5) -> dict:
    """带指数退避的 GET —— 代理抖动会间歇 RemoteDisconnected，必须重试。"""
    import time
    last = None
    for i in range(tries):
        try:
            r = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})).open(
                urllib.request.Request(url, headers=UA), timeout=45)
            return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:                                  # noqa: BLE001
            last = e
            time.sleep(0.8 * (2 ** i))                          # 0.8/1.6/3.2/6.4s
    raise RuntimeError(f"GET 失败（{tries} 次重试）: {last}")


def probe_industry(keyword: str) -> list[dict]:
    """东财行业板块成分股数量（必须实测，不能估）。"""
    d = _get("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=200&po=1&np=1"
             "&fltt=2&invt=2&fid=f3&fs=m:90+t:2&fields=f12,f14,f20")
    hits = [(r["f12"], r["f14"]) for r in ((d.get("data") or {}).get("diff") or [])
            if keyword in (r.get("f14") or "")]
    out = []
    for code, name in hits[:10]:
        try:
            d2 = _get("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=1&po=1&np=1"
                      "&fltt=2&invt=2&fid=f3&"
                      f"fs=b:{code}&fields=f12,f14")
            out.append(dict(board=code, name=name, n=((d2.get("data") or {}).get("total"))))
        except Exception as e:                                  # 单点失败不中断
            out.append(dict(board=code, name=name, n=None, err=str(e)[:60]))
    # f12 已是完整板块代码（如 BK0428），不得再拼 "BK" 前缀
    return out


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--widths", default="40,50,60,80,100,150,200")
    ap.add_argument("--years", default="8,10,13")
    ap.add_argument("--effect", nargs="*", default=["0.111:农业池净利同比(乐观基准)",
                                                    "0.0314:CSI800同因子(现实基准)"])
    ap.add_argument("--freq", choices=["fundamental", "price"], default="fundamental")
    ap.add_argument("--probe-industry", default="", help="按关键词实测东财行业成分股数")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if args.probe_industry:
        for r in probe_industry(args.probe_industry):
            print(f"  {r['board']}  {r['name']}  n={r.get('n')}  {r.get('err','')}")
        return 0

    sg = load_sigma_true()
    print("=== 新池前置功效预检 ===")
    for r in sg["rows"]:
        print(f"  实测源 {r['strategy']:24s} σ_true={r['sigma_true']:.5f} "
              f"(N_obs={r['n_obs']}, 效应 {r['effect']:+.4f}, MDE {r['mde']:.4f})")
    if sg["cons"] is None:
        print("  ⚠ 无有效 σ_true，无法外推"); return 1
    print(f"  保守口径 σ_true = {sg['cons']:.5f}（中位数）  "
          f"乐观口径 = {sg['opt']:.5f}（最小）")

    effects = []
    for e in args.effect:
        v, _, lab = e.partition(":")
        effects.append((lab or v, float(v)))
    ppy = PPY_FUND if args.freq == "fundamental" else PPY_PRICE
    widths = [int(x) for x in args.widths.split(",")]
    years = [int(x) for x in args.years.split(",")]

    print(f"\n口径：{args.freq}  N_eff = {ppy} × 年数  基准效应量：")
    for lab, eff in effects:
        print(f"  {lab}: {eff:+.4f}")

    print("\n--- MDE 矩阵（保守 σ_true）---")
    hdr = "  N\\年 " + "".join(f"{y:>10d}" for y in years)
    print(hdr)
    rows = mde_matrix(sg["cons"], widths, years, ppy, effects)
    by_n: dict[int, list[dict]] = {}
    for r in rows:
        by_n.setdefault(r["n_cross"], []).append(r)
    for n in widths:
        line = f"  {n:>4d} " + "".join(f"{r['mde']:>10.4f}" for r in by_n[n])
        print(line)

    print(f"\n--- |效应|/MDE ≥ 1 所需最小截面宽度（保守口径）---")
    verdict_rows = []
    for lab, eff in effects:
        for y in years:
            nmin = min_width(sg["cons"], eff, y, ppy)
            verdict_rows.append(dict(effect_lab=lab, effect=eff, years=y,
                                     n_eff=y * ppy, min_n_cross=nmin))
            print(f"  效应 {eff:+.4f} ({lab.split('(')[0]})  {y}年(N_eff={y*ppy}) "
                  f"→ 需 ≥ {nmin} 只" if nmin else
                  f"  效应 {eff:+.4f}  {y}年 → 4000 只内不可达")

    print("\n--- 候选配置逐格判定 ---")
    for r in rows:
        lab0, eff0 = effects[0]
        rat = r[f"ratio_{lab0}"]
        flag = "✓可检出" if rat >= 1 else "✗测不出"
        print(f"  N={r['n_cross']:>4d} {r['years']:>2d}年  N_eff={r['n_eff']:>3d}  "
              f"MDE={r['mde']:.4f} ({classify(r['mde'])})  "
              f"|最优基准|/MDE={rat:.2f} {flag}")

    if args.out:
        p = Path(args.out)
    else:
        d = ROOT / "outputs" / datetime.now().strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        p = d / "newpool_preflight.json"
    p.write_text(json.dumps(dict(generated_at=datetime.now().isoformat(timespec="seconds"),
                                 sigma=sg, freq=args.freq, ppy=ppy,
                                 effects=[dict(label=l, effect=e) for l, e in effects],
                                 matrix=rows, min_width=verdict_rows),
                            ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
