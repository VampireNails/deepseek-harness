#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ashare_agri_backtest.py — A 股农业池第三关：经济可行性（扣成本回测）

与港股版 equity_layer_backtest.py 的**三处关键差异**（不是换数据源，是换判定口径）：

1. 【不可做空】A 股农业股（中小市值为主）融券券源近乎为零，多空组合 Q5-Q1
   的收益在 A 股**不可实现**。港股版把它当主口径是错误的推广。
   本脚本主口径 = 纯多头 Q5 相对「池内等权基准」的超额收益；
   多空 Q5-Q1 仅作为「信息含量」参考输出，不参与可行性判定。

2. 【涨跌停约束】A 股有 ±10%/±20% 涨跌幅限制。信号发出后若当日涨停则买不进、
   跌停则卖不出。脚本统计信号组的涨跌停占比作为**可执行性折扣**，
   并提供 --exclude-limit 选项做保守口径（剔除当日涨停股）。

3. 【后复权价】价格序列必须取后复权（hfq），**禁止使用前复权**。
   实测前复权在长周期 + 高分红标的上会被扣成负数：牧原股份 002714 前复权
   最低 −3.60 元、506 个交易日价格 ≤0.5，另有 23 只最低价被压到 0~4 元区间，
   日收益出现 ±833% 的荒谬值。这是**股票级**缺陷，截断时间区间无法规避。
   后复权以上市首日为锚向后累加，价格恒为正、日收益必然落在涨跌停限内
   —— 这本身就是强校验（本脚本的 hfq_qc 即据此判定数据源是否异常）。
   数据源：腾讯 fqkline hfq（东财域名在批量采集后被代理拒绝，
   已与东财 badj 在 000798 上交叉校验：日收益差异中位数 2.8e-4、最大 1.8e-3）。

成本口径（2026 年 A 股实际费率，往返一次 = 买+卖）
-----------------------------------------------
    印花税   卖出单边 0.05%（2023-08-28 起由 0.10% 下调）
    过户费   沪深双边各 0.001%  → 往返 0.002%
    佣金     双边各 ~0.025%（万 2.5）→ 往返 0.05%
    滑点     中小市值 + 农业股波动大，单边 0.10%~0.35%
  → 三档往返合计：低 0.30% / 中 0.50% / 高 0.80%
    与港股档位数值一致，便于跨市场同口径对比。

用法
----
    python ashare_agri_backtest.py
    python ashare_agri_backtest.py --exclude-limit
    python ashare_agri_backtest.py --min-cross 30 --start 2018-01-01
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.stats import rankdata

from equity_narrow_pool_power import OUT_DIR, HOLDING_PERIODS, newey_west_t, _roll_mean
from ashare_hfq_access import qc_bad_codes, QcMissing

# 路径：先用 .parent 取到 macro-analysis 目录，再上溯 3 层到工作区根。
# ⚠️ 直接写 Path(__file__).resolve().parents[3] 会少一层，报 "unable to open database file"。
_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]
# ⚠️ 独立库：后复权行情由 ashare_badj_collect.py 写入 ashare_agri_hfq_xq.sqlite，
#    不写主库（主库可能被另一会话的采集进程持有写锁，且「学习产物只写独立库」）
DB = ROOT / "outputs" / "ashare_agri_hfq_xq.sqlite"

COST_SCENARIOS = [
    ("低 0.30%（低滑点）", 0.0030),
    ("中 0.50%（含常规滑点）", 0.0050),
    ("高 0.80%（含冲击成本）", 0.0080),
]

# 符号取自 ashare_agri_validate.py 的中性化 IC 方向（IC 为负 → 取 -1）
STRATEGIES = [
    ("reversal_5d", [("reversal_5d", +1)], "单因子·5日反转"),
    ("momentum_20d", [("momentum_20d", -1)], "单因子·20日反转"),
    ("turnover_level_20d", [("turnover_level_20d", -1)], "单因子·低换手"),
    ("volatility_20d", [("volatility_20d", -1)], "单因子·低波动"),
    ("price_to_ma20", [("price_to_ma20", -1)], "单因子·价格/MA20"),
    ("comp_rev_lowvol", [("reversal_5d", +1), ("volatility_20d", -1)],
     "合成·反转+低波"),
    ("comp_turn_mom", [("turnover_level_20d", -1), ("momentum_20d", -1)],
     "合成·低换手+反转"),
]

