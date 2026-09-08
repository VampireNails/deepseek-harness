# -*- coding: utf-8 -*-
"""A股农业窄池 —— Monte Carlo 零分布检验（决定性一击）。

【要回答的两个问题】
前面三关的结论是「双样本 0/28，最优 IR 0.59（全样本）/ 0.19（2018子样本）」，
归因又证明超额 100% 来自风格押注。但还剩下两个没回答干净的问题：

  Q1  IR 0.59 是【真信号】还是【运气】？
      —— 随机选股能跑出多高的 IR？如果随机也能跑出 0.59，那它就是噪声。

  Q2  风格溢价本身有多大？
      —— 把股票标签整体打乱（保留信号的时间序列结构与换手节奏，只让
         "信号值配错真实股票"）。若零分布中心显著为正，说明风格溢价是真的；
         此时真实 IR 是否还显著超出零分布，决定了「我们是否只是抽到了风格
         溢价的一个好样本」。

【两个零假设口径】
  ① random：每期随机选 top20% 持有（带同样的缓冲带、再平衡节奏、成本）
     → 纯随机选股的 IR 分布。回答 Q1。这是最公平、最直接的口径：
       持仓数相同、换手率相同、成本相同，唯一差别是「选股有没有信息」。

  ② shift：对股票维度施加【一个全局排列】，应用到所有期
     → 保留信号的完整时间序列路径（因此保留风格结构与换手特性），
       只破坏「哪个信号值对应哪只真实股票」。回答 Q2。
     ⚠️ 不能用「每期独立置换」：那会让持仓每期全变，换手率暴涨、成本吞噬
        一切，零分布被人为压到负偏，反而让真实 IR 显得更显著（不公平）。

【为什么这个检验比 IC 的 t 统计更可信】
回测是从真实组合收益时间序列直接算 IR，它天然吃进了截面相关、序列相关、
换手成本的全部影响，不需要任何分布假设。零分布用同样的回测引擎构造，
因此是【同口径对比】—— 这绕开了 IC t 统计被截面相关/猪周期虚高的整个问题。
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

import numpy as np

# ⚠️ 路径必须写成 _HERE.parents[3]（_HERE = 脚本所在目录）。
#    直接写 Path(__file__).resolve().parents[2] 会落在 git 仓库内
#    （deepseek-harness/outputs/），把产物写进版本库 —— 已发生过一次。
_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]
sys.path.insert(0, str(_HERE))

from ashare_agri_backtest import (  # noqa: E402
    DB, PRICE_TABLE, STRATEGIES, MIN_CROSS_DEFAULT, COST_SCENARIOS,
    load_panel, compute_factors, build_signal, board_of, build_tradable,
)
from ashare_agri_buffer import run_buffer  # noqa: E402

OUT_DIR = ROOT / "outputs" / "2026-09-02"

# 最优配置（来自 ashare_agri_buffer 全样本扫描）
BEST = {"strategy": "comp_turn_mom", "holding": 20, "buy": 0.20, "sell": 0.35}
COST_MID = 0.005


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-mc", type=int, default=200, help="Monte Carlo 次数")
    ap.add_argument("--min-cross", type=int, default=MIN_CROSS_DEFAULT)
    ap.add_argument("--holding", type=int, default=BEST["holding"])
    ap.add_argument("--buy", type=float, default=BEST["buy"])
    ap.add_argument("--sell", type=float, default=BEST["sell"])
    ap.add_argument("--start", default="", help="子样本起始日，如 2018-01-01")
    ap.add_argument("--seed", type=int, default=20260902)
    args = ap.parse_args()

    print("=" * 78)
    print("A股农业窄池 · Monte Carlo 零分布检验")
    print("=" * 78)

    conn = sqlite3.connect(str(DB), timeout=30)
    try:
        qc_bad = {r[0] for r in conn.execute("SELECT code FROM hfq_qc WHERE bad = 1")}
    except sqlite3.OperationalError:
        qc_bad = set()
    dates, codes, _o, close, volume, amount, turn = load_panel(conn, PRICE_TABLE)
    conn.close()

    # ⚠️ 必须用 build_tradable（与 buffer 扫描口径严格一致）。
    #    手写 isfinite(close) 会纳入停牌/无成交额/次新股未上市单元，IR 虚高 40%。
    tradable = build_tradable(close, volume, amount, codes, qc_bad,
                              exclude_b=True, exclude_limit=False)
    T, M = close.shape
    # ⚠️ board_of 返回 (板块名, 涨跌幅限制) 元组，要取 [1] 才是数值
    lim_per_col = np.array([board_of(c)[1] for c in codes], dtype=float)
    print(f"可交易单元占比: {tradable.mean():.1%}")
    t0 = 0
    if args.start:
        w = np.where(dates >= args.start)[0]
        t0 = int(w[0]) if len(w) else 0
    print(f"\n面板 {T} 天 × {M} 只   区间 {dates[t0]} ~ {dates[-1]}"
          f"{'（子样本）' if args.start else '（全样本）'}")
    print(f"配置: {BEST['strategy']}  H={args.holding}  缓冲 {args.buy:.2f}→{args.sell:.2f}"
          f"  成本 {COST_MID:.2%}")

    factors = compute_factors(close, volume, amount, turn)
    sig_key = BEST["strategy"]
    # STRATEGIES 是 (key, comps, label) 三元组列表
    comp_list = next((v for k, v, _lab in STRATEGIES if k == sig_key), None)
    if comp_list is None:
        raise SystemExit(f"未找到策略 {sig_key}")
    signal = build_signal(factors, comp_list, tradable, args.min_cross)

    rng = np.random.default_rng(args.seed)

    def stats_of(sig):
        """⚠️ run_buffer 返回的键名是 ir / net_excess_ann / annual_turnover_x，
        与 ashare_agri_backtest.run_backtest 的嵌套结构（excess_net.xxx）不同。"""
        r = run_buffer(sig, close, dates, args.holding, COST_MID, tradable,
                       args.min_cross, args.buy, args.sell, t0=t0,
                       lim_per_col=lim_per_col)
        if not r:
            return None
        return {
            "ir": float(r["ir"]),
            "ann": float(r["net_excess_ann"]),
            "turnover": float(r["annual_turnover_x"]),
            "n_periods": int(r.get("n_periods", 0)),
            "pos_years": int(r.get("pos_years", 0)),
            "n_years": int(r.get("n_years", 0)),
        }

    def make_ar1(mask, rho, gen):
        """AR(1) 随机信号：rho 越大越平滑 → 持仓越稳定 → 换手越低。

        ⚠️ 为什么需要它：纯 iid 随机信号每期都重排，缓冲带拦不住，换手会跑到
           8.0x（真实策略只有 5.4x），成本凭空多出 1.3pp，零分布被人为压低，
           反过来把真实 IR 衬托得虚假显著。必须先把换手率校准到同一水平，
           零分布才有可比性。
        """
        e = gen.standard_normal(signal.shape)
        x = np.empty_like(e)
        x[0] = e[0]
        for t in range(1, signal.shape[0]):
            x[t] = rho * x[t - 1] + math.sqrt(max(1.0 - rho ** 2, 0.0)) * e[t]
        return np.where(mask, x, np.nan)

    real = stats_of(signal)
    print(f"\n真实信号:  IR = {real['ir']:.3f}   年化净超额 = {real['ann']:+.2%}"
          f"   单边换手 = {real['turnover']:.1f}x")

    # ---------------------------------------------------- ① random
    print("\n" + "=" * 78)
    print(f"① random 口径：AR(1) 随机信号，换手校准到 {real['turnover']:.1f}x，B={args.n_mc}")
    print("=" * 78)
    rnd = np.isfinite(signal)
    # 校准 rho（固定种子，保证可复现）
    lo, hi = 0.0, 0.995
    for _ in range(9):
        mid = 0.5 * (lo + hi)
        s = stats_of(make_ar1(rnd, mid, np.random.default_rng(12345)))
        if s is None:
            break
        # 换手偏高 → 需要更平滑 → 增大 rho
        if s["turnover"] > real["turnover"]:
            lo = mid
        else:
            hi = mid
    rho_star = 0.5 * (lo + hi)
    chk = stats_of(make_ar1(rnd, rho_star, np.random.default_rng(12345)))
    print(f"    校准 rho = {rho_star:.4f}  →  换手 {chk['turnover']:.1f}x"
          f"（真实 {real['turnover']:.1f}x）")

    irs_r, ann_r, tov_r = [], [], []
    for b in range(args.n_mc):
        rs = make_ar1(rnd, rho_star, rng)
        s = stats_of(rs)
        if s:
            irs_r.append(s["ir"]); ann_r.append(s["ann"]); tov_r.append(s["turnover"])
        if (b + 1) % 25 == 0:
            print(f"    ... {b+1}/{args.n_mc}")
    irs_r = np.array(irs_r); ann_r = np.array(ann_r); tov_r = np.array(tov_r)

    def summarize(name, arr_ir, arr_ann, arr_tov, real_ir, real_ann, real_tov):
        p_two = float((np.abs(arr_ir) >= abs(real_ir)).mean())
        p_one = float((arr_ir >= real_ir).mean())
        pct = float((arr_ir < real_ir).mean())
        print(f"\n  【{name}】  B={len(arr_ir)}")
        print(f"    真实:      IR {real_ir:+.3f}   年化 {real_ann:+.2%}   换手 {real_tov:.1f}x")
        print(f"    零分布:    IR 均值 {arr_ir.mean():+.3f}  std {arr_ir.std():.3f}"
              f"  [{np.percentile(arr_ir,2.5):+.3f}, {np.percentile(arr_ir,97.5):+.3f}] 95%")
        print(f"    零分布:    年化均值 {arr_ann.mean():+.2%}  std {arr_ann.std():.2%}"
              f"   换手均值 {arr_tov.mean():.1f}x")
        print(f"    真实 IR 分位: {pct:.1%}")
        print(f"    单尾 p (随机 >= 真实)      = {p_one:.4f}")
        print(f"    双尾 p (|随机| >= |真实|)  = {p_two:.4f}")
        return {
            "real_ir": real_ir, "real_ann": real_ann, "real_turnover": real_tov,
            "null_ir_mean": float(arr_ir.mean()), "null_ir_std": float(arr_ir.std()),
            "null_ir_p2_5": float(np.percentile(arr_ir, 2.5)),
            "null_ir_p97_5": float(np.percentile(arr_ir, 97.5)),
            "null_ann_mean": float(arr_ann.mean()), "null_ann_std": float(arr_ann.std()),
            "null_turnover_mean": float(arr_tov.mean()),
            "real_percentile": pct, "p_one_sided": p_one, "p_two_sided": p_two,
            "B": int(len(arr_ir)),
        }

    res_r = summarize("random · 纯随机选股", irs_r, ann_r, tov_r,
                      real["ir"], real["ann"], real["turnover"])

    # ---------------------------------------------------- ② shift
    print("\n" + "=" * 78)
    print(f"② shift 口径：股票维度全局置换（保留信号时序与换手结构），B={args.n_mc}")
    print("=" * 78)
    irs_s, ann_s, tov_s = [], [], []
    for b in range(args.n_mc):
        perm = rng.permutation(M)
        s = stats_of(signal[:, perm])
        if s:
            irs_s.append(s["ir"]); ann_s.append(s["ann"]); tov_s.append(s["turnover"])
        if (b + 1) % 25 == 0:
            print(f"    ... {b+1}/{args.n_mc}")
    irs_s = np.array(irs_s); ann_s = np.array(ann_s); tov_s = np.array(tov_s)
    res_s = summarize("shift · 风格置换", irs_s, ann_s, tov_s,
                      real["ir"], real["ann"], real["turnover"])

    # ---------------------------------------------------- 判定
    print("\n" + "=" * 78)
    print("判定")
    print("=" * 78)
    print(f"\n  【判定基准】两个口径回答不同问题，以【shift】为主判据：")
    print(f"    random = 信号里有没有信息（换手已校准到 {real['turnover']:.1f}x，可比）")
    print(f"    shift  = 扣掉风格溢价后还剩什么（换手天然一致，最严格）")

    pr, ps = res_r["p_one_sided"], res_s["p_one_sided"]
    if pr < 0.05:
        print(f"\n  ✓ Q1: 真实 IR {real['ir']:.3f} 显著超出随机选股（p={pr:.4f}）")
        print(f"    → 信号里确实有【非随机的选股信息】，不是纯运气")
    else:
        print(f"\n  ✗ Q1: 真实 IR {real['ir']:.3f} 未超出随机选股（p={pr:.4f}）")
        print(f"    → 无法区分于运气，策略不成立")

    if ps < 0.05:
        print(f"\n  ✓ Q2: 真实 IR 显著超出【风格置换】零分布（p={ps:.4f}）")
        print(f"    → 收益不只是风格溢价，还包含真实的【个股选择】成分")
    else:
        print(f"\n  ✗ Q2: 真实 IR 未超出风格置换零分布（p={ps:.4f}）")
        print(f"    → 零分布中心 {res_s['null_ir_mean']:+.3f}，真实策略没能显著跑赢")
        print(f"      「把同一套风格结构配到随机股票上」的结果")
        print(f"    → 说明收益主要来自风格/结构，而非个股选择能力")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = args.start.replace("-", "") if args.start else "full"
    out = {
        "meta": {
            "strategy": sig_key, "holding": args.holding,
            "buy": args.buy, "sell": args.sell, "cost": COST_MID,
            "start": args.start or "full", "n_mc": args.n_mc, "seed": args.seed,
            "date_range": f"{dates[t0]} ~ {dates[-1]}",
        },
        "real": real,
        "random": {**res_r, "ar1_rho": float(rho_star)},
        "shift": res_s,
    }
    op = OUT_DIR / f"ashare_agri_mc_{tag}.json"
    op.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出: {op}")


if __name__ == "__main__":
    main()