N_QUANTILES = 5
MIN_CROSS_DEFAULT = 30


PRICE_TABLE = "daily_quotes_hfq"   # 后复权（腾讯 fqkline hfq）
LIQ_TABLE = "daily_liquidity"      # 成交量/成交额/换手率（东财，从主库复制）


def board_of(code: str):
    """返回 (板块, 涨跌幅限制)。北交所 ±30%、创业板/科创板 ±20%、主板 ±10%。"""
    if code.startswith(("920", "430", "83", "87", "92")):
        return "BSE", 0.30
    if code.startswith(("200", "900")):
        return "BSHARE", 0.10
    if code.startswith(("300", "301", "688")):
        return "CREA/STAR", 0.20
    return "MAIN", 0.10


def load_panel(conn: sqlite3.Connection, table: str = PRICE_TABLE,
               amount_fallback: bool = True):
    """价格取后复权表，成交量/额/换手率从 daily_liquidity 按 (code,trade_date) 关联。

    ⚠️ 腾讯 hfq 接口只返回成交量（手），没有成交额与换手率，而 amihud、
       换手率等因子必须用成交额 → 必须关联，不能用 hfq 表自带的 volume。

    ★ amount_fallback（为宽池新增）
        宽基池没有 daily_liquidity 表（那是农业池从主库复制来的）。此时若直接
        LEFT JOIN 会报 "no such table"。开启本开关后回退为：
            amount ≈ volume × 100 × close     turn = NaN
        ⚠️ 近似口径，后复权价与真实价的比值逐股不同 → **绝对量级不可信**。
        只可用于：①可交易性判定（是否非零）②流动性风格基的截面排序。
        **严禁**用于 amihud 的绝对量级或任何跨期比较。
    """
    has_liq = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (LIQ_TABLE,)).fetchone())
    if has_liq:
        rows = conn.execute(
            f"SELECT p.trade_date, p.code, p.open, p.close, "
            f"       l.volume, l.amount, l.turnover_pct "
            f"FROM {table} p "
            f"LEFT JOIN {LIQ_TABLE} l ON l.code = p.code "
            f"          AND l.trade_date = p.trade_date "
            f"WHERE p.close IS NOT NULL ORDER BY p.trade_date, p.code"
        ).fetchall()
        # ★ 表存在 ≠ 关联得上。实测宽基池库里 daily_liquidity 是
        #   ashare_badj_collect.copy_liquidity 从【农业池主库整表复制】来的，
        #   与宽池代码几乎零交集 → 命中率 ~0，amount 全 NULL，
        #   build_tradable 的 (a20 > 0) 会把可交易单元判成 0.0%，
        #   且【不报错、只是后续所有统计静默为空】。必须查命中率。
        if rows and amount_fallback:
            # ★ 命中率必须以 turnover_pct(r[6]) 为准，不能用 amount(r[5])：
            #   派生换手率行只写了 turnover_pct、amount 留 NULL，用 amount 判会
            #   误算成 2.3% 而丢弃全部派生换手率（宽池 comp_turn_mom 由此被静默拒掉）。
            hit = sum(1 for r in rows if r[6] is not None) / len(rows)
            if hit < 0.50:
                print(f"（{LIQ_TABLE} 存在但 turnover_pct 关联命中率仅 {hit:.1%} → 判定为不可用，"
                      f"回退 volume×100×close）")
                rows = conn.execute(
                    f"SELECT trade_date, code, open, close, volume, NULL, NULL "
                    f"FROM {table} WHERE close IS NOT NULL "
                    f"ORDER BY trade_date, code"
                ).fetchall()
    elif amount_fallback:
        print(f"（{LIQ_TABLE} 不存在 → amount 回退为 volume×100×close，"
              f"仅用于可交易判定与截面排序，绝对量级不可信）")
        rows = conn.execute(
            f"SELECT trade_date, code, open, close, volume, NULL, NULL "
            f"FROM {table} WHERE close IS NOT NULL ORDER BY trade_date, code"
        ).fetchall()
    else:
        raise SystemExit(f"缺少 {LIQ_TABLE}，且未开启 amount_fallback")
    if not rows:
        raise SystemExit(f"{table} 为空，请先运行 ashare_badj_collect.py")
    dates = sorted({r[0] for r in rows})
    codes = sorted({r[1] for r in rows})
    di = {d: i for i, d in enumerate(dates)}
    ci = {c: j for j, c in enumerate(codes)}
    n, m = len(dates), len(codes)
    open_ = np.full((n, m), np.nan)
    close = np.full((n, m), np.nan)
    volume = np.full((n, m), np.nan)
    amount = np.full((n, m), np.nan)
    turn = np.full((n, m), np.nan)
    for d, c, o, cl, v, a, t in rows:
        i, j = di[d], ci[c]
        open_[i, j] = o
        close[i, j] = cl
        volume[i, j] = v if v is not None else 0.0
        if a is not None:
            amount[i, j] = a
        elif amount_fallback and v and cl and np.isfinite(cl):
            # 回退：成交额 ≈ 成交量(手) × 100 股/手 × 收盘价
            amount[i, j] = float(v) * 100.0 * float(cl)
        if t is not None:
            turn[i, j] = t
    return np.array(dates), np.array(codes), open_, close, volume, amount, turn


def build_tradable(close, volume, amount, codes, qc_bad,
                   exclude_b=True, exclude_limit=False, lim_per_col=None,
                   min_history=0):
    """可交易掩码的【唯一权威构造】。所有脚本必须复用，不要各自手写。

    ⚠️ 历史教训：ashare_agri_mc.py 第一版只写了 np.isfinite(close)，
       把停牌日、无成交额日、次新股未上市区间全部当成可交易单元，
       真实 IR 从 0.59 虚高到 0.829（+40%）。任何"自己写一遍"的冲动
       都会重蹈覆辙 —— 差异极其隐蔽，因为回测照样能跑出漂亮数字。

    组成（顺序有意义）：
      1. 不停牌（volume > 0）且收盘价有效
      2. 剔除 B 股（200/900 开头）
      3. 【可选】剔除涨停日（分板块：主板 10% / 创业板科创板 20% / 北交所 30%）
      4. 20 日平均成交额 > 0（腾讯 hfq 接口无成交额，须靠 LEFT JOIN 补齐；
         拿不到成交额的单元视为不可交易）
      5. 剔除 QC 黑名单（后复权价格系统性偏差的股票）
      6. 【可选 min_history，宽池必开】点入时上市满 N 个有效交易日

    ★ 为什么需要 min_history（宽基池的硬需求）
        东财「业绩报表」是按【公司】存的，不是按【A股上市状态】存的。
        实测 601728 中国电信（A股 2021-08 上市）在 2013 年报就有数据 —— 那是
        H 股/集团合并报表。科创板里也有 136 只"2013 年就有财报"。
        → **"某期有财报" ≠ "当时这只 A 股可交易"**。
        唯一的可靠判据是价格序列的实际起始点，即本过滤。
        默认 0 = 不启用，保证农业池既有结论不受影响。
    """
    prev_close = np.vstack([close[:1], close[:-1]])
    with np.errstate(divide="ignore", invalid="ignore"):
        chg = close / prev_close - 1.0
    tradable = (volume > 0) & np.isfinite(close)
    if exclude_b:
        is_b = np.array([c.startswith(("200", "900")) for c in codes])
        if is_b.any():
            tradable[:, is_b] = False
    if exclude_limit:
        if lim_per_col is None:
            lim_per_col = np.array([board_of(c)[1] for c in codes], dtype=float)
        tradable &= ~(np.isfinite(chg) & (chg >= lim_per_col * 0.95))
    a20 = _roll_mean(amount, 20)
    tradable &= np.isfinite(a20) & (a20 > 0)
    if qc_bad:
        tradable[:, np.array([c in qc_bad for c in codes])] = False
    if min_history and min_history > 0:
        # 累计已上市交易日数：从有有效收盘价的第一天起算（点入时口径，
        # 每只股票各自独立计数，不依赖任何外部"上市日期"字段）
        listed = np.cumsum(np.isfinite(close), axis=0)
        tradable &= listed >= int(min_history)
    return tradable


def compute_factors(close, volume, amount, turn):
    f = {}

    def sdiv(cur, lag):
        out = np.full_like(cur, np.nan)
        if cur.shape[0] > lag:
            with np.errstate(divide="ignore", invalid="ignore"):
                out[lag:] = cur[lag:] / cur[:-lag] - 1.0
        return out

    with np.errstate(divide="ignore", invalid="ignore"):
        f["momentum_20d"] = sdiv(close, 20)
        f["momentum_60d"] = sdiv(close, 60)
        f["reversal_5d"] = -sdiv(close, 5)

        r1 = sdiv(close, 1)
        var20 = _roll_mean(r1 ** 2, 20) - _roll_mean(r1, 20) ** 2
        f["volatility_20d"] = np.sqrt(np.where(var20 > 0, var20, np.nan))
        f["price_to_ma20"] = close / _roll_mean(close, 20) - 1.0

        v20 = _roll_mean(volume, 20)
        v20p = np.full_like(volume, np.nan)
        v20p[20:] = v20[:-20]
        f["volume_ratio_20d"] = np.where(v20p > 0, v20 / np.where(v20p > 0, v20p, 1.0), np.nan)

        t20 = _roll_mean(turn, 20)
        t20p = np.full_like(turn, np.nan)
        t20p[20:] = t20[:-20]
        f["turnover_level_20d"] = np.log(np.where(t20 > 0, t20, np.nan))
        f["turnover_ratio_20d"] = np.where(t20p > 0, t20 / np.where(t20p > 0, t20p, 1.0), np.nan)

        a20 = _roll_mean(amount, 20)
        ill = np.where((a20 > 0) & np.isfinite(r1),
                       np.abs(r1) / np.where(a20 > 0, a20, 1.0), np.nan)
        f["amihud_20d"] = _roll_mean(ill, 20) * 1e8
    return f


def fwd(close, h):
    out = np.full_like(close, np.nan)
    if close.shape[0] > h:
        with np.errstate(divide="ignore", invalid="ignore"):
            out[:-h] = close[h:] / close[:-h] - 1.0
    return out


def build_signal(factors, comps, tradable, min_cross):
    """把多个因子按符号合成为截面 z-score 信号（只在可交易股票内排序）。"""
    T = next(iter(factors.values())).shape[0]
    M = tradable.shape[1]
    zs = []
    for fk, sign in comps:
        fm = factors[fk]
        z = np.full_like(fm, np.nan)
        for t in range(T):
            m = np.isfinite(fm[t]) & tradable[t]
            if int(m.sum()) >= min_cross:
                r = rankdata(fm[t][m]).astype(np.float64)
                s = r.std()
                z[t, m] = (r - r.mean()) / (s if s > 0 else 1.0) * sign
        zs.append(z)
    return np.nanmean(np.stack(zs, axis=0), axis=0)


def run_backtest(signal, close, dates, h, cost_rate, tradable, min_cross,
                 t0=0, lim_per_col=None):
    """非重叠再平衡回测。主口径 = 多头 Q5 相对池内等权基准的超额。

    t0          : 起始日索引（--start）。因子仍用全样本计算以保证预热，
                  只把再平衡起点推后，避免截断导致首期无信号。
    lim_per_col : 每列的涨跌停限（主板 0.10 / 创业板科创板 0.20 / 北交所 0.30）。
                  涨停判定必须分板块，否则会把创业板 +15% 的正常上涨误判为涨停。
    """
    T, M = close.shape
    ret_h = fwd(close, h)
    reb = list(range(t0, T - h, h))

    w_prev = np.zeros(M)
    ls_prev_l = np.zeros(M)
    ls_prev_s = np.zeros(M)

    q_rets = [[] for _ in range(N_QUANTILES)]
    long_r, bench_r, ls_r, turns, dts, hit_limit = [], [], [], [], [], []

    for t in reb:
        s_row, r_row, m0 = signal[t], ret_h[t], tradable[t]
        m = np.isfinite(s_row) & np.isfinite(r_row) & m0
        n = int(m.sum())
        if n < min_cross:
            continue
        idx = np.where(m)[0]
        sv, rv = s_row[idx], r_row[idx]

        order = np.argsort(sv)
        qsize = len(order) / N_QUANTILES
        cuts = [int(round(k * qsize)) for k in range(N_QUANTILES + 1)]

        for q in range(N_QUANTILES):
            sel = order[cuts[q]:cuts[q + 1]]
            if len(sel):
                q_rets[q].append(float(rv[sel].mean()))

        hi = idx[order[cuts[-2]:cuts[-1]]]   # Q5 多头
        lo = idx[order[cuts[0]:cuts[1]]]     # Q1 空头（仅供参考）

        # 涨停不可买入占比：用调仓当日相对前收的涨幅近似
        prev_close = close[t - 1] if t > 0 else close[t]
        with np.errstate(divide="ignore", invalid="ignore"):
            chg = close[t] / prev_close - 1.0
        up_thr = 0.095 if lim_per_col is None else lim_per_col * 0.95
        lim = np.isfinite(chg) & (chg >= up_thr)
        hit_limit.append(float(lim[hi].mean()) if len(hi) else 0.0)

        # 纯多头：Q5 等权；基准：当期全部可交易股等权
        w = np.zeros(M)
        w[hi] = 1.0 / len(hi)
        bench = float(np.nanmean(rv))
        turn = 0.5 * float(np.abs(w - w_prev).sum())
        w_prev = w

        w_l = np.zeros(M)
        w_l[hi] = 1.0 / len(hi)
        w_s = np.zeros(M)
        w_s[lo] = 1.0 / len(lo)
        ls_turn = 0.5 * (float(np.abs(w_l - ls_prev_l).sum())
                         + float(np.abs(w_s - ls_prev_s).sum()))
        ls_prev_l, ls_prev_s = w_l, w_s

        long_r.append(float(rv[order[cuts[-2]:cuts[-1]]].mean()))
        bench_r.append(bench)
        ls_r.append(float(rv[order[cuts[-2]:cuts[-1]]].mean())
                    - float(rv[order[cuts[0]:cuts[1]]].mean()))
        turns.append(turn)
        dts.append(str(dates[t]))

    if len(long_r) < 30:
        return None

    L = np.asarray(long_r)
    B = np.asarray(bench_r)
    LS = np.asarray(ls_r)
    tv = np.asarray(turns)
    ppy = 243.0 / h

    # 多头净额：扣掉多头单边换手产生的成本
    net_long = L - tv * cost_rate
    # 超额（相对池内等权基准），同样扣成本
    exc = net_long - B
    # 多空仅供信息含量参考：换手是多空两侧合计，成本按两侧计
    net_ls = LS - tv * 2 * cost_rate

    def _stats(x, label):
        mu, se, t = newey_west_t(x, lags=0)
        ann = mu * ppy
        vol = float(x.std(ddof=1)) * np.sqrt(ppy)
        eq = np.cumprod(1 + x)
        dd = float((eq / np.maximum.accumulate(eq) - 1).min())
        return {"label": label, "ann_return": round(float(ann), 4),
                "ann_vol": round(vol, 4),
                "sharpe": round(float(ann / vol) if vol > 0 else float("nan"), 3),
                "max_drawdown": round(dd, 4), "t": round(float(t), 2),
                "n_periods": int(len(x))}

    yearly = {}
    for v, d in zip(exc, dts):
        yearly.setdefault(d[:4], []).append(v)
    yl = [{"year": y, "n": len(v), "excess_mean": round(float(np.mean(v)), 5),
           "excess_ann": round(float(np.mean(v)) * ppy, 4)}
          for y, v in sorted(yearly.items())]

    # 超额的信息比率（相对基准的跟踪误差）
    te = float(exc.std(ddof=1)) * np.sqrt(ppy)
    ir = float(exc.mean() * ppy / te) if te > 0 else float("nan")

    return {
        "long_abs": _stats(L, "多头Q5绝对(未扣成本)"),
        "bench": _stats(B, "池内等权基准"),
        "excess_net": {**_stats(exc, "多头超额(扣成本)"),
                       "tracking_error_ann": round(te, 4),
                       "information_ratio": round(ir, 3)},
        "long_short_ref": _stats(net_ls, "多空Q5-Q1(参考·A股不可实现)"),
        "avg_turnover_per_rebalance": round(float(tv.mean()), 3),
        "annual_turnover_x": round(float(tv.mean()) * ppy, 1),
        "cost_drag_ann": round(float(tv.mean() * cost_rate * ppy), 4),
        "limit_up_hit_rate_in_Q5": round(float(np.mean(hit_limit)), 4),
        "quantile_ann_return": [round(float(np.mean(q)) * ppy, 4) if q else None
                                for q in q_rets],
        "yearly_excess": yl,
        "start": dts[0], "end": dts[-1],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-cross", type=int, default=MIN_CROSS_DEFAULT)
    ap.add_argument("--exclude-limit", action="store_true",
                    # ⚠️ 坑⑲：help 字符串会被 argparse 做 % 展开，裸 `%` 后接中文
                    # （原为 `9.5%的`）会让 `--help` 直接崩溃，而正常运行**不触发**
                    # ⇒ 只有跑 --help 才暴露。含 % 的 help 一律写成 `%%`。
                    help="保守口径：调仓当日涨幅≥9.5%%的股票视为买不进，剔除")
    ap.add_argument("--exclude-b", action="store_true", default=True,
                    help="剔除 B 股（200/900 开头，流动性极差）")
    ap.add_argument("--db", default=str(DB),
                    help="指定数据库（默认 outputs/ashare_agri_hfq_xq.sqlite，"
                         "由 ashare_badj_collect.py 生成）")
    ap.add_argument("--price-table", default=PRICE_TABLE,
                    help=f"价格表：{PRICE_TABLE}(后复权,默认)")
    ap.add_argument("--no-qc", action="store_true",
                    help="关闭质检闸门（默认开启）。仅用于无 hfq_qc 的库；"
                         "报告会标注 qc_applied=false，不得与开闸结果混用")
    ap.add_argument("--out", default=None,
                    help="输出 JSON 路径。**跨库复用时必须显式指定**（坑⑱：默认文件名"
                         "不随 --db 变化，会覆盖另一池的结论产物）")
    ap.add_argument("--start", default="",
                    help="回测起始日 YYYY-MM-DD（因子仍用全样本预热，"
                         "只把再平衡起点推后）")
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"数据库不存在: {db}\n请先运行 ashare_badj_collect.py")
    conn = sqlite3.connect(str(db))
    dates, codes, open_, close, volume, amount, turn = load_panel(conn, args.price_table)
    # QC 黑名单：后复权序列未通过「价格为正 + 日收益落在涨跌停限内」校验的标的直接剔除。
    # 实测 600540：东财无复权/前复权均干净（0~1 天超限），腾讯 hfq 却有 110 天
    # 收益落在 10.5%~12% —— 复权因子本身有误。这类标的必须剔除，不能靠截断规避。
    # ⚠️ 必须在 conn.close() 之前查（下面立刻关闭连接）。
    # ★ 坑⑰（2026-09-03）：原写法 `except sqlite3.OperationalError: qc_bad = set()`
    #   会在质检表缺失/改名/库选错时**静默放行全部数据**——不报错、不告警，
    #   跑出来的结论看不出有没有过质检。这正是「数据无法保证」的真正发生点。
    #   改为走统一访问层：缺表即 QcMissing（fail-loud）；确需放行须显式 --no-qc
    #   并在报告里写 qc_applied=false。
    if args.no_qc:
        qc_bad = set()
    else:
        try:
            qc_bad = qc_bad_codes(db, "hfq_qc", conn)
        except QcMissing as e:
            raise SystemExit(f"{e}\n\n如确需在无质检的库上运行，请显式加 --no-qc，"
                             f"报告中会标注 qc_applied=false。")
    conn.close()
    T, M = close.shape
    print(f"面板: {T} 日 × {M} 只   {dates[0]} ~ {dates[-1]}   "
          f"质检闸门: {'关闭(⚠ 含未质检数据)' if args.no_qc else '开启'}")

    # 分板块涨跌停限：创业板/科创板 ±20%、主板 ±10%、北交所 ±30%
    lim_per_col = np.array([board_of(c)[1] for c in codes], dtype=float)
    boards = {}
    for c in codes:
        b = board_of(c)[0]
        boards[b] = boards.get(b, 0) + 1
    print("板块构成:", boards)

    # 可交易掩码：排除停牌(volume=0)、B股、按 --exclude-limit 排除涨停
    prev_close = np.vstack([close[:1], close[:-1]])
    with np.errstate(divide="ignore", invalid="ignore"):
        chg = close / prev_close - 1.0
    tradable = (volume > 0) & np.isfinite(close)
    if args.exclude_b:
        is_b = np.array([c.startswith(("200", "900")) for c in codes])
        tradable[:, is_b] = False
        if is_b.any():
            print(f"剔除 B 股: {int(is_b.sum())} 只")
    if args.exclude_limit:
        tradable &= ~(np.isfinite(chg) & (chg >= lim_per_col * 0.95))
    a20 = _roll_mean(amount, 20)
    tradable &= np.isfinite(a20) & (a20 > 0)
    if qc_bad:
        bad_col = np.array([c in qc_bad for c in codes])
        tradable[:, bad_col] = False
    print(f"可交易单元占比: {tradable.mean():.1%}")

    # --start：把再平衡起点推到该日期（含）之后的第一个交易日。
    # 因子已在全样本上算好，因此不需要额外的预热窗口。
    t0 = 0
    if args.start:
        hit = np.where(dates >= args.start)[0]
        if len(hit) == 0:
            raise SystemExit(f"--start {args.start} 超出数据范围 {dates[0]}~{dates[-1]}")
        t0 = int(hit[0])
        print(f"起始日 {args.start} → 索引 {t0}（{dates[t0]}），"
              f"回测区间 {dates[t0]} ~ {dates[-1]}")

    factors = compute_factors(close, volume, amount, turn)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "panel": {"days": int(T), "tickers": int(M), "start": str(dates[0]), "end": str(dates[-1])},
        # 质检留痕：任何结论都必须能回答「剔了几只、闸门开没开」（坑⑰）
        "qc": {"applied": not args.no_qc, "n_excluded": len(qc_bad),
               "excluded": sorted(qc_bad)},
        "db_snapshot": str(db),
        "design": {
            "main_metric": "多头Q5 相对池内等权基准的扣成本超额（A股不可做空，多空仅作参考）",
            "rebalance": "non-overlapping every H trading days",
            "quintiles": N_QUANTILES,
            "price": f"后复权 hfq（表 {args.price_table}，腾讯 fqkline；价格恒为正）",
            "backtest_window": f"{dates[t0]} ~ {dates[-1]}" + (
                f"（--start {args.start}）" if args.start else "（全样本）"),
            "board_limits": {b: n for b, n in boards.items()},
            "cost_model": "cost_per_period = turnover(one-way) × round-trip rate",
            "cost_basis": "印花税0.05%(卖出单边)+过户费0.002%+佣金0.05%+滑点",
            "exclude_limit_up": bool(args.exclude_limit),
            "exclude_b_shares": bool(args.exclude_b),
        },
        "caveats": [
            "主口径为纯多头超额：A股农业股无券源，多空组合不可实现",
            "价格用后复权(hfq)：前复权会被高分红扣成负数（牧原实测 -3.60 元、506 日非正价）",
            "成交额/换手率来自东财无复权表（腾讯 hfq 无该字段），按 (code,trade_date) 关联",
            "标的池已剔除北交所(9只)与B股(3只)：流动性不足以建仓",
            "涨停/跌停导致的部分不可成交未逐笔模拟，仅统计Q5内涨停占比作为折扣参考",
            "未考虑指数熔断、停牌导致的调仓顺延",
        ],
        "strategies": {},
    }

    for skey, comps, slabel in STRATEGIES:
        print(f"\n{'=' * 108}")
        print(f"【{slabel}】")
        signal = build_signal(factors, comps, tradable, args.min_cross)
        report["strategies"][skey] = {"label": slabel, "components": comps, "by_holding": {}}

        for h in HOLDING_PERIODS:
            print(f"\n  --- 持有 {h} 日（每 {h} 日调仓，非重叠）---")
            print(f"  {'成本情景':<22}{'多头超额(年化)':>15}{'IR':>8}{'多空参考(年化)':>15}"
                  f"{'年均换手(倍)':>13}{'成本拖累':>10}{'Q5涨停占比':>11}")
            report["strategies"][skey]["by_holding"][str(h)] = {}
            for clabel, crate in COST_SCENARIOS:
                r = run_backtest(signal, close, dates, h, crate, tradable,
                                 args.min_cross, t0=t0, lim_per_col=lim_per_col)
                if not r:
                    print(f"  {clabel:<22}样本不足")
                    continue
                e = r["excess_net"]
                print(f"  {clabel:<22}{e['ann_return']:>15.2%}{e['information_ratio']:>8.2f}"
                      f"{r['long_short_ref']['ann_return']:>15.2%}"
                      f"{r['annual_turnover_x']:>13.1f}{r['cost_drag_ann']:>10.2%}"
                      f"{r['limit_up_hit_rate_in_Q5']:>11.1%}")
                report["strategies"][skey]["by_holding"][str(h)][clabel] = {
                    "cost_rate": crate, **r}

            mid = run_backtest(signal, close, dates, h, COST_SCENARIOS[1][1],
                               tradable, args.min_cross, t0=t0,
                               lim_per_col=lim_per_col)
            if mid:
                qs = mid["quantile_ann_return"]
                print("      五分位年化绝对收益 Q1→Q5: " +
                      "  ".join(f"{v:+.2%}" if v is not None else "NA" for v in qs))
                mono = (all(v is not None for v in qs)
                        and all(qs[i] <= qs[i + 1] + 1e-9 for i in range(len(qs) - 1)))
                ys = mid["yearly_excess"]
                pos = sum(1 for y in ys if y["excess_ann"] > 0)
                print(f"      单调性: {'✓ 严格单调' if mono else '✗ 非单调'}   "
                      f"分年度超额为正: {pos}/{len(ys)} 年")
                print("      " + "  ".join(f"{y['year']}:{y['excess_ann']:+.1%}" for y in ys))
                report["strategies"][skey]["by_holding"][str(h)]["_mid"] = {
                    "quantile_ann_return": qs, "monotonic": bool(mono),
                    "yearly_excess": ys,
                    "years_positive": int(pos), "years_total": len(ys)}

    # ★ 坑⑱（2026-09-03 实测事故）：输出文件名原为硬编码 "ashare_agri_backtest.json"，
    #   不随 --db 变化 ⇒ 用 --db 指向**别的池**时，会静默覆盖另一份结论产物。
    #   实测：outputs/2026-09-02/ashare_agri_backtest.json 的 db_snapshot 是
    #   ashare_semi_hfq_xq.sqlite（半导体池，79 只），即它并非农业池产物，却占了农业池的文件名；
    #   我按默认库重跑时把它覆盖成了农业池（95 只），已用备份恢复。
    #   修法：默认库保持原名（兼容既有引用），非默认库自动加库名后缀；并支持 --out 显式指定。
    if args.out:
        out = Path(args.out)
    elif db.resolve() == Path(DB).resolve():
        out = OUT_DIR / "ashare_agri_backtest.json"
    else:
        out = OUT_DIR / f"ashare_agri_backtest_{db.stem}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入: {out}")
    print("\n判定口径（第三关·经济可行性）：")
    print("  ✓ 通过 = 中成本档多头超额年化 > 0 且 IR ≥ 0.5 且 分年度为正 ≥ 2/3 年数 且 五分位单调")


if __name__ == "__main__":
    main()
